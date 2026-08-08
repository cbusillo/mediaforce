"""Pure execution-readiness preflight for AV1 protocol-v4 revision 3."""

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
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_manifest_identity import (
    AV1_V4R3_MANIFEST_APPROVED_AT,
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
    assert_av1_v4r3_ordinal_window_plan,
    assert_av1_v4r3_ordinal_window_plan_draft,
    av1_v4r3_ordinal_window_plan_seam_sha256,
    build_av1_v4r3_ordinal_window_plan_draft,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request import (
    AV1V4R3QualificationRequestError,
    assert_av1_v4r3_qualification_request,
)


AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA = (
    "mediaforce.av1_cold_start_v4r3_execution_preflight"
)
AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA_VERSION = 1
AV1_V4R3_EXECUTION_PREFLIGHT_CONTRACT_VERSION = "av1v4r3execpreflight1"
AV1_V4R3_EXECUTION_PREFLIGHT_STATE = "owner_execution_ready_non_authorizing"
AV1_V4R3_EXECUTION_PREFLIGHT_AUTHORITY = (
    "av1_v4_manifest_revision_3_execution_preflight"
)
AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD = (
    "manifest_revision_3_execution_preflight_approved"
)
AV1_V4R3_EXECUTION_PREFLIGHT_REGISTRY_KEY = (
    f"{AV1_V4R3_MANIFEST_ID}:revision:{AV1_V4_R3_MANIFEST_REVISION}:execution-preflight"
)
AV1_V4R3_EXECUTION_PREFLIGHT_READINESS_STATE = "ready_pending_owner_execution_grant"
AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE: Mapping[str, object] = MappingProxyType(
    {
        "execution_grant_creation_authorized": False,
        "ordinal_grant_creation_authorized": False,
        "media_read_authorized": False,
        "retry_authorized": False,
        "resume_authorized": False,
        "substitution_authorized": False,
        "backfill_authorized": False,
        "manifest_payload_mutation_authorized": False,
        "revision_2_artifact_reuse_authorized": False,
        "reviewed_artifact_set": "owner_frozen_revision_3_qualification_cohort",
        "requested_activity": "execution_readiness_assessment_only",
    }
)
AV1_V4R3_EXECUTION_PREFLIGHT_READINESS: Mapping[str, bool] = MappingProxyType(
    {
        "all_invocation_digests_match": True,
        "all_closure_ids_match": True,
        "aggregate_budget_fits": True,
        "request_active_at_created_at": True,
        "repository_clean": True,
        "repository_matches_reviewed": True,
        "media_bytes_read": False,
    }
)


_PREFLIGHT_ID_RE = re.compile(r"av1v4r3preflight_[0-9a-f]{32}\Z")
_REQUEST_ID_RE = re.compile(r"av1v4r3req_[0-9a-f]{32}\Z")
_FREEZE_ID_RE = re.compile(r"av1vfreeze4r3_[0-9a-f]{32}\Z")
_BUNDLE_ID_RE = re.compile(r"av1v4r3prepbundle_[0-9a-f]{32}\Z")
_CONFIG_HMAC_ID_RE = re.compile(r"av1v4r3confighmac_[0-9a-f]{32}\Z")
_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"av1v4r3runtime_[0-9a-f]{32}\Z")
_CLOSURE_ID_RE = re.compile(r"av1vclosure4r3_[0-9a-f]{32}\Z")
_LEDGER_CLOSURE_ID_RE = re.compile(r"av1v4r3ledger_[0-9a-f]{32}\Z")
_SIZE_GOAL_ID_RE = re.compile(r"av1v4r3size_[0-9a-f]{32}\Z")
_TRANSFORM_PLAN_ID_RE = re.compile(r"tp1_[0-9a-f]{32}\Z")
_RUN_REGISTRY_ID_RE = re.compile(r"av1v4r3runreg_[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SCOPE_FALSE_FIELDS = (
    "execution_grant_creation_authorized",
    "ordinal_grant_creation_authorized",
    "media_read_authorized",
    "retry_authorized",
    "resume_authorized",
    "substitution_authorized",
    "backfill_authorized",
    "manifest_payload_mutation_authorized",
    "revision_2_artifact_reuse_authorized",
)
_ORDINAL_READINESS_KEYS = {
    "ordinal",
    "asset_id",
    "closure_id",
    "ledger_closure_id",
    "size_goal_id",
    "transform_plan_id",
    "invocation_sha256",
    "digest_matches_request",
    "closure_order_matches_manifest",
    "media_read_performed",
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
            "manifest_valid_until",
            "preflight_id",
            "payload_sha256",
            "state",
            "authority",
            "registry_key",
            "owner_principal",
            "created_at",
            "valid_until",
            "r3_request_id",
            "r3_request_payload_sha256",
            "r3_request_requested_at",
            "r3_request_valid_until",
            "freeze_id",
            "freeze_payload_sha256",
            "preparation_bundle_id",
            "preparation_bundle_payload_sha256",
            "effective_config_hmac_id",
            "path_privacy_key_id",
            "toolchain_id",
            "runtime_id",
            "reviewed_repository",
            "requesting_repository",
            "preflight_repository",
            "r3_closure_ids",
            "r3_invocation_digests",
            "ordinal_readiness",
            "ordinal_window_plan_seam_sha256",
            "run_registry_id",
            "plan_opens_at",
            "plan_closes_at",
            "plan_span_seconds",
            "readiness",
            "blockers",
            "execution_readiness_state",
            "unresolved_execution_bindings",
            AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD,
            "preflight_scope",
            "selection_or_partition_use_allowed",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3ExecutionPreflightError(ValueError):
    """Raised when a revision-3 execution preflight is invalid."""


def build_av1_v4r3_execution_preflight(
    *,
    qualification_request: Mapping[str, Any],
    ordinal_window_plan_draft: Mapping[str, Any],
    owner_principal: str,
    preflight_repository_commit: str,
    preflight_repository_tree: str,
    created_at: str,
) -> dict[str, Any]:
    try:
        assert_av1_v4r3_qualification_request(qualification_request)
    except AV1V4R3QualificationRequestError as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight request is invalid"
        ) from exc
    try:
        assert_av1_v4r3_ordinal_window_plan_draft(ordinal_window_plan_draft)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight plan draft is invalid"
        ) from exc
    request = object_dict(qualification_request)
    draft = object_dict(ordinal_window_plan_draft)
    _assert_owner(owner_principal)
    if request.get("owner_principal") != owner_principal:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight owner does not match the request"
        )
    preflight_repository = {
        "commit": preflight_repository_commit,
        "tree": preflight_repository_tree,
    }
    _assert_repository(preflight_repository, "preflight")
    _assert_build_bindings(
        request=request,
        draft=draft,
        preflight_repository=preflight_repository,
        created_at=created_at,
    )
    closures = build_av1_v4_r3_all_closure_payloads()
    invocation_digests = [
        dict(object_dict(item)) for item in object_list(request["invocation_digests"])
    ]
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_EXECUTION_PREFLIGHT_STATE,
        "authority": AV1_V4R3_EXECUTION_PREFLIGHT_AUTHORITY,
        "registry_key": AV1_V4R3_EXECUTION_PREFLIGHT_REGISTRY_KEY,
        "owner_principal": owner_principal,
        "created_at": created_at,
        "valid_until": draft["plan_closes_at"],
        "r3_request_id": request["request_id"],
        "r3_request_payload_sha256": request["payload_sha256"],
        "r3_request_requested_at": request["requested_at"],
        "r3_request_valid_until": request["valid_until"],
        "freeze_id": request["freeze_id"],
        "freeze_payload_sha256": request["freeze_payload_sha256"],
        "preparation_bundle_id": request["preparation_bundle_id"],
        "preparation_bundle_payload_sha256": request[
            "preparation_bundle_payload_sha256"
        ],
        "effective_config_hmac_id": request["effective_config_hmac_id"],
        "path_privacy_key_id": request["path_privacy_key_id"],
        "toolchain_id": request["toolchain_id"],
        "runtime_id": request["runtime_id"],
        "reviewed_repository": dict(object_dict(request["reviewed_repository"])),
        "requesting_repository": dict(object_dict(request["requesting_repository"])),
        "preflight_repository": preflight_repository,
        "r3_closure_ids": list(object_list(request["closure_ids"])),
        "r3_invocation_digests": invocation_digests,
        "ordinal_readiness": _build_ordinal_readiness(closures, invocation_digests),
        "ordinal_window_plan_seam_sha256": (
            av1_v4r3_ordinal_window_plan_seam_sha256(draft)
        ),
        "run_registry_id": draft["run_registry_id"],
        "plan_opens_at": draft["plan_opens_at"],
        "plan_closes_at": draft["plan_closes_at"],
        "plan_span_seconds": draft["plan_span_seconds"],
        "readiness": dict(AV1_V4R3_EXECUTION_PREFLIGHT_READINESS),
        "blockers": [],
        "execution_readiness_state": (AV1_V4R3_EXECUTION_PREFLIGHT_READINESS_STATE),
        "unresolved_execution_bindings": _build_unresolved_execution_bindings(closures),
        AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD: True,
        "preflight_scope": dict(AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE),
        "selection_or_partition_use_allowed": False,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r3_execution_preflight(bound)
    return bound


