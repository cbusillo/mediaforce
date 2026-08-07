from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_contains_private_text,
    av1_validation_v4_source_stream_selection_payload,
)
from mediaforce.tuning.av1_validation_v4_path_privacy import (
    AV1_VALIDATION_V4_INSTANCE_PATH_ROLES,
)


AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA = (
    "mediaforce.av1_cold_start_v4_effective_config_snapshot"
)
AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA_VERSION = 2
AV1_VALIDATION_V4_EFFECTIVE_CONFIG_CONTRACT_VERSION = "av1v4config2"

_CONFIG_ID_RE = re.compile(r"av1vconfig4_[0-9a-f]{32}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_CONFIGURATION_MODES = {
    "balanced_full_search_baseline": "baseline",
    "balanced_frozen_search_hint": "guided",
}
_VIDEO_POLICY = {
    "compression_intent": "balanced",
    "compression_intent_confirmed": True,
    "compression_intent_schema_version": 1,
    "compression_intent_source": "operator",
    "max_crf": 45,
    "min_crf": 10,
}
_OMITTED_SEARCH_KWARGS = [
    "cadence_decision",
    "cadence_evidence",
    "cadence_source_fingerprint",
    "detected_crop",
    "host",
    "quality_temp_dir",
]
_ALLOWED_KEYS = frozenset({
    "config_id",
    "configuration_modes",
    "contract_version",
    "dedicated_instance_paths",
    "frozen_extra_search_kwargs",
    "manifest_id",
    "manifest_payload_sha256",
    "manifest_revision",
    "media_bytes_must_not_be_read_during_preparation",
    "media_processing_subprocess_allowed_during_preparation",
    "omitted_search_kwargs",
    "payload_sha256",
    "protocol_version",
    "repository",
    "schema",
    "schema_version",
    "source_paths",
    "source_stream_constraints",
    "video_policy",
})


class AV1ValidationV4PreparationConfigError(ValueError):
    pass


def build_av1_validation_v4_effective_config_snapshot(
    *,
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    frozen_extra_search_kwargs: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "repository": {
            "commit": repository_commit,
            "tree": repository_tree,
        },
        "source_paths": {
            asset_id: str(source_paths[asset_id])
            for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
        },
        "dedicated_instance_paths": {
            role: str(dedicated_instance_paths[role])
            for role in sorted(AV1_VALIDATION_V4_INSTANCE_PATH_ROLES)
        },
        "configuration_modes": _CONFIGURATION_MODES,
        "video_policy": _VIDEO_POLICY,
        "frozen_extra_search_kwargs": {
            asset_id: object_dict(frozen_extra_search_kwargs[asset_id])
            for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
        },
        "omitted_search_kwargs": _OMITTED_SEARCH_KWARGS,
        "source_stream_constraints": (
            av1_validation_v4_source_stream_selection_payload()
        ),
        "media_bytes_must_not_be_read_during_preparation": True,
        "media_processing_subprocess_allowed_during_preparation": False,
    }
    bound = _bind_identity(payload)
    assert_av1_validation_v4_effective_config_snapshot(bound)
    return bound


def assert_av1_validation_v4_effective_config_snapshot(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized or set(materialized) != _ALLOWED_KEYS:
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config shape is invalid"
        )
    expected = {
        "schema": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_EFFECTIVE_CONFIG_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "configuration_modes": _CONFIGURATION_MODES,
        "video_policy": _VIDEO_POLICY,
        "omitted_search_kwargs": _OMITTED_SEARCH_KWARGS,
        "source_stream_constraints": (
            av1_validation_v4_source_stream_selection_payload()
        ),
        "media_bytes_must_not_be_read_during_preparation": True,
        "media_processing_subprocess_allowed_during_preparation": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config binding is invalid"
        )
    repository = object_dict(materialized.get("repository"))
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config repository binding is invalid"
        )
    _assert_path_mapping(
        materialized.get("source_paths"),
        set(AV1_VALIDATION_V4_SOURCE_IDS),
        "source",
    )
    _assert_path_mapping(
        materialized.get("dedicated_instance_paths"),
        set(AV1_VALIDATION_V4_INSTANCE_PATH_ROLES),
        "instance",
    )
    search_kwargs = object_dict(materialized.get("frozen_extra_search_kwargs"))
    if set(search_kwargs) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config search kwargs are incomplete"
        )
    for value in search_kwargs.values():
        kwargs = object_dict(value)
        if (
            set(kwargs) != {"height", "source_codec", "width"}
            or isinstance(kwargs.get("height"), bool)
            or not isinstance(kwargs.get("height"), int)
            or kwargs["height"] <= 0
            or isinstance(kwargs.get("width"), bool)
            or not isinstance(kwargs.get("width"), int)
            or kwargs["width"] <= 0
            or not isinstance(kwargs.get("source_codec"), str)
            or not kwargs["source_codec"]
            or av1_validation_v4_contains_private_text(kwargs["source_codec"])
        ):
            raise AV1ValidationV4PreparationConfigError(
                "AV1 v4 effective config search kwargs are invalid"
            )
    _assert_public_fields(materialized)
    config_id = str(materialized.get("config_id") or "")
    if not _CONFIG_ID_RE.fullmatch(config_id):
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config ID is invalid"
        )
    if config_id != av1_validation_v4_effective_config_id(materialized):
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config ID does not match"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config digest does not match"
        )


def serialize_av1_validation_v4_effective_config_snapshot(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_effective_config_snapshot(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def load_av1_validation_v4_effective_config_snapshot(
    path: Path,
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4PreparationConfigError(
            "AV1 v4 effective config bytes are not canonical"
        )
    assert_av1_validation_v4_effective_config_snapshot(payload)
    return payload


def av1_validation_v4_effective_config_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"config_id", "payload_sha256"}
    }
    return f"av1vconfig4_{stable_json_hash(semantic)[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["config_id"] = av1_validation_v4_effective_config_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_path_mapping(value: Any, expected: set[str], label: str) -> None:
    mapping = object_dict(value)
    if set(mapping) != expected:
        raise AV1ValidationV4PreparationConfigError(
            f"AV1 v4 effective config {label} path set is invalid"
        )
    for path_value in mapping.values():
        _assert_canonical_path(path_value, label)


def _assert_canonical_path(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise AV1ValidationV4PreparationConfigError(
            f"AV1 v4 effective config {label} path is invalid"
        )
    path = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "//" in value
        or value.endswith("/")
        or ".." in path.parts
        or path.as_posix() != value
    ):
        raise AV1ValidationV4PreparationConfigError(
            f"AV1 v4 effective config {label} path is invalid"
        )


def _assert_public_fields(payload: Mapping[str, Any]) -> None:
    private_keys = {"dedicated_instance_paths", "source_paths"}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key not in private_keys:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4PreparationConfigError(
                "AV1 v4 effective config exposes an unexpected private path"
            )

    visit(payload)


if set(_CONFIGURATION_MODES) != set(AV1_VALIDATION_V4_CONFIGURATIONS):
    raise RuntimeError("AV1 v4 effective config modes are incomplete")
