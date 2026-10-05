import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mediaforce.ops.login_item import LOGIN_ITEM_LABEL


@pytest.mark.parametrize("service", ["matching", "other_checkout", "other_program", "absent"])
def test_stop_unloads_only_this_checkout_and_preserves_runtime_lock(tmp_path: Path, service: str) -> None:
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "mediaforce-dev.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/mediaforce-dev.sh", script)
    home = tmp_path / "home"
    state = home / "Library/Application Support/mediaforce"
    state.mkdir(parents=True)
    lock = state / "mediaforce-web.lock"
    lock_bytes = b'{"pid":424242,"owner":"preserve this runtime"}\n'
    lock.write_bytes(lock_bytes)
    pid_file = state / "mediaforce-web.pid"
    pid_file.write_text("")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "calls.jsonl"
    stub = "#!" + sys.executable + "\n" + '''
import json
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
with Path(os.environ["DEV_TEST_LOG"]).open("a") as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
if name == "id":
    print(4242)
elif name == "launchctl" and sys.argv[1] == "print":
    if sys.argv[2] != "gui/4242/" + os.environ["DEV_TEST_LABEL"]:
        sys.exit(1)
    service = os.environ["DEV_TEST_SERVICE"]
    if service == "absent":
        sys.exit(1)
    repo = os.environ["DEV_TEST_REPO"] if service != "other_checkout" else "/other/checkout"
    program = "mediaforce-web" if service != "other_program" else "another-service"
    print(f"working directory = {repo}\\nprogram = {program}")
elif name not in {"launchctl", "ps", "lsof", "python3", "sleep"}:
    raise AssertionError(name)
'''
    for command in ("id", "launchctl", "ps", "lsof", "python3", "sleep"):
        binary = binaries / command
        binary.write_text(stub)
        binary.chmod(0o755)
    environment = {
        "PATH": str(binaries) + os.pathsep + "/usr/bin:/bin",
        "HOME": str(home),
        "DEV_TEST_LOG": str(log),
        "DEV_TEST_LABEL": LOGIN_ITEM_LABEL,
        "DEV_TEST_SERVICE": service,
        "DEV_TEST_REPO": str(repo),
    }

    result = subprocess.run(
        ["bash", str(script), "stop", "backend"], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert lock.read_bytes() == lock_bytes
    assert not pid_file.exists()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    target = "gui/4242/" + LOGIN_ITEM_LABEL
    assert ["launchctl", "print", target] in calls
    bootouts = [call for call in calls if call[:2] == ["launchctl", "bootout"]]
    assert bootouts == ([["launchctl", "bootout", target]] if service == "matching" else [])
