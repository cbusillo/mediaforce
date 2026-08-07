from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_REVISED_AT,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_LAYOUT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_guided_warm_start_identities,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_PREPARATION_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_record"
)
AV1_VALIDATION_V4_PREPARATION_SCHEMA_VERSION = 2
AV1_VALIDATION_V4_PREPARATION_STATE = "prepared_unfrozen"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_RIGHTS_ATTESTATION_ID_RE = re.compile(r"av1vrights4_[0-9a-f]{32}\Z")
_INSTANCE_PATH_HMAC_ID_RE = re.compile(r"av1vpath4_[0-9a-f]{32}\Z")
_SOURCE_PATH_HMAC_ID_RE = re.compile(r"av1vsource4_[0-9a-f]{32}\Z")
_RUNTIME_COMPATIBILITY_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_PATH_PRIVACY_KEY_ID_RE = re.compile(r"av1vpathkey4_[0-9a-f]{32}\Z")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:[\\/]")
_FORBIDDEN_PATH_PREFIXES = ("/Users/", "/Volumes/", "/opt/homebrew/")
_INSTANCE_PATH_ROLES = frozenset({
    "runtime_lock",
    "source_root",
    "state_root",
    "temp_root",
})
_COVERED_REQUIREMENTS = frozenset({
    "ab_av1_sha256_and_version",
    "all_traversal_invocation_sha256",
    "dedicated_instance_path_hmac_ids",
    "effective_config_sha256",
    "ffmpeg_sha256_and_version",
    "ffprobe_sha256_and_version",
    "guided_warm_start_identities_by_source",
    "media_bytes_must_not_be_read",
    "path_privacy_key_id",
    "repository_commit_and_tree",
    "required_before_owner_freeze",
    "runtime_compatibility_id",
    "source_path_hmac_ids",
    "subprocess_execution_accounting",
})
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "dedicated_instance_path_hmac_ids",
    "discovery_public_sha256",
    "effective_config_sha256",
    "experiment_id",
    "guided_warm_start_identities",
    "invocations",
    "manifest_id",
    "manifest_revision",
    "manifest_payload_sha256",
    "media_bytes_read",
    "payload_sha256",
    "preparation_id",
    "prepared_at",
    "protocol_version",
    "path_privacy_key_id",
    "repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "rights_attested_at",
    "runtime_compatibility_id",
    "runtime_compatibility_scope",
    "schema",
    "schema_version",
    "source_path_hmac_ids",
    "state",
    "builder_subprocess_executed",
    "media_processing_subprocess_executed",
    "tool_version_probe_subprocess_executed",
    "toolchain",
    "valid_until",
}) | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS


class AV1ValidationV4PreparationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4ToolIdentity:
    version: str
    binary_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV4InvocationIdentity:
    ordinal: int
    asset_id: str
    configuration: str
    source_path_hmac_id: str
    invocation_sha256: str
    base_config_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV4PreparationInputs:
    prepared_at: str
    repository_commit: str
    repository_tree: str
    effective_config_sha256: str
    ffmpeg: AV1ValidationV4ToolIdentity
    ffprobe: AV1ValidationV4ToolIdentity
    ab_av1: AV1ValidationV4ToolIdentity
    dedicated_instance_path_hmac_ids: Mapping[str, str]
    source_path_hmac_ids: Mapping[str, str]
    runtime_compatibility_id: str
    guided_warm_start_identities: Mapping[str, Mapping[str, Any]]
    invocations: Sequence[AV1ValidationV4InvocationIdentity]
    path_privacy_key_id: str
    tool_version_probe_subprocess_executed: bool


def av1_validation_v4_preparation_covered_requirements() -> frozenset[str]:
    return _COVERED_REQUIREMENTS


