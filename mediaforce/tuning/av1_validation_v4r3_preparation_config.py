"""Pure private effective-config contract for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    AV1_V4R3_INSTANCE_PATH_ROLES,
)
from mediaforce.tuning.av1_validation_v4r3_paths import (
    AV1V4R3CanonicalPathError,
    canonical_av1_v4r3_absolute_posix_path,
)


AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_effective_config_snapshot"
)
AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA_VERSION = 1
AV1_V4R3_EFFECTIVE_CONFIG_CONTRACT_VERSION = "av1v4r3config1"

_CONFIG_ID_RE = re.compile(r"av1v4r3config_[0-9a-f]{32}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONFIGURATION_MODES = {
    "balanced_full_search_baseline": "baseline",
    "balanced_frozen_search_hint": "guided",
}
_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "manifest_id",
            "manifest_payload_sha256",
            "config_id",
            "payload_sha256",
            "repository",
            "source_paths",
            "dedicated_instance_paths",
            "quality_temp_paths",
            "configuration_modes",
            "video_policy",
            "closure_ids",
            "media_paths_opened_count",
            "media_paths_statted_count",
            "media_bytes_read_count",
            "media_processing_subprocess_count",
            "selection_or_partition_use_allowed",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3PreparationConfigError(ValueError):
    """Raised when a revision-3 private config snapshot is invalid."""


def build_av1_v4r3_effective_config_snapshot(
    *,
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
) -> dict[str, Any]:
    """Build the canonical private config without touching any supplied path."""

    payload: dict[str, Any] = {
        "schema": AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA,
        "schema_version": AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EFFECTIVE_CONFIG_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "repository": {"commit": repository_commit, "tree": repository_tree},
        "source_paths": dict(source_paths),
        "dedicated_instance_paths": dict(dedicated_instance_paths),
        "quality_temp_paths": dict(quality_temp_paths),
        "configuration_modes": _CONFIGURATION_MODES,
        "video_policy": dict(AV1_V4_R3_FULL_VIDEO_POLICY),
        "closure_ids": [
            closure["closure_id"] for closure in build_av1_v4_r3_all_closure_payloads()
        ],
        "media_paths_opened_count": 0,
        "media_paths_statted_count": 0,
        "media_bytes_read_count": 0,
        "media_processing_subprocess_count": 0,
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_effective_config_snapshot(bound)
    return bound


def assert_av1_v4r3_effective_config_snapshot(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config shape is invalid"
        )
    expected = {
        "schema": AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA,
        "schema_version": AV1_V4R3_EFFECTIVE_CONFIG_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EFFECTIVE_CONFIG_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "configuration_modes": _CONFIGURATION_MODES,
        "video_policy": dict(AV1_V4_R3_FULL_VIDEO_POLICY),
        "closure_ids": [
            closure["closure_id"] for closure in build_av1_v4_r3_all_closure_payloads()
        ],
        "media_paths_opened_count": 0,
        "media_paths_statted_count": 0,
        "media_bytes_read_count": 0,
        "media_processing_subprocess_count": 0,
        "selection_or_partition_use_allowed": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config binding is invalid"
        )
    _assert_repository(materialized.get("repository"))
    _assert_path_mapping(materialized.get("source_paths"), AV1_VALIDATION_V4_SOURCE_IDS)
    _assert_path_mapping(
        materialized.get("dedicated_instance_paths"), AV1_V4R3_INSTANCE_PATH_ROLES
    )
    _assert_path_mapping(
        materialized.get("quality_temp_paths"), AV1_VALIDATION_V4_SOURCE_IDS
    )
    _assert_path_relationships(materialized)
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3PreparationConfigError(
                f"AV1 v4 r3 effective config cannot authorize {field}"
            )
    config_id = str(materialized.get("config_id") or "")
    if not _CONFIG_ID_RE.fullmatch(config_id) or config_id != _config_id(materialized):
        raise AV1V4R3PreparationConfigError("AV1 v4 r3 effective config ID is invalid")
    _assert_payload_sha256(materialized)


def serialize_av1_v4r3_effective_config_snapshot(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_effective_config_snapshot(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_effective_config_snapshot(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config bytes are not canonical"
        )
    assert_av1_v4r3_effective_config_snapshot(payload)
    return payload


def _config_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"config_id", "payload_sha256"}
    }
    return f"av1v4r3config_{stable_json_hash(semantic)[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["config_id"] = _config_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_repository(value: Any) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config repository is invalid"
        )


def _assert_path_mapping(value: Any, expected_keys: set[str] | frozenset[str]) -> None:
    mapping = object_dict(value)
    if set(mapping) != set(expected_keys):
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config path set is invalid"
        )
    canonical_paths: list[str] = []
    try:
        for path in mapping.values():
            if not isinstance(path, str):
                raise AV1V4R3CanonicalPathError("path must be text")
            canonical_paths.append(canonical_av1_v4r3_absolute_posix_path(path))
    except AV1V4R3CanonicalPathError as exc:
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config path is invalid"
        ) from exc
    if len(set(canonical_paths)) != len(canonical_paths):
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config paths must be distinct"
        )


def _assert_payload_sha256(payload: Mapping[str, Any]) -> None:
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config digest does not match"
        )


def _assert_path_relationships(payload: Mapping[str, Any]) -> None:
    sources = {str(path) for path in object_dict(payload.get("source_paths")).values()}
    quality_temps = {
        str(path) for path in object_dict(payload.get("quality_temp_paths")).values()
    }
    instances = object_dict(payload.get("dedicated_instance_paths"))
    source_root = f"{instances['source_root']}/"
    temp_root = f"{instances['temp_root']}/"
    if (
        sources & quality_temps
        or any(not path.startswith(source_root) for path in sources)
        or any(not path.startswith(temp_root) for path in quality_temps)
    ):
        raise AV1V4R3PreparationConfigError(
            "AV1 v4 r3 effective config path relationships are invalid"
        )


if set(_CONFIGURATION_MODES) != set(AV1_VALIDATION_V4_CONFIGURATIONS):
    raise RuntimeError("AV1 v4 r3 effective config modes are incomplete")
