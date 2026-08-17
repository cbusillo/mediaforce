import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from fastapi import HTTPException

from mediaforce.library.staged_integrity import CheckedStagedOutput, CheckedStagedOutputUnavailable
from mediaforce.web.app import _checked_output_preview_stream_action
from mediaforce.web.checked_output_preview import InvalidByteRange, checked_output_stream_response


class CheckedOutputPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "checked.mkv"
        self.path.write_bytes(b"0123456789")
        stat_result = self.path.stat()
        self.output = CheckedStagedOutput(
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

    def test_rechecks_identity_during_streaming(self) -> None:
        response = checked_output_stream_response(self.output, None)
        self.path.write_bytes(b"changed output")

        with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "changed after validation"):
            asyncio.run(self._body(response))

    def test_app_maps_invalid_range_and_unavailable_output(self) -> None:
        config = Mock(paths=Mock(db_path=Path("unused.sqlite3")))
        readonly_context = MagicMock()
        readonly_context.__enter__.return_value = object()
        with (
            patch("mediaforce.web.app.open_readonly_db", return_value=readonly_context),
            patch("mediaforce.web.app.checked_staged_output", return_value=self.output),
        ):
            with self.assertRaises(HTTPException) as invalid_range:
                _checked_output_preview_stream_action(config, "movies/Ready", "bytes=20-")

        self.assertEqual(invalid_range.exception.status_code, 416)
        self.assertEqual(invalid_range.exception.headers, {"Content-Range": "bytes */10"})
        self.assertEqual(
            invalid_range.exception.detail,
            {
                "code": "checked_output_range_invalid",
                "message": "The requested HTTP bytes range is not satisfiable.",
            },
        )

        readonly_context = MagicMock()
        readonly_context.__enter__.return_value = object()
        with (
            patch("mediaforce.web.app.open_readonly_db", return_value=readonly_context),
            patch(
                "mediaforce.web.app.checked_staged_output",
                side_effect=CheckedStagedOutputUnavailable("checked_output_drifted", "Changed."),
            ),
        ):
            with self.assertRaises(HTTPException) as unavailable:
                _checked_output_preview_stream_action(config, "movies/Ready", None)

        self.assertEqual(unavailable.exception.status_code, 409)
        self.assertEqual(
            unavailable.exception.detail,
            {"code": "checked_output_drifted", "message": "Changed."},
        )

    def test_detects_atomic_replacement_during_streaming(self) -> None:
        response = checked_output_stream_response(self.output, None)
        replacement = self.path.with_name("replacement.mkv")
        replacement.write_bytes(b"abcdefghij")
        os.utime(replacement, ns=(self.output.mtime_ns, self.output.mtime_ns))
        os.replace(replacement, self.path)

        with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "changed after validation"):
            asyncio.run(self._body(response))

    @staticmethod
    async def _body(response: object) -> bytes:
        chunks = []
        async for chunk in response.body_iterator:  # type: ignore[attr-defined]
            chunks.append(chunk)
        return b"".join(chunks)
