from __future__ import annotations

import subprocess
import os
import signal
import shlex
import select
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mediaforce.encoding.runner import remote_script_ending_with_connection


def _wait_until(condition: Any, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


class RemoteEncodeConnectionWatchTests(unittest.TestCase):
    """Run the wrapper in a real shell; a local pipe stands in for the SSH connection."""

    def _start(self, script: str, owned: Path, *, pass_fds: tuple[int, ...] = ()) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["sh", "-c", remote_script_ending_with_connection(script, owned)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds,
        )

    def test_losing_the_connection_ends_its_encoder_and_preserves_output_for_verified_cleanup(self) -> None:
        with TemporaryDirectory() as raw_root:
            owned = Path(raw_root) / "Episode 01.partial.mkv"
            marker = Path(raw_root) / "finished"
            read_fd, write_fd = os.pipe()
            producer = (
                'import os,sys,time; f=open(sys.argv[1],"w"); f.write("data"); f.flush(); '
                'os.write(int(sys.argv[2]),b"R"); time.sleep(60); open(sys.argv[3],"w").close()'
            )
            encoder = shlex.join([sys.executable, "-c", producer, str(owned), str(write_fd), str(marker)])
            process = self._start(encoder, owned, pass_fds=(write_fd,))
            os.close(write_fd)
            def family_ended(timeout: float) -> bool:
                return bool(select.select([read_fd], [], [], timeout)[0]) and os.read(read_fd, 1) == b""
            def finish_fixture() -> None:
                # The unreaped parent reserves its PID. Its explicitly created
                # session contains only this fixture, including an inherited proof pipe.
                try:
                    if not family_ended(0):
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            if not family_ended(0):
                                raise
                    process.wait(timeout=5)
                finally:
                    os.close(read_fd)
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream is not None:
                            stream.close()
            self.addCleanup(finish_fixture)
            self.assertTrue(select.select([read_fd], [], [], 15)[0], "the child must start with its proof pipe")
            self.assertEqual(os.read(read_fd, 1), b"R")
            reader = subprocess.Popen(
                [sys.executable, "-c", 'import sys,time; f=open(sys.argv[1]); print("reading",flush=True); time.sleep(60)', str(owned)],
                stdout=subprocess.PIPE, text=True,
            )
            self.addCleanup(reader.wait)
            self.addCleanup(lambda: reader.poll() is None and reader.kill())
            assert reader.stdout is not None
            self.addCleanup(reader.stdout.close)
            self.assertTrue(select.select([reader.stdout], [], [], 15)[0])
            self.assertEqual(reader.stdout.readline().strip(), "reading")
            assert process.stdin is not None
            process.stdin.close()
            self.assertTrue(family_ended(15), "the command's children must exit, including after KILL escalation")
            self.assertEqual(owned.read_text(), "data", "verified queue cleanup owns removal")
            self.assertIsNone(reader.poll(), "the reader is outside the command's process family")
            self.assertFalse(marker.exists())

    def test_a_finished_encode_keeps_its_output_and_exit_status(self) -> None:
        with TemporaryDirectory() as raw_root:
            owned = Path(raw_root) / "Episode 02.partial.mkv"
            final = Path(raw_root) / "Episode 02.mkv"
            process = self._start(f"echo data > '{owned}' && mv -f '{owned}' '{final}'", owned)
            self.assertEqual(process.wait(timeout=15), 0)
            self.assertTrue(final.exists())
            assert process.stdin is not None
            process.stdin.close()
            time.sleep(0.5)
            self.assertTrue(final.exists(), "closing the connection after success must not touch the result")

            failing = self._start("exit 7", Path(raw_root) / "Episode 03.partial.mkv")
            self.assertEqual(failing.wait(timeout=15), 7)


if __name__ == "__main__":
    unittest.main()
