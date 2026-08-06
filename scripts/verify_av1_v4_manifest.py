#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence


MANIFEST_ID = "av1vmanifest4_63f7d31daaf390fae657a42123a88f15"
PAYLOAD_SHA256 = "sha256:91b1c85c0c51544e1e7a2352d68b1025a838b9f5f3dad0deb5c3b9175e9ed589"
DISCOVERY_SHA256 = "sha256:cf4c771992bed9eaddc94ef415ebfe16e9d5582016831b7412abf28503b6c697"
FALSE_AUTHORITY_FIELDS = frozenset({
    "activation_authorized",
    "derivation_authorized",
    "dogfood_authorized",
    "empirical_authority_conferred",
    "evidence_creation_authorized",
    "evidence_eligible",
    "holdout_authorized",
    "key_creation_authorized",
    "manifest_freeze_authorized",
    "media_ingest_authorized",
    "media_publication_authorized",
    "media_read_authorized",
    "partition_construction_authorized",
    "private_inventory_read_authorized",
    "public_bundle_activation_allowed",
    "public_traversal_authorized",
    "publication_authorized",
    "qualification_execution_authorized",
    "retry_authorized",
    "runtime_execution_authorized",
})
FORBIDDEN_TEXT = ("/Users/", "/Volumes/", "/opt/homebrew/", '"workspace"')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate the non-executing AV1 v4 manifest draft.",
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("discovery_public", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _load_canonical(args.manifest, "manifest")
        discovery = _load_canonical(args.discovery_public, "discovery projection")
        _validate(payload, discovery, args.discovery_public.read_bytes())
        if args.as_of:
            _assert_current(payload, args.as_of)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(str(exc), file=sys.stderr)
        return 1
    summary = {
        "ok": True,
        "protocol_version": payload["protocol_version"],
        "experiment_id": payload["experiment_id"],
        "manifest_id": payload["manifest_id"],
        "payload_sha256": payload["payload_sha256"],
        "state": payload["state"],
        "source_count": len(payload["sources"]),
        "traversal_count": payload["qualification_matrix"]["traversal_count"],
        **{
            field: payload[field]
            for field in sorted(FALSE_AUTHORITY_FIELDS)
        },
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        print(
            f"AV1 v4 draft valid: {summary['manifest_id']} "
            f"({summary['source_count']} sources, {summary['traversal_count']} traversals)"
        )
    return 0


def _load_canonical(path: Path, label: str) -> dict[str, object]:
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"AV1 v4 {label} must contain an object")
    if raw != _canonical(payload) + b"\n":
        raise ValueError(f"AV1 v4 {label} bytes are not canonical")
    return payload


def _validate(
    payload: dict[str, object],
    discovery: dict[str, object],
    discovery_bytes: bytes,
) -> None:
    if payload.get("schema") != "mediaforce.av1_cold_start_v4_manifest":
        raise ValueError("AV1 v4 manifest schema is invalid")
    if payload.get("protocol_version") != 4 or payload.get("experiment_id") != "av1_cold_start_v4":
        raise ValueError("AV1 v4 manifest identity is invalid")
    if payload.get("manifest_id") != MANIFEST_ID:
        raise ValueError("AV1 v4 manifest ID is invalid")
    if payload.get("payload_sha256") != PAYLOAD_SHA256:
        raise ValueError("AV1 v4 payload SHA-256 is invalid")
    without_sha = {key: value for key, value in payload.items() if key != "payload_sha256"}
    if f"sha256:{hashlib.sha256(_canonical(without_sha)).hexdigest()}" != PAYLOAD_SHA256:
        raise ValueError("AV1 v4 payload SHA-256 does not recompute")
    discovery_sha = f"sha256:{hashlib.sha256(discovery_bytes).hexdigest()}"
    if discovery_sha != DISCOVERY_SHA256 or payload.get("discovery_public_sha256") != discovery_sha:
        raise ValueError("AV1 v4 discovery projection digest is invalid")
    if payload.get("sources") != discovery.get("sources"):
        raise ValueError("AV1 v4 discovery sources do not match")
    if len(payload.get("sources") or []) != 4:
        raise ValueError("AV1 v4 manifest requires four sources")
    matrix = payload.get("qualification_matrix")
    if not isinstance(matrix, dict) or matrix.get("traversal_count") != 8:
        raise ValueError("AV1 v4 manifest requires eight traversals")
    for field in FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False or discovery.get(field) is not False:
            raise ValueError(f"AV1 v4 authority {field} must remain false")
    combined = json.dumps([payload, discovery], sort_keys=True)
    if any(value in combined for value in FORBIDDEN_TEXT):
        raise ValueError("AV1 v4 public payload exposes machine-local state")


def _assert_current(payload: dict[str, object], as_of: str) -> None:
    current = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if current.tzinfo is None:
        raise ValueError("AV1 v4 activity checks require timezone-aware time")
    drafted = datetime.fromisoformat(str(payload["drafted_at"]).replace("Z", "+00:00"))
    valid_until = datetime.fromisoformat(str(payload["valid_until"]).replace("Z", "+00:00"))
    if current < drafted or current >= valid_until:
        raise ValueError("AV1 v4 draft is not active at the requested time")


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


if __name__ == "__main__":
    raise SystemExit(main())