def assert_av1_v4r3_execution_preflight(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ALLOWED_KEYS:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight shape is invalid"
        )
    expected = {
        "schema": AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "manifest_id": AV1_V4R3_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        "manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
        "state": AV1_V4R3_EXECUTION_PREFLIGHT_STATE,
        "authority": AV1_V4R3_EXECUTION_PREFLIGHT_AUTHORITY,
        "registry_key": AV1_V4R3_EXECUTION_PREFLIGHT_REGISTRY_KEY,
        "readiness": dict(AV1_V4R3_EXECUTION_PREFLIGHT_READINESS),
        "blockers": [],
        "execution_readiness_state": (AV1_V4R3_EXECUTION_PREFLIGHT_READINESS_STATE),
        AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD: True,
        "preflight_scope": dict(AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE),
        "selection_or_partition_use_allowed": False,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight binding is invalid"
        )
    if (
        any(
            type(materialized.get(field)) is not int
            for field in (
                "schema_version",
                "protocol_version",
                "manifest_revision",
                "plan_span_seconds",
            )
        )
        or materialized.get(AV1_V4R3_EXECUTION_PREFLIGHT_APPROVAL_FIELD) is not True
        or materialized.get("selection_or_partition_use_allowed") is not False
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight scalar type is invalid"
        )
    _assert_scope(materialized.get("preflight_scope"))
    _assert_readiness(materialized.get("readiness"))
    if object_list(materialized.get("blockers")) != []:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight blockers must be empty"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3ExecutionPreflightError(
                f"AV1 v4 r3 execution preflight cannot authorize {field}"
            )
    _assert_no_private_text(materialized)
    _assert_owner(materialized.get("owner_principal"))
    for field in (
        "reviewed_repository",
        "requesting_repository",
        "preflight_repository",
    ):
        _assert_repository(materialized.get(field), field)
    if not (
        materialized.get("reviewed_repository")
        == materialized.get("requesting_repository")
        == materialized.get("preflight_repository")
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight repositories do not match"
        )
    _assert_identifiers(materialized)
    _assert_sha_fields(materialized)
    _assert_timeline(materialized)
    closures = build_av1_v4_r3_all_closure_payloads()
    invocation_digests = _assert_invocations(materialized, closures)
    if materialized.get("ordinal_window_plan_seam_sha256") != _expected_plan_seam(
        materialized
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight plan seam does not match"
        )
    if object_list(materialized.get("ordinal_readiness")) != _build_ordinal_readiness(
        closures, invocation_digests
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight ordinal readiness is invalid"
        )
    if object_dict(
        materialized.get("unresolved_execution_bindings")
    ) != _build_unresolved_execution_bindings(closures):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight unresolved bindings are invalid"
        )
    preflight_id = str(materialized.get("preflight_id") or "")
    if not _PREFLIGHT_ID_RE.fullmatch(
        preflight_id
    ) or preflight_id != av1_v4r3_execution_preflight_id(materialized):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight ID is invalid"
        )
    without_sha = {
        key: value for key, value in materialized.items() if key != "payload_sha256"
    }
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight digest does not match"
        )


