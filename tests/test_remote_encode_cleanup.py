import shlex
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mediaforce.encoding.remote_cleanup import end_remote_output_writers
from mediaforce.web.runtime import encode_runtime


def _row(pid: int, parent: int, command: str, *, birth: str = "14:00:00", state: str = "S") -> str:
    return f"{pid} {parent} {state} Fri Oct 9 {birth} 2026 {command}\n"


def _result(output: str = "1 0 S Fri Oct 9 14:00:00 2026 sshd\n", *, status: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], status, output, "")


def test_restart_ends_writer_and_connection_watch_but_preserves_other_encode() -> None:
    output = Path("/staging/Show [US]/episode.partial.mkv")
    wrapper = (
        "sh -lc mediaforce_connection_watch=$!; "
        f"pkill -TERM -f -- {shlex.quote(str(output))} 2>/dev/null"
    )
    inventory = (
        _row(10, 1, wrapper)
        + _row(11, 10, f"ffmpeg -i /source/episode.mkv {output}")
        + _row(12, 10, "cat")
        + _row(20, 1, f"ffmpeg -i /source/other.mkv {output}.other")
        + _row(21, 1, f"ffmpeg -i {output} /staging/review.mkv")
    )
    remaining = _row(12, 1, "cat") + _row(20, 1, f"ffmpeg -i /source/other.mkv {output}.other")
    runner = Mock(side_effect=[_result(inventory), _result(f"f4\naw\nn{output}\n"), _result(), _result(remaining), _result(), _result()])
    with patch("mediaforce.encoding.remote_cleanup.time.sleep"):
        end_remote_output_writers({"host": "fixture"}, output, run_command=runner)
    term = runner.call_args_list[2].args[1]
    kill = runner.call_args_list[4].args[1]
    assert set(term[5::2]) == {"10", "11", "12"}
    assert kill[4:] == ["KILL", "12", "Fri Oct 9 14:00:00 2026 cat"]


def test_reused_pid_is_not_killed() -> None:
    output = Path("/staging/episode.partial.mkv")
    runner = Mock(side_effect=[
        _result(_row(11, 1, f"ffmpeg {output}")), _result(f"f4\naw\nn{output}\n"), _result(),
        _result(_row(11, 1, "ffmpeg /other.mkv", birth="14:01:00")),
    ])
    with patch("mediaforce.encoding.remote_cleanup.time.sleep"):
        end_remote_output_writers({"host": "fixture"}, output, run_command=runner)
    assert runner.call_count == 4


def test_output_named_as_an_input_does_not_authorize_a_signal() -> None:
    output = Path("/staging/episode.partial.mkv")
    runner = Mock(side_effect=[
        _result(_row(11, 1, f"ffmpeg -i {output} /another {output}")),
        _result(f"f4\nar\nn{output}\nf5\naw\nn/another {output}\n"),
    ])
    with pytest.raises(RuntimeError, match="Could not verify"):
        end_remote_output_writers({"host": "fixture"}, output, run_command=runner)
    assert runner.call_count == 2


def test_unreaped_zombie_is_no_longer_a_writer() -> None:
    output = Path("/staging/episode.partial.mkv")
    runner = Mock(return_value=_result(_row(11, 1, f"ffmpeg {output}", state="Z")))
    end_remote_output_writers({"host": "fixture"}, output, run_command=runner)
    assert runner.call_count == 1


def test_stubborn_remote_writer_keeps_cleanup_unproven() -> None:
    output = Path("/staging/episode.partial.mkv")
    inventory = _result(_row(11, 1, f"ffmpeg {output}"))
    runner = Mock(side_effect=[inventory, _result(f"f4\naw\nn{output}\n"), _result(), inventory, _result(), inventory])
    with patch("mediaforce.encoding.remote_cleanup.time.sleep"), pytest.raises(RuntimeError, match="still ending"):
        end_remote_output_writers({"host": "fixture"}, output, run_command=runner)


@pytest.mark.parametrize("inventory", [_result(status=255), _result("truncated process row"), _result("")])
def test_unknown_inventory_never_deletes_the_output(inventory: subprocess.CompletedProcess[str]) -> None:
    runner = Mock(return_value=inventory)
    with patch("mediaforce.web.runtime.encode_runtime.run_remote_command", runner):
        result = encode_runtime._remove_stale_staging_path(
            Path("/staging/episode.partial.mkv"), host={"mode": "ssh", "host": "fixture"},
        )
    assert result.outcome == encode_runtime._StagingPathCleanupOutcome.CLEANUP_DEFERRED
    assert runner.call_count == 1


def test_mounted_controller_file_is_kept_until_remote_writer_is_gone(tmp_path: Path) -> None:
    output = tmp_path / "episode.partial.mkv"
    output.write_text("unfinished")
    runner = Mock(return_value=_result(status=255))
    with patch("mediaforce.web.runtime.encode_runtime.run_remote_command", runner):
        result = encode_runtime._remove_stale_staging_path(
            output, host={"mode": "ssh", "host": "fixture"}, prefer_remote=False,
        )
    assert result.outcome == encode_runtime._StagingPathCleanupOutcome.CLEANUP_DEFERRED
    assert output.read_text() == "unfinished"


def test_output_removal_follows_verified_process_exit() -> None:
    output = Path("/staging/episode.partial.mkv")
    runner = Mock(side_effect=[_result(_row(11, 1, f"ffmpeg {output}")), _result(f"f4\naw\nn{output}\n"), _result(), _result(), _result()])
    with patch("mediaforce.web.runtime.encode_runtime.run_remote_command", runner), patch(
        "mediaforce.encoding.remote_cleanup.time.sleep",
    ):
        result = encode_runtime._remove_stale_staging_path(output, host={"mode": "ssh", "host": "fixture"})
    assert result.outcome == encode_runtime._StagingPathCleanupOutcome.CLEANED
    assert runner.call_args_list[-1].args[1][2] == f"rm -f {output}"
    assert all(call.kwargs["wake_before_connect"] is False for call in runner.call_args_list)
