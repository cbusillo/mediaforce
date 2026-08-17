import asyncio
import tempfile
import unittest
from pathlib import Path

from mediaforce.library.staged_integrity import CheckedStagedOutput, CheckedStagedOutputUnavailable
from mediaforce.web.checked_output_preview import InvalidByteRange, checked_output_stream_response


class CheckedOutputPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "checked.mkv"
        self.path.write_bytes(b"0123456789")
        stat_result = self.path.stat()
        self.output = CheckedStagedOutput(
            item_id=1,
            rel_path="movies/Ready/Feature.mkv",
            path=self.path,
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_streams_single_byte_range_with_browser_headers(self) -> None:
        response = checked_output_stream_response(self.output, "bytes=2-5")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(response.headers["content-type"], "video/x-matroska")
        self.assertEqual(asyncio.run(self._body(response)), b"2345")

    def test_supports_suffix_range_and_rejects_multiple_ranges(self) -> None:
        response = checked_output_stream_response(self.output, "bytes=-3")

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 7-9/10")
        self.assertEqual(asyncio.run(self._body(response)), b"789")
        with self.assertRaises(InvalidByteRange):
            checked_output_stream_response(self.output, "bytes=0-1,4-5")

    def test_rechecks_recorded_identity_before_streaming(self) -> None:
        self.path.write_bytes(b"changed output")

        with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "changed after validation"):
            checked_output_stream_response(self.output, None)

    @staticmethod
    async def _body(response: object) -> bytes:
        chunks = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk)
        return b"".join(chunks)
