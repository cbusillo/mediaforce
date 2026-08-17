from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
from typing import BinaryIO

from fastapi.responses import StreamingResponse

from mediaforce.library.staged_integrity import CheckedStagedOutput, CheckedStagedOutputUnavailable

_STREAM_CHUNK_SIZE = 1024 * 1024
_VIDEO_MEDIA_TYPES = {
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
}


class InvalidByteRange(ValueError):
    pass


def checked_output_media_type(path: Path) -> str:
    return _VIDEO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def checked_output_stream_response(
        output: CheckedStagedOutput,
        range_header: str | None,
) -> StreamingResponse:
    try:
        file_handle = output.path.open("rb")
    except OSError as error:
        raise CheckedStagedOutputUnavailable(
            "checked_output_unavailable",
            "The checked output is no longer reachable from this computer.",
        ) from error
    try:
        _assert_file_identity(file_handle, output)
        byte_range = _parse_byte_range(range_header, output.size_bytes)
        if byte_range is None:
            start, end = 0, output.size_bytes - 1
            status_code = 200
        else:
            start, end = byte_range
            status_code = 206
        content_length = end - start + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Cache-Control": "no-store",
            "ETag": f'W/"{output.size_bytes:x}-{output.mtime_ns:x}"',
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{output.size_bytes}"
        return StreamingResponse(
            _stream_checked_file(file_handle, output, start=start, length=content_length),
            status_code=status_code,
            media_type=checked_output_media_type(output.path),
            headers=headers,
        )
    except Exception:
        file_handle.close()
        raise


def _parse_byte_range(range_header: str | None, size_bytes: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    unit, separator, value = range_header.strip().partition("=")
    if separator != "=" or unit.lower() != "bytes" or not value or "," in value:
        raise InvalidByteRange("Only one HTTP bytes range is supported.")
    start_text, dash, end_text = value.strip().partition("-")
    if dash != "-" or (not start_text and not end_text):
        raise InvalidByteRange("The HTTP bytes range is malformed.")
    try:
        if not start_text:
            suffix_length = int(end_text)
            if suffix_length <= 0:
                raise InvalidByteRange("The HTTP suffix range must be positive.")
            start = max(size_bytes - suffix_length, 0)
            end = size_bytes - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size_bytes - 1
    except ValueError as error:
        raise InvalidByteRange("The HTTP bytes range is malformed.") from error
    if start < 0 or start >= size_bytes or end < start:
        raise InvalidByteRange("The requested HTTP bytes range is not satisfiable.")
    return start, min(end, size_bytes - 1)


def _stream_checked_file(
        file_handle: BinaryIO,
        output: CheckedStagedOutput,
        *,
        start: int,
        length: int,
) -> Iterator[bytes]:
    remaining = length
    try:
        file_handle.seek(start)
        while remaining:
            _assert_file_identity(file_handle, output)
            chunk = file_handle.read(min(_STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                raise CheckedStagedOutputUnavailable(
                    "checked_output_drifted",
                    "The checked output changed while it was being previewed.",
                )
            _assert_file_identity(file_handle, output)
            remaining -= len(chunk)
            yield chunk
    finally:
        file_handle.close()


def _assert_file_identity(file_handle: BinaryIO, output: CheckedStagedOutput) -> None:
    open_stat = os.fstat(file_handle.fileno())
    try:
        path_stat = output.path.stat()
    except OSError as error:
        raise CheckedStagedOutputUnavailable(
            "checked_output_drifted",
            "The checked output changed after validation. Run the final file check again before previewing it.",
        ) from error
    if (
            open_stat.st_size != output.size_bytes
            or open_stat.st_mtime_ns != output.mtime_ns
            or path_stat.st_size != output.size_bytes
            or path_stat.st_mtime_ns != output.mtime_ns
            or open_stat.st_dev != path_stat.st_dev
            or open_stat.st_ino != path_stat.st_ino
    ):
        raise CheckedStagedOutputUnavailable(
            "checked_output_drifted",
            "The checked output changed after validation. Run the final file check again before previewing it.",
        )
