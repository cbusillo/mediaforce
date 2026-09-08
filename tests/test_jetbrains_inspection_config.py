from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import shutil
import sys

import pytest
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import parse


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PROFILE = ROOT / "config" / "jetbrains" / "Mediaforce.xml"
GENERATED_PROFILE = ROOT / ".idea" / "inspectionProfiles" / "Mediaforce.xml"


def _matches(path: str, pattern: str) -> bool:
    path_segments = path.split("/")
    pattern_segments = pattern.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        elif pattern_segments[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_segments) and match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_segments)
                and fnmatch.fnmatchcase(path_segments[path_index], pattern_segments[pattern_index])
                and match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def _inspection_config() -> dict[str, Any]:
    github = json.loads((ROOT / ".github" / "github.json").read_text())
    return github["qualityGate"]["inspection"]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [path.decode() for path in result.stdout.split(b"\0") if path]


def test_jetbrains_inspection_lanes_are_language_owned() -> None:
    inspection = _inspection_config()

    assert inspection["profile"] == "Mediaforce"
    assert inspection["prepare"] == "bash scripts/prepare-jetbrains-inspection.sh"
    assert inspection["requiredGeneratedState"] == [
        ".venv",
        ".idea/mediaforce.iml",
        ".idea/modules.xml",
        ".idea/misc.xml",
        ".idea/inspectionProfiles/Mediaforce.xml",
        "frontend/.idea/inspectionProfiles/Mediaforce.xml",
        "frontend/node_modules",
        "frontend/.svelte-kit",
    ]

    lanes = {lane["id"]: lane for lane in inspection["lanes"]}
    assert set(lanes) == {"python", "frontend"}
    assert lanes["python"] == {
        "id": "python",
        "ide": "PyCharm",
        "required": True,
        "include": [
            "*.py",
            "bin/**/*.py",
            "mediaforce/**/*.py",
            "scripts/**/*.py",
            "tests/**/*.py",
        ],
    }
    assert lanes["frontend"] == {
        "id": "frontend",
        "ide": "WebStorm",
        "required": False,
        "projectPath": "frontend",
        "include": [
            "frontend/**/*.svelte",
            "frontend/**/*.ts",
            "frontend/**/*.js",
            "frontend/**/*.mjs",
            "frontend/**/*.cjs",
            "frontend/**/*.css",
            "frontend/**/*.html",
            "frontend/package.json",
            "frontend/tsconfig.json",
        ],
        "exclude": [
            "frontend/build/**",
            "frontend/.svelte-kit/**",
            "frontend/node_modules/**",
        ],
    }


def test_all_tracked_source_files_match_exactly_one_lane() -> None:
    lanes = _inspection_config()["lanes"]
    frontend_suffixes = {".svelte", ".ts", ".js", ".mjs", ".cjs", ".css", ".html"}

    source_files = [
        path
        for path in _tracked_files()
        if path.endswith(".py")
        or (
            path.startswith("frontend/")
            and (
                Path(path).suffix in frontend_suffixes
                or path in {"frontend/package.json", "frontend/tsconfig.json"}
            )
        )
    ]

    for path in source_files:
        matching_lanes = [
            lane["id"]
            for lane in lanes
            if any(_matches(path, pattern) for pattern in lane["include"])
            and not any(_matches(path, pattern) for pattern in lane.get("exclude", []))
        ]
        assert matching_lanes == (["frontend"] if path.startswith("frontend/") else ["python"]), path


def test_shared_profile_is_bounded_and_generated_state_is_ignored() -> None:
    profile = parse(CANONICAL_PROFILE).getroot().find("profile")
    assert profile is not None
    profile_name = profile.find("option[@name='myName']")
    assert profile_name is not None
    assert profile_name.attrib["value"] == "Mediaforce"

    tools = {tool.attrib["class"]: tool.attrib for tool in profile.findall("inspection_tool")}
    assert {name for name, attributes in tools.items() if attributes["enabled"] == "false"} == {
        "ES6ConvertLetToConst",
        "ExceptionCaughtLocallyJS",
        "HtmlUnknownAttribute",
        "JSRemoveUnnecessaryParentheses",
        "SpellCheckingInspection",
    }
    assert {name for name, attributes in tools.items() if attributes["enabled"] == "true"} == {
        "Eslint",
        "HtmlRequiredAltAttribute",
        "JSUnusedGlobalSymbols",
    }

    assert "/inspectionProfiles/Mediaforce.xml" in (ROOT / ".idea" / ".gitignore").read_text().splitlines()
    assert subprocess.run(
        ["git", "ls-files", "--error-unmatch", GENERATED_PROFILE.relative_to(ROOT)],
        cwd=ROOT,
        capture_output=True,
    ).returncode != 0
    if GENERATED_PROFILE.exists():
        assert GENERATED_PROFILE.read_bytes() == CANONICAL_PROFILE.read_bytes()

    generated_frontend_profile = ROOT / "frontend" / ".idea" / "inspectionProfiles" / "Mediaforce.xml"
    assert "/.idea/" in (ROOT / "frontend" / ".gitignore").read_text().splitlines()
    if generated_frontend_profile.exists():
        assert generated_frontend_profile.read_bytes() == CANONICAL_PROFILE.read_bytes()

    generated_module = ROOT / ".idea" / "mediaforce.iml"
    if generated_module.exists():
        module_xml = generated_module.read_text()
        assert '<module external.system.id="pyproject.toml" type="PYTHON_MODULE" version="4">' in module_xml
        assert '<content url="file://$MODULE_DIR$">' in module_xml
        assert '<excludeFolder url="file://$MODULE_DIR$/frontend" />' in module_xml
        assert '<sourceFolder url="file://$MODULE_DIR$/tests" isTestSource="true" />' in module_xml
        assert 'file://$MODULE_DIR$/..' not in module_xml

    prepare_script = ROOT / "scripts" / "prepare-jetbrains-inspection.sh"
    assert os.access(prepare_script, os.X_OK)


