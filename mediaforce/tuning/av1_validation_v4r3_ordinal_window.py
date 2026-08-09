from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
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
    AV1_V4R3_MANIFEST_VALID_UNTIL,
)


# ---------------------------------------------------------------------------
# Schema / contract constants
# ---------------------------------------------------------------------------

AV1_V4R3_OW_PLAN_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_plan"
AV1_V4R3_OW_PLAN_SCHEMA_VERSION = 1
AV1_V4R3_OW_PLAN_CONTRACT_VERSION = "av1v4r3owplan1"

AV1_V4R3_OW_GRANT_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_grant"
AV1_V4R3_OW_GRANT_SCHEMA_VERSION = 1
AV1_V4R3_OW_GRANT_CONTRACT_VERSION = "av1v4r3owgrant1"
AV1_V4R3_OW_GRANT_AUTHORITY = "av1_v4r3_ordinal_window_sequencing"

AV1_V4R3_OW_CLAIM_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_claim"
AV1_V4R3_OW_CLAIM_SCHEMA_VERSION = 1
AV1_V4R3_OW_CLAIM_CONTRACT_VERSION = "av1v4r3owclaim1"

AV1_V4R3_OW_STARTED_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_started"
AV1_V4R3_OW_STARTED_SCHEMA_VERSION = 1
AV1_V4R3_OW_STARTED_CONTRACT_VERSION = "av1v4r3owstart1"

AV1_V4R3_OW_OUTCOME_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_outcome"
AV1_V4R3_OW_OUTCOME_SCHEMA_VERSION = 2
AV1_V4R3_OW_OUTCOME_CONTRACT_VERSION = "av1v4r3owout2"
_AV1_V4R3_OW_OUTCOME_V1_CONTRACT_VERSION = "av1v4r3owout1"
AV1_V4R3_OW_FAILURE_PHASES = frozenset({"production_search", "runtime_identity"})
AV1_V4R3_OW_FAILURE_CLASSES = frozenset(
    {
        "interrupted",
        "qualification_contract_error",
        "quality_search_error",
        "runtime_identity_error",
        "sample_encode_error",
        "target_size_search_error",
        "unexpected_error",
    }
)
AV1_V4R3_OW_FAILURE_SEARCH_STATUSES = frozenset(
    {"bound_exhausted", "infeasible", "needs_review", "quality_conflict"}
)

AV1_V4R3_OW_TERMINAL_SCHEMA = "mediaforce.av1_cold_start_v4r3_ordinal_window_terminal"
AV1_V4R3_OW_TERMINAL_SCHEMA_VERSION = 1
AV1_V4R3_OW_TERMINAL_CONTRACT_VERSION = "av1v4r3owterm1"

# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

AV1_V4R3_OW_SETUP_SECONDS: int = 300
AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX: int = 13_200
AV1_V4R3_OW_TEARDOWN_SECONDS: int = 300
AV1_V4R3_OW_WINDOW_SECONDS_MAX: int = (
    AV1_V4R3_OW_SETUP_SECONDS
    + AV1_V4R3_OW_TRAVERSAL_SECONDS_MAX
    + AV1_V4R3_OW_TEARDOWN_SECONDS
)  # = 13_800
AV1_V4R3_OW_ORDINAL_COUNT: int = AV1_VALIDATION_V4_TRAVERSAL_COUNT  # = 8
AV1_V4R3_OW_AGGREGATE_SECONDS_MAX: int = (
    AV1_V4R3_OW_ORDINAL_COUNT * AV1_V4R3_OW_WINDOW_SECONDS_MAX
)  # = 110_400
AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX: int = 129_600

