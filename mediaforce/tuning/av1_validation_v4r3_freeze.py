"""Pure owner-freeze contract for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import json
import re
from types import MappingProxyType
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_APPROVED_AT,
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    AV1_V4R3_PREPARATION_BUNDLE_STATE,
    AV1_V4R3_PREPARATION_INVOCATION_COUNT,
    assert_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    assert_av1_v4r3_path_privacy_key_custody,
    assert_av1_v4r3_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    assert_av1_v4r3_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_measurement import (
    assert_av1_v4r3_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1_V4R3_RIGHTS_STATE,
    assert_av1_v4r3_rights_attestation,
)


AV1_V4R3_FREEZE_SCHEMA = "mediaforce.av1_cold_start_v4r3_owner_freeze"
AV1_V4R3_FREEZE_SCHEMA_VERSION = 1
AV1_V4R3_FREEZE_CONTRACT_VERSION = "av1v4r3freeze1"
AV1_V4R3_FREEZE_STATE = "owner_frozen_materialized"
AV1_V4R3_FREEZE_AUTHORITY = "av1_v4_manifest_revision_3_owner_freeze"
AV1_V4R3_FREEZE_APPROVAL_FIELD = "manifest_revision_3_owner_freeze_approved"
AV1_V4R3_FREEZE_MAX_DECISION_LAG_SECONDS = 86_400
AV1_V4R3_FREEZE_REGISTRY_KEY = (
    f"{AV1_V4R3_MANIFEST_ID}:revision:{AV1_V4_R3_MANIFEST_REVISION}"
)
AV1_V4R3_FREEZE_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "execution_grant_creation_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "qualification_request_creation_authorized": False,
        "retry_authorized": False,
        "reviewed_artifact_set": (
            "rights_grant_claim_custody_prepared_bundle_terminal_measurement"
        ),
    }
)

_FREEZE_ID_RE = re.compile(r"av1vfreeze4r3_[0-9a-f]{32}\Z")
_RIGHTS_ID_RE = re.compile(r"av1vrights4r3_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vprepgrant4r3_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1v4r3prepclaim_[0-9a-f]{32}\Z")
_CUSTODY_ID_RE = re.compile(r"av1v4r3keycustody_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"av1v4r3prepbundle_[0-9a-f]{32}\Z")
_MEASUREMENT_ID_RE = re.compile(r"av1v4r3prepmeas_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r3runtime_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
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
            "manifest_valid_until",
            "freeze_id",
            "payload_sha256",
            "state",
            "authority",
            "registry_key",
            "owner_principal",
            "decided_at",
            "reviewed_repository",
            "materializer_repository",
            "rights_attestation_id",
            "rights_attestation_payload_sha256",
            "rights_attested_at",
            "preparation_grant_id",
            "preparation_grant_payload_sha256",
            "preparation_grant_authorized_at",
            "preparation_grant_valid_until",
            "claim_id",
            "claim_payload_sha256",
            "claimed_at",
            "custody_id",
            "custody_payload_sha256",
            "path_privacy_key_id",
            "custody_created_at",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "preparation_bundle_state",
            "effective_config_hmac_id",
            "toolchain_id",
            "runtime_id",
            "closure_ids",
            "invocation_digests",
            "measurement_id",
            "measurement_payload_sha256",
            "measurement_state",
            "measurement_started_at",
            "measurement_completed_at",
            "freeze_scope",
            "selection_or_partition_use_allowed",
            AV1_V4R3_FREEZE_APPROVAL_FIELD,
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3FreezeError(ValueError):
    """Raised when a revision-3 owner-freeze invariant is violated."""


def build_av1_v4r3_owner_freeze(
    *,
    rights_attestation: Mapping[str, Any],
    preparation_grant: Mapping[str, Any],
    preparation_claim: Mapping[str, Any],
    key_custody: Mapping[str, Any],
    preparation_bundle: Mapping[str, Any],
    preparation_measurement: Mapping[str, Any],
    owner_principal: str,
    decided_at: str,
    materializer_repository_commit: str,
    materializer_repository_tree: str,
) -> dict[str, Any]:
    """Build one path-free owner freeze over existing successful evidence."""

    rights = object_dict(rights_attestation)
    grant = object_dict(preparation_grant)
    claim = object_dict(preparation_claim)
    custody = object_dict(key_custody)
    bundle = object_dict(preparation_bundle)
    measurement = object_dict(preparation_measurement)
    assert_av1_v4r3_rights_attestation(rights)
    assert_av1_v4r3_preparation_grant(grant)
    assert_av1_v4r3_preparation_claim(claim)
    assert_av1_v4r3_path_privacy_key_custody(custody)
    assert_av1_v4r3_preparation_bundle(bundle)
    assert_av1_v4r3_preparation_measurement(measurement)
    _assert_reviewed_chain(
        rights=rights,
        grant=grant,
        claim=claim,
        custody=custody,
        bundle=bundle,
        measurement=measurement,
        owner_principal=owner_principal,
        decided_at=decided_at,
    )
    materializer_repository = {
        "commit": materializer_repository_commit,
        "tree": materializer_repository_tree,
    }
    _assert_repository(materializer_repository, "materializer")
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_FREEZE_SCHEMA,
        "schema_version": AV1_V4R3_FREEZE_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_FREEZE_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_FREEZE_STATE,
        "authority": AV1_V4R3_FREEZE_AUTHORITY,
        "registry_key": AV1_V4R3_FREEZE_REGISTRY_KEY,
        "owner_principal": owner_principal,
        "decided_at": decided_at,
        "reviewed_repository": dict(object_dict(bundle["repository"])),
        "materializer_repository": materializer_repository,
        "rights_attestation_id": rights["attestation_id"],
        "rights_attestation_payload_sha256": rights["payload_sha256"],
        "rights_attested_at": rights["attested_at"],
        "preparation_grant_id": grant["grant_id"],
        "preparation_grant_payload_sha256": grant["payload_sha256"],
        "preparation_grant_authorized_at": grant["authorized_at"],
        "preparation_grant_valid_until": grant["valid_until"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "claimed_at": claim["claimed_at"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "path_privacy_key_id": custody["key_id"],
        "custody_created_at": custody["created_at"],
        "preparation_bundle_id": bundle["bundle_id"],
        "preparation_bundle_payload_sha256": bundle["payload_sha256"],
        "preparation_bundle_state": bundle["state"],
        "effective_config_hmac_id": bundle["effective_config_hmac_id"],
        "toolchain_id": bundle["toolchain_id"],
        "runtime_id": bundle["runtime_id"],
        "closure_ids": list(object_list(bundle["closure_ids"])),
        "invocation_digests": [
            dict(object_dict(item))
            for item in object_list(bundle["invocation_digests"])
        ],
        "measurement_id": measurement["measurement_id"],
        "measurement_payload_sha256": measurement["payload_sha256"],
        "measurement_state": measurement["state"],
        "measurement_started_at": measurement["started_at"],
        "measurement_completed_at": measurement["completed_at"],
        AV1_V4R3_FREEZE_APPROVAL_FIELD: True,
        "freeze_scope": dict(AV1_V4R3_FREEZE_SCOPE),
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_owner_freeze(bound)
    return bound


def assert_av1_v4r3_owner_freeze(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze shape is invalid")
    expected = {
        "schema": AV1_V4R3_FREEZE_SCHEMA,
        "schema_version": AV1_V4R3_FREEZE_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_FREEZE_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_FREEZE_STATE,
        "authority": AV1_V4R3_FREEZE_AUTHORITY,
        "registry_key": AV1_V4R3_FREEZE_REGISTRY_KEY,
        "preparation_bundle_state": AV1_V4R3_PREPARATION_BUNDLE_STATE,
        "measurement_state": "terminal_success",
        AV1_V4R3_FREEZE_APPROVAL_FIELD: True,
        "freeze_scope": dict(AV1_V4R3_FREEZE_SCOPE),
        "selection_or_partition_use_allowed": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze binding is invalid")
    if (
        any(
            type(materialized.get(field)) is not int
            for field in ("schema_version", "protocol_version", "manifest_revision")
        )
        or materialized.get(AV1_V4R3_FREEZE_APPROVAL_FIELD) is not True
        or materialized.get("selection_or_partition_use_allowed") is not False
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze scalar type is invalid")
    scope = object_dict(materialized.get("freeze_scope"))
    if (
        set(scope) != set(AV1_V4R3_FREEZE_SCOPE)
        or any(
            scope.get(field) is not False
            for field in (
                "execution_grant_creation_authorized",
                "manifest_payload_mutation_authorized",
                "qualification_request_creation_authorized",
                "retry_authorized",
            )
        )
        or scope.get("reviewed_artifact_set")
        != AV1_V4R3_FREEZE_SCOPE["reviewed_artifact_set"]
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze scope is invalid")
    _assert_no_private_text(materialized)
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3FreezeError(f"AV1 v4 r3 owner freeze cannot authorize {field}")
    _assert_repository(materialized.get("reviewed_repository"), "reviewed")
    _assert_repository(materialized.get("materializer_repository"), "materializer")
    _assert_identifiers(materialized)
    _assert_sha_fields(materialized)
    _assert_owner(materialized.get("owner_principal"))
    _assert_timeline(materialized)
    _assert_frozen_invocations(materialized)
    freeze_id = str(materialized.get("freeze_id") or "")
    if not _FREEZE_ID_RE.fullmatch(freeze_id) or freeze_id != _freeze_id(materialized):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze ID is invalid")
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze digest does not match")


def serialize_av1_v4r3_owner_freeze(payload: Mapping[str, Any]) -> bytes:
    try:
        materialized = json.loads(canonical_json_bytes(payload))
        assert_av1_v4r3_owner_freeze(materialized)
    except AV1V4R3FreezeError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze payload is invalid") from exc
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_owner_freeze(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
        canonical = canonical_json_bytes(payload) + b"\n"
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze bytes are unreadable") from exc
    if not isinstance(payload, dict) or data != canonical:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze bytes are not canonical")
    try:
        assert_av1_v4r3_owner_freeze(payload)
    except AV1V4R3FreezeError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze payload is invalid") from exc
    return payload


def _assert_reviewed_chain(
    *,
    rights: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    bundle: Mapping[str, Any],
    measurement: Mapping[str, Any],
    owner_principal: str,
    decided_at: str,
) -> None:
    if rights.get("state") != AV1_V4R3_RIGHTS_STATE:
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze rights are not attested")
    if (
        owner_principal != rights.get("owner_principal")
        or owner_principal != grant.get("owner_principal")
        or owner_principal != claim.get("owner_principal")
        or owner_principal != custody.get("owner_principal")
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze owner binding is invalid")
    chain_checks = (
        grant.get("rights_attestation_id") == rights.get("attestation_id"),
        grant.get("rights_attestation_payload_sha256") == rights.get("payload_sha256"),
        claim.get("preparation_grant_id") == grant.get("grant_id"),
        claim.get("preparation_grant_payload_sha256") == grant.get("payload_sha256"),
        claim.get("rights_attestation_id") == rights.get("attestation_id"),
        claim.get("rights_attestation_payload_sha256") == rights.get("payload_sha256"),
        custody.get("claim_id") == claim.get("claim_id"),
        custody.get("claim_payload_sha256") == claim.get("payload_sha256"),
        bundle.get("rights_attestation_id") == rights.get("attestation_id"),
        bundle.get("rights_attestation_payload_sha256") == rights.get("payload_sha256"),
        bundle.get("preparation_grant_id") == grant.get("grant_id"),
        bundle.get("preparation_grant_payload_sha256") == grant.get("payload_sha256"),
        bundle.get("claim_id") == claim.get("claim_id"),
        bundle.get("claim_payload_sha256") == claim.get("payload_sha256"),
        bundle.get("custody_id") == custody.get("custody_id"),
        bundle.get("custody_payload_sha256") == custody.get("payload_sha256"),
        bundle.get("path_privacy_key_id") == custody.get("key_id"),
        measurement.get("preparation_grant_id") == grant.get("grant_id"),
        measurement.get("claim_id") == claim.get("claim_id"),
        measurement.get("custody_id") == custody.get("custody_id"),
        measurement.get("preparation_bundle_id") == bundle.get("bundle_id"),
        measurement.get("preparation_bundle_payload_sha256")
        == bundle.get("payload_sha256"),
        measurement.get("effective_config_hmac_id")
        == bundle.get("effective_config_hmac_id"),
    )
    if not all(chain_checks):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze evidence chain is invalid")
    if (
        bundle.get("state") != AV1_V4R3_PREPARATION_BUNDLE_STATE
        or measurement.get("state") != "terminal_success"
        or measurement.get("failure") is not None
    ):
        raise AV1V4R3FreezeError(
            "AV1 v4 r3 owner freeze requires terminal-success preparation"
        )
    repositories = (
        object_dict(grant.get("repository")),
        object_dict(claim.get("repository")),
        object_dict(custody.get("repository")),
        object_dict(bundle.get("repository")),
    )
    if any(repository != repositories[0] for repository in repositories[1:]):
        raise AV1V4R3FreezeError(
            "AV1 v4 r3 owner freeze reviewed repository is inconsistent"
        )
    _assert_decision_window(
        rights_at=rights.get("attested_at"),
        grant_at=grant.get("authorized_at"),
        grant_valid_until=grant.get("valid_until"),
        claimed_at=claim.get("claimed_at"),
        custody_at=custody.get("created_at"),
        measurement_started_at=measurement.get("started_at"),
        measurement_completed_at=measurement.get("completed_at"),
        decided_at=decided_at,
    )


def _assert_timeline(payload: Mapping[str, Any]) -> None:
    _assert_decision_window(
        rights_at=payload.get("rights_attested_at"),
        grant_at=payload.get("preparation_grant_authorized_at"),
        grant_valid_until=payload.get("preparation_grant_valid_until"),
        claimed_at=payload.get("claimed_at"),
        custody_at=payload.get("custody_created_at"),
        measurement_started_at=payload.get("measurement_started_at"),
        measurement_completed_at=payload.get("measurement_completed_at"),
        decided_at=payload.get("decided_at"),
    )


def _assert_decision_window(
    *,
    rights_at: Any,
    grant_at: Any,
    grant_valid_until: Any,
    claimed_at: Any,
    custody_at: Any,
    measurement_started_at: Any,
    measurement_completed_at: Any,
    decided_at: Any,
) -> None:
    manifest_approved = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest approved"
    )
    rights = _parse_timestamp(rights_at, "rights attested")
    grant = _parse_timestamp(grant_at, "grant authorized")
    grant_until = _parse_timestamp(grant_valid_until, "grant valid until")
    claimed = _parse_timestamp(claimed_at, "claim")
    custody = _parse_timestamp(custody_at, "custody")
    measurement_started = _parse_timestamp(
        measurement_started_at, "measurement started"
    )
    measurement_completed = _parse_timestamp(
        measurement_completed_at, "measurement completed"
    )
    decision = _parse_timestamp(decided_at, "decision")
    manifest_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest valid until"
    )
    if (
        not manifest_approved
        <= rights
        <= grant
        <= claimed
        <= custody
        <= measurement_started
        <= measurement_completed
        <= decision
        or decision >= grant_until
        or decision >= manifest_until
        or decision - measurement_completed
        > timedelta(seconds=AV1_V4R3_FREEZE_MAX_DECISION_LAG_SECONDS)
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze timeline is invalid")


def _assert_frozen_invocations(payload: Mapping[str, Any]) -> None:
    closure_ids = object_list(payload.get("closure_ids"))
    invocations = object_list(payload.get("invocation_digests"))
    expected_closure_ids = [
        closure["closure_id"] for closure in build_av1_v4_r3_all_closure_payloads()
    ]
    if (
        len(closure_ids) != AV1_V4R3_PREPARATION_INVOCATION_COUNT
        or len(invocations) != AV1_V4R3_PREPARATION_INVOCATION_COUNT
        or closure_ids != expected_closure_ids
    ):
        raise AV1V4R3FreezeError(
            "AV1 v4 r3 owner freeze requires exactly eight invocations"
        )
    seen: set[str] = set()
    for index, item in enumerate(invocations, start=1):
        invocation = object_dict(item)
        digest = str(invocation.get("invocation_sha256") or "")
        if (
            set(invocation)
            != {
                "ordinal",
                "closure_id",
                "invocation_sha256",
                "r3_repository",
                "r3_toolchain_id",
            }
            or type(invocation.get("ordinal")) is not int
            or invocation.get("ordinal") != index
            or invocation.get("closure_id") != closure_ids[index - 1]
            or invocation.get("r3_repository") != payload.get("reviewed_repository")
            or invocation.get("r3_toolchain_id") != payload.get("toolchain_id")
            or not _SHA256_RE.fullmatch(digest)
            or digest in seen
        ):
            raise AV1V4R3FreezeError(
                "AV1 v4 r3 owner freeze invocation binding is invalid"
            )
        seen.add(digest)


def _assert_identifiers(payload: Mapping[str, Any]) -> None:
    patterns = {
        "rights_attestation_id": _RIGHTS_ID_RE,
        "preparation_grant_id": _GRANT_ID_RE,
        "claim_id": _CLAIM_ID_RE,
        "custody_id": _CUSTODY_ID_RE,
        "path_privacy_key_id": _KEY_ID_RE,
        "preparation_bundle_id": _BUNDLE_ID_RE,
        "measurement_id": _MEASUREMENT_ID_RE,
        "effective_config_hmac_id": _CONFIG_HMAC_ID_RE,
        "toolchain_id": _TOOLCHAIN_ID_RE,
        "runtime_id": _RUNTIME_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze identity is invalid")


def _assert_sha_fields(payload: Mapping[str, Any]) -> None:
    for field in (
        "manifest_payload_sha256",
        "rights_attestation_payload_sha256",
        "preparation_grant_payload_sha256",
        "claim_payload_sha256",
        "custody_payload_sha256",
        "preparation_bundle_payload_sha256",
        "measurement_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3FreezeError(f"AV1 v4 r3 owner freeze {field} is invalid")


def _assert_repository(value: Any, label: str) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3FreezeError(
            f"AV1 v4 r3 owner freeze {label} repository is invalid"
        )


def _assert_owner(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or av1_validation_v4_contains_private_text(value)
    ):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze principal is invalid")


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3FreezeError(f"AV1 v4 r3 owner freeze {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3FreezeError(f"AV1 v4 r3 owner freeze {label} is invalid") from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3FreezeError(
            f"AV1 v4 r3 owner freeze {label} must use canonical UTC seconds"
        )
    return normalized


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_private_text(child)
    elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
        raise AV1V4R3FreezeError("AV1 v4 r3 owner freeze exposes machine-local text")


def _freeze_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"freeze_id", "payload_sha256"}
    }
    return f"av1vfreeze4r3_{stable_json_hash(semantic)[:32]}"


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["freeze_id"] = _freeze_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))
