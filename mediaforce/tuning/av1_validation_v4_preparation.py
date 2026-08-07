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
    AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_SCOPE,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_LAYOUT,
    AV1_VALIDATION_V4_SOURCE_STREAM_SELECTION,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
    av1_validation_v4_guided_warm_start_identities,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1_VALIDATION_V4_RIGHTS_ATTESTED_STATE,
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    AV1ValidationV4PreparationGrantError,
    assert_av1_validation_v4_preparation_grant,
    assert_av1_validation_v4_preparation_grant_active,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    AV1ValidationV4PreparationClaimError,
    assert_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_runtime_compatibility import (
    AV1ValidationV4RuntimeCompatibilityError,
    av1_validation_v4_runtime_compatibility_id_from_payload,
)


AV1_VALIDATION_V4_PREPARATION_SCHEMA = (
    "mediaforce.av1_cold_start_v4_preparation_record"
)
AV1_VALIDATION_V4_PREPARATION_SCHEMA_VERSION = 4
AV1_VALIDATION_V4_PREPARATION_STATE = "prepared_unfrozen"

_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_PREPARATION_ID_RE = re.compile(r"av1vprep4_[0-9a-f]{32}\Z")
_RIGHTS_ATTESTATION_ID_RE = re.compile(r"av1vrights4_[0-9a-f]{32}\Z")
_PREPARATION_GRANT_ID_RE = re.compile(r"av1vprepgrant4_[0-9a-f]{32}\Z")
_PREPARATION_CLAIM_ID_RE = re.compile(r"av1vprepclaim4_[0-9a-f]{32}\Z")
_INSTANCE_PATH_HMAC_ID_RE = re.compile(r"av1vpath4_[0-9a-f]{32}\Z")
_SOURCE_PATH_HMAC_ID_RE = re.compile(r"av1vsource4_[0-9a-f]{32}\Z")
_RUNTIME_COMPATIBILITY_ID_RE = re.compile(r"av1vruntime4_[0-9a-f]{32}\Z")
_PATH_PRIVACY_KEY_ID_RE = re.compile(r"av1vpathkey4_[0-9a-f]{32}\Z")
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
    "media_bytes_read_count",
    "payload_sha256",
    "preparation_id",
    "preparation_grant_authorized_at",
    "preparation_grant_id",
    "preparation_grant_payload_sha256",
    "preparation_grant_valid_until",
    "preparation_claim_id",
    "preparation_claim_payload_sha256",
    "preparation_claimed_at",
    "prepared_at",
    "protocol_version",
    "path_privacy_key_id",
    "repository",
    "rights_attestation_id",
    "rights_attestation_payload_sha256",
    "rights_attested_at",
    "runtime_compatibility_id",
    "runtime_compatibility_payload",
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
    video_stream_index: int
    excluded_stream_indexes: Sequence[int]


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
    runtime_compatibility_payload: Mapping[str, Any]
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
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
) -> dict[str, Any]:
    rights = object_dict(rights_attestation)
    _assert_completed_rights_attestation(rights)
    grant = object_dict(preparation_grant)
    _assert_preparation_grant_bundle_binding(
        grant,
        rights,
        repository_commit=inputs.repository_commit,
        repository_tree=inputs.repository_tree,
        as_of=inputs.prepared_at,
    )
    claim = object_dict(preparation_claim)
    _assert_preparation_claim_bundle_binding(
        claim,
        grant,
        rights,
        repository_commit=inputs.repository_commit,
        repository_tree=inputs.repository_tree,
        prepared_at=inputs.prepared_at,
    )
    prepared_at = _parse_timestamp(inputs.prepared_at, "prepared_at")
    rights_attested_at = _parse_timestamp(
        str(rights.get("attested_at") or ""),
        "rights_attested_at",
    )
    if rights_attested_at > prepared_at:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 rights attestation cannot postdate preparation"
        )
    runtime_compatibility_payload = object_dict(
        inputs.runtime_compatibility_payload
    )
    try:
        runtime_compatibility_id = (
            av1_validation_v4_runtime_compatibility_id_from_payload(
                runtime_compatibility_payload
            )
        )
    except AV1ValidationV4RuntimeCompatibilityError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility payload is invalid"
        ) from exc
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
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "preparation_grant_authorized_at": grant["authorized_at"],
        "preparation_grant_valid_until": grant["valid_until"],
        "preparation_claim_id": claim["claim_id"],
        "preparation_claim_payload_sha256": claim["payload_sha256"],
        "preparation_claimed_at": claim["claimed_at"],
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
        "runtime_compatibility_id": runtime_compatibility_id,
        "runtime_compatibility_payload": runtime_compatibility_payload,
        "runtime_compatibility_scope": AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_SCOPE,
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
                "video_stream_index": invocation.video_stream_index,
                "excluded_stream_indexes": list(invocation.excluded_stream_indexes),
            }
            for invocation in inputs.invocations
        ],
        "path_privacy_key_id": inputs.path_privacy_key_id,
        "media_bytes_read": False,
        "media_bytes_read_count": 0,
        "builder_subprocess_executed": False,
        "media_processing_subprocess_executed": False,
        "tool_version_probe_subprocess_executed": (
            inputs.tool_version_probe_subprocess_executed
        ),
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_identity(payload)
    assert_av1_validation_v4_preparation_bundle(bound, rights, grant, claim)
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
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
) -> None:
    rights = object_dict(rights_attestation)
    _assert_completed_rights_attestation(rights)
    grant = object_dict(preparation_grant)
    try:
        assert_av1_validation_v4_preparation_grant(grant)
    except AV1ValidationV4PreparationGrantError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant is invalid"
        ) from exc
    claim = object_dict(preparation_claim)
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
    repository = object_dict(record.get("repository"))
    _assert_preparation_grant_bundle_binding(
        grant,
        rights,
        repository_commit=str(repository.get("commit") or ""),
        repository_tree=str(repository.get("tree") or ""),
        as_of=str(record.get("prepared_at") or ""),
    )
    _assert_preparation_claim_bundle_binding(
        claim,
        grant,
        rights,
        repository_commit=str(repository.get("commit") or ""),
        repository_tree=str(repository.get("tree") or ""),
        prepared_at=str(record.get("prepared_at") or ""),
    )
    if (
        record.get("preparation_grant_id") != grant.get("grant_id")
        or record.get("preparation_grant_payload_sha256")
        != grant.get("payload_sha256")
        or record.get("preparation_grant_authorized_at")
        != grant.get("authorized_at")
        or record.get("preparation_grant_valid_until")
        != grant.get("valid_until")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant binding does not match"
        )
    if (
        record.get("preparation_claim_id") != claim.get("claim_id")
        or record.get("preparation_claim_payload_sha256")
        != claim.get("payload_sha256")
        or record.get("preparation_claimed_at") != claim.get("claimed_at")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim binding does not match"
        )