def serialize_av1_v4r3_execution_preflight(payload: Mapping[str, Any]) -> bytes:
    try:
        materialized = json.loads(canonical_json_bytes(payload))
        assert_av1_v4r3_execution_preflight(materialized)
    except AV1V4R3ExecutionPreflightError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight payload is invalid"
        ) from exc
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_execution_preflight(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
        canonical = canonical_json_bytes(payload) + b"\n"
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight bytes are unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight bytes are not canonical"
        )
    try:
        assert_av1_v4r3_execution_preflight(payload)
    except AV1V4R3ExecutionPreflightError:
        raise
    except (TypeError, ValueError) as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight payload is invalid"
        ) from exc
    return payload


def av1_v4r3_execution_preflight_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"preflight_id", "payload_sha256"}
    }
    return f"av1v4r3preflight_{stable_json_hash(semantic)[:32]}"


def assert_av1_v4r3_ordinal_plan_preflight_pair(
    *, plan: Mapping[str, Any], preflight: Mapping[str, Any]
) -> None:
    try:
        assert_av1_v4r3_ordinal_window_plan(plan)
        assert_av1_v4r3_execution_preflight(preflight)
    except (AV1V4R3OrdinalWindowError, AV1V4R3ExecutionPreflightError) as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 ordinal plan and preflight pair is invalid"
        ) from exc
    plan_payload = object_dict(plan)
    preflight_payload = object_dict(preflight)
    bindings = (
        (plan_payload.get("r3_preflight_id"), preflight_payload.get("preflight_id")),
        (
            av1_v4r3_ordinal_window_plan_seam_sha256(plan_payload),
            preflight_payload.get("ordinal_window_plan_seam_sha256"),
        ),
        (plan_payload.get("r3_manifest_id"), preflight_payload.get("manifest_id")),
        (
            plan_payload.get("r3_manifest_valid_until"),
            preflight_payload.get("manifest_valid_until"),
        ),
        (plan_payload.get("r3_request_id"), preflight_payload.get("r3_request_id")),
        (
            plan_payload.get("r3_request_valid_until"),
            preflight_payload.get("r3_request_valid_until"),
        ),
        (
            plan_payload.get("r3_repository"),
            preflight_payload.get("reviewed_repository"),
        ),
        (plan_payload.get("r3_toolchain_id"), preflight_payload.get("toolchain_id")),
        (plan_payload.get("r3_closure_ids"), preflight_payload.get("r3_closure_ids")),
        (
            plan_payload.get("r3_invocation_digests"),
            preflight_payload.get("r3_invocation_digests"),
        ),
        (plan_payload.get("run_registry_id"), preflight_payload.get("run_registry_id")),
        (plan_payload.get("plan_opens_at"), preflight_payload.get("plan_opens_at")),
        (plan_payload.get("plan_closes_at"), preflight_payload.get("plan_closes_at")),
        (
            plan_payload.get("plan_span_seconds"),
            preflight_payload.get("plan_span_seconds"),
        ),
    )
    if any(left != right for left, right in bindings):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 ordinal plan and preflight bindings do not match"
        )


