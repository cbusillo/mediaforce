"""Pure per-ordinal execution authority for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import re
from types import MappingProxyType
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight import (
    AV1V4R3ExecutionPreflightError,
    assert_av1_v4r3_execution_preflight,
    assert_av1_v4r3_ordinal_plan_preflight_pair,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
)
from mediaforce.tuning.av1_validation_v4r3_manifest_identity import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_ORDINAL_COUNT,
    assert_av1_v4r3_ordinal_window_grant,
)


AV1_V4R3_EXECUTION_GRANT_SCHEMA = "mediaforce.av1_cold_start_v4r3_execution_grant"
AV1_V4R3_EXECUTION_GRANT_SCHEMA_VERSION = 1
AV1_V4R3_EXECUTION_GRANT_CONTRACT_VERSION = "av1v4r3execgrant1"
AV1_V4R3_EXECUTION_GRANT_STATE = "owner_authorized_one_ordinal"
AV1_V4R3_EXECUTION_GRANT_AUTHORITY = "av1_v4r3_qualification_execution"
AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS = frozenset(
    {
        "media_read_authorized",
        "qualification_execution_authorized",
        "runtime_execution_authorized",
    }
)
AV1_V4R3_EXECUTION_GRANT_FALSE_FIELDS = (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    - AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS
)
AV1_V4R3_EXECUTION_GRANT_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "ordinal_scope": "exactly_one",
        "single_use_authorized": True,
        "adaptive_continuation_authorized": False,
        "resume_authorized": False,
        "substitution_authorized": False,
        "backfill_authorized": False,
    }
)

_GRANT_ID_RE = re.compile(r"av1v4r3execgrant_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAN_ID_RE = re.compile(r"av1vordplan4r3_[0-9a-f]{32}\Z")
_PREFLIGHT_ID_RE = re.compile(r"av1v4r3preflight_[0-9a-f]{32}\Z")
_REQUEST_ID_RE = re.compile(r"av1v4r3req_[0-9a-f]{32}\Z")
_FREEZE_ID_RE = re.compile(r"av1vfreeze4r3_[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"av1v4r3prepbundle_[0-9a-f]{32}\Z")
_CONFIG_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r3runtime_[0-9a-f]{32}\Z")
_SEQUENCING_GRANT_ID_RE = re.compile(r"av1vordgrant4r3_[0-9a-f]{32}\Z")
_CLOSURE_ID_RE = re.compile(r"av1vclosure4r3_[0-9a-f]{32}\Z")
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
            "grant_id",
            "payload_sha256",
            "state",
            "authority",
            "owner_principal",
            "authorized_at",
            "valid_until",
            "plan_id",
            "plan_payload_sha256",
            "preflight_id",
            "preflight_payload_sha256",
            "request_id",
            "request_payload_sha256",
            "freeze_id",
            "freeze_payload_sha256",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "effective_config_hmac_id",
            "path_privacy_key_id",
            "toolchain_id",
            "runtime_id",
            "reviewed_repository",
            "sequencing_grant_id",
            "sequencing_grant_payload_sha256",
            "ordinal",
            "asset_id",
            "closure_id",
            "invocation_sha256",
            "grant_scope",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3ExecutionGrantError(ValueError):
    pass


def build_av1_v4r3_execution_grant(
    *,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    try:
        assert_av1_v4r3_ordinal_plan_preflight_pair(plan=plan, preflight=preflight)
        assert_av1_v4r3_ordinal_window_grant(sequencing_grant)
    except (AV1V4R3ExecutionPreflightError, AV1V4R3OrdinalWindowError) as exc:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant predecessor chain is invalid"
        ) from exc
    preflight_payload = object_dict(preflight)
    sequencing_payload = object_dict(sequencing_grant)
    if sequencing_payload.get("plan_id") != plan.get(
        "plan_id"
    ) or sequencing_payload.get("plan_payload_sha256") != plan.get("payload_sha256"):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant sequencing grant does not match the plan"
        )
    if preflight_payload.get("owner_principal") != owner_principal:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant owner does not match the preflight"
        )
    ordinal = sequencing_payload.get("ordinal")
    readiness = _readiness_for_ordinal(preflight_payload, ordinal)
    _assert_window(
        authorized_at=authorized_at,
        valid_until=valid_until,
        plan=plan,
        preflight=preflight_payload,
        sequencing_grant=sequencing_payload,
    )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_EXECUTION_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_EXECUTION_GRANT_STATE,
        "authority": AV1_V4R3_EXECUTION_GRANT_AUTHORITY,
        "owner_principal": owner_principal,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "preflight_id": preflight_payload["preflight_id"],
        "preflight_payload_sha256": preflight_payload["payload_sha256"],
        "request_id": preflight_payload["r3_request_id"],
        "request_payload_sha256": preflight_payload["r3_request_payload_sha256"],
        "freeze_id": preflight_payload["freeze_id"],
        "freeze_payload_sha256": preflight_payload["freeze_payload_sha256"],
        "preparation_bundle_id": preflight_payload["preparation_bundle_id"],
        "preparation_bundle_payload_sha256": preflight_payload[
            "preparation_bundle_payload_sha256"
        ],
        "effective_config_hmac_id": preflight_payload["effective_config_hmac_id"],
        "path_privacy_key_id": preflight_payload["path_privacy_key_id"],
        "toolchain_id": preflight_payload["toolchain_id"],
        "runtime_id": preflight_payload["runtime_id"],
        "reviewed_repository": dict(
            object_dict(preflight_payload["reviewed_repository"])
        ),
        "sequencing_grant_id": sequencing_payload["grant_id"],
        "sequencing_grant_payload_sha256": sequencing_payload["payload_sha256"],
        "ordinal": ordinal,
        "asset_id": readiness["asset_id"],
        "closure_id": readiness["closure_id"],
        "invocation_sha256": readiness["invocation_sha256"],
        "grant_scope": dict(AV1_V4R3_EXECUTION_GRANT_SCOPE),
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    for field in AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS:
        payload[field] = True
    bound = _bind_identity(payload)
    assert_av1_v4r3_execution_grant_chain(
        plan=plan,
        preflight=preflight,
        sequencing_grant=sequencing_grant,
        execution_grant=bound,
    )
    return bound


def assert_av1_v4r3_execution_grant(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant shape is invalid")
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_EXECUTION_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_EXECUTION_GRANT_STATE,
        "authority": AV1_V4R3_EXECUTION_GRANT_AUTHORITY,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant binding is invalid")
    if not _canonical_equal(
        materialized.get("grant_scope"), dict(AV1_V4R3_EXECUTION_GRANT_SCOPE)
    ):
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant scope is invalid")
    _assert_exact_integers(
        materialized,
        {
            "schema_version": AV1_V4R3_EXECUTION_GRANT_SCHEMA_VERSION,
            "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
            "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        },
    )
    if any(
        materialized.get(field) is not True
        for field in AV1_V4R3_EXECUTION_GRANT_AUTHORIZED_FIELDS
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant authority is invalid"
        )
    if any(
        materialized.get(field) is not False
        for field in AV1_V4R3_EXECUTION_GRANT_FALSE_FIELDS
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant false authority is invalid"
        )
    _assert_identifiers(materialized)
    ordinal = materialized.get("ordinal")
    if type(ordinal) is not int or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant ordinal is invalid")
    _assert_owner(materialized.get("owner_principal"))
    _assert_repository(materialized.get("reviewed_repository"))
    _assert_window_values(materialized)
    grant_id = str(materialized.get("grant_id") or "")
    if not _GRANT_ID_RE.fullmatch(grant_id) or grant_id != av1_v4r3_execution_grant_id(
        materialized
    ):
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant ID is invalid")
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant digest is invalid")


def assert_av1_v4r3_execution_grant_chain(
    *,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
) -> None:
    try:
        assert_av1_v4r3_ordinal_plan_preflight_pair(plan=plan, preflight=preflight)
        assert_av1_v4r3_ordinal_window_grant(sequencing_grant)
        assert_av1_v4r3_execution_grant(execution_grant)
    except (
        AV1V4R3ExecutionPreflightError,
        AV1V4R3OrdinalWindowError,
        AV1V4R3ExecutionGrantError,
    ) as exc:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant chain is invalid"
        ) from exc
    plan_payload = object_dict(plan)
    preflight_payload = object_dict(preflight)
    sequencing_payload = object_dict(sequencing_grant)
    grant = object_dict(execution_grant)
    readiness = _readiness_for_ordinal(preflight_payload, grant["ordinal"])
    bindings = (
        (grant.get("plan_id"), plan_payload.get("plan_id")),
        (grant.get("plan_payload_sha256"), plan_payload.get("payload_sha256")),
        (sequencing_payload.get("plan_id"), plan_payload.get("plan_id")),
        (
            sequencing_payload.get("plan_payload_sha256"),
            plan_payload.get("payload_sha256"),
        ),
        (grant.get("preflight_id"), preflight_payload.get("preflight_id")),
        (
            grant.get("preflight_payload_sha256"),
            preflight_payload.get("payload_sha256"),
        ),
        (grant.get("request_id"), preflight_payload.get("r3_request_id")),
        (
            grant.get("request_payload_sha256"),
            preflight_payload.get("r3_request_payload_sha256"),
        ),
        (grant.get("freeze_id"), preflight_payload.get("freeze_id")),
        (
            grant.get("freeze_payload_sha256"),
            preflight_payload.get("freeze_payload_sha256"),
        ),
        (
            grant.get("preparation_bundle_id"),
            preflight_payload.get("preparation_bundle_id"),
        ),
        (
            grant.get("preparation_bundle_payload_sha256"),
            preflight_payload.get("preparation_bundle_payload_sha256"),
        ),
        (
            grant.get("effective_config_hmac_id"),
            preflight_payload.get("effective_config_hmac_id"),
        ),
        (
            grant.get("path_privacy_key_id"),
            preflight_payload.get("path_privacy_key_id"),
        ),
        (grant.get("toolchain_id"), preflight_payload.get("toolchain_id")),
        (grant.get("runtime_id"), preflight_payload.get("runtime_id")),
        (
            grant.get("reviewed_repository"),
            preflight_payload.get("reviewed_repository"),
        ),
        (grant.get("owner_principal"), preflight_payload.get("owner_principal")),
        (grant.get("sequencing_grant_id"), sequencing_payload.get("grant_id")),
        (
            grant.get("sequencing_grant_payload_sha256"),
            sequencing_payload.get("payload_sha256"),
        ),
        (grant.get("ordinal"), sequencing_payload.get("ordinal")),
        (grant.get("asset_id"), readiness.get("asset_id")),
        (grant.get("closure_id"), readiness.get("closure_id")),
        (grant.get("invocation_sha256"), readiness.get("invocation_sha256")),
    )
    if any(left != right for left, right in bindings):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant predecessor binding is invalid"
        )
    _assert_window(
        authorized_at=str(grant["authorized_at"]),
        valid_until=str(grant["valid_until"]),
        plan=plan_payload,
        preflight=preflight_payload,
        sequencing_grant=sequencing_payload,
    )


def assert_av1_v4r3_execution_grant_active(
    payload: Mapping[str, Any], *, as_of: str
) -> None:
    assert_av1_v4r3_execution_grant(payload)
    current = _parse_timestamp(as_of, "as_of")
    if (
        not _parse_timestamp(payload["authorized_at"], "authorized_at")
        <= current
        < _parse_timestamp(payload["valid_until"], "valid_until")
    ):
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant is not active")


def assert_av1_v4r3_execution_grant_for_ordinal(
    payload: Mapping[str, Any], *, ordinal: int
) -> None:
    assert_av1_v4r3_execution_grant(payload)
    if type(ordinal) is not int or payload.get("ordinal") != ordinal:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant does not authorize this ordinal"
        )


def serialize_av1_v4r3_execution_grant(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_execution_grant(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_execution_grant(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
        canonical = canonical_json_bytes(payload) + b"\n"
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant bytes are not canonical"
        )
    assert_av1_v4r3_execution_grant(payload)
    return payload


def av1_v4r3_execution_grant_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"grant_id", "payload_sha256"}
    }
    return f"av1v4r3execgrant_{stable_json_hash(semantic)[:32]}"


def _readiness_for_ordinal(
    preflight: Mapping[str, Any], ordinal: Any
) -> dict[str, Any]:
    if type(ordinal) is not int:
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant ordinal is invalid")
    for item in object_list(preflight.get("ordinal_readiness")):
        readiness = object_dict(item)
        if readiness.get("ordinal") == ordinal:
            return readiness
    raise AV1V4R3ExecutionGrantError(
        "AV1 v4 r3 execution grant readiness is unavailable"
    )


def _assert_window(
    *,
    authorized_at: str,
    valid_until: str,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
) -> None:
    authorized = _parse_timestamp(authorized_at, "authorized_at")
    valid = _parse_timestamp(valid_until, "valid_until")
    lower = max(
        _parse_timestamp(plan["plan_opens_at"], "plan opens_at"),
        _parse_timestamp(preflight["created_at"], "preflight created_at"),
        _parse_timestamp(sequencing_grant["authorized_at"], "sequencing authorized_at"),
    )
    upper = min(
        _parse_timestamp(plan["plan_closes_at"], "plan closes_at"),
        _parse_timestamp(preflight["valid_until"], "preflight valid_until"),
        _parse_timestamp(preflight["r3_request_valid_until"], "request valid_until"),
        _parse_timestamp(preflight["manifest_valid_until"], "manifest valid_until"),
        _parse_timestamp(sequencing_grant["valid_until"], "sequencing valid_until"),
    )
    if not lower <= authorized < valid <= upper:
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant window is invalid")


def _assert_window_values(payload: Mapping[str, Any]) -> None:
    if (
        not _parse_timestamp(payload["authorized_at"], "authorized_at")
        < _parse_timestamp(payload["valid_until"], "valid_until")
        <= _parse_timestamp(payload["manifest_valid_until"], "manifest valid_until")
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant timeline is invalid"
        )


def _assert_identifiers(payload: Mapping[str, Any]) -> None:
    patterns = {
        "plan_id": _PLAN_ID_RE,
        "preflight_id": _PREFLIGHT_ID_RE,
        "request_id": _REQUEST_ID_RE,
        "freeze_id": _FREEZE_ID_RE,
        "preparation_bundle_id": _BUNDLE_ID_RE,
        "effective_config_hmac_id": _CONFIG_ID_RE,
        "path_privacy_key_id": _KEY_ID_RE,
        "toolchain_id": _TOOLCHAIN_ID_RE,
        "runtime_id": _RUNTIME_ID_RE,
        "sequencing_grant_id": _SEQUENCING_GRANT_ID_RE,
        "closure_id": _CLOSURE_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant identity is invalid"
        )
    for field in (
        "manifest_payload_sha256",
        "payload_sha256",
        "plan_payload_sha256",
        "preflight_payload_sha256",
        "request_payload_sha256",
        "freeze_payload_sha256",
        "preparation_bundle_payload_sha256",
        "sequencing_grant_payload_sha256",
        "invocation_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3ExecutionGrantError(
                "AV1 v4 r3 execution grant digest identity is invalid"
            )
    if payload.get("asset_id") not in AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant asset is invalid")


def _assert_repository(value: Any) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant repository is invalid"
        )


def _assert_exact_integers(
    payload: Mapping[str, Any], expected: Mapping[str, int]
) -> None:
    if any(
        type(payload.get(field)) is not int or payload.get(field) != value
        for field, value in expected.items()
    ):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant integer binding is invalid"
        )


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except (TypeError, ValueError) as exc:
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant contains non-canonical data"
        ) from exc


def _assert_owner(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or av1_validation_v4_contains_private_text(value)
    ):
        raise AV1V4R3ExecutionGrantError("AV1 v4 r3 execution grant owner is invalid")


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_private_text(child)
    elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
        raise AV1V4R3ExecutionGrantError(
            "AV1 v4 r3 execution grant exposes private text"
        )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3ExecutionGrantError(
            f"AV1 v4 r3 execution grant {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3ExecutionGrantError(
            f"AV1 v4 r3 execution grant {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3ExecutionGrantError(
            f"AV1 v4 r3 execution grant {label} must use canonical UTC seconds"
        )
    return normalized


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["grant_id"] = av1_v4r3_execution_grant_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))
