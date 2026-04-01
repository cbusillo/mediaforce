import io
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, cast


def run_encode_command(
        *,
        ffmpeg_cmd: list[str],
        temp_output: Path,
        staging_path: Path,
        overwrite: bool,
        process_controller: Any,
        host: dict[str, Any] | None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        execution_mode_for_host: Callable[[dict[str, Any] | None], str],
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
        remote_shell_path_export_line: Callable[[], str],
        ssh_client_options: Callable[[], list[str]],
        ffmpeg_command_with_progress: Callable[[list[str]], list[str]],
        run_tracked_encode_command: Callable[..., subprocess.CompletedProcess[str]],
        run_tracked_process: Callable[..., subprocess.CompletedProcess[str]],
        run_streamed_remote_encode_command: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    host_mode = execution_mode_for_host(host)
    if host_mode != "ssh":
        return run_tracked_encode_command(
            ffmpeg_cmd[:-1] + [str(temp_output)],
            process_controller=process_controller,
            progress_callback=progress_callback,
        )

    if host_media_access_for_host(host) == "stream":
        return run_streamed_remote_encode_command(
            ffmpeg_cmd=ffmpeg_cmd,
            temp_output=temp_output,
            source_path=Path(ffmpeg_cmd[ffmpeg_cmd.index("-i") + 1]),
            process_controller=process_controller,
            host=host,
            progress_callback=progress_callback,
        )

    ssh_host = str((host or {}).get("key") or (host or {}).get("host") or "").strip()
    if not ssh_host:
        raise RuntimeError("Remote encode host is missing an SSH target.")

    remote_ffmpeg_cmd = list(ffmpeg_cmd[:-1]) + [str(temp_output)]
    remote_ffmpeg_cmd[0] = Path(remote_ffmpeg_cmd[0]).name
    remote_ffmpeg_cmd = ffmpeg_command_with_progress(remote_ffmpeg_cmd)
    remote_script_parts = [
        remote_shell_path_export_line(),
        f"mkdir -p {shlex.quote(str(staging_path.parent))}",
        f"rm -f {shlex.quote(str(temp_output))}",
    ]
    if overwrite:
        remote_script_parts.append(f"rm -f {shlex.quote(str(staging_path))}")
    remote_script_parts.extend(
        [
            shlex.join(remote_ffmpeg_cmd),
            f"mv -f {shlex.quote(str(temp_output))} {shlex.quote(str(staging_path))}",
        ]
    )
    ssh_cmd = [
        "ssh",
        *ssh_client_options(),
        ssh_host,
        "sh",
        "-lc",
        " && ".join(remote_script_parts),
    ]
    return run_tracked_process(
        ssh_cmd,
        process_controller=process_controller,
        progress_callback=progress_callback,
    )


def run_streamed_remote_encode_command(
        *,
        ffmpeg_cmd: list[str],
        temp_output: Path,
        source_path: Path,
        process_controller: Any,
        host: dict[str, Any] | None,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        ssh_client_options: Callable[[], list[str]],
        build_streaming_remote_ffmpeg_command: Callable[..., list[str]],
        update_ffmpeg_progress_state: Callable[..., dict[str, Any] | None],
        process_cancelled_error: type[Exception],
) -> subprocess.CompletedProcess[str]:
    ssh_host = str((host or {}).get("key") or (host or {}).get("host") or "").strip()
    if not ssh_host:
        raise RuntimeError("Remote encode host is missing an SSH target.")

    remote_ffmpeg_cmd = build_streaming_remote_ffmpeg_command(
        ffmpeg_cmd,
        source_path=source_path,
        output_path=temp_output,
        executable_path=str((host or {}).get("ffmpeg_path") or "") or None,
    )
    ssh_cmd = [
        "ssh",
        *ssh_client_options(),
        ssh_host,
        *remote_ffmpeg_cmd,
    ]

    process_controller.throw_if_cancelled() if process_controller is not None else None
    process = subprocess.Popen(ssh_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               bufsize=0)
    process_handle = cast(subprocess.Popen[str], process)
    if process_controller is not None:
        process_controller.attach(process_handle)

    stderr_lines: list[str] = []
    progress_state: dict[str, str] = {}
    start_time = time.monotonic()

    def pump_source() -> None:
        if process.stdin is None:
            return
        try:
            with source_path.open("rb") as source_file:
                shutil.copyfileobj(source_file, process.stdin, length=1024 * 1024)
        except BrokenPipeError:
            return
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def collect_output() -> None:
        if process.stdout is None:
            return
        with temp_output.open("wb") as output_file:
            shutil.copyfileobj(process.stdout, output_file, length=1024 * 1024)
        process.stdout.close()

    def consume_stderr() -> None:
        if process.stderr is None:
            return
        stream = io.TextIOWrapper(process.stderr, encoding="utf-8", errors="replace")
        for line in iter(stream.readline, ""):
            stderr_lines.append(line)
            if progress_callback is None:
                continue
            snapshot = update_ffmpeg_progress_state(
                progress_state,
                line,
                elapsed_seconds=time.monotonic() - start_time,
            )
            if snapshot is not None:
                progress_callback(snapshot)
        stream.close()

    source_thread = threading.Thread(target=pump_source, daemon=True)
    output_thread = threading.Thread(target=collect_output, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    source_thread.start()
    output_thread.start()
    stderr_thread.start()

    try:
        return_code = process.wait()
    finally:
        source_thread.join()
        output_thread.join()
        stderr_thread.join()
        if process_controller is not None:
            process_controller.clear(process_handle)

    if process_controller is not None and process_controller.cancelled:
        raise process_cancelled_error("Operation was cancelled.")
    return subprocess.CompletedProcess(ssh_cmd, return_code, "", "".join(stderr_lines))


def run_tracked_process(
        cmd: list[str],
        *,
        process_controller: Any,
        progress_callback: Callable[[dict[str, Any]], None] | None,
        run_command: Callable[[list[str]], subprocess.CompletedProcess[str]],
        update_ffmpeg_progress_state: Callable[..., dict[str, Any] | None],
        process_cancelled_error: type[Exception],
) -> subprocess.CompletedProcess[str]:
    if process_controller is None and progress_callback is None:
        return run_command(cmd)

    process_controller.throw_if_cancelled() if process_controller is not None else None
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    if process_controller is not None:
        process_controller.attach(process)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    progress_state: dict[str, str] = {}
    start_time = time.monotonic()

    def consume_stdout() -> None:
        if process.stdout is None:
            return
        for line in iter(process.stdout.readline, ""):
            stdout_lines.append(line)
        process.stdout.close()

    def consume_stderr() -> None:
        if process.stderr is None:
            return
        for line in iter(process.stderr.readline, ""):
            stderr_lines.append(line)
            if progress_callback is None:
                continue
            snapshot = update_ffmpeg_progress_state(
                progress_state,
                line,
                elapsed_seconds=time.monotonic() - start_time,
            )
            if snapshot is not None:
                progress_callback(snapshot)
        process.stderr.close()

    stdout_thread = threading.Thread(target=consume_stdout, daemon=True)
    stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    try:
        return_code = process.wait()
    finally:
        stdout_thread.join()
        stderr_thread.join()
        if process_controller is not None:
            process_controller.clear(process)

    if process_controller is not None and process_controller.cancelled:
        raise process_cancelled_error("Operation was cancelled.")
    return subprocess.CompletedProcess(cmd, return_code, "".join(stdout_lines), "".join(stderr_lines))