def _assert_build_bindings(
    *,
    request: Mapping[str, Any],
    draft: Mapping[str, Any],
    preflight_repository: Mapping[str, Any],
    created_at: str,
) -> None:
    expected_pairs = (
        (draft.get("r3_manifest_id"), request.get("manifest_id")),
        (draft.get("r3_manifest_valid_until"), request.get("manifest_valid_until")),
        (draft.get("r3_request_id"), request.get("request_id")),
        (draft.get("r3_request_valid_until"), request.get("valid_until")),
        (draft.get("r3_repository"), request.get("reviewed_repository")),
        (draft.get("r3_toolchain_id"), request.get("toolchain_id")),
        (draft.get("r3_closure_ids"), request.get("closure_ids")),
        (draft.get("r3_invocation_digests"), request.get("invocation_digests")),
    )
    if any(left != right for left, right in expected_pairs):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight request and plan do not match"
        )
    if not (
        request.get("reviewed_repository")
        == request.get("requesting_repository")
        == preflight_repository
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight repository drift is not allowed"
        )
    opens = _parse_timestamp(draft.get("plan_opens_at"), "plan_opens_at")
    created = _parse_timestamp(created_at, "created_at")
    requested = _parse_timestamp(request.get("requested_at"), "request_requested_at")
    request_until = _parse_timestamp(request.get("valid_until"), "request_valid_until")
    if created != opens or not (requested <= created < request_until):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight creation time is invalid"
        )


