import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mediaforce.ops.login_item import LOGIN_ITEM_LABEL


@pytest.mark.parametrize("service,running,process", [
    (service, "idle", "owned") for service in ("matching", "other_checkout", "other_program", "absent")
] + [
    ("absent", running, process) for running in ("pid_file", "listener", "lock") for process in ("owned", "foreign")
])
def test_stop_unloads_only_this_checkout_and_preserves_runtime_lock(
    tmp_path: Path, service: str, running: str, process: str,
) -> None:
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "mediaforce-dev.sh"
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts/mediaforce-dev.sh", script)
    home = tmp_path / "home"
    state = home / "Library/Application Support/mediaforce"
    state.mkdir(parents=True)
    lock = state / "mediaforce-web.lock"
    lock_bytes = b'{"owner":"preserve this runtime"}\n'
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
elif name == "ps" and sys.argv[1:3] == ["-p", os.environ["DEV_TEST_PID"]]:
    if sys.argv[-1] == "command=":
        repo = os.environ["DEV_TEST_REPO"] if os.environ["DEV_TEST_PROCESS"] == "owned" else "/foreign/checkout"
        print(repo + "/.venv/bin/mediaforce-web")
    elif sys.argv[-1] == "ppid=":
        print(0)
elif name == "lsof" and os.environ["DEV_TEST_RUNNING"] == "listener":
    seen = Path(os.environ["DEV_TEST_LISTENER_SEEN"])
    if not seen.exists():
        seen.touch()
        print(os.environ["DEV_TEST_PID"])
elif name == "python3":
    assert sys.argv[1] == "-c"
    assert Path(sys.argv[-1]) == Path(os.environ["DEV_TEST_LOCK"])
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
elif name == "dirname":
    print(Path(sys.argv[1]).parent)
elif name == "sed":
    print(sys.stdin.read().strip())
elif name == "awk":
    if "-v" not in sys.argv:
        for line in sys.stdin:
            if line.split():
                print(line.split()[0])
elif name == "sort":
    print("\\n".join(sorted(set(sys.stdin.read().splitlines()))))
elif name == "paste":
    print(",".join(sys.stdin.read().splitlines()))
elif name == "tr":
    print(sys.stdin.read().lower(), end="")
elif name == "rm":
    for argument in sys.argv[1:]:
        if argument.startswith("-"):
            continue
        target = Path(argument)
        assert target.resolve().is_relative_to(Path(os.environ["HOME"]).resolve())
        target.unlink(missing_ok=True)
elif name not in {"launchctl", "ps", "lsof", "sleep"}:
    raise AssertionError(name)
'''
    for command in ("id", "launchctl", "ps", "lsof", "python3", "sleep", "dirname", "sed", "awk", "sort", "paste", "tr", "rm"):
        binary = binaries / command
        binary.write_text(stub)
        binary.chmod(0o755)
    environment = {
        "PATH": str(binaries),
        "HOME": str(home),
        "DEV_TEST_LOG": str(log),
        "DEV_TEST_LABEL": LOGIN_ITEM_LABEL,
        "DEV_TEST_SERVICE": service,
        "DEV_TEST_REPO": str(repo),
        "DEV_TEST_RUNNING": running,
        "DEV_TEST_PROCESS": process,
        "DEV_TEST_LOCK": str(lock),
        "DEV_TEST_LISTENER_SEEN": str(tmp_path / "listener-seen"),
    }

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        environment["DEV_TEST_PID"] = str(child.pid)
        if running == "pid_file":
            pid_file.write_text(str(child.pid))
        elif running == "lock":
            lock_bytes = json.dumps({"pid": child.pid, "owner": "runtime fixture"}).encode()
            lock.write_bytes(lock_bytes)
        result = subprocess.run(
            ["/bin/bash", str(script), "stop", "backend"], cwd=tmp_path,
            env=environment, capture_output=True, text=True, timeout=10,
        )
        if running == "idle" or process == "foreign":
            assert child.poll() is None
        else:
            assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert lock.read_bytes() == lock_bytes
    assert not pid_file.exists()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    target = "gui/4242/" + LOGIN_ITEM_LABEL
    assert ["launchctl", "print", target] in calls
    bootouts = [call for call in calls if call[:2] == ["launchctl", "bootout"]]
    assert bootouts == ([["launchctl", "bootout", target]] if service == "matching" else [])