def build_av1_validation_v4_preparation_record(
    *,
    inputs: AV1ValidationV4PreparationInputs,
    rights_attestation: Mapping[str, Any],
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    _assert_completed_rights_attestation(rights)
    prepared_at = _parse_timestamp(inputs.prepared_at, "prepared_at")
    rights_attested_at = _parse_timestamp(
        str(rights.get("attested_at") or ""),
        "rights_attested_at",
    )
    if rights_attested_at > prepared_at:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 rights attestation cannot postdate preparation"
        )
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_PREPARATION_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PREPARATION_SCHEMA_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": AV1_VALIDATION_V4_PREPARATION_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "discovery_public_sha256": AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "prepared_at": inputs.prepared_at,
        "valid_until": AV1_VALIDATION_V4_VALID_UNTIL,
        "repository": {
            "commit": inputs.repository_commit,
            "tree": inputs.repository_tree,
        },
        "effective_config_sha256": inputs.effective_config_sha256,
        "toolchain": {
            "ffmpeg": _tool_payload(inputs.ffmpeg),
            "ffprobe": _tool_payload(inputs.ffprobe),
            "ab_av1": _tool_payload(inputs.ab_av1),
        },
        "dedicated_instance_path_hmac_ids": dict(
            inputs.dedicated_instance_path_hmac_ids
        ),
        "source_path_hmac_ids": dict(inputs.source_path_hmac_ids),
        "runtime_compatibility_id": inputs.runtime_compatibility_id,
        "runtime_compatibility_scope": "host_toolchain_config",
        "guided_warm_start_identities": {
            str(asset_id): object_dict(identity)
            for asset_id, identity in inputs.guided_warm_start_identities.items()
        },
        "invocations": [
            {
                "ordinal": invocation.ordinal,
                "asset_id": invocation.asset_id,
                "configuration": invocation.configuration,
                "mode": _mode_for_configuration(invocation.configuration),
                "source_path_hmac_id": invocation.source_path_hmac_id,
                "sha256": invocation.invocation_sha256,
                "base_config_sha256": invocation.base_config_sha256,
            }
            for invocation in inputs.invocations
        ],
        "path_privacy_key_id": inputs.path_privacy_key_id,
        "media_bytes_read": False,
        "builder_subprocess_executed": False,
        "media_processing_subprocess_executed": False,
        "tool_version_probe_subprocess_executed": (
            inputs.tool_version_probe_subprocess_executed
        ),
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_preparation_bundle(bound, rights)
    return bound


def _assert_av1_validation_v4_preparation_record_structure(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation payload is invalid"
        )
    unknown_fields = set(materialized) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown_fields:
        raise AV1ValidationV4PreparationError(
            f"AV1 v4 preparation contains unknown fields: {sorted(unknown_fields)}"
        )
    _assert_no_private_paths(materialized)
    _assert_base_binding(materialized)
    _assert_false_authorities(materialized)
    _assert_repository(materialized)
    _assert_toolchain(materialized)
    _assert_path_identities(materialized)
    _assert_runtime_and_warm_start(materialized)
    _assert_invocations(materialized)
    _assert_identity(materialized)


def assert_av1_validation_v4_preparation_bundle(
    preparation: Mapping[str, Any],
    rights_attestation: Mapping[str, Any],
) -> None:
    rights = object_dict(rights_attestation)
    _assert_completed_rights_attestation(rights)
    record = object_dict(preparation)
    _assert_av1_validation_v4_preparation_record_structure(record)
    if (
        record.get("rights_attestation_id") != rights.get("attestation_id")
        or record.get("rights_attestation_payload_sha256")
        != rights.get("payload_sha256")
        or record.get("rights_attested_at") != rights.get("attested_at")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation rights attestation binding does not match"
        )


def serialize_av1_validation_v4_preparation_record(
    payload: Mapping[str, Any],
    *,
    rights_attestation: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_bundle(
        materialized,
        rights_attestation,
    )
    return canonical_json_bytes(materialized) + b"\n"


def av1_validation_v4_preparation_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"preparation_id", "payload_sha256"}
    }
    return f"av1vprep4_{stable_json_hash(semantic)[:32]}"


def _tool_payload(identity: AV1ValidationV4ToolIdentity) -> dict[str, str]:
    return {
        "version": identity.version,
        "binary_sha256": identity.binary_sha256,
    }


