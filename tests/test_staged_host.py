from __future__ import annotations

import hashlib
import os
import subprocess
import time
import unittest
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from mediaforce.encoding import runner, staged_host
from mediaforce.encoding.staged_host import (
    KEEPER_PID_FILE,
    SCRATCH_DIR_PREFIX,
    STAGED_JOB_KEY,
    StagedJob,
    StagedScratchError,
    host_scratch_root,
    keeper_script,
    remote_ffmpeg_command,
    required_scratch_bytes,
    staged_job,
    staged_job_for_host,
    sweep_script,
)


def _wait_until(condition: Any, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


class ScratchScriptTests(unittest.TestCase):
    """Run the host-side scripts in a real shell; they need no SSH to prove their behaviour."""

    def test_keeper_holds_the_directory_and_removes_it_when_the_connection_closes(self) -> None:
        with TemporaryDirectory() as raw_root:
            scratch_dir = PurePosixPath(raw_root) / f"{SCRATCH_DIR_PREFIX}job"
            keeper = subprocess.Popen(
                ["sh", "-c", keeper_script(scratch_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            assert keeper.stdout is not None and keeper.stdin is not None
            self.assertEqual(keeper.stdout.readline().strip(), b"ready")
            pid_file = Path(str(scratch_dir)) / KEEPER_PID_FILE
            self.assertEqual(int(pid_file.read_text()), keeper.pid)
            (Path(str(scratch_dir)) / "source.mkv").write_bytes(b"x" * 1024)

            keeper.stdin.close()
            keeper.wait(timeout=10)

            self.assertFalse(Path(str(scratch_dir)).exists())

    def test_keeper_removes_the_directory_when_it_is_killed(self) -> None:
        with TemporaryDirectory() as raw_root:
            scratch_dir = PurePosixPath(raw_root) / f"{SCRATCH_DIR_PREFIX}job"
            keeper = subprocess.Popen(
                ["sh", "-c", keeper_script(scratch_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            )
            assert keeper.stdout is not None
            keeper.stdout.readline()

            keeper.terminate()
            keeper.wait(timeout=10)

            self.assertTrue(_wait_until(lambda: not Path(str(scratch_dir)).exists()))

    def test_sweep_removes_dead_jobs_and_keeps_live_and_unrelated_directories(self) -> None:
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            live = root / f"{SCRATCH_DIR_PREFIX}live"
            dead = root / f"{SCRATCH_DIR_PREFIX}dead"
            starting = root / f"{SCRATCH_DIR_PREFIX}starting"
            abandoned = root / f"{SCRATCH_DIR_PREFIX}abandoned"
            unrelated = root / "someone-elses-folder"
            for directory in (live, dead, starting, abandoned, unrelated):
                directory.mkdir()
                (directory / "payload").write_bytes(b"x")
            (live / KEEPER_PID_FILE).write_text(str(os.getpid()))
            finished = subprocess.Popen(["true"])
            finished.wait()
            (dead / KEEPER_PID_FILE).write_text(str(finished.pid))
            old = time.time() - (staged_host.ORPHAN_GRACE_MINUTES + 5) * 60
            os.utime(abandoned, (old, old))

            result = subprocess.run(["sh", "-c", sweep_script(PurePosixPath(raw_root))], capture_output=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(live.exists())
            self.assertTrue(starting.exists())
            self.assertTrue(unrelated.exists())
            self.assertFalse(dead.exists())
            self.assertFalse(abandoned.exists())

    def test_sweep_tolerates_a_missing_root(self) -> None:
        result = subprocess.run(["sh", "-c", sweep_script(PurePosixPath("/nonexistent/mediaforce-scratch"))])
        self.assertEqual(result.returncode, 0)


class StagedHostTests(unittest.TestCase):
    def test_scratch_root_must_be_an_absolute_folder_below_the_filesystem_root(self) -> None:
        self.assertEqual(host_scratch_root({"scratch_root": "/var/tmp/scratch"}), PurePosixPath("/var/tmp/scratch"))
        for unsafe in ("", "  ", "/", "relative/scratch", None):
            with self.subTest(unsafe=unsafe):
                self.assertIsNone(host_scratch_root({"scratch_root": unsafe}))
        self.assertIsNone(host_scratch_root(None))

    def test_scratch_requirement_covers_source_output_and_search_samples(self) -> None:
        forty_gib = 40 * 1024 ** 3
        self.assertGreater(required_scratch_bytes(forty_gib), 2 * forty_gib)

    def test_remote_ffmpeg_command_reads_and_writes_scratch_files(self) -> None:
        job = StagedJob(
            scratch_dir=PurePosixPath("/scratch/.mediaforce-staged-a"),
            source_path=PurePosixPath("/scratch/.mediaforce-staged-a/source.mp4"),
            output_path=PurePosixPath("/scratch/.mediaforce-staged-a/output.mkv"),
        )
        command = remote_ffmpeg_command(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-i", "/Volumes/media/tv/Show/E01.mp4", "-map", "0:0", "/staging/E01.partial.mkv"],
            job,
        )
        self.assertEqual(
            command,
            ["ffmpeg", "-y", "-i", str(job.source_path), "-map", "0:0", str(job.output_path)],
        )
        self.assertEqual(staged_job_for_host({STAGED_JOB_KEY: job.to_payload()}), job)
        self.assertIsNone(staged_job_for_host({"key": "plain-host"}))

    def _run_staged_job(self, *, free_kib: int, corrupt: bool = False) -> tuple[list[str], list[str]]:
        """Drive staged_job with local shells standing in for the SSH connections."""
        scripts: list[str] = []
        copies: list[str] = []
        with TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source = root / "Episode.MP4"
            source.write_bytes(b"source-bytes" * 1000)
            scratch_root = root / "scratch"

            def run_remote(_host: Any, command: list[str], _timeout: int, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
                script = command[-1]
                scripts.append(script)
                if script.startswith("mkdir -p") and "df -Pk" in script:
                    return subprocess.CompletedProcess(command, 0, f"{free_kib}\n", "")
                if "sha256sum" in script:
                    staged = next(scratch_root.glob(f"{SCRATCH_DIR_PREFIX}*/source.mp4"))
                    digest = hashlib.sha256(staged.read_bytes() + (b"!" if corrupt else b"")).hexdigest()
                    return subprocess.CompletedProcess(command, 0, f"{digest}\n", "")
                return subprocess.run(["sh", "-c", script], capture_output=True, text=True)

            def local_popen(argv: list[str], **kwargs: Any) -> Any:
                remote_command = argv[-1]
                copies.append(remote_command)
                kwargs.pop("start_new_session", None)
                return subprocess.Popen(["sh", "-c", remote_command], **kwargs)

            host = {"key": "stage-host", "scratch_root": str(scratch_root)}
            try:
                with staged_job(
                        host, source, output_suffix=".mkv", ssh_target="stage-host", ssh_options=[],
                        run_remote_command=run_remote, popen=local_popen,
                ) as job:
                    staged_source = Path(str(job.source_path))
                    self.assertEqual(staged_source.read_bytes(), source.read_bytes())
                    self.assertEqual(staged_source.suffix, ".mp4")
                    self.assertEqual(Path(str(job.output_path)).suffix, ".mkv")
            finally:
                self.assertTrue(_wait_until(lambda: not list(scratch_root.glob(f"{SCRATCH_DIR_PREFIX}*"))))
        return scripts, copies

    def test_staged_job_copies_verifies_and_always_removes_the_scratch_directory(self) -> None:
        scripts, copies = self._run_staged_job(free_kib=10 * 1024 * 1024)
        self.assertTrue(scripts[0].startswith("root="), "the sweep must run before the job stages anything")
        self.assertTrue(any(script.startswith("rm -rf ") for script in scripts))
        self.assertTrue(any(copy.startswith("cat > ") for copy in copies))

    def test_staged_job_refuses_when_scratch_space_is_short(self) -> None:
        with self.assertRaisesRegex(StagedScratchError, "scratch folder has"):
            self._run_staged_job(free_kib=1024)

    def test_staged_job_rejects_a_copy_that_does_not_match_and_still_cleans_up(self) -> None:
        with self.assertRaisesRegex(StagedScratchError, "does not match"):
            self._run_staged_job(free_kib=10 * 1024 * 1024, corrupt=True)


class StagedRunnerTests(unittest.TestCase):
    def test_run_encode_command_encodes_on_the_host_then_pulls_the_output(self) -> None:
        job = StagedJob(
            scratch_dir=PurePosixPath("/scratch/.mediaforce-staged-a"),
            source_path=PurePosixPath("/scratch/.mediaforce-staged-a/source.mp4"),
            output_path=PurePosixPath("/scratch/.mediaforce-staged-a/output.mkv"),
        )
        host = {"key": "stage-host", "host": "root@stage-host", "mode": "ssh", "media_access": "stream",
                STAGED_JOB_KEY: job.to_payload()}
        commands: list[list[str]] = []

        def tracked(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def forbidden_stream(**_kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise AssertionError("a staged host must not fall back to piping the source")

        with patch.object(runner, "pull_output") as pull:
            result = runner.run_encode_command(
                ffmpeg_cmd=["ffmpeg", "-i", "/Volumes/media/tv/Show/E01.mp4", "/staging/E01.partial.mkv"],
                temp_output=Path("/staging/E01.partial.mkv"),
                staging_path=Path("/staging/E01.mkv"),
                overwrite=False,
                process_controller=None,
                host=host,
                execution_mode_for_host=lambda _host: "ssh",
                host_media_access_for_host=lambda _host: "stream",
                remote_shell_path_export_line=lambda: "export PATH=/usr/local/bin:$PATH",
                ssh_client_options=lambda: ["-o", "BatchMode=yes"],
                ffmpeg_command_with_progress=lambda cmd: cmd,
                run_tracked_process_fn=tracked,
                run_streamed_remote_encode_command_fn=forbidden_stream,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(commands[0][:4], ["ssh", "-o", "BatchMode=yes", "root@stage-host"])
        self.assertIn(str(job.source_path), commands[0][-1])
        self.assertIn(str(job.output_path), commands[0][-1])
        self.assertNotIn("pipe:", commands[0][-1])
        pull.assert_called_once()
        self.assertEqual(pull.call_args.args[:2], (job, Path("/staging/E01.partial.mkv")))


if __name__ == "__main__":
    unittest.main()