if AV1_V4R3_OW_AGGREGATE_SECONDS_MAX > AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX:
    raise RuntimeError(
        "AV1 v4 r3 ordinal window aggregate budget exceeds the public run maximum; "
        f"{AV1_V4R3_OW_AGGREGATE_SECONDS_MAX} > {AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX}"
    )

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_PLAN_ID_RE = re.compile(r"av1vordplan4r3_[0-9a-f]{32}\Z")
_GRANT_ID_RE = re.compile(r"av1vordgrant4r3_[0-9a-f]{32}\Z")
_CLAIM_ID_RE = re.compile(r"av1vordclaim4r3_[0-9a-f]{32}\Z")
_STARTED_ID_RE = re.compile(r"av1vordstart4r3_[0-9a-f]{32}\Z")
_OUTCOME_ID_RE = re.compile(r"av1vordout4r3_[0-9a-f]{32}\Z")
_TERMINAL_ID_RE = re.compile(r"av1vordterm4r3_[0-9a-f]{32}\Z")
_R3_MANIFEST_ID_RE = re.compile(r"av1vmanifest4r3_[0-9a-f]{32}\Z")
_R3_REQUEST_ID_RE = re.compile(r"av1v4r3req_[0-9a-f]{32}\Z")
_R3_PREFLIGHT_ID_RE = re.compile(r"av1v4r3preflight_[0-9a-f]{32}\Z")
_R3_TOOLCHAIN_ID_RE = re.compile(r"av1v4r3toolchain_[0-9a-f]{32}\Z")
_R3_CLOSURE_ID_RE = re.compile(r"av1vclosure4r3_[0-9a-f]{32}\Z")
_RUN_REGISTRY_ID_RE = re.compile(r"av1v4r3runreg_[0-9a-f]{64}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GIT_OBJECT_ID_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# ---------------------------------------------------------------------------
# Allowed-key sets (module-level frozensets, never inline)
# ---------------------------------------------------------------------------

_PLAN_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "plan_id",
            "payload_sha256",
            "r3_manifest_id",
            "r3_manifest_valid_until",
            "r3_request_id",
            "r3_request_valid_until",
            "r3_preflight_id",
            "r3_repository",
            "r3_toolchain_id",
            "r3_closure_ids",
            "r3_invocation_digests",
            "plan_opens_at",
            "plan_closes_at",
            "plan_span_seconds",
            "run_registry_id",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)
_PLAN_DRAFT_ALLOWED_KEYS = _PLAN_ALLOWED_KEYS - {
    "plan_id",
    "payload_sha256",
    "r3_preflight_id",
}

_GRANT_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "grant_id",
            "payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "ordinal",
            "authority",
            "authorized_at",
            "valid_until",
            "admission_opens_at",
            "admission_closes_at",
            "plan_opens_at",
            "plan_closes_at",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

_CLAIM_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "claim_id",
            "payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "grant_id",
            "grant_payload_sha256",
            "ordinal",
            "claimed_at",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

_STARTED_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "started_id",
            "payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "grant_id",
            "grant_payload_sha256",
            "claim_id",
            "claim_payload_sha256",
            "ordinal",
            "started_at",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

_OUTCOME_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "outcome_id",
            "payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "started_id",
            "started_payload_sha256",
            "ordinal",
            "outcome_at",
            "success",
            "failure_phase",
            "failure_class",
            "failure_search_status",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

_TERMINAL_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "terminal_id",
            "payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "terminal_at",
            "ordinal_count_successful",
            "success_all_ordinals",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class AV1V4R3OrdinalWindowError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_timestamp(value: Any, label: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3OrdinalWindowError(f"AV1 v4 r3 ordinal window {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window {label} must be canonical UTC seconds"
        )
    return normalized


def _assert_no_private_text(
    payload: Mapping[str, Any],
    *,
    skip_keys: frozenset[str] = frozenset(),
) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window artifact exposes machine-local text"
            )

    for key, value in payload.items():
        if key not in skip_keys:
            visit(value)


def _assert_run_registry_id(value: Any) -> None:
    if not isinstance(value, str) or not _RUN_REGISTRY_ID_RE.fullmatch(value):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window run registry ID is invalid"
        )


def _assert_repository(repo: Any, label: str) -> None:
    r = object_dict(repo)
    if set(r) != {"commit", "tree"} or any(
        not _GIT_OBJECT_ID_RE.fullmatch(str(r.get(field) or ""))
        for field in ("commit", "tree")
    ):
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window {label} repository is invalid"
        )


def _assert_r3_closures(ids: Any) -> None:
    items = object_list(ids)
    if len(items) != AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window closure ID list must have exactly "
            f"{AV1_V4R3_OW_ORDINAL_COUNT} entries"
        )
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not _R3_CLOSURE_ID_RE.fullmatch(item):
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window closure ID is invalid"
            )
        if item in seen:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window closure IDs must be unique"
            )
        seen.add(item)
    expected = [
        str(closure["closure_id"]) for closure in build_av1_v4_r3_all_closure_payloads()
    ]
    if items != expected:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window closure IDs do not match revision-3 order"
        )


