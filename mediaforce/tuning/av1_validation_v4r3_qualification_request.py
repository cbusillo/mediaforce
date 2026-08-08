"""Pure qualification-request contract for AV1 protocol-v4 revision 3."""

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
from mediaforce.tuning.av1_validation_v4r3_freeze import (
    AV1V4R3FreezeError,
    AV1_V4R3_FREEZE_APPROVAL_FIELD,
    AV1_V4R3_FREEZE_SCOPE,
    AV1_V4R3_FREEZE_STATE,
    assert_av1_v4r3_owner_freeze,
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
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
)


AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_qualification_request"
)
AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA_VERSION = 1
AV1_V4R3_QUALIFICATION_REQUEST_CONTRACT_VERSION = "av1v4r3qreq1"
AV1_V4R3_QUALIFICATION_REQUEST_STATE = "owner_submitted_non_authorizing"
AV1_V4R3_QUALIFICATION_REQUEST_AUTHORITY = (
    "av1_v4_manifest_revision_3_qualification_request"
)
AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD = (
    "manifest_revision_3_qualification_request_approved"
)
AV1_V4R3_QUALIFICATION_REQUEST_MAX_SUBMISSION_LAG_SECONDS = 86_400
AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS = 172_800
AV1_V4R3_QUALIFICATION_REQUEST_REGISTRY_KEY = (
    f"{AV1_V4R3_MANIFEST_ID}:revision:{AV1_V4_R3_MANIFEST_REVISION}:"
    "qualification-request"
)
AV1_V4R3_QUALIFICATION_REQUEST_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "ordinal_window_plan_creation_authorized": False,
        "execution_preflight_creation_authorized": False,
        "execution_grant_creation_authorized": False,
        "retry_authorized": False,
        "resume_authorized": False,
        "substitution_authorized": False,
        "backfill_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "revision_2_artifact_reuse_authorized": False,
        "reviewed_artifact_set": "owner_frozen_revision_3_preparation_cohort",
        "requested_activity": "ordinal_window_planning_admission_only",
    }
)

if AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX:
    raise RuntimeError(
        "AV1 v4 r3 qualification request TTL cannot contain the public run maximum"
    )


_REQUEST_ID_RE = re.compile(r"av1v4r3req_[0-9a-f]{32}\Z")
_FREEZE_ID_RE = re.compile(r"av1vfreeze4r3_[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"av1v4r3prepbundle_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r3runtime_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REQUEST_SCOPE_FALSE_FIELDS = (
    "ordinal_window_plan_creation_authorized",
    "execution_preflight_creation_authorized",
    "execution_grant_creation_authorized",
    "retry_authorized",
    "resume_authorized",
    "substitution_authorized",
    "backfill_authorized",
    "manifest_payload_mutation_authorized",
    "revision_2_artifact_reuse_authorized",
)
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
            "request_id",
            "payload_sha256",
            "state",
            "authority",
            "registry_key",
            "owner_principal",
            "requested_at",
            "valid_until",
            "request_ttl_seconds",
            "reviewed_repository",
            "requesting_repository",
            "freeze_id",
            "freeze_payload_sha256",
            "freeze_decided_at",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "effective_config_hmac_id",
            "path_privacy_key_id",
            "toolchain_id",
            "runtime_id",
            "closure_ids",
            "invocation_digests",
            "requested_ordinal_count",
            "request_scope",
            "selection_or_partition_use_allowed",
            AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD,
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3QualificationRequestError(ValueError):
    """Raised when a revision-3 qualification request is invalid."""


def build_av1_v4r3_qualification_request(
    *,
    owner_freeze: Mapping[str, Any],
    owner_principal: str,
    requested_at: str,
    valid_until: str,
    requesting_repository_commit: str,
    requesting_repository_tree: str,
) -> dict[str, Any]:
    try:
        assert_av1_v4r3_owner_freeze(owner_freeze)
    except AV1V4R3FreezeError as exc:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request owner freeze is invalid"
        ) from exc
    freeze = object_dict(owner_freeze)
    _assert_owner(owner_principal)
    if freeze.get("owner_principal") != owner_principal:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request owner does not match the freeze"
        )
    if (
        freeze.get("state") != AV1_V4R3_FREEZE_STATE
        or freeze.get(AV1_V4R3_FREEZE_APPROVAL_FIELD) is not True
        or object_dict(freeze.get("freeze_scope")) != dict(AV1_V4R3_FREEZE_SCOPE)
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request freeze state is invalid"
        )
    requesting_repository = {
        "commit": requesting_repository_commit,
        "tree": requesting_repository_tree,
    }
    _assert_repository(requesting_repository, "requesting")
    request_ttl_seconds = _assert_timeline_values(
        freeze_decided_at=freeze.get("decided_at"),
        requested_at=requested_at,
        valid_until=valid_until,
    )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA,
        "schema_version": AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_QUALIFICATION_REQUEST_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_QUALIFICATION_REQUEST_STATE,
        "authority": AV1_V4R3_QUALIFICATION_REQUEST_AUTHORITY,
        "registry_key": AV1_V4R3_QUALIFICATION_REQUEST_REGISTRY_KEY,
        "owner_principal": owner_principal,
        "requested_at": requested_at,
        "valid_until": valid_until,
        "request_ttl_seconds": request_ttl_seconds,
        "reviewed_repository": dict(object_dict(freeze["reviewed_repository"])),
        "requesting_repository": requesting_repository,
        "freeze_id": freeze["freeze_id"],
        "freeze_payload_sha256": freeze["payload_sha256"],
        "freeze_decided_at": freeze["decided_at"],
        "preparation_bundle_id": freeze["preparation_bundle_id"],
        "preparation_bundle_payload_sha256": freeze[
            "preparation_bundle_payload_sha256"
        ],
        "effective_config_hmac_id": freeze["effective_config_hmac_id"],
        "path_privacy_key_id": freeze["path_privacy_key_id"],
        "toolchain_id": freeze["toolchain_id"],
        "runtime_id": freeze["runtime_id"],
        "closure_ids": list(object_list(freeze["closure_ids"])),
        "invocation_digests": [
            dict(object_dict(item))
            for item in object_list(freeze["invocation_digests"])
        ],
        "requested_ordinal_count": AV1_V4R3_OW_ORDINAL_COUNT,
        AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD: True,
        "request_scope": dict(AV1_V4R3_QUALIFICATION_REQUEST_SCOPE),
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_qualification_request(bound)
    return bound


