from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256,
    AV1_VALIDATION_V4_DRAFTED_AT,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_PREPARATION_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_record"
)
AV1_VALIDATION_V4_PREPARATION_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PREPARATION_STATE = "prepared_unfrozen"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_RIGHTS_ATTESTATION_ID_RE = re.compile(r"av1vrights4_[0-9a-f]{32}\Z")
_INSTANCE_PATH_HMAC_ID_RE = re.compile(r"av1vpath4_[0-9a-f]{32}\Z")
_SOURCE_PATH_HMAC_ID_RE = re.compile(r"av1vsource4_[0-9a-f]{32}\Z")
_RUNTIME_COMPATIBILITY_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_QUALIFICATION_KEY_ID_RE = re.compile(r"av1vqkey4_[0-9a-f]{32}\Z")
_SEARCH_SIGNATURE_ID_RE = re.compile(r"acss1_[A-Za-z0-9_.:-]{8,185}\Z")
_COHORT_ID_RE = re.compile(r"acsh1_[A-Za-z0-9_.:-]{8,185}\Z")
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
    "baseline_and_guided_invocation_sha256",
    "dedicated_instance_path_hmac_ids",
    "effective_config_sha256",
    "ffmpeg_sha256_and_version",
    "ffprobe_sha256_and_version",
    "guided_warm_start_identity",
    "media_bytes_must_not_be_read",
    "qualification_key_id",
    "repository_commit_and_tree",
    "required_before_owner_freeze",
    "runtime_compatibility_id",
    "source_path_hmac_ids",
})
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "dedicated_instance_path_hmac_ids",
    "discovery_public_sha256",
    "effective_config_sha256",
    "experiment_id",
    "guided_warm_start_identity",
    "invocations",
    "manifest_id",
    "manifest_payload_sha256",
    "media_bytes_read",
    "payload_sha256",
    "preparation_id",
    "prepared_at",
    "protocol_version",
    "qualification_key_id",
    "repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "rights_attested_at",
    "runtime_compatibility_id",
    "schema",
    "schema_version",
    "source_path_hmac_ids",
    "state",
    "subprocess_executed",
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
    guided_search_signature_id: str
    guided_cohort_id: str
    guided_warm_start_payload_sha256: str
    baseline_invocation_sha256: str
    baseline_base_config_sha256: str
    guided_invocation_sha256: str
    guided_base_config_sha256: str
    qualification_key_id: str


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
        "guided_warm_start_identity": {
            "search_signature_id": inputs.guided_search_signature_id,
            "cohort_id": inputs.guided_cohort_id,
            "payload_sha256": inputs.guided_warm_start_payload_sha256,
        },
        "invocations": {
            "baseline": {
                "sha256": inputs.baseline_invocation_sha256,
                "base_config_sha256": inputs.baseline_base_config_sha256,
            },
            "guided": {
                "sha256": inputs.guided_invocation_sha256,
                "base_config_sha256": inputs.guided_base_config_sha256,
            },
        },
        "qualification_key_id": inputs.qualification_key_id,
        "media_bytes_read": False,
        "subprocess_executed": False,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_preparation_bundle(bound, rights)
    return bound


def assert_av1_validation_v4_preparation_record(
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
    assert_av1_validation_v4_preparation_record(record)
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
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_record(materialized)
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
        or payload.get("experiment_id") != AV1_VALIDATION_V4_EXPERIMENT_ID
        or payload.get("state") != AV1_VALIDATION_V4_PREPARATION_STATE
        or payload.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or payload.get("manifest_payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or payload.get("discovery_public_sha256")
        != AV1_VALIDATION_V4_DISCOVERY_PUBLIC_SHA256
        or payload.get("valid_until") != AV1_VALIDATION_V4_VALID_UNTIL
        or payload.get("media_bytes_read") is not False
        or payload.get("subprocess_executed") is not False
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation binding is invalid"
        )
    prepared_at = _parse_timestamp(payload.get("prepared_at"), "prepared_at")
    rights_attested_at = _parse_timestamp(
        payload.get("rights_attested_at"),
        "rights_attested_at",
    )
    drafted_at = _parse_timestamp(AV1_VALIDATION_V4_DRAFTED_AT, "drafted_at")
    valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL, "valid_until")
    if not drafted_at <= rights_attested_at <= prepared_at < valid_until:
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
    if not _QUALIFICATION_KEY_ID_RE.fullmatch(
        str(payload.get("qualification_key_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation qualification key ID is invalid"
        )
    warm_start = object_dict(payload.get("guided_warm_start_identity"))
    if set(warm_start) != {"cohort_id", "payload_sha256", "search_signature_id"}:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation guided warm-start identity is invalid"
        )
    if not _SEARCH_SIGNATURE_ID_RE.fullmatch(
        str(warm_start.get("search_signature_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation search signature ID is invalid"
        )
    if not _COHORT_ID_RE.fullmatch(str(warm_start.get("cohort_id") or "")):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation cohort ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(warm_start.get("payload_sha256") or "")):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation warm-start digest is invalid"
        )


def _assert_invocations(payload: Mapping[str, Any]) -> None:
    invocations = object_dict(payload.get("invocations"))
    if set(invocations) != {"baseline", "guided"}:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation invocation set is invalid"
        )
    normalized: dict[str, dict[str, Any]] = {}
    for mode, raw_invocation in invocations.items():
        invocation = object_dict(raw_invocation)
        if set(invocation) != {"base_config_sha256", "sha256"}:
            raise AV1ValidationV4PreparationError(
                f"AV1 v4 preparation {mode} invocation is invalid"
            )
        for field in ("base_config_sha256", "sha256"):
            if not _SHA256_RE.fullmatch(str(invocation.get(field) or "")):
                raise AV1ValidationV4PreparationError(
                    f"AV1 v4 preparation {mode} {field} is invalid"
                )
        normalized[mode] = invocation
    if normalized["baseline"]["sha256"] == normalized["guided"]["sha256"]:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 baseline and guided invocation digests must differ"
        )
    if (
        normalized["baseline"]["base_config_sha256"]
        != normalized["guided"]["base_config_sha256"]
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 baseline and guided base config digests must match"
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