def _assert_r3_invocation_digests(
    digests: Any,
    *,
    closure_ids: list[str],
    repository: Mapping[str, Any],
    toolchain_id: str,
) -> None:
    items = object_list(digests)
    if len(items) != AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window invocation digest list must have exactly "
            f"{AV1_V4R3_OW_ORDINAL_COUNT} entries"
        )
    seen: set[str] = set()
    expected_repository = dict(object_dict(repository))
    for index, item in enumerate(items, start=1):
        digest = object_dict(item)
        if set(digest) != {
            "ordinal",
            "closure_id",
            "invocation_sha256",
            "r3_repository",
            "r3_toolchain_id",
        }:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest shape is invalid"
            )
        if digest.get("ordinal") != index:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest ordinal is invalid"
            )
        if digest.get("closure_id") != closure_ids[index - 1]:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest closure binding is invalid"
            )
        invocation_sha256 = digest.get("invocation_sha256")
        if not isinstance(invocation_sha256, str) or not _SHA256_RE.fullmatch(
            invocation_sha256
        ):
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest is invalid"
            )
        if digest.get("r3_repository") != expected_repository:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest repository binding is invalid"
            )
        if digest.get("r3_toolchain_id") != toolchain_id:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digest toolchain binding is invalid"
            )
        if invocation_sha256 in seen:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window invocation digests must be unique"
            )
        seen.add(invocation_sha256)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_plan(
    *,
    r3_manifest_id: str,
    r3_manifest_valid_until: str,
    r3_request_id: str,
    r3_request_valid_until: str,
    r3_preflight_id: str,
    r3_repository: Mapping[str, Any],
    r3_toolchain_id: str,
    r3_closure_ids: list[str],
    r3_invocation_digests: list[Mapping[str, Any]],
    plan_opens_at: str,
    plan_closes_at: str,
    run_registry_id: str,
) -> dict[str, Any]:
    draft = build_av1_v4r3_ordinal_window_plan_draft(
        r3_manifest_id=r3_manifest_id,
        r3_manifest_valid_until=r3_manifest_valid_until,
        r3_request_id=r3_request_id,
        r3_request_valid_until=r3_request_valid_until,
        r3_repository=r3_repository,
        r3_toolchain_id=r3_toolchain_id,
        r3_closure_ids=r3_closure_ids,
        r3_invocation_digests=r3_invocation_digests,
        plan_opens_at=plan_opens_at,
        plan_closes_at=plan_closes_at,
        run_registry_id=run_registry_id,
    )
    return finalize_av1_v4r3_ordinal_window_plan(
        draft=draft,
        r3_preflight_id=r3_preflight_id,
    )


def build_av1_v4r3_ordinal_window_plan_draft(
    *,
    r3_manifest_id: str,
    r3_manifest_valid_until: str,
    r3_request_id: str,
    r3_request_valid_until: str,
    r3_repository: Mapping[str, Any],
    r3_toolchain_id: str,
    r3_closure_ids: list[str],
    r3_invocation_digests: list[Mapping[str, Any]],
    plan_opens_at: str,
    plan_closes_at: str,
    run_registry_id: str,
) -> dict[str, Any]:
    opens = _parse_timestamp(plan_opens_at, "plan_opens_at")
    closes = _parse_timestamp(plan_closes_at, "plan_closes_at")
    manifest_until = _parse_timestamp(
        r3_manifest_valid_until, "r3_manifest_valid_until"
    )
    request_until = _parse_timestamp(r3_request_valid_until, "r3_request_valid_until")
    if opens >= closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan opens_at must be before closes_at"
        )
    span_seconds = int((closes - opens).total_seconds())
    if not (
        AV1_V4R3_OW_AGGREGATE_SECONDS_MAX
        <= span_seconds
        <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan span is outside the public run bounds"
        )
    manifest_approved = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest_approved_at"
    )
    if opens < manifest_approved:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan opens before manifest approval"
        )
    if closes >= manifest_until:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan closes_at must be before manifest valid_until"
        )
    if closes >= request_until:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan closes_at must be before request valid_until"
        )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_PLAN_SCHEMA,
        "schema_version": AV1_V4R3_OW_PLAN_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_PLAN_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "r3_manifest_id": r3_manifest_id,
        "r3_manifest_valid_until": r3_manifest_valid_until,
        "r3_request_id": r3_request_id,
        "r3_request_valid_until": r3_request_valid_until,
        "r3_repository": dict(object_dict(r3_repository)),
        "r3_toolchain_id": r3_toolchain_id,
        "r3_closure_ids": list(r3_closure_ids),
        "r3_invocation_digests": [
            dict(object_dict(item)) for item in r3_invocation_digests
        ],
        "plan_opens_at": plan_opens_at,
        "plan_closes_at": plan_closes_at,
        "plan_span_seconds": span_seconds,
        "run_registry_id": run_registry_id,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_plan_draft(materialized)
    return materialized


