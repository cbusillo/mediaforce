from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE_BYTES = b"opaque fixture profile: preserve these bytes\n"


@pytest.fixture
def preparation_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "isolated worktree"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/prepare-jetbrains-inspection.sh", repo / "scripts/prepare-jetbrains-inspection.sh")
    shutil.copyfile(ROOT / "scripts/prepare-jetbrains-state.mjs", repo / "scripts/prepare-jetbrains-state.mjs")
    (repo / "config/jetbrains").mkdir(parents=True)
    (repo / "config/jetbrains/Mediaforce.xml").write_bytes(PROFILE_BYTES)
    (repo / "frontend").mkdir()
    for name in ("package.json", "package-lock.json"):
        (repo / "frontend" / name).write_text("{}")
    skills = tmp_path / "code-home/skills/jetbrains-inspection/scripts"
    skills.mkdir(parents=True)
    (skills / "prepare-python-project.py").write_text("# stubbed by fake uv")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = "#!" + sys.executable + "\n" + r'''
import hashlib
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
with Path(os.environ["PREP_TEST_LOG"]).open("a") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\n")
if name == "uv":
    assert sys.argv[1:3] == ["run", "--no-project"]
    repo = Path(sys.argv[sys.argv.index("--repo") + 1])
    idea = repo / ".idea"
    idea.mkdir(exist_ok=True)
    module = '<module type="PYTHON_MODULE" version="4">\n    <content url="file://$MODULE_DIR$">\n    </content>\n</module>\n'
    (idea / "mediaforce.iml").write_text(module)
elif name == "node":
    assert Path(sys.argv[1]).is_file()
    if sys.argv[2] == "normalize":
        if os.environ.get("PREP_TEST_NODE_FAIL"):
            print("fixture normalization failed", file=sys.stderr)
            sys.exit(9)
    elif sys.argv[2] == "digest":
        digest = hashlib.sha256()
        for path in sys.argv[3:]:
            digest.update(Path(path).read_bytes())
        print(digest.hexdigest(), end="")
    else:
        raise AssertionError(sys.argv)
elif name == "npm":
    assert sys.argv[1] == "--prefix" and sys.argv[3] == "ci"
    frontend = Path(sys.argv[2])
    modules = frontend / "node_modules"
    (modules / ".bin").mkdir(parents=True, exist_ok=True)
    (modules / ".package-lock.json").write_text("{}")
    binary = modules / ".bin/svelte-kit"
    binary.write_text(Path(sys.argv[0]).read_text())
    binary.chmod(0o755)
elif name == "svelte-kit":
    assert sys.argv[1:] == ["sync"]
    if os.environ.get("PREP_TEST_SYNC_FAIL"):
        sys.exit(9)
    Path(".svelte-kit").mkdir(exist_ok=True)
else:
    raise AssertionError(name)
'''
    for name in ("uv", "npm", "node"):
        path = binaries / name
        path.write_text(stub)
        path.chmod(0o755)
    environment = {"HOME": str(tmp_path / "home"), "CODE_HOME": str(tmp_path / "code-home"),
                   "PATH": str(binaries) + os.pathsep + "/usr/bin:/bin",
                   "PREP_TEST_LOG": str(tmp_path / "calls.jsonl")}
    return repo, environment


def _run_preparation(repo: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["/bin/bash", str(repo / "scripts/prepare-jetbrains-inspection.sh")],
                          cwd=repo.parent, env=environment, capture_output=True, text=True, timeout=10)


def _preparation_calls(environment: dict[str, str], name: str) -> list[list[str]]:
    log = Path(environment["PREP_TEST_LOG"])
    return [call for line in log.read_text().splitlines() if (call := json.loads(line))[0] == name]


def test_preparation_copies_profiles_and_caches_successful_frontend_state(
        preparation_sandbox: tuple[Path, dict[str, str]],
) -> None:
    repo, environment = preparation_sandbox
    result = _run_preparation(repo, environment)
    assert result.returncode == 0, result.stderr
    profiles = [repo / ".idea/inspectionProfiles/Mediaforce.xml", repo / "frontend/.idea/inspectionProfiles/Mediaforce.xml"]
    timestamps = [path.stat().st_mtime_ns for path in profiles]
    assert all(path.read_bytes() == PROFILE_BYTES for path in profiles)
    assert _run_preparation(repo, environment).returncode == 0
    assert [path.stat().st_mtime_ns for path in profiles] == timestamps
    assert len(_preparation_calls(environment, "npm")) == 1
    assert len(_preparation_calls(environment, "svelte-kit")) == 1
    (repo / "frontend/.svelte-kit").rmdir()
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 1
    assert len(_preparation_calls(environment, "svelte-kit")) == 2
    (repo / "frontend/node_modules/.package-lock.json").unlink()
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 2
    (repo / "frontend/package.json").write_text('{"name":"changed"}')
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 3
    (repo / "frontend/package-lock.json").write_text('{"changed":true}')
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 4


@pytest.mark.parametrize("failure", ["normalize", "svelte", "duplicate"])
def test_preparation_failure_is_explicit_and_preserves_unowned_modules(
        preparation_sandbox: tuple[Path, dict[str, str]], failure: str,
) -> None:
    repo, environment = preparation_sandbox
    extra = repo / ".idea/mediaforce@1.iml"
    if failure == "normalize":
        environment["PREP_TEST_NODE_FAIL"] = "1"
    elif failure == "svelte":
        environment["PREP_TEST_SYNC_FAIL"] = "1"
    else:
        extra.parent.mkdir()
        extra.write_text("operator IDE state")
    result = _run_preparation(repo, environment)
    assert result.returncode != 0
    assert not (repo / "frontend/node_modules/.mediaforce-dependencies.sha256").exists()
    if failure == "normalize":
        assert "fixture normalization failed" in result.stderr
        assert _preparation_calls(environment, "npm") == []
    elif failure == "duplicate":
        assert "Review duplicate IDE module" in result.stderr
        assert extra.read_text() == "operator IDE state"
        assert not Path(environment["PREP_TEST_LOG"]).exists()


def test_failed_dependency_repair_invalidates_previous_success_stamp(
        preparation_sandbox: tuple[Path, dict[str, str]],
) -> None:
    repo, environment = preparation_sandbox
    assert _run_preparation(repo, environment).returncode == 0
    (repo / "frontend/node_modules/.package-lock.json").unlink()
    environment["PREP_TEST_SYNC_FAIL"] = "1"
    assert _run_preparation(repo, environment).returncode != 0
    assert not (repo / "frontend/node_modules/.mediaforce-dependencies.sha256").exists()
    del environment["PREP_TEST_SYNC_FAIL"]
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 3
