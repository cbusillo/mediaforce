"""Verify-only execution authority for AV1 protocol-v4 revision 4.

This module intentionally has no grant or claim minting API.  Owner-created
artifacts can be asserted, deserialized, and have their public identifiers
recomputed, but callers must not be able to build, serialize, publish, or
materialize execution authority here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_EXPERIMENT_ID,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_MANIFEST_REVISION,
    AV1_V4R4_POLICY_VALUES_SHA256,
    AV1_V4R4_PROTOCOL_VERSION,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    assert_av1_v4r4_ordinal_registry_claim,
    assert_av1_v4r4_ordinal_registry_grant,
    assert_av1_v4r4_ordinal_registry_plan,
)


AV1_V4R4_EXECUTION_GRANT_SCHEMA = "mediaforce.av1_cold_start_v4r4_execution_grant"
AV1_V4R4_EXECUTION_GRANT_SCHEMA_VERSION = 1
AV1_V4R4_EXECUTION_GRANT_CONTRACT_VERSION = "av1v4r4execgrant1"
AV1_V4R4_EXECUTION_CLAIM_SCHEMA = "mediaforce.av1_cold_start_v4r4_execution_claim"
AV1_V4R4_EXECUTION_CLAIM_SCHEMA_VERSION = 1
AV1_V4R4_EXECUTION_CLAIM_CONTRACT_VERSION = "av1v4r4execclaim1"
AV1_V4R4_EXECUTION_GRANTED_AUTHORITY_FIELDS = frozenset(
    {
        "media_read_authorized",
        "qualification_execution_authorized",
        "runtime_execution_authorized",
    }
)

_COMMON_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "protocol_version",
    "manifest_revision",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
}
_GRANT_KEYS = _COMMON_KEYS | {
    "execution_grant_id",
    "payload_sha256",
    "owner_principal",
    "plan_id",
    "plan_payload_sha256",
    "sequencing_grant_id",
    "sequencing_grant_payload_sha256",
    "ordinal",
    "asset_id",
    "content_class",
    "role",
    "configuration",
    "target_size_bytes",
    "source_cap_total_bytes",
    "policy_values_sha256",
    "authorized_at",
    "valid_until",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_CLAIM_KEYS = _COMMON_KEYS | {
    "execution_claim_id",
    "payload_sha256",
    "owner_principal",
    "plan_id",
    "plan_payload_sha256",
    "sequencing_claim_id",
    "sequencing_claim_payload_sha256",
    "execution_grant_id",
    "execution_grant_payload_sha256",
    "ordinal",
    "claimed_at",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS

_EXECUTION_GRANT_ID_RE = re.compile(r"av1v4r4execgrant_[0-9a-f]{32}\Z")
_EXECUTION_CLAIM_ID_RE = re.compile(r"av1v4r4execclaim_[0-9a-f]{32}\Z")
_PLAN_ID_RE = re.compile(r"av1v4r4ordplan_[0-9a-f]{32}\Z")
_SEQUENCING_GRANT_ID_RE = re.compile(r"av1v4r4ordgrant_[0-9a-f]{32}\Z")
_SEQUENCING_CLAIM_ID_RE = re.compile(r"av1v4r4ordclaim_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OWNER_PRINCIPAL_RE = re.compile(r"[a-z][a-z0-9_.:@-]{2,127}\Z")


class AV1V4R4ExecutionAuthorityError(ValueError):
    """Raised when r4 execution authority is malformed or misbound."""


def av1_v4r4_execution_grant_public_id(payload: Mapping[str, Any]) -> str:
    semantic = _semantic_without(payload, "execution_grant_id")
    return "av1v4r4execgrant_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("execution-grant"), "payload": semantic}
    )[:32]


def av1_v4r4_execution_claim_public_id(payload: Mapping[str, Any]) -> str:
    semantic = _semantic_without(payload, "execution_claim_id")
    return "av1v4r4execclaim_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("execution-claim"), "payload": semantic}
    )[:32]


def av1_v4r4_execution_authority_payload_sha256(payload: Mapping[str, Any]) -> str:
    materialized = {key: value for key, value in dict(payload).items() if key != "payload_sha256"}
    return f"sha256:{stable_json_hash(materialized)}"


def assert_av1_v4r4_execution_grant(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _GRANT_KEYS:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution grant shape is invalid")
    _assert_common(
        materialized,
        schema=AV1_V4R4_EXECUTION_GRANT_SCHEMA,
        schema_version=AV1_V4R4_EXECUTION_GRANT_SCHEMA_VERSION,
        contract_version=AV1_V4R4_EXECUTION_GRANT_CONTRACT_VERSION,
    )
    ordinal = materialized.get("ordinal")
    layout = _layout_for_ordinal(ordinal)
    if (
        materialized.get("asset_id") != layout["asset_id"]
        or materialized.get("content_class") != layout["content_class"]
        or materialized.get("role") != layout["role"]
        or materialized.get("configuration") != layout["configuration"]
        or materialized.get("target_size_bytes") != layout["target_size_bytes"]
        or materialized.get("source_cap_total_bytes") != layout["source_cap_total_bytes"]
        or materialized.get("policy_values_sha256") != AV1_V4R4_POLICY_VALUES_SHA256
    ):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution grant ordinal binding is invalid")
    if not _OWNER_PRINCIPAL_RE.fullmatch(str(materialized.get("owner_principal") or "")):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution owner principal is invalid")
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
        or not _SEQUENCING_GRANT_ID_RE.fullmatch(str(materialized.get("sequencing_grant_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("sequencing_grant_payload_sha256") or ""))
    ):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution grant chain binding is invalid")
    authorized = _parse_ts(materialized.get("authorized_at"))
    valid = _parse_ts(materialized.get("valid_until"))
    if valid <= authorized:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution grant window is invalid")
    _assert_grant_authority(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="execution_grant_id",
        id_pattern=_EXECUTION_GRANT_ID_RE,
        expected_id=av1_v4r4_execution_grant_public_id(materialized),
    )


def assert_av1_v4r4_execution_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _CLAIM_KEYS:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution claim shape is invalid")
    _assert_common(
        materialized,
        schema=AV1_V4R4_EXECUTION_CLAIM_SCHEMA,
        schema_version=AV1_V4R4_EXECUTION_CLAIM_SCHEMA_VERSION,
        contract_version=AV1_V4R4_EXECUTION_CLAIM_CONTRACT_VERSION,
    )
    _layout_for_ordinal(materialized.get("ordinal"))
    if not _OWNER_PRINCIPAL_RE.fullmatch(str(materialized.get("owner_principal") or "")):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution owner principal is invalid")
    if (
        not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or ""))
        or not _SEQUENCING_CLAIM_ID_RE.fullmatch(str(materialized.get("sequencing_claim_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("sequencing_claim_payload_sha256") or ""))
        or not _EXECUTION_GRANT_ID_RE.fullmatch(str(materialized.get("execution_grant_id") or ""))
        or not _SHA256_RE.fullmatch(str(materialized.get("execution_grant_payload_sha256") or ""))
    ):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution claim chain binding is invalid")
    _parse_ts(materialized.get("claimed_at"))
    _assert_grant_authority(materialized)
    _assert_no_private_text(materialized)
    _assert_identity(
        materialized,
        id_field="execution_claim_id",
        id_pattern=_EXECUTION_CLAIM_ID_RE,
        expected_id=av1_v4r4_execution_claim_public_id(materialized),
    )


def assert_av1_v4r4_execution_chain(
    *,
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    now: datetime,
) -> None:
    plan_payload = dict(plan)
    seq_grant_payload = dict(sequencing_grant)
    seq_claim_payload = dict(sequencing_claim)
    exec_grant_payload = dict(execution_grant)
    exec_claim_payload = dict(execution_claim)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_grant(seq_grant_payload)
    assert_av1_v4r4_ordinal_registry_claim(seq_claim_payload)
    assert_av1_v4r4_execution_grant(exec_grant_payload)
    assert_av1_v4r4_execution_claim(exec_claim_payload)
    if now.tzinfo is None or now.utcoffset() is None:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution clock is invalid")
    current = now.astimezone(UTC).replace(microsecond=0)
    plan_opens = _parse_ts(plan_payload["plan_opens_at"])
    plan_closes = _parse_ts(plan_payload["plan_closes_at"])
    seq_opens = _parse_ts(seq_grant_payload["admission_opens_at"])
    seq_closes = _parse_ts(seq_grant_payload["admission_closes_at"])
    exec_opens = _parse_ts(exec_grant_payload["authorized_at"])
    exec_closes = _parse_ts(exec_grant_payload["valid_until"])
    if not plan_opens <= exec_opens < exec_closes <= plan_closes:
        raise AV1V4R4ExecutionAuthorityError(
            "AV1 v4 r4 execution grant window is outside the plan interval"
        )
    if not seq_opens <= exec_opens < exec_closes <= seq_closes:
        raise AV1V4R4ExecutionAuthorityError(
            "AV1 v4 r4 execution grant window is outside the sequencing interval"
        )
    if not plan_opens <= current < plan_closes:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution plan is inactive")
    if not seq_opens <= current < seq_closes:
        raise AV1V4R4ExecutionAuthorityError(
            "AV1 v4 r4 sequencing grant is inactive"
        )
    if not exec_opens <= current < exec_closes:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution grant is inactive")
    if (
        exec_grant_payload["plan_id"] != plan_payload["plan_id"]
        or exec_grant_payload["plan_payload_sha256"] != plan_payload["payload_sha256"]
        or exec_grant_payload["sequencing_grant_id"] != seq_grant_payload["grant_id"]
        or exec_grant_payload["sequencing_grant_payload_sha256"] != seq_grant_payload["payload_sha256"]
        or exec_claim_payload["plan_id"] != plan_payload["plan_id"]
        or exec_claim_payload["plan_payload_sha256"] != plan_payload["payload_sha256"]
        or exec_claim_payload["sequencing_claim_id"] != seq_claim_payload["claim_id"]
        or exec_claim_payload["sequencing_claim_payload_sha256"] != seq_claim_payload["payload_sha256"]
        or exec_claim_payload["execution_grant_id"] != exec_grant_payload["execution_grant_id"]
        or exec_claim_payload["execution_grant_payload_sha256"] != exec_grant_payload["payload_sha256"]
        or exec_claim_payload["owner_principal"] != exec_grant_payload["owner_principal"]
        or exec_claim_payload["ordinal"] != exec_grant_payload["ordinal"]
        or exec_grant_payload["ordinal"] != seq_grant_payload["ordinal"]
        or exec_claim_payload["ordinal"] != seq_claim_payload["ordinal"]
    ):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution chain binding is invalid")
    claimed_at = _parse_ts(exec_claim_payload["claimed_at"])
    if claimed_at > current:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution claim is in the future")
    if claimed_at < _parse_ts(seq_claim_payload["claimed_at"]):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution claim time regressed")
    if not exec_opens <= claimed_at < exec_closes:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution claim is outside the grant interval")
    if not seq_opens <= claimed_at < seq_closes:
        raise AV1V4R4ExecutionAuthorityError(
            "AV1 v4 r4 execution claim is outside the sequencing interval"
        )
    if not plan_opens <= claimed_at < plan_closes:
        raise AV1V4R4ExecutionAuthorityError(
            "AV1 v4 r4 execution claim is outside the plan interval"
        )


def deserialize_av1_v4r4_execution_grant(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r4_execution_grant, "grant")


def deserialize_av1_v4r4_execution_claim(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r4_execution_claim, "claim")


def _common_payload(*, schema: str, schema_version: int, contract_version: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "schema_version": schema_version,
        "contract_version": contract_version,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    }


def _assert_common(payload: Mapping[str, Any], *, schema: str, schema_version: int, contract_version: str) -> None:
    expected = _common_payload(schema=schema, schema_version=schema_version, contract_version=contract_version)
    if any(payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution artifact binding is invalid")


def _assert_grant_authority(payload: Mapping[str, Any]) -> None:
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        expected = field in AV1_V4R4_EXECUTION_GRANTED_AUTHORITY_FIELDS
        if payload.get(field) is not expected:
            raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution authority fields are invalid")


def _assert_identity(payload: Mapping[str, Any], *, id_field: str, id_pattern: re.Pattern[str], expected_id: str) -> None:
    artifact_id = str(payload.get(id_field) or "")
    if not id_pattern.fullmatch(artifact_id) or artifact_id != expected_id:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution artifact ID is invalid")
    if payload.get("payload_sha256") != av1_v4r4_execution_authority_payload_sha256(payload):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution artifact digest is invalid")


def _semantic_without(payload: Mapping[str, Any], id_field: str) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(payload).items()
        if key not in {id_field, "payload_sha256"}
    }


def _deserialize(data: bytes, assertion: Any, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R4ExecutionAuthorityError(f"AV1 v4 r4 execution {label} bytes are unreadable") from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R4ExecutionAuthorityError(f"AV1 v4 r4 execution {label} bytes are not canonical")
    assertion(payload)
    return payload


def _layout_for_ordinal(ordinal: Any) -> dict[str, Any]:
    layout = av1_v4r4_ordinal_layout()
    if type(ordinal) is not int or not 1 <= ordinal <= len(layout):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution ordinal is invalid")
    return layout[ordinal - 1]


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution timestamp is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution timestamp is invalid") from exc
    return parsed.replace(tzinfo=UTC)


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R4ExecutionAuthorityError("AV1 v4 r4 execution artifact contains machine-local text")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_text(child)