def serialize_av1_validation_v4_preparation_record(
    payload: Mapping[str, Any],
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_preparation_bundle(
        materialized,
        rights_attestation,
        preparation_grant,
        preparation_claim,
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


def _assert_preparation_grant_bundle_binding(
    grant: Mapping[str, Any],
    rights: Mapping[str, Any],
    *,
    repository_commit: str,
    repository_tree: str,
    as_of: str,
) -> None:
    try:
        assert_av1_validation_v4_preparation_grant_active(grant, as_of=as_of)
    except AV1ValidationV4PreparationGrantError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation requires an active preparation grant"
        ) from exc
    grant_repository = object_dict(grant.get("repository"))
    if (
        grant.get("rights_attestation_id") != rights.get("attestation_id")
        or grant.get("rights_attestation_payload_sha256")
        != rights.get("payload_sha256")
        or grant.get("rights_attested_at") != rights.get("attested_at")
        or grant.get("owner_principal") != rights.get("owner_principal")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant rights binding does not match"
        )
    if (
        grant_repository.get("commit") != repository_commit
        or grant_repository.get("tree") != repository_tree
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant repository binding does not match"
        )


def _assert_preparation_claim_bundle_binding(
    claim: Mapping[str, Any],
    grant: Mapping[str, Any],
    rights: Mapping[str, Any],
    *,
    repository_commit: str,
    repository_tree: str,
    prepared_at: str,
) -> None:
    try:
        assert_av1_validation_v4_preparation_claim(claim)
    except AV1ValidationV4PreparationClaimError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim is invalid"
        ) from exc
    repository = object_dict(claim.get("repository"))
    if (
        claim.get("preparation_grant_id") != grant.get("grant_id")
        or claim.get("preparation_grant_payload_sha256")
        != grant.get("payload_sha256")
        or claim.get("rights_attestation_id") != rights.get("attestation_id")
        or claim.get("rights_attestation_payload_sha256")
        != rights.get("payload_sha256")
        or claim.get("owner_principal") != rights.get("owner_principal")
        or repository.get("commit") != repository_commit
        or repository.get("tree") != repository_tree
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim bundle binding does not match"
        )
    claimed_at = _parse_timestamp(claim.get("claimed_at"), "preparation_claimed_at")
    prepared = _parse_timestamp(prepared_at, "prepared_at")
    if claimed_at > prepared:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim cannot postdate preparation"
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
        or isinstance(payload.get("media_bytes_read_count"), bool)
        or not isinstance(payload.get("media_bytes_read_count"), int)
        or payload.get("media_bytes_read_count") != 0
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
    if not _PREPARATION_GRANT_ID_RE.fullmatch(
        str(payload.get("preparation_grant_id") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant ID is invalid"
        )
    if not _SHA256_RE.fullmatch(
        str(payload.get("preparation_grant_payload_sha256") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation grant digest is invalid"
        )
    grant_authorized_at = _parse_timestamp(
        payload.get("preparation_grant_authorized_at"),
        "preparation_grant_authorized_at",
    )
    grant_valid_until = _parse_timestamp(
        payload.get("preparation_grant_valid_until"),
        "preparation_grant_valid_until",
    )
    if not grant_authorized_at <= prepared_at < grant_valid_until:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation falls outside its grant window"
        )
    if not _PREPARATION_CLAIM_ID_RE.fullmatch(
        str(payload.get("preparation_claim_id") or "")
    ) or not _SHA256_RE.fullmatch(
        str(payload.get("preparation_claim_payload_sha256") or "")
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim identity is invalid"
        )
    claimed_at = _parse_timestamp(
        payload.get("preparation_claimed_at"),
        "preparation_claimed_at",
    )
    if not grant_authorized_at <= claimed_at <= prepared_at:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation claim timestamp is invalid"
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
    if (
        payload.get("runtime_compatibility_scope")
        != AV1_VALIDATION_V4_RUNTIME_COMPATIBILITY_SCOPE
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility scope is invalid"
        )
    runtime_payload = object_dict(payload.get("runtime_compatibility_payload"))
    try:
        expected_runtime_id = av1_validation_v4_runtime_compatibility_id_from_payload(
            runtime_payload
        )
    except AV1ValidationV4RuntimeCompatibilityError as exc:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility payload is invalid"
        ) from exc
    if payload.get("runtime_compatibility_id") != expected_runtime_id:
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility binding is invalid"
        )
    if (
        runtime_payload.get("effective_config_sha256")
        != payload.get("effective_config_sha256")
        or object_dict(runtime_payload.get("toolchain"))
        != object_dict(payload.get("toolchain"))
    ):
        raise AV1ValidationV4PreparationError(
            "AV1 v4 preparation runtime compatibility measurements do not match"
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
    if len(invocations) != AV1_VALIDATION_V4_TRAVERSAL_COUNT or any(not invocation for invocation in invocations):
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
            "excluded_stream_indexes",
            "mode",
            "ordinal",
            "sha256",
            "source_path_hmac_id",
            "video_stream_index",
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
        expected_streams = AV1_VALIDATION_V4_SOURCE_STREAM_SELECTION.get(asset_id)
        if expected_streams is None or (
            invocation.get("video_stream_index")
            != expected_streams["video_stream_index"]
            or invocation.get("excluded_stream_indexes")
            != list(expected_streams["excluded_stream_indexes"])
        ):
            raise AV1ValidationV4PreparationError(
                "AV1 v4 preparation invocation stream selection is invalid"
            )
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
            if av1_validation_v4_contains_private_text(value):
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