def _assert_completed_rights_attestation(
    rights_attestation: Mapping[str, Any],
) -> None:
    try:
        assert_av1_validation_v4_rights_attestation(rights_attestation)
    except AV1ValidationV4RightsError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation rights attestation is invalid"
        ) from exc
    if rights_attestation.get("state") != AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation requires a completed owner rights attestation"
        )


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["preparation_id"] = av1_validation_v4_preparation_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_base_binding(payload: Mapping[str, Any]) -> None:
    if (
        payload.get("schema") != AV1_VALIDATION_V4_PREPARATION_SCHEMA
        or payload.get("schema_version")
        != AV1_VALIDATION_V4_PREPARATION_SCHEMA_VERSION
        or payload.get("protocol_version") != AV1_VALIDATION_V4_PROTOCOL_VERSION
        or payload.get("manifest_revision") != AV1_VALIDATION_V4_MANIFEST_REVISION
        or payload.get("experiment_id") != AV1_VALIDATION_V4_EXPERIMENT_ID
        or payload.get("state") != AV1_VALIDATION_V4_PREPARATION_STATE
        or payload.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or payload.get("manifest_payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or payload.get("discovery_public_sha256")
        != AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256
        or payload.get("valid_until") != AV1_VALIDATION_V4_VALID_UNTIL
        or payload.get("media_bytes_read") is not False
        or payload.get("builder_subprocess_executed") is not False
        or payload.get("media_processing_subprocess_executed") is not False
        or not isinstance(payload.get("tool_version_probe_subprocess_executed"), bool)
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation binding is invalid"
        )
    prepared_at = _parse_timestamp(payload.get("prepared_at"), "prepared_at")
    rights_attested_at = _parse_timestamp(
        payload.get("rights_attested_at"),
        "rights_attested_at",
    )
    active_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT, "revised_at")
    valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL, "valid_until")
    if not active_at <= rights_attested_at <= prepared_at < valid_until:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation timestamps are outside the manifest window"
        )
    if not _RIGHTS_ATTESTATION_ID_RE.fullmatch(
        str(payload.get("rights_attestation_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation rights attestation ID is invalid"
        )
    if not _SHA256_RE.fullmatch(
        str(payload.get("rights_attestation_payload_sha256") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation rights attestation digest is invalid"
        )


def _assert_false_authorities(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if payload.get(field) is not False:
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation cannot authorize {field}"
            )


def _assert_repository(payload: Mapping[str, Any]) -> None:
    repository = object_dict(payload.get("repository"))
    if set(repository) != {"commit", "tree"}:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation repository binding is invalid"
        )
    for field in ("commit", "tree"):
        if not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or "")):
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation repository {field} is invalid"
            )
    if not _SHA256_RE.fullmatch(str(payload.get("effective_config_sha256") or "")):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation effective config digest is invalid"
        )


def _assert_toolchain(payload: Mapping[str, Any]) -> None:
    toolchain = object_dict(payload.get("toolchain"))
    if set(toolchain) != {"ab_av1", "ffmpeg", "ffprobe"}:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation toolchain set is invalid"
        )
    for name, raw_identity in toolchain.items():
        identity = object_dict(raw_identity)
        if set(identity) != {"binary_sha256", "version"}:
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation {name} identity is invalid"
            )
        version = identity.get("version")
        if (
            not isinstance(version, str)
            or not version.strip()
            or len(version) > 512
        ):
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation {name} version is invalid"
            )
        if not _SHA256_RE.fullmatch(str(identity.get("binary_sha256") or "")):
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation {name} binary digest is invalid"
            )


def _assert_path_identities(payload: Mapping[str, Any]) -> None:
    instance_ids = object_dict(payload.get("dedicated_instance_path_hmac_ids"))
    if set(instance_ids) != _INSTANCE_PATH_ROLES:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation instance path identity set is invalid"
        )
    for value in instance_ids.values():
        if not _INSTANCE_PATH_HMAC_ID_RE.fullmatch(str(value or "")):
            raise AV1ValidationV4PreparationError(
                "AV1 v4 preparation instance path HMAC ID is invalid"
            )
    if len(set(instance_ids.values())) != len(instance_ids):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation instance path HMAC IDs must be unique"
        )
    source_ids = object_dict(payload.get("source_path_hmac_ids"))
    if set(source_ids) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation source path identity set is invalid"
        )
    for value in source_ids.values():
        if not _SOURCE_PATH_HMAC_ID_RE.fullmatch(str(value or "")):
            raise AV1ValidationV4PreparationError(
                "AV1 v4 preparation source path HMAC ID is invalid"
            )
    if len(set(source_ids.values())) != len(source_ids):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation source path HMAC IDs must be unique"
        )


