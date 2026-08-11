#!/usr/bin/env python3
"""Checkout-only owner CLI for dormant AV1 v4r4 execution authority."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4r4_execution_issuer import (
    issue_av1_v4r4_execution_claim,
    issue_av1_v4r4_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    av1_v4r4_ordinal_registry_binding,
)


_ERROR_RESULT = {
    "schema": "mediaforce.av1_cold_start_v4r4_execution_issuer_cli_result",
    "schema_version": 1,
    "ok": False,
    "operation": None,
    "created": False,
    "ordinal": None,
    "execution_grant_id": None,
    "execution_grant_payload_sha256": None,
    "execution_claim_id": None,
    "execution_claim_payload_sha256": None,
    "sequencing_claim_created": False,
    "media_execution_started": False,
    "media_read_authorized": None,
    "qualification_execution_authorized": None,
    "runtime_execution_authorized": None,
    "dogfood_authorized": None,
}


class _SilentArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError("invalid arguments")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise ValueError("invalid arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _SilentArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("grant", "claim"))
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--registry-key-file", type=Path, required=True)
    parser.add_argument("--owner-principal", required=True)
    parser.add_argument("--confirm-owner-principal", required=True)
    parser.add_argument("--ordinal", type=int, required=True)
    parser.add_argument("--confirm-ordinal", type=int, required=True)
    parser.add_argument("--valid-until")
    try:
        args = parser.parse_args(argv)
        key = _read_registry_key(args.registry_key_file)
        binding = av1_v4r4_ordinal_registry_binding(args.registry, key=key)
        common = {
            "repository_root": args.repository_root,
            "binding": binding,
            "owner_principal": args.owner_principal,
            "confirmed_owner_principal": args.confirm_owner_principal,
            "ordinal": args.ordinal,
            "confirmed_ordinal": args.confirm_ordinal,
        }
        if args.operation == "grant":
            if not args.valid_until:
                raise ValueError("invalid arguments")
            result = issue_av1_v4r4_execution_grant(
                **common,
                valid_until=args.valid_until,
            )
            payload = _grant_result(result)
        else:
            if args.valid_until is not None:
                raise ValueError("invalid arguments")
            result = issue_av1_v4r4_execution_claim(**common)
            payload = _claim_result(result)
        exit_code = 0
    except (Exception, KeyboardInterrupt, SystemExit):
        payload = dict(_ERROR_RESULT)
        exit_code = 1
    _emit(payload)
    return exit_code


def _grant_result(result: Any) -> dict[str, Any]:
    grant = result.grant
    return {
        **_ERROR_RESULT,
        "ok": True,
        "operation": "grant",
        "created": result.created,
        "ordinal": grant["ordinal"],
        "execution_grant_id": grant["execution_grant_id"],
        "execution_grant_payload_sha256": grant["payload_sha256"],
        "media_read_authorized": grant["media_read_authorized"],
        "qualification_execution_authorized": grant[
            "qualification_execution_authorized"
        ],
        "runtime_execution_authorized": grant["runtime_execution_authorized"],
        "dogfood_authorized": grant["dogfood_authorized"],
    }


def _claim_result(result: Any) -> dict[str, Any]:
    claim = result.claim
    return {
        **_ERROR_RESULT,
        "ok": True,
        "operation": "claim",
        "created": result.created,
        "ordinal": claim["ordinal"],
        "execution_grant_id": claim["execution_grant_id"],
        "execution_grant_payload_sha256": claim["execution_grant_payload_sha256"],
        "execution_claim_id": claim["execution_claim_id"],
        "execution_claim_payload_sha256": claim["payload_sha256"],
        "media_read_authorized": claim["media_read_authorized"],
        "qualification_execution_authorized": claim[
            "qualification_execution_authorized"
        ],
        "runtime_execution_authorized": claim["runtime_execution_authorized"],
        "dogfood_authorized": claim["dogfood_authorized"],
    }


def _read_registry_key(path: Path) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("invalid key custody")
    metadata = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or not 32 <= metadata.st_size <= 4096
    ):
        raise ValueError("invalid key custody")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not isinstance(nofollow, int) or nofollow == 0:
        raise ValueError("invalid key custody")
    fd = os.open(path, os.O_RDONLY | nofollow)
    try:
        opened = os.fstat(fd)
        if not _same_file(metadata, opened):
            raise ValueError("invalid key custody")
        data = _read_exactly(fd, metadata.st_size)
        if os.read(fd, 1) or not _same_file(opened, os.fstat(fd)):
            raise ValueError("invalid key custody")
    finally:
        os.close(fd)
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


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
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


def _read_exactly(fd: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, remaining)
        if not chunk:
            raise ValueError("invalid key custody")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _emit(payload: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(dict(payload)) + b"\n")


if __name__ == "__main__":
    raise SystemExit(main())
