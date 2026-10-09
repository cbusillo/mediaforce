from __future__ import annotations

import subprocess
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

    def _start(self, script: str, owned: Path) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["sh", "-c", remote_script_ending_with_connection(script, owned)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_losing_the_connection_ends_its_encoder_and_preserves_output_for_verified_cleanup(self) -> None:
        with TemporaryDirectory() as raw_root:
            owned = Path(raw_root) / "Episode 01.partial.mkv"
            marker = Path(raw_root) / "finished"
            # A stand-in encoder: its command line names the output, like ffmpeg's does.
            encoder = "sh -c 'echo data > \"$0\"; sleep 60; touch " + str(marker) + "' '" + str(owned) + "'"
            process = self._start(encoder, owned)
            self.assertTrue(_wait_until(owned.exists))
            assert process.stdin is not None

            import sys
            reader = subprocess.Popen([sys.executable, "-c", "import sys,time; f=open(sys.argv[1]); time.sleep(60)", str(owned)])
            self.addCleanup(reader.wait)
            self.addCleanup(lambda: reader.poll() is None and reader.kill())
            process.stdin.close()  # the controller stopped, restarted, or the link dropped

            self.assertTrue(owned.exists(), "verified queue cleanup owns unfinished output removal")
            process.wait(timeout=15)

            time.sleep(0.5)
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