def assert_av1_v4r3_qualification_request(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request shape is invalid"
        )
    expected = {
        "schema": AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA,
        "schema_version": AV1_V4R3_QUALIFICATION_REQUEST_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_QUALIFICATION_REQUEST_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_QUALIFICATION_REQUEST_STATE,
        "authority": AV1_V4R3_QUALIFICATION_REQUEST_AUTHORITY,
        "registry_key": AV1_V4R3_QUALIFICATION_REQUEST_REGISTRY_KEY,
        "requested_ordinal_count": AV1_V4R3_OW_ORDINAL_COUNT,
        AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD: True,
        "request_scope": dict(AV1_V4R3_QUALIFICATION_REQUEST_SCOPE),
        "selection_or_partition_use_allowed": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request binding is invalid"
        )
    if (
        any(
            type(materialized.get(field)) is not int
            for field in (
                "schema_version",
                "protocol_version",
                "manifest_revision",
                "request_ttl_seconds",
                "requested_ordinal_count",
            )
        )
        or materialized.get(AV1_V4R3_QUALIFICATION_REQUEST_APPROVAL_FIELD) is not True
        or materialized.get("selection_or_partition_use_allowed") is not False
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request scalar type is invalid"
        )
    _assert_request_scope(materialized.get("request_scope"))
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3QualificationRequestError(
                f"AV1 v4 r3 qualification request cannot authorize {field}"
            )
    _assert_no_private_text(materialized)
    _assert_repository(materialized.get("reviewed_repository"), "reviewed")
    _assert_repository(materialized.get("requesting_repository"), "requesting")
    _assert_owner(materialized.get("owner_principal"))
    _assert_identifiers(materialized)
    _assert_sha_fields(materialized)
    ttl_seconds = _assert_timeline_values(
        freeze_decided_at=materialized.get("freeze_decided_at"),
        requested_at=materialized.get("requested_at"),
        valid_until=materialized.get("valid_until"),
    )
    if materialized.get("request_ttl_seconds") != ttl_seconds:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request TTL does not match"
        )
    _assert_requested_invocations(materialized)
    request_id = str(materialized.get("request_id") or "")
    if not _REQUEST_ID_RE.fullmatch(
        request_id
    ) or request_id != av1_v4r3_qualification_request_id(materialized):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request ID is invalid"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request digest does not match"
        )


def serialize_av1_v4r3_qualification_request(payload: Mapping[str, Any]) -> bytes:
    try:
        materialized = json.loads(canonical_json_bytes(payload))
        assert_av1_v4r3_qualification_request(materialized)
    except AV1V4R3QualificationRequestError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request payload is invalid"
        ) from exc
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_qualification_request(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
        canonical = canonical_json_bytes(payload) + b"\n"
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request bytes are not canonical"
        )
    try:
        assert_av1_v4r3_qualification_request(payload)
    except AV1V4R3QualificationRequestError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request payload is invalid"
        ) from exc
    return payload