def _assert_runtime_and_warm_start(payload: Mapping[str, Any]) -> None:
    if not _RUNTIME_COMPATIBILITY_ID_RE.fullmatch(
        str(payload.get("runtime_compatibility_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility ID is invalid"
        )
    if payload.get("runtime_compatibility_scope") != "host_toolchain_config":
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility scope is invalid"
        )
    if not _PATH_PRIVACY_KEY_ID_RE.fullmatch(
        str(payload.get("path_privacy_key_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation path privacy key ID is invalid"
        )
    warm_starts = object_dict(payload.get("guided_warm_start_identities"))
    expected_warm_starts = av1_validation_v4_guided_warm_start_identities()
    if warm_starts != expected_warm_starts:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation guided warm-start identities are invalid"
        )


def _assert_invocations(payload: Mapping[str, Any]) -> None:
    invocations = [object_dict(value) for value in object_list(payload.get("invocations"))]
    if len(invocations) != 8 or any(not invocation for invocation in invocations):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation invocation set is invalid"
        )
    role_rank = {"primary": 0, "confirmation": 1}
    expected_assets = tuple(
        asset_id
        for asset_id, content_class, role in sorted(
            AV1_VALIDATION_V4_SOURCE_LAYOUT,
            key=lambda item: (role_rank[item[2]], item[1]),
        )
    )
    expected = [
        (ordinal, asset_id, configuration, _mode_for_configuration(configuration))
        for ordinal, (asset_id, configuration) in enumerate(
            (
                (asset_id, configuration)
                for asset_id in expected_assets
                for configuration in AV1_VALIDATION_V4_CONFIGURATIONS
            ),
            start=1,
        )
    ]
    actual = [
        (
            invocation.get("ordinal"),
            invocation.get("asset_id"),
            invocation.get("configuration"),
            invocation.get("mode"),
        )
        for invocation in invocations
    ]
    if actual != expected:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation invocation order is invalid"
        )
    invocation_digests: list[str] = []
    base_config_by_source: dict[str, set[str]] = {}
    source_path_ids = object_dict(payload.get("source_path_hmac_ids"))
    for invocation in invocations:
        if set(invocation) != {
            "asset_id",
            "base_config_sha256",
            "configuration",
            "mode",
            "ordinal",
            "sha256",
            "source_path_hmac_id",
        }:
            raise AV1ValidationV4PreparationError(
                "AV1 v4 preparation invocation identity is invalid"
            )
        for field in ("base_config_sha256", "sha256"):
            if not _SHA256_RE.fullmatch(str(invocation.get(field) or "")):
                raise AV1ValidationV4PreparationError(
                    f"AV1 v4 preparation invocation {field} is invalid"
                )
        asset_id = str(invocation["asset_id"])
        if invocation.get("source_path_hmac_id") != source_path_ids.get(asset_id):
            raise AV1ValidationV4PreparationError(
                "AV1 v4 preparation invocation source path binding is invalid"
            )
        invocation_digests.append(str(invocation["sha256"]))
        base_config_by_source.setdefault(asset_id, set()).add(
            str(invocation["base_config_sha256"])
        )
    if len(set(invocation_digests)) != len(invocation_digests):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 traversal invocation digests must differ"
        )
    if any(len(digests) != 1 for digests in base_config_by_source.values()):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 baseline and guided base config digests must match within source"
        )


def _mode_for_configuration(configuration: str) -> str:
    if configuration == "balanced_full_search_baseline":
        return "baseline"
    if configuration == "balanced_frozen_search_hint":
        return "guided"
    raise AV1ValidationV4PreparationError(
        "AV1 v4 preparation configuration is invalid"
    )


def _assert_identity(payload: Mapping[str, Any]) -> None:
    preparation_id = str(payload.get("preparation_id") or "")
    if not _PREPARATION_ID_RE.fullmatch(preparation_id):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation ID is invalid"
        )
    if preparation_id != av1_validation_v4_preparation_id(payload):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation ID does not match its payload"
        )
    payload_without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    expected_sha256 = f"sha256:{stable_json_hash(payload_without_sha)}"
    if payload.get("payload_sha256") != expected_sha256:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation payload SHA-256 does not match"
        )


def _assert_no_private_paths(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child_value in value.values():
                visit(child_value)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            if (
                value.startswith("/")
                or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
                or any(prefix in value for prefix in _FORBIDDEN_PATH_PREFIXES)
            ):
                raise AV1ValidationV4PreparationError(
                    "AV1 v4 preparation exposes a machine-local path"
                )

    visit(payload)


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise AV1ValidationV4PreparationError(
            f"AV1 v4 preparation {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4PreparationError(
            f"AV1 v4 preparation {label} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise AV1ValidationV4PreparationError(
            f"AV1 v4 preparation {label} must be timezone-aware"
        )
    return parsed.astimezone(UTC)