def assert_av1_v4r3_ordinal_window_plan_draft(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _PLAN_DRAFT_ALLOWED_KEYS:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan draft shape is invalid"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_OW_PLAN_SCHEMA,
        "schema_version": AV1_V4R3_OW_PLAN_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_PLAN_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "r3_manifest_id": AV1_V4R3_MANIFEST_ID,
        "r3_manifest_valid_until": AV1_V4R3_MANIFEST_VALID_UNTIL,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan draft binding is invalid"
        )
    if any(
        type(materialized.get(field)) is not int
        for field in (
            "schema_version",
            "protocol_version",
            "manifest_revision",
            "plan_span_seconds",
        )
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan draft scalar type is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window plan cannot authorize {field}"
            )
    if not _R3_REQUEST_ID_RE.fullmatch(str(materialized.get("r3_request_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan request ID is invalid"
        )
    if not _R3_TOOLCHAIN_ID_RE.fullmatch(
        str(materialized.get("r3_toolchain_id") or "")
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan toolchain ID is invalid"
        )
    _assert_repository(materialized.get("r3_repository"), "r3")
    closure_ids = object_list(materialized.get("r3_closure_ids"))
    _assert_r3_closures(closure_ids)
    _assert_r3_invocation_digests(
        materialized.get("r3_invocation_digests"),
        closure_ids=closure_ids,
        repository=object_dict(materialized.get("r3_repository")),
        toolchain_id=str(materialized.get("r3_toolchain_id")),
    )
    _assert_run_registry_id(materialized.get("run_registry_id"))
    opens = _parse_timestamp(materialized.get("plan_opens_at"), "plan_opens_at")
    closes = _parse_timestamp(materialized.get("plan_closes_at"), "plan_closes_at")
    request_until = _parse_timestamp(
        materialized.get("r3_request_valid_until"), "r3_request_valid_until"
    )
    manifest_approved = _parse_timestamp(
        AV1_V4R3_MANIFEST_APPROVED_AT, "manifest_approved_at"
    )
    manifest_until = _parse_timestamp(
        AV1_V4R3_MANIFEST_VALID_UNTIL, "manifest_valid_until"
    )
    if not (manifest_approved <= opens < closes):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan draft timeline is invalid"
        )
    span_seconds = int((closes - opens).total_seconds())
    if not (
        AV1_V4R3_OW_AGGREGATE_SECONDS_MAX
        <= span_seconds
        <= AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan span is outside the public run bounds"
        )
    if materialized.get("plan_span_seconds") != span_seconds:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan span seconds does not match"
        )
    if closes >= manifest_until:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan closes_at must be before manifest valid_until"
        )
    if closes >= request_until:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan closes_at must be before request valid_until"
        )


def av1_v4r3_ordinal_window_plan_seam_sha256(
    payload: Mapping[str, Any],
) -> str:
    materialized = object_dict(payload)
    if set(materialized) == _PLAN_ALLOWED_KEYS:
        assert_av1_v4r3_ordinal_window_plan(materialized)
    elif set(materialized) == _PLAN_DRAFT_ALLOWED_KEYS:
        assert_av1_v4r3_ordinal_window_plan_draft(materialized)
    else:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan seam payload shape is invalid"
        )
    semantic = {
        key: value
        for key, value in materialized.items()
        if key not in {"r3_preflight_id", "plan_id", "payload_sha256"}
    }
    return f"sha256:{stable_json_hash(semantic)}"


def finalize_av1_v4r3_ordinal_window_plan(
    *, draft: Mapping[str, Any], r3_preflight_id: str
) -> dict[str, Any]:
    materialized = object_dict(draft)
    assert_av1_v4r3_ordinal_window_plan_draft(materialized)
    if not _R3_PREFLIGHT_ID_RE.fullmatch(r3_preflight_id):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan preflight ID is invalid"
        )
    payload = dict(materialized)
    payload["r3_preflight_id"] = r3_preflight_id
    bound = _bind_plan_identity(payload)
    assert_av1_v4r3_ordinal_window_plan(bound)
    return bound


def assert_av1_v4r3_ordinal_window_plan(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _PLAN_ALLOWED_KEYS:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan shape is invalid"
        )
    draft = {
        key: value
        for key, value in materialized.items()
        if key not in {"r3_preflight_id", "plan_id", "payload_sha256"}
    }
    assert_av1_v4r3_ordinal_window_plan_draft(draft)
    if not _R3_PREFLIGHT_ID_RE.fullmatch(
        str(materialized.get("r3_preflight_id") or "")
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan preflight ID is invalid"
        )
    plan_id = str(materialized.get("plan_id") or "")
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise AV1V4R3OrdinalWindowError("AV1 v4 r3 ordinal window plan ID is invalid")
    if plan_id != av1_v4r3_ordinal_window_plan_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan payload SHA-256 does not match"
        )


def av1_v4r3_ordinal_window_plan_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"plan_id", "payload_sha256"}
    }
    return f"av1vordplan4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_plan(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_plan(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_plan(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window plan bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_plan(payload)
    return payload


def _bind_plan_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["plan_id"] = av1_v4r3_ordinal_window_plan_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


