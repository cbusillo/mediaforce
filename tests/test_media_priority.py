import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from mediaforce import execution
from mediaforce.core import process_control
from mediaforce.core.process_control import run_command
from mediaforce.core.schedule_deadline import SCHEDULE_CLOSE_DEADLINE_KEY, guard_command_for_schedule_deadline
from mediaforce.hosts import status_helpers
from mediaforce.hosts.transport import run_remote_command


@pytest.mark.parametrize("platform, prefix", [
    ("darwin", ["nice", "-n", "19"]),
    ("linux", ["nice", "-n", "19", "ionice", "-c", "3"]),
])
def test_controller_media_runs_at_background_priority(platform: str, prefix: list[str]) -> None:
    command = ["/opt/fixture/ffmpeg", "-i", "file name.mkv", "-f", "null", "-"]
    with patch("sys.platform", platform), patch.object(subprocess, "run") as launch:
        run_command(command)
    launched = launch.call_args.args[0]
    assert launched[len(prefix):] == command
    assert [Path(arg).name for arg in launched[:len(prefix)]] == prefix


def _install_worker_tools(tmp_path: Path, system: str, tool: str, body: str) -> None:
    scripts = {
        "uname": f"echo {system}",
        "nice": 'export CPU_PRIORITY="$1 $2"; shift 2; exec "$@"',
        "ionice": 'export DISK_PRIORITY="$1 $2"; shift 2; exec "$@"',
        tool: 'printf "%s/%s\\n" "$CPU_PRIORITY" "$DISK_PRIORITY"; ' + body,
    }
    for name, body in scripts.items():
        path = tmp_path / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o700)


@pytest.mark.parametrize("system, tool, expected", [
    ("Linux", "ab-av1", "-n 19/-c 3"),
    ("Linux", "ffprobe", "-n 19/-c 3"),
    ("Darwin", "ffmpeg", "-n 19/"),
])
def test_remote_media_inherits_priority_on_the_execution_host(
        tmp_path: Path, system: str, tool: str, expected: str,
) -> None:
    _install_worker_tools(tmp_path, system, tool, 'printf "%s\\n" "$1"; exit 7')
    ssh = Mock()
    argument = "sample 'with spaces' $(false).mkv"
    run_remote_command(
        {"host": "fixture.invalid"}, [tool, argument], 10, ssh_target_for_host=lambda _: "fixture.invalid",
        remote_shell_path_export_line=lambda: ":", run_remote_ssh=ssh, wake_before_connect=False,
    )
    script = ssh.call_args.args[-1]
    result = subprocess.run(["/bin/sh", "-c", script], env={"PATH": str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 7
    assert result.stdout.splitlines() == [expected, argument]


def test_streamed_encode_runs_the_remote_encoder_at_idle_disk_priority(tmp_path: Path) -> None:
    source = tmp_path / "input.mkv"
    source.write_bytes(b"fixture video")
    output = tmp_path / "output.mkv"
    _install_worker_tools(tmp_path, "Linux", "ffmpeg", "/bin/cat")
    popen = subprocess.Popen

    def fixture_ssh(command: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        assert command[0] == "ssh"
        shell, flag, script = shlex.split(command[-1])
        assert (shell, flag) == ("sh", "-lc")
        return popen(["/bin/sh", "-c", script], env={"PATH": str(tmp_path / "empty-login-path")}, **kwargs)

    with (patch.object(subprocess, "Popen", side_effect=fixture_ssh), patch.object(execution, "ssh_client_options", return_value=[]),
          patch.object(execution, "remote_shell_path_export_line", return_value="export PATH="+shlex.quote(str(tmp_path)))):
        result = execution._run_streamed_remote_encode_command(
            ffmpeg_cmd=["ffmpeg", "-i", str(source), str(output)],
            source_path=source, temp_output=output, host={"key": "fixture.invalid", "ffmpeg_path": "/retired/ffmpeg"}, process_controller=None, progress_callback=None,
        )
    assert result.returncode == 0
    assert output.read_bytes() == b"-n 19/-c 3\nfixture video"


def test_linux_worker_checks_disk_priority_before_taking_work(tmp_path: Path) -> None:
    tools = {"ffmpeg": True, "ffmpeg_libsvtav1": True, "ab_av1": True, "ffmpeg_xpsnr": True}
    host: dict[str, object] = {"capabilities": ["encode_queue"]}
    _install_worker_tools(tmp_path, "Linux", "true", "exit 0")
    for utility in (Path("/usr/bin/tr"), Path("/bin/date"), Path("/usr/bin/readlink")):
        (tmp_path / utility.name).symlink_to(utility)
    with patch.object(status_helpers, "remote_shell_path_export_line", return_value=":"):
        script = status_helpers._remote_status_script(paths=[], repo_path="", include_expensive_tools=False)
    ionice = tmp_path / "ionice"
    for state in ("available", "denied", "missing"):
        if state == "denied":
            ionice.write_text("#!/bin/sh\nexit 1\n")
        elif state == "missing":
            ionice.unlink()
        result = subprocess.run(["/bin/sh", "-c", script], env={"PATH": str(tmp_path)}, capture_output=True, text=True, check=True)
        snapshot = status_helpers._parse_remote_status_output(result.stdout)["tools"]
        issues = status_helpers._host_capability_issues(
            host, tools={**tools, "low_priority": snapshot["low_priority"]}, platform_name="linux", repo_path=None, repo_path_exists=True,
        )
        assert bool(issues) == (state != "available")


def test_trusted_quality_tool_is_deprioritized_after_its_identity_check() -> None:
    command = ["/fixture/ab-av1", "sample-encode"]
    with patch("sys.platform", "darwin"), patch.object(process_control, "_run_managed_command") as launch:
        process_control.run_trusted_local_orchestrator_command(command)
    assert launch.call_args.args[0] == ["/usr/bin/nice", "-n", "19", *command]


def test_schedule_watchdog_starts_the_low_priority_media_command() -> None:
    command = ["ffmpeg", "-i", "two words.mkv"]
    with patch("sys.platform", "darwin"), patch(
        "mediaforce.core.schedule_deadline.guard_shell_script_for_schedule_deadline",
    ) as guard:
        guard_command_for_schedule_deadline(command, {SCHEDULE_CLOSE_DEADLINE_KEY: "2030-01-01T00:00:00Z"}, process_controller=Mock())
    assert shlex.split(guard.call_args.args[0]) == ["/usr/bin/nice", "-n", "19", *command]