def _assert_timeline(payload: Mapping[str, Any]) -> None:
    manifest_approved = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest_approved_at"
    )
    request_requested = _parse_timestamp(
        payload.get("r3_request_requested_at"), "request_requested_at"
    )
    request_until = _parse_timestamp(
        payload.get("r3_request_valid_until"), "request_valid_until"
    )
    created = _parse_timestamp(payload.get("created_at"), "created_at")
    valid = _parse_timestamp(payload.get("valid_until"), "valid_until")
    plan_opens = _parse_timestamp(payload.get("plan_opens_at"), "plan_opens_at")
    plan_closes = _parse_timestamp(payload.get("plan_closes_at"), "plan_closes_at")
    manifest_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest_valid_until"
    )
    if not (
        manifest_approved
        <= request_requested
        <= created
        == plan_opens
        < valid
        == plan_closes
        < request_until
        <= manifest_until
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight timeline is invalid"
        )
    span_seconds = int((plan_closes - plan_opens).total_seconds())
    if payload.get("plan_span_seconds") != span_seconds or not (
        AV1_V4R3_OW_AGGREGATE_SECONDS_MAX
        <= span_seconds
        <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight plan span is invalid"
        )


def _expected_plan_seam(payload: Mapping[str, Any]) -> str:
    try:
        draft = build_av1_v4r3_ordinal_window_plan_draft(
            r3_manifest_id=str(payload["manifest_id"]),
            r3_manifest_valid_until=str(payload["manifest_valid_until"]),
            r3_request_id=str(payload["r3_request_id"]),
            r3_request_valid_until=str(payload["r3_request_valid_until"]),
            r3_repository=object_dict(payload["reviewed_repository"]),
            r3_toolchain_id=str(payload["toolchain_id"]),
            r3_closure_ids=list(object_list(payload["r3_closure_ids"])),
            r3_invocation_digests=[
                dict(object_dict(item))
                for item in object_list(payload["r3_invocation_digests"])
            ],
            plan_opens_at=str(payload["plan_opens_at"]),
            plan_closes_at=str(payload["plan_closes_at"]),
            run_registry_id=str(payload["run_registry_id"]),
        )
    except (AV1V4R3OrdinalWindowError, KeyError) as exc:
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight plan fields are invalid"
        ) from exc
    return av1_v4r3_ordinal_window_plan_seam_sha256(draft)