# ---------------------------------------------------------------------------
# Grant
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_grant(
    *,
    plan: Mapping[str, Any],
    ordinal: int,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    plan_payload = object_dict(plan)
    try:
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant plan is invalid"
        ) from exc
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant ordinal must be an integer"
        )
    if not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant ordinal is out of range"
        )
    plan_opens = _parse_timestamp(plan_payload["plan_opens_at"], "plan_opens_at")
    plan_closes = _parse_timestamp(plan_payload["plan_closes_at"], "plan_closes_at")
    auth_at = _parse_timestamp(authorized_at, "authorized_at")
    valid_ut = _parse_timestamp(valid_until, "valid_until")
    if auth_at < plan_opens:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant authorized_at is before plan opens"
        )
    if valid_ut > plan_closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant valid_until is after plan closes"
        )
    if auth_at >= valid_ut:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant authorized_at must be before valid_until"
        )
    window_seconds = int((valid_ut - auth_at).total_seconds())
    if window_seconds > AV1_V4R3_OW_WINDOW_SECONDS_MAX:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant window exceeds the maximum"
        )
    admission_opens = auth_at + timedelta(seconds=AV1_V4R3_OW_SETUP_SECONDS)
    admission_closes = valid_ut - timedelta(seconds=AV1_V4R3_OW_TEARDOWN_SECONDS)
    if admission_opens >= admission_closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant window is too short for admission"
        )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_OW_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "ordinal": ordinal,
        "authority": AV1_V4R3_OW_GRANT_AUTHORITY,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        "admission_opens_at": admission_opens.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "admission_closes_at": admission_closes.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "plan_opens_at": plan_payload["plan_opens_at"],
        "plan_closes_at": plan_payload["plan_closes_at"],
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_grant_identity(payload)
    assert_av1_v4r3_ordinal_window_grant(bound)
    return bound


def assert_av1_v4r3_ordinal_window_grant(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant payload is invalid"
        )
    unknown = set(materialized) - _GRANT_ALLOWED_KEYS
    if unknown:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window grant contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_OW_GRANT_SCHEMA,
        "schema_version": AV1_V4R3_OW_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "authority": AV1_V4R3_OW_GRANT_AUTHORITY,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window grant cannot authorize {field}"
            )
    ordinal = materialized.get("ordinal")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant ordinal is invalid"
        )
    if not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant plan ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant plan digest is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant payload digest is invalid"
        )
    plan_opens = _parse_timestamp(materialized.get("plan_opens_at"), "plan_opens_at")
    plan_closes = _parse_timestamp(materialized.get("plan_closes_at"), "plan_closes_at")
    auth_at = _parse_timestamp(materialized.get("authorized_at"), "authorized_at")
    valid_ut = _parse_timestamp(materialized.get("valid_until"), "valid_until")
    admit_opens = _parse_timestamp(
        materialized.get("admission_opens_at"), "admission_opens_at"
    )
    admit_closes = _parse_timestamp(
        materialized.get("admission_closes_at"), "admission_closes_at"
    )
    if auth_at < plan_opens:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant authorized_at is before plan opens"
        )
    if valid_ut > plan_closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant valid_until is after plan closes"
        )
    if auth_at >= valid_ut:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant authorized_at must be before valid_until"
        )
    if int((valid_ut - auth_at).total_seconds()) > AV1_V4R3_OW_WINDOW_SECONDS_MAX:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant window exceeds the maximum"
        )
    expected_admit_opens = auth_at + timedelta(seconds=AV1_V4R3_OW_SETUP_SECONDS)
    expected_admit_closes = valid_ut - timedelta(seconds=AV1_V4R3_OW_TEARDOWN_SECONDS)
    if admit_opens != expected_admit_opens:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant admission_opens_at is incorrect"
        )
    if admit_closes != expected_admit_closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant admission_closes_at is incorrect"
        )
    if admit_opens >= admit_closes:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant window is too short for admission"
        )
    grant_id = str(materialized.get("grant_id") or "")
    if not _GRANT_ID_RE.fullmatch(grant_id):
        raise AV1V4R3OrdinalWindowError("AV1 v4 r3 ordinal window grant ID is invalid")
    if grant_id != av1_v4r3_ordinal_window_grant_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant payload SHA-256 does not match"
        )


def av1_v4r3_ordinal_window_grant_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"grant_id", "payload_sha256"}
    }
    return f"av1vordgrant4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_grant(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_grant(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_grant(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window grant bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_grant(payload)
    return payload


def _bind_grant_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["grant_id"] = av1_v4r3_ordinal_window_grant_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


