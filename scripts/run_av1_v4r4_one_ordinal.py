#!/usr/bin/env python3
"""Checkout-only JSON CLI for one AV1 v4r4 ordinal."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.execution import search_quality_for_source
from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
    deserialize_av1_v4r4_execution_claim,
    deserialize_av1_v4r4_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner import (
    AV1V4R4OneOrdinalRuntimeInputs,
    run_av1_v4r4_one_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_preparation import (
    deserialize_av1_v4r4_execution_preflight,
    deserialize_av1_v4r4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    av1_v4r4_ordinal_registry_binding,
    deserialize_av1_v4r4_ordinal_registry_claim,
    deserialize_av1_v4r4_ordinal_registry_grant,
    deserialize_av1_v4r4_ordinal_registry_plan,
)


_EXIT_BY_DISPOSITION = {
    "selected_success": 0,
    "bounded_quality_conflict": 2,
    "fatal_failure": 1,
}
_MAX_PRIVATE_REQUEST_BYTES = 64 * 1024
_ERROR_RESULT = {
    "schema": "mediaforce.av1_cold_start_v4r4_one_ordinal_cli_result",
    "schema_version": 1,
    "completed": False,
    "disposition": "fatal_failure",
    "ordinal": None,
    "outcome_id": None,
    "outcome_publication_id": None,
    "terminal_publication_id": None,
    "failure_search_reason": None,
    "conflict_quality_gap_band": None,
    "conflict_size_gap_band": None,
}


class _SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise ValueError("invalid arguments")


def main(argv: list[str] | None = None) -> int:
    parser = _SilentArgumentParser(description="Run one AV1 v4r4 ordinal from JSON artifacts.")
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-key-file", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--qualification-request", type=Path, required=True)
    parser.add_argument("--execution-preflight", type=Path, required=True)
    parser.add_argument("--sequencing-grant", type=Path, required=True)
    parser.add_argument("--sequencing-claim", type=Path, required=True)
    parser.add_argument("--execution-grant", type=Path, required=True)
    parser.add_argument("--execution-claim", type=Path, required=True)
    parser.add_argument("--runtime-request", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        key = _read_custody_key(args.registry_key_file)
        binding = av1_v4r4_ordinal_registry_binding(args.registry, key=key)
        runtime_inputs = _load_runtime_inputs(args.runtime_request)
        result = run_av1_v4r4_one_ordinal(
            binding=binding,
            qualification_request=deserialize_av1_v4r4_qualification_request(
                args.qualification_request.read_bytes()
            ),
            execution_preflight=deserialize_av1_v4r4_execution_preflight(
                args.execution_preflight.read_bytes()
            ),
            plan=deserialize_av1_v4r4_ordinal_registry_plan(args.plan.read_bytes()),
            sequencing_grant=deserialize_av1_v4r4_ordinal_registry_grant(args.sequencing_grant.read_bytes()),
            sequencing_claim=deserialize_av1_v4r4_ordinal_registry_claim(args.sequencing_claim.read_bytes()),
            execution_grant=deserialize_av1_v4r4_execution_grant(args.execution_grant.read_bytes()),
            execution_claim=deserialize_av1_v4r4_execution_claim(args.execution_claim.read_bytes()),
            runtime_inputs=runtime_inputs,
            search_quality_for_source=search_quality_for_source,
        )
        public = dict(result.public_result)
        exit_code = _EXIT_BY_DISPOSITION[str(public["disposition"])]
    except (Exception, KeyboardInterrupt, SystemExit):
        public = dict(_ERROR_RESULT)
        exit_code = 1
    _emit(public)
    return exit_code


def _read_custody_key(path: Path) -> bytes:
    data = _read_private_file(
        path,
        minimum_bytes=32,
        maximum_bytes=4096,
        error_message="invalid key custody",
    )
    stripped = data.strip()
    if len(stripped) >= 64:
        try:
            decoded = bytes.fromhex(stripped.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            decoded = b""
        if len(decoded) >= 32 and stripped == decoded.hex().encode("ascii"):
            return decoded
    if len(data) >= 32:
        return data
    raise ValueError("invalid key custody")


def _load_runtime_inputs(path: Path) -> AV1V4R4OneOrdinalRuntimeInputs:
    request = _load_private_request(path)
    source_path = _absolute_path_value(request.get("source_path"))
    quality_temp_path = _absolute_path_value(request.get("quality_temp_path"))
    width = request.get("width")
    height = request.get("height")
    source_codec = request.get("source_codec")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or width <= 0
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height <= 0
        or not isinstance(source_codec, str)
        or not source_codec.strip()
    ):
        raise ValueError("invalid private runtime request")
    return AV1V4R4OneOrdinalRuntimeInputs(
        source_path=source_path,
        quality_temp_path=quality_temp_path,
        width=width,
        height=height,
        source_codec=source_codec.strip(),
    )


def _load_private_request(path: Path) -> dict[str, Any]:
    data = _read_private_file(
        path,
        minimum_bytes=32,
        maximum_bytes=_MAX_PRIVATE_REQUEST_BYTES,
        error_message="invalid private runtime request",
    )
    payload = json.loads(data)
    if not isinstance(payload, dict) or set(payload) != {"source_path", "quality_temp_path", "width", "height", "source_codec"}:
        raise ValueError("invalid private runtime request")
    return payload


def _read_private_file(
    path: Path,
    *,
    minimum_bytes: int,
    maximum_bytes: int,
    error_message: str,
) -> bytes:
    path = _absolute_path(path)
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(error_message) from exc
    if not _valid_private_file_metadata(
        metadata,
        minimum_bytes=minimum_bytes,
        maximum_bytes=maximum_bytes,
    ):
        raise ValueError(error_message)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ValueError(error_message)
    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError as exc:
        raise ValueError(error_message) from exc
    try:
        opened = os.fstat(fd)
        if not _same_private_file_snapshot(metadata, opened):
            raise ValueError(error_message)
        data = _read_exactly(fd, opened.st_size, error_message)
        if os.read(fd, 1) or not _same_private_file_snapshot(opened, os.fstat(fd)):
            raise ValueError(error_message)
    except OSError as exc:
        raise ValueError(error_message) from exc
    finally:
        os.close(fd)
    return data


def _valid_private_file_metadata(
    metadata: os.stat_result,
    *,
    minimum_bytes: int,
    maximum_bytes: int,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_uid == os.geteuid()
        and metadata.st_nlink == 1
        and minimum_bytes <= metadata.st_size <= maximum_bytes
    )


def _same_private_file_snapshot(
    first: os.stat_result,
    second: os.stat_result,
) -> bool:
    return all(
        getattr(first, field) == getattr(second, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _read_exactly(fd: int, size: int, error_message: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise ValueError(error_message)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _absolute_path_value(value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid private runtime request")
    return _absolute_path(Path(value))


def _absolute_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("path must be absolute")
    return path


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")


if __name__ == "__main__":
    raise SystemExit(main())