def _assert_invocations(
    payload: Mapping[str, Any], closures: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    closure_ids = object_list(payload.get("r3_closure_ids"))
    invocations = [
        dict(object_dict(item))
        for item in object_list(payload.get("r3_invocation_digests"))
    ]
    expected_closure_ids = [closure["closure_id"] for closure in closures]
    if (
        len(closure_ids) != AV1_V4R3_OW_ORDINAL_COUNT
        or len(invocations) != AV1_V4R3_OW_ORDINAL_COUNT
        or closure_ids != expected_closure_ids
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight invocation count is invalid"
        )
    seen: set[str] = set()
    for index, invocation in enumerate(invocations, start=1):
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
            raise AV1V4R3ExecutionPreflightError(
                "AV1 v4 r3 execution preflight invocation binding is invalid"
            )
        seen.add(digest)
    return invocations


def _build_ordinal_readiness(
    closures: list[dict[str, Any]], invocations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    readiness: list[dict[str, Any]] = []
    for closure, invocation in zip(closures, invocations, strict=True):
        size_goal = object_dict(closure.get("size_goal"))
        transform_plan = object_dict(closure.get("transform_plan"))
        ledger = object_dict(closure.get("stream_ledger_closure"))
        readiness.append(
            {
                "ordinal": closure["ordinal"],
                "asset_id": closure["asset_id"],
                "closure_id": closure["closure_id"],
                "ledger_closure_id": ledger["ledger_closure_id"],
                "size_goal_id": size_goal["size_goal_id"],
                "transform_plan_id": transform_plan["transform_plan_id"],
                "invocation_sha256": invocation["invocation_sha256"],
                "digest_matches_request": True,
                "closure_order_matches_manifest": True,
                "media_read_performed": False,
            }
        )
    return readiness


def _build_unresolved_execution_bindings(
    closures: list[dict[str, Any]],
) -> dict[str, Any]:
    assets: dict[str, dict[str, Any]] = {}
    for closure in closures:
        asset_id = str(closure["asset_id"])
        ledger = object_dict(closure.get("stream_ledger_closure"))
        assets.setdefault(
            asset_id,
            {
                "ledger_closure_id": ledger["ledger_closure_id"],
                "production_stream_plan_id": None,
                "stream_budget_ledger_id": None,
            },
        )
    return {
        "resolution_stage": "runner_admission_under_owner_execution_grant",
        "must_verify_returned_ledger_id": True,
        "assets": assets,
    }


def _assert_scope(value: Any) -> None:
    scope = object_dict(value)
    if (
        set(scope) != set(AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE)
        or any(scope.get(field) is not False for field in _SCOPE_FALSE_FIELDS)
        or scope.get("reviewed_artifact_set")
        != AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE["reviewed_artifact_set"]
        or scope.get("requested_activity")
        != AV1_V4R3_EXECUTION_PREFLIGHT_SCOPE["requested_activity"]
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight scope is invalid"
        )


def _assert_readiness(value: Any) -> None:
    readiness = object_dict(value)
    if set(readiness) != set(AV1_V4R3_EXECUTION_PREFLIGHT_READINESS) or any(
        readiness.get(field) is not expected
        for field, expected in AV1_V4R3_EXECUTION_PREFLIGHT_READINESS.items()
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight readiness is invalid"
        )


def _assert_identifiers(payload: Mapping[str, Any]) -> None:
    patterns = {
        "r3_request_id": _REQUEST_ID_RE,
        "freeze_id": _FREEZE_ID_RE,
        "preparation_bundle_id": _BUNDLE_ID_RE,
        "effective_config_hmac_id": _CONFIG_HMAC_ID_RE,
        "path_privacy_key_id": _KEY_ID_RE,
        "toolchain_id": _TOOLCHAIN_ID_RE,
        "runtime_id": _RUNTIME_ID_RE,
        "run_registry_id": _RUN_REGISTRY_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight identity is invalid"
        )
    for item in object_list(payload.get("ordinal_readiness")):
        readiness = object_dict(item)
        if (
            set(readiness) != _ORDINAL_READINESS_KEYS
            or not _CLOSURE_ID_RE.fullmatch(str(readiness.get("closure_id") or ""))
            or not _LEDGER_CLOSURE_ID_RE.fullmatch(
                str(readiness.get("ledger_closure_id") or "")
            )
            or not _SIZE_GOAL_ID_RE.fullmatch(str(readiness.get("size_goal_id") or ""))
            or not _TRANSFORM_PLAN_ID_RE.fullmatch(
                str(readiness.get("transform_plan_id") or "")
            )
        ):
            raise AV1V4R3ExecutionPreflightError(
                "AV1 v4 r3 execution preflight ordinal identity is invalid"
            )


def _assert_sha_fields(payload: Mapping[str, Any]) -> None:
    for field in (
        "manifest_payload_sha256",
        "r3_request_payload_sha256",
        "freeze_payload_sha256",
        "preparation_bundle_payload_sha256",
        "ordinal_window_plan_seam_sha256",
        "payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(payload.get(field) or "")):
            raise AV1V4R3ExecutionPreflightError(
                f"AV1 v4 r3 execution preflight {field} is invalid"
            )


def _assert_repository(value: Any, label: str) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(repository.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3ExecutionPreflightError(
            f"AV1 v4 r3 execution preflight {label} repository is invalid"
        )


def _assert_owner(value: Any) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 128
        or av1_validation_v4_contains_private_text(value)
    ):
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight owner is invalid"
        )


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3ExecutionPreflightError(
            f"AV1 v4 r3 execution preflight {label} is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3ExecutionPreflightError(
            f"AV1 v4 r3 execution preflight {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3ExecutionPreflightError(
            f"AV1 v4 r3 execution preflight {label} must use canonical UTC seconds"
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
        raise AV1V4R3ExecutionPreflightError(
            "AV1 v4 r3 execution preflight exposes machine-local text"
        )


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["preflight_id"] = av1_v4r3_execution_preflight_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))