# ---------------------------------------------------------------------------
# Claim
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_claim(
    *,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    plan_payload = object_dict(plan)
    grant_payload = object_dict(grant)
    try:
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim plan is invalid"
        ) from exc
    try:
        assert_av1_v4r3_ordinal_window_grant(grant_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim grant is invalid"
        ) from exc
    if grant_payload.get("plan_id") != plan_payload.get("plan_id") or grant_payload.get(
        "plan_payload_sha256"
    ) != plan_payload.get("payload_sha256"):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim grant does not reference the plan"
        )
    _parse_timestamp(claimed_at, "claimed_at")
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_CLAIM_SCHEMA,
        "schema_version": AV1_V4R3_OW_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "grant_id": grant_payload["grant_id"],
        "grant_payload_sha256": grant_payload["payload_sha256"],
        "ordinal": grant_payload["ordinal"],
        "claimed_at": claimed_at,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_claim_identity(payload)
    assert_av1_v4r3_ordinal_window_claim(bound)
    return bound


def assert_av1_v4r3_ordinal_window_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim payload is invalid"
        )
    unknown = set(materialized) - _CLAIM_ALLOWED_KEYS
    if unknown:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window claim contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_OW_CLAIM_SCHEMA,
        "schema_version": AV1_V4R3_OW_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window claim cannot authorize {field}"
            )
    if not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim plan ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim plan digest is invalid"
        )
    if not _GRANT_ID_RE.fullmatch(str(materialized.get("grant_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim grant ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("grant_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim grant digest is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim payload digest is invalid"
        )
    ordinal = materialized.get("ordinal")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim ordinal is invalid"
        )
    _parse_timestamp(materialized.get("claimed_at"), "claimed_at")
    claim_id = str(materialized.get("claim_id") or "")
    if not _CLAIM_ID_RE.fullmatch(claim_id):
        raise AV1V4R3OrdinalWindowError("AV1 v4 r3 ordinal window claim ID is invalid")
    if claim_id != av1_v4r3_ordinal_window_claim_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim payload SHA-256 does not match"
        )


def av1_v4r3_ordinal_window_claim_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"claim_id", "payload_sha256"}
    }
    return f"av1vordclaim4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_claim(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_claim(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_claim(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window claim bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_claim(payload)
    return payload


def _bind_claim_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["claim_id"] = av1_v4r3_ordinal_window_claim_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


# ---------------------------------------------------------------------------
# Started
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_started(
    *,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    started_at: str,
) -> dict[str, Any]:
    plan_payload = object_dict(plan)
    grant_payload = object_dict(grant)
    claim_payload = object_dict(claim)
    try:
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started plan is invalid"
        ) from exc
    try:
        assert_av1_v4r3_ordinal_window_grant(grant_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started grant is invalid"
        ) from exc
    try:
        assert_av1_v4r3_ordinal_window_claim(claim_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started claim is invalid"
        ) from exc
    if claim_payload.get("plan_id") != plan_payload.get("plan_id"):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started claim does not reference the plan"
        )
    if claim_payload.get("grant_id") != grant_payload.get("grant_id"):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started claim does not reference the grant"
        )
    _parse_timestamp(started_at, "started_at")
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_STARTED_SCHEMA,
        "schema_version": AV1_V4R3_OW_STARTED_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_STARTED_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "grant_id": grant_payload["grant_id"],
        "grant_payload_sha256": grant_payload["payload_sha256"],
        "claim_id": claim_payload["claim_id"],
        "claim_payload_sha256": claim_payload["payload_sha256"],
        "ordinal": claim_payload["ordinal"],
        "started_at": started_at,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_started_identity(payload)
    assert_av1_v4r3_ordinal_window_started(bound)
    return bound


def assert_av1_v4r3_ordinal_window_started(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started payload is invalid"
        )
    unknown = set(materialized) - _STARTED_ALLOWED_KEYS
    if unknown:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window started contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_OW_STARTED_SCHEMA,
        "schema_version": AV1_V4R3_OW_STARTED_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_STARTED_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window started cannot authorize {field}"
            )
    if not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started plan ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started plan digest is invalid"
        )
    if not _GRANT_ID_RE.fullmatch(str(materialized.get("grant_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started grant ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("grant_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started grant digest is invalid"
        )
    if not _CLAIM_ID_RE.fullmatch(str(materialized.get("claim_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started claim ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("claim_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started claim digest is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started payload digest is invalid"
        )
    ordinal = materialized.get("ordinal")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started ordinal is invalid"
        )
    _parse_timestamp(materialized.get("started_at"), "started_at")
    started_id = str(materialized.get("started_id") or "")
    if not _STARTED_ID_RE.fullmatch(started_id):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started ID is invalid"
        )
    if started_id != av1_v4r3_ordinal_window_started_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started payload SHA-256 does not match"
        )


def av1_v4r3_ordinal_window_started_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"started_id", "payload_sha256"}
    }
    return f"av1vordstart4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_started(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_started(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_started(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started record is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window started bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_started(payload)
    return payload