def av1_v4r3_qualification_request_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"request_id", "payload_sha256"}
    }
    return f"av1v4r3req_{stable_json_hash(semantic)[:32]}"


def av1_v4r3_qualification_request_valid_until(requested_at: str) -> str:
    requested = _parse_timestamp(requested_at, "requested_at")
    manifest_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest_valid_until"
    )
    valid_until = min(
        requested + timedelta(seconds=AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS),
        manifest_until,
    )
    if (valid_until - requested).total_seconds() <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request cannot contain the public run window"
        )
    return valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_request_scope(value: Any) -> None:
    scope = object_dict(value)
    if (
        set(scope) != set(AV1_V4R3_QUALIFICATION_REQUEST_SCOPE)
        or any(scope.get(field) is not False for field in _REQUEST_SCOPE_FALSE_FIELDS)
        or scope.get("reviewed_artifact_set")
        != AV1_V4R3_QUALIFICATION_REQUEST_SCOPE["reviewed_artifact_set"]
        or scope.get("requested_activity")
        != AV1_V4R3_QUALIFICATION_REQUEST_SCOPE["requested_activity"]
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request scope is invalid"
        )


def _assert_timeline_values(
    *, freeze_decided_at: Any, requested_at: Any, valid_until: Any
) -> int:
    manifest_approved = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest_approved_at"
    )
    freeze_decided = _parse_timestamp(freeze_decided_at, "freeze_decided_at")
    requested = _parse_timestamp(requested_at, "requested_at")
    valid = _parse_timestamp(valid_until, "valid_until")
    manifest_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest_valid_until"
    )
    expected_valid_until = min(
        requested + timedelta(seconds=AV1_V4R3_QUALIFICATION_REQUEST_TTL_SECONDS),
        manifest_until,
    )
    if not (manifest_approved <= freeze_decided <= requested < valid <= manifest_until):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request timeline is invalid"
        )
    if requested - freeze_decided > timedelta(
        seconds=AV1_V4R3_QUALIFICATION_REQUEST_MAX_SUBMISSION_LAG_SECONDS
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request submission lag is too long"
        )
    if valid != expected_valid_until:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request validity deadline is invalid"
        )
    ttl_seconds = int((valid - requested).total_seconds())
    if ttl_seconds <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX:
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request window is too short"
        )
    return ttl_seconds


def _assert_requested_invocations(payload: Mapping[str, Any]) -> None:
    closure_ids = object_list(payload.get("closure_ids"))
    invocations = object_list(payload.get("invocation_digests"))
    expected_closure_ids = [
        closure["closure_id"] for closure in build_av1_v4_r3_all_closure_payloads()
    ]
    if (
        len(closure_ids) != AV1_V4R3_OW_ORDINAL_COUNT
        or len(invocations) != AV1_V4R3_OW_ORDINAL_COUNT
        or closure_ids != expected_closure_ids
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request requires exactly eight invocations"
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
            raise AV1V4R3QualificationRequestError(
                "AV1 v4 r3 qualification request invocation binding is invalid"
            )
        seen.add(digest)


def _assert_identifiers(payload: Mapping[str, Any]) -> None:
    patterns = {
        "freeze_id": _FREEZE_ID_RE,
        "preparation_bundle_id": _BUNDLE_ID_RE,
        "effective_config_hmac_id": _CONFIG_HMAC_ID_RE,
        "path_privacy_key_id": _KEY_ID_RE,
        "toolchain_id": _TOOLCHAIN_ID_RE,
        "runtime_id": _RUNTIME_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request identity is invalid"
        )


def _assert_sha_fields(payload: Mapping[str, Any]) -> None:
    for field in (
        "manifest_payload_sha256",
        "freeze_payload_sha256",
        "preparation_bundle_payload_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3QualificationRequestError(
                f"AV1 v4 r3 qualification request {field} is invalid"
            )


def _assert_repository(value: Any, label: str) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3QualificationRequestError(
            f"AV1 v4 r3 qualification request {label} repository is invalid"
        )


def _assert_owner(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or av1_validation_v4_contains_private_text(value)
    ):
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request owner is invalid"
        )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3QualificationRequestError(
            f"AV1 v4 r3 qualification request {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3QualificationRequestError(
            f"AV1 v4 r3 qualification request {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3QualificationRequestError(
            f"AV1 v4 r3 qualification request {label} must use canonical UTC seconds"
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
        raise AV1V4R3QualificationRequestError(
            "AV1 v4 r3 qualification request exposes machine-local text"
        )


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["request_id"] = av1_v4r3_qualification_request_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))