def test_native_frontend_checks_remain_required() -> None:
    quality_gate = json.loads((ROOT / ".github" / "github.json").read_text())["qualityGate"]

    assert quality_gate["typecheck"]["frontend"] == "npm --prefix frontend run check"
    assert quality_gate["lint"]["frontend"] == "npm --prefix frontend run lint"
    assert quality_gate["test"]["frontend"] == "npm --prefix frontend test"


@pytest.fixture
def preparation_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    if shutil.which("node") is None:
        pytest.skip("Preparation requires Node; no dependencies are installed by this test")
    repo = tmp_path / "isolated worktree"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/prepare-jetbrains-inspection.sh", repo / "scripts/prepare-jetbrains-inspection.sh")
    (repo / "config/jetbrains").mkdir(parents=True)
    shutil.copyfile(CANONICAL_PROFILE, repo / "config/jetbrains/Mediaforce.xml")
    (repo / "frontend").mkdir()
    for name in ("package.json", "package-lock.json"):
        (repo / "frontend" / name).write_text("{}")
    skills = tmp_path / "code-home/skills/jetbrains-inspection/scripts"
    skills.mkdir(parents=True)
    (skills / "prepare-python-project.py").write_text("# stubbed by fake uv")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = "#!" + sys.executable + "\n" + r'''
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
    if os.environ.get("PREP_TEST_BAD_MODULE"):
        module = "unsupported helper output"
    (idea / "mediaforce.iml").write_text(module)
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
    for name in ("uv", "npm"):
        path = binaries / name
        path.write_text(stub)
        path.chmod(0o755)
    environment = {**os.environ, "CODE_HOME": str(tmp_path / "code-home"),
                   "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                   "PREP_TEST_LOG": str(tmp_path / "calls.jsonl")}
    return repo, environment


def _run_preparation(repo: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(repo / "scripts/prepare-jetbrains-inspection.sh")],
                          cwd=repo.parent, env=environment, capture_output=True, text=True, timeout=10)


def _preparation_calls(environment: dict[str, str], name: str) -> list[list[str]]:
    log = Path(environment["PREP_TEST_LOG"])
    return [call for line in log.read_text().splitlines() if (call := json.loads(line))[0] == name]


def test_preparation_normalizes_module_and_caches_successful_frontend_state(
        preparation_sandbox: tuple[Path, dict[str, str]],
) -> None:
    repo, environment = preparation_sandbox
    result = _run_preparation(repo, environment)
    assert result.returncode == 0, result.stderr
    module = parse(repo / ".idea/mediaforce.iml").getroot()
    assert module.attrib["external.system.id"] == "pyproject.toml"
    content = module.find("content")
    assert content is not None and content.attrib["url"] == "file://$MODULE_DIR$"
    exclusion = content.find("excludeFolder")
    assert exclusion is not None
    assert exclusion.attrib["url"] == "file://$MODULE_DIR$/frontend"
    profiles = [repo / ".idea/inspectionProfiles/Mediaforce.xml", repo / "frontend/.idea/inspectionProfiles/Mediaforce.xml"]
    timestamps = [path.stat().st_mtime_ns for path in profiles]
    assert all(path.read_bytes() == CANONICAL_PROFILE.read_bytes() for path in profiles)
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
    (repo / "frontend/package-lock.json").write_text('{"changed":true}')
    assert _run_preparation(repo, environment).returncode == 0
    assert len(_preparation_calls(environment, "npm")) == 3


@pytest.mark.parametrize("failure", ["module", "svelte", "duplicate"])
def test_preparation_failure_is_explicit_and_preserves_unowned_modules(
        preparation_sandbox: tuple[Path, dict[str, str]], failure: str,
) -> None:
    repo, environment = preparation_sandbox
    extra = repo / ".idea/mediaforce@1.iml"
    if failure == "module":
        environment["PREP_TEST_BAD_MODULE"] = "1"
    elif failure == "svelte":
        environment["PREP_TEST_SYNC_FAIL"] = "1"
    else:
        extra.parent.mkdir()
        extra.write_text("operator IDE state")
    result = _run_preparation(repo, environment)
    assert result.returncode != 0
    assert not (repo / "frontend/node_modules/.mediaforce-dependencies.sha256").exists()
    if failure == "module":
        assert "Unexpected Python preparation module format" in result.stderr
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
