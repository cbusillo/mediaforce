import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from mediaforce import execution
from mediaforce.core.process_control import run_command
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
        return popen(["/bin/sh", "-c", script], env={"PATH": str(tmp_path)}, **kwargs)

    with patch.object(subprocess, "Popen", side_effect=fixture_ssh), patch.object(execution, "ssh_client_options", return_value=[]):
        result = execution._run_streamed_remote_encode_command(
            ffmpeg_cmd=["ffmpeg", "-i", str(source), str(output)],
            source_path=source, temp_output=output, host={"key": "fixture.invalid"}, process_controller=None, progress_callback=None,
        )
    assert result.returncode == 0
    assert output.read_bytes() == b"-n 19/-c 3\nfixture video"
