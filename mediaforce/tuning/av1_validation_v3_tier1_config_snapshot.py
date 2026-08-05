from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash


AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_config_snapshot"
)
AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA_VERSION = 1

_PATH_FIELDS = (
    "project_root",
    "config_path",
    "db_path",
    "run_manifest_dir",
    "web_state_dir",
    "review_dir",
    "runtime_settings_path",
    "runtime_reservation_dir",
)


class AV1ValidationV3Tier1ConfigSnapshotError(ValueError):
    pass


def av1_validation_v3_tier1_config_snapshot_sha256(
    snapshot_bytes: bytes,
) -> str:
    if type(snapshot_bytes) is not bytes or not snapshot_bytes:
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot bytes are invalid"
        )
    identity = {
        "schema": "mediaforce.av1_cold_start_v3_tier1_config_identity",
        "schema_version": 1,
        "byte_length": len(snapshot_bytes),
        "config_bytes_sha256": (
            f"sha256:{hashlib.sha256(snapshot_bytes).hexdigest()}"
        ),
    }
    return f"sha256:{stable_json_hash(identity)}"


def build_av1_validation_v3_tier1_config_snapshot(
    config: MediaforceConfig,
) -> bytes:
    payload = {
        "schema": AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA_VERSION,
        "effective_config": config.raw,
        "resolved_paths": {
            field: _resolved_path_value(getattr(config.paths, field))
            for field in _PATH_FIELDS
        },
    }
    try:
        return canonical_json_bytes(payload) + b"\n"
    except (TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 effective config is not canonicalizable"
        ) from exc


def validate_av1_validation_v3_tier1_config_snapshot(
    snapshot_bytes: bytes,
) -> Mapping[str, Any]:
    if type(snapshot_bytes) is not bytes or not snapshot_bytes:
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot bytes are invalid"
        )
    try:
        payload = json.loads(snapshot_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot is invalid"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "schema_version",
        "effective_config",
        "resolved_paths",
    }:
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot keys are invalid"
        )
    if (
        payload.get("schema") != AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA
        or payload.get("schema_version")
        != AV1_VALIDATION_V3_TIER1_CONFIG_SNAPSHOT_SCHEMA_VERSION
        or not isinstance(payload.get("effective_config"), dict)
    ):
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot identity is invalid"
        )
    resolved_paths = payload.get("resolved_paths")
    if (
        not isinstance(resolved_paths, dict)
        or set(resolved_paths) != set(_PATH_FIELDS)
        or any(
            value is not None and not isinstance(value, str)
            for value in resolved_paths.values()
        )
    ):
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot paths are invalid"
        )
    if snapshot_bytes != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 config snapshot bytes are not canonical"
        )
    return payload


def assert_av1_validation_v3_tier1_config_snapshot_matches(
    config: MediaforceConfig,
    snapshot_bytes: bytes,
) -> None:
    validate_av1_validation_v3_tier1_config_snapshot(snapshot_bytes)
    if snapshot_bytes != build_av1_validation_v3_tier1_config_snapshot(config):
        raise AV1ValidationV3Tier1ConfigSnapshotError(
            "AV1 v3 Tier 1 effective config changed after the snapshot"
        )


def _resolved_path_value(value: Path | None) -> str | None:
    if value is None:
        return None
    return str(value.expanduser().resolve())