def _bind_started_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["started_id"] = av1_v4r3_ordinal_window_started_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_outcome(
    *,
    plan: Mapping[str, Any],
    started: Mapping[str, Any],
    outcome_at: str,
    success: bool,
    failure_phase: str | None = None,
    failure_class: str | None = None,
    failure_search_status: str | None = None,
) -> dict[str, Any]:
    plan_payload = object_dict(plan)
    started_payload = object_dict(started)
    try:
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome plan is invalid"
        ) from exc
    try:
        assert_av1_v4r3_ordinal_window_started(started_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome started record is invalid"
        ) from exc
    if started_payload.get("plan_id") != plan_payload.get(
        "plan_id"
    ) or started_payload.get("plan_payload_sha256") != plan_payload.get(
        "payload_sha256"
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome started does not reference the plan"
        )
    _parse_timestamp(outcome_at, "outcome_at")
    if not isinstance(success, bool):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome success must be a boolean"
        )
    _assert_outcome_failure_fields(
        success=success,
        failure_phase=failure_phase,
        failure_class=failure_class,
        failure_search_status=failure_search_status,
    )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_OUTCOME_SCHEMA,
        "schema_version": AV1_V4R3_OW_OUTCOME_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_OUTCOME_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "started_id": started_payload["started_id"],
        "started_payload_sha256": started_payload["payload_sha256"],
        "ordinal": started_payload["ordinal"],
        "outcome_at": outcome_at,
        "success": success,
        "failure_phase": failure_phase,
        "failure_class": failure_class,
        "failure_search_status": failure_search_status,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_outcome_identity(payload)
    assert_av1_v4r3_ordinal_window_outcome(bound)
    return bound


def assert_av1_v4r3_ordinal_window_outcome(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome payload is invalid"
        )
    unknown = set(materialized) - _OUTCOME_ALLOWED_KEYS
    if unknown:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window outcome contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    schema_version = materialized.get("schema_version")
    contract_version = materialized.get("contract_version")
    if schema_version == 1 and contract_version == _AV1_V4R3_OW_OUTCOME_V1_CONTRACT_VERSION:
        legacy_keys = _OUTCOME_ALLOWED_KEYS - {
            "failure_phase",
            "failure_class",
            "failure_search_status",
        }
        unknown = set(materialized) - legacy_keys
        if unknown:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window outcome contains unknown fields: {sorted(unknown)}"
            )
    elif schema_version != AV1_V4R3_OW_OUTCOME_SCHEMA_VERSION or contract_version != AV1_V4R3_OW_OUTCOME_CONTRACT_VERSION:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome binding is invalid"
        )
    elif set(materialized) != _OUTCOME_ALLOWED_KEYS:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome fields do not match schema v2"
        )
    expected = {
        "schema": AV1_V4R3_OW_OUTCOME_SCHEMA,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window outcome cannot authorize {field}"
            )
    if not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome plan ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome plan digest is invalid"
        )
    if not _STARTED_ID_RE.fullmatch(str(materialized.get("started_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome started ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("started_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome started digest is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome payload digest is invalid"
        )
    ordinal = materialized.get("ordinal")
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome ordinal is invalid"
        )
    _parse_timestamp(materialized.get("outcome_at"), "outcome_at")
    success = materialized.get("success")
    if not isinstance(success, bool):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome success must be a boolean"
        )
    if schema_version == AV1_V4R3_OW_OUTCOME_SCHEMA_VERSION:
        _assert_outcome_failure_fields(
            success=success,
            failure_phase=materialized.get("failure_phase"),
            failure_class=materialized.get("failure_class"),
            failure_search_status=materialized.get("failure_search_status"),
        )
    outcome_id = str(materialized.get("outcome_id") or "")
    if not _OUTCOME_ID_RE.fullmatch(outcome_id):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome ID is invalid"
        )
    if outcome_id != av1_v4r3_ordinal_window_outcome_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome payload SHA-256 does not match"
        )


def _assert_outcome_failure_fields(
    *,
    success: bool,
    failure_phase: Any,
    failure_class: Any,
    failure_search_status: Any,
) -> None:
    if success:
        if any(value is not None for value in (failure_phase, failure_class, failure_search_status)):
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 successful outcome cannot carry failure classification"
            )
        return
    if failure_phase not in AV1_V4R3_OW_FAILURE_PHASES:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 failed outcome phase is invalid"
        )
    if failure_class not in AV1_V4R3_OW_FAILURE_CLASSES:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 failed outcome class is invalid"
        )
    if failure_search_status is not None and failure_search_status not in AV1_V4R3_OW_FAILURE_SEARCH_STATUSES:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 failed outcome search status is invalid"
        )


def av1_v4r3_ordinal_window_outcome_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"outcome_id", "payload_sha256"}
    }
    return f"av1vordout4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_outcome(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_outcome(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_outcome(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window outcome bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_outcome(payload)
    return payload


def _bind_outcome_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["outcome_id"] = av1_v4r3_ordinal_window_outcome_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


# ---------------------------------------------------------------------------
# Terminal
# ---------------------------------------------------------------------------


def build_av1_v4r3_ordinal_window_terminal(
    *,
    plan: Mapping[str, Any],
    outcomes: list[Mapping[str, Any]],
    terminal_at: str,
) -> dict[str, Any]:
    plan_payload = object_dict(plan)
    try:
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
    except AV1V4R3OrdinalWindowError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal plan is invalid"
        ) from exc
    if not isinstance(outcomes, list) or len(outcomes) > AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal outcomes must be a list of at most "
            f"{AV1_V4R3_OW_ORDINAL_COUNT} entries"
        )
    ordinal_count_successful = 0
    for outcome in outcomes:
        outcome_payload = object_dict(outcome)
        try:
            assert_av1_v4r3_ordinal_window_outcome(outcome_payload)
        except AV1V4R3OrdinalWindowError as exc:
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window terminal outcome is invalid"
            ) from exc
        if outcome_payload.get("plan_id") != plan_payload.get(
            "plan_id"
        ) or outcome_payload.get("plan_payload_sha256") != plan_payload.get(
            "payload_sha256"
        ):
            raise AV1V4R3OrdinalWindowError(
                "AV1 v4 r3 ordinal window terminal outcome does not reference the plan"
            )
        if outcome_payload.get("success") is True:
            ordinal_count_successful += 1
    success_all_ordinals = ordinal_count_successful == AV1_V4R3_OW_ORDINAL_COUNT
    _parse_timestamp(terminal_at, "terminal_at")
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_OW_TERMINAL_SCHEMA,
        "schema_version": AV1_V4R3_OW_TERMINAL_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_TERMINAL_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "terminal_at": terminal_at,
        "ordinal_count_successful": ordinal_count_successful,
        "success_all_ordinals": success_all_ordinals,
    }
    payload.update({field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS})
    bound = _bind_terminal_identity(payload)
    assert_av1_v4r3_ordinal_window_terminal(bound)
    return bound


def assert_av1_v4r3_ordinal_window_terminal(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if not materialized:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal payload is invalid"
        )
    unknown = set(materialized) - _TERMINAL_ALLOWED_KEYS
    if unknown:
        raise AV1V4R3OrdinalWindowError(
            f"AV1 v4 r3 ordinal window terminal contains unknown fields: {sorted(unknown)}"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_V4R3_OW_TERMINAL_SCHEMA,
        "schema_version": AV1_V4R3_OW_TERMINAL_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_OW_TERMINAL_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
    }
    if any(materialized.get(k) != v for k, v in expected.items()):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R3OrdinalWindowError(
                f"AV1 v4 r3 ordinal window terminal cannot authorize {field}"
            )
    if not _PLAN_ID_RE.fullmatch(str(materialized.get("plan_id") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal plan ID is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("plan_payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal plan digest is invalid"
        )
    if not _SHA256_RE.fullmatch(str(materialized.get("payload_sha256") or "")):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal payload digest is invalid"
        )
    _parse_timestamp(materialized.get("terminal_at"), "terminal_at")
    success_all_ordinals = materialized.get("success_all_ordinals")
    if not isinstance(success_all_ordinals, bool):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal success_all_ordinals must be a boolean"
        )
    ordinal_count_successful = materialized.get("ordinal_count_successful")
    if (
        not isinstance(ordinal_count_successful, int)
        or isinstance(ordinal_count_successful, bool)
        or not 0 <= ordinal_count_successful <= AV1_V4R3_OW_ORDINAL_COUNT
    ):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal ordinal_count_successful is invalid"
        )
    terminal_id = str(materialized.get("terminal_id") or "")
    if not _TERMINAL_ID_RE.fullmatch(terminal_id):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal ID is invalid"
        )
    if terminal_id != av1_v4r3_ordinal_window_terminal_id(materialized):
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal ID does not match its payload"
        )
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal payload SHA-256 does not match"
        )


def av1_v4r3_ordinal_window_terminal_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        k: v for k, v in payload.items() if k not in {"terminal_id", "payload_sha256"}
    }
    return f"av1vordterm4r3_{stable_json_hash(semantic)[:32]}"


def serialize_av1_v4r3_ordinal_window_terminal(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_ordinal_window_terminal(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_ordinal_window_terminal(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal is unreadable"
        ) from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R3OrdinalWindowError(
            "AV1 v4 r3 ordinal window terminal bytes are not canonical"
        )
    assert_av1_v4r3_ordinal_window_terminal(payload)
    return payload


def _bind_terminal_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["terminal_id"] = av1_v4r3_ordinal_window_terminal_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))
