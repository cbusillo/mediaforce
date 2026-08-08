"""Pure execution-claim and runner-admission contracts for AV1 v4 revision 3."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.streams import ProductionStreamPlan, StreamPlanIdentityError
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4r3_execution_grant import (
    AV1V4R3ExecutionGrantError,
    assert_av1_v4r3_execution_grant,
    assert_av1_v4r3_execution_grant_active,
    assert_av1_v4r3_execution_grant_chain,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight import (
    AV1V4R3ExecutionPreflightError,
    assert_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_PROTOCOL_VERSION,
    av1_v4_r3_resolved_size_goal_payload,
    av1_v4_r3_stream_ledger_closure_payload,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_ORDINAL_COUNT,
    assert_av1_v4r3_ordinal_window_grant,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    AV1_V4R3_INSTANCE_PATH_ROLES,
)
from mediaforce.tuning.stream_budget import (
    StreamBudgetIdentityError,
    StreamBudgetLedger,
)
from mediaforce.tuning.size_goals import size_goal_from_policy


AV1_V4R3_EXECUTION_CLAIM_SCHEMA = "mediaforce.av1_cold_start_v4r3_execution_claim"
AV1_V4R3_EXECUTION_CLAIM_SCHEMA_VERSION = 1
AV1_V4R3_EXECUTION_CLAIM_CONTRACT_VERSION = "av1v4r3execclaim1"
AV1_V4R3_EXECUTION_CLAIM_STATE = "execution_grant_claim_declared_non_authorizing"

AV1_V4R3_RUNNER_ADMISSION_SCHEMA = "mediaforce.av1_cold_start_v4r3_runner_admission"
AV1_V4R3_RUNNER_ADMISSION_SCHEMA_VERSION = 1
AV1_V4R3_RUNNER_ADMISSION_CONTRACT_VERSION = "av1v4r3admit1"
AV1_V4R3_RUNNER_ADMISSION_STATE = "runtime_bindings_admitted_non_authorizing"

_CLAIM_ID_RE = re.compile(r"av1v4r3execclaim_[0-9a-f]{32}\Z")
_ADMISSION_ID_RE = re.compile(r"av1v4r3admit_[0-9a-f]{32}\Z")
_EXECUTION_GRANT_ID_RE = re.compile(r"av1v4r3execgrant_[0-9a-f]{32}\Z")
_SEQUENCING_GRANT_ID_RE = re.compile(r"av1vordgrant4r3_[0-9a-f]{32}\Z")
_PLAN_ID_RE = re.compile(r"av1vordplan4r3_[0-9a-f]{32}\Z")
_PREFLIGHT_ID_RE = re.compile(r"av1v4r3preflight_[0-9a-f]{32}\Z")
_CLOSURE_ID_RE = re.compile(r"av1vclosure4r3_[0-9a-f]{32}\Z")
_SOURCE_PATH_ID_RE = re.compile(r"av1vsource4r3_[0-9a-f]{32}\Z")
_INSTANCE_PATH_ID_RE = re.compile(r"av1vpath4r3_[0-9a-f]{32}\Z")
_QUALITY_TEMP_ID_RE = re.compile(r"av1vqtemp4r3_[0-9a-f]{32}\Z")
_QUALITY_KEY_ID_RE = re.compile(r"av1vqtkey4r3_[0-9a-f]{32}\Z")
_PATH_KEY_ID_RE = re.compile(r"av1vpathkey4r3_[0-9a-f]{32}\Z")
_STREAM_PLAN_ID_RE = re.compile(r"sp1_[0-9a-f]{32}\Z")
_LEDGER_ID_RE = re.compile(r"sb1_[0-9a-f]{32}\Z")
_TRANSFORM_PLAN_ID_RE = re.compile(r"tp1_[0-9a-f]{32}\Z")
_SOURCE_ID_RE = re.compile(r"src1_[0-9a-f]{24}\Z")
_LEDGER_CLOSURE_ID_RE = re.compile(r"av1v4r3ledger_[0-9a-f]{32}\Z")
_SIZE_GOAL_ID_RE = re.compile(r"av1v4r3size_[0-9a-f]{32}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")

_STREAM_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "plan_id",
        "source_id",
        "source_fingerprint",
        "policy_hash",
        "output_container",
        "attachments_known",
        "copy_unknown_attachments",
        "streams",
    }
)
_LEDGER_KEYS = frozenset(
    {
        "schema_version",
        "ledger_id",
        "source",
        "policy_hash",
        "size_goal",
        "stream_plan",
        "entries",
        "totals",
        "source_relative_cap",
        "feasibility",
        "uncertainty",
    }
)
_LEDGER_SOURCE_KEYS = frozenset(
    {
        "source_id",
        "source_fingerprint",
        "source_size_bytes",
        "source_video_bitrate_bps",
        "duration_seconds",
    }
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
            "state",
            "owner_principal",
            "claimed_at",
            "execution_grant_id",
            "execution_grant_payload_sha256",
            "sequencing_grant_id",
            "sequencing_grant_payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "preflight_id",
            "preflight_payload_sha256",
            "ordinal",
            "asset_id",
            "closure_id",
            "invocation_sha256",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)

_ADMISSION_ALLOWED_KEYS = (
    frozenset(
        {
            "schema",
            "schema_version",
            "contract_version",
            "protocol_version",
            "manifest_revision",
            "admission_id",
            "payload_sha256",
            "state",
            "owner_principal",
            "admitted_at",
            "execution_claim_id",
            "execution_claim_payload_sha256",
            "execution_grant_id",
            "execution_grant_payload_sha256",
            "sequencing_grant_id",
            "sequencing_grant_payload_sha256",
            "plan_id",
            "plan_payload_sha256",
            "preflight_id",
            "preflight_payload_sha256",
            "ordinal",
            "asset_id",
            "closure_id",
            "invocation_sha256",
            "source_path_hmac_id",
            "instance_path_hmac_ids",
            "quality_temp_hmac_id",
            "quality_temp_key_id",
            "path_privacy_key_id",
            "production_source_id",
            "production_stream_plan_id",
            "production_stream_plan_payload_sha256",
            "stream_budget_ledger_id",
            "stream_budget_ledger_payload_sha256",
            "ledger_closure_id",
            "size_goal_id",
            "transform_plan_id",
            "returned_stream_budget_ledger_id",
            "returned_stream_budget_ledger_payload_sha256",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1V4R3RunnerAdmissionError(ValueError):
    pass


def build_av1_v4r3_execution_claim(
    *,
    execution_grant: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    try:
        assert_av1_v4r3_execution_grant_active(execution_grant, as_of=claimed_at)
        assert_av1_v4r3_ordinal_window_grant(sequencing_grant)
    except (AV1V4R3ExecutionGrantError, AV1V4R3OrdinalWindowError) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 execution claim predecessor is invalid"
        ) from exc
    grant = object_dict(execution_grant)
    sequencing = object_dict(sequencing_grant)
    _assert_grant_sequencing_bindings(grant, sequencing)
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_EXECUTION_CLAIM_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "state": AV1_V4R3_EXECUTION_CLAIM_STATE,
        "owner_principal": grant["owner_principal"],
        "claimed_at": claimed_at,
        "execution_grant_id": grant["grant_id"],
        "execution_grant_payload_sha256": grant["payload_sha256"],
        "sequencing_grant_id": sequencing["grant_id"],
        "sequencing_grant_payload_sha256": sequencing["payload_sha256"],
        "plan_id": grant["plan_id"],
        "plan_payload_sha256": grant["plan_payload_sha256"],
        "preflight_id": grant["preflight_id"],
        "preflight_payload_sha256": grant["preflight_payload_sha256"],
        "ordinal": grant["ordinal"],
        "asset_id": grant["asset_id"],
        "closure_id": grant["closure_id"],
        "invocation_sha256": grant["invocation_sha256"],
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload, "claim_id", "av1v4r3execclaim")
    assert_av1_v4r3_execution_claim_chain(
        execution_claim=bound,
        execution_grant=execution_grant,
        sequencing_grant=sequencing_grant,
    )
    return bound


def assert_av1_v4r3_execution_claim(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _CLAIM_ALLOWED_KEYS:
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 execution claim shape is invalid")
    _assert_common_non_authorizing(materialized)
    expected = {
        "schema": AV1_V4R3_EXECUTION_CLAIM_SCHEMA,
        "schema_version": AV1_V4R3_EXECUTION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_EXECUTION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "state": AV1_V4R3_EXECUTION_CLAIM_STATE,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 execution claim binding is invalid"
        )
    _assert_exact_integers(
        materialized,
        {
            "schema_version": AV1_V4R3_EXECUTION_CLAIM_SCHEMA_VERSION,
            "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
            "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        },
    )
    _assert_chain_identifiers(materialized)
    _parse_timestamp(materialized.get("claimed_at"), "claimed_at")
    claim_id = str(materialized.get("claim_id") or "")
    if not _CLAIM_ID_RE.fullmatch(claim_id) or claim_id != _identity_id(
        materialized, "claim_id", "av1v4r3execclaim"
    ):
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 execution claim ID is invalid")
    _assert_payload_digest(materialized, "execution claim")


def assert_av1_v4r3_execution_claim_chain(
    *,
    execution_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
) -> None:
    try:
        assert_av1_v4r3_execution_claim(execution_claim)
        assert_av1_v4r3_execution_grant(execution_grant)
        assert_av1_v4r3_ordinal_window_grant(sequencing_grant)
    except (
        AV1V4R3RunnerAdmissionError,
        AV1V4R3ExecutionGrantError,
        AV1V4R3OrdinalWindowError,
    ) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 execution claim chain is invalid"
        ) from exc
    claim = object_dict(execution_claim)
    grant = object_dict(execution_grant)
    sequencing = object_dict(sequencing_grant)
    _assert_claim_grant_bindings(claim, grant, sequencing)
    claimed_at = str(claim["claimed_at"])
    try:
        assert_av1_v4r3_execution_grant_active(grant, as_of=claimed_at)
    except AV1V4R3ExecutionGrantError as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 execution claim is outside the execution grant"
        ) from exc
    _assert_sequencing_admission_time(sequencing, claimed_at, "claimed_at")


def build_av1_v4r3_runner_admission(
    *,
    plan: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    preflight: Mapping[str, Any],
    admitted_at: str,
    source_path_hmac_id: str,
    instance_path_hmac_ids: Mapping[str, str],
    quality_temp_hmac_id: str,
    quality_temp_key_id: str,
    production_stream_plan: Mapping[str, Any],
    stream_budget_ledger: Mapping[str, Any],
    transform_plan_id: str,
    returned_stream_budget_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        assert_av1_v4r3_execution_grant_chain(
            plan=plan,
            preflight=preflight,
            sequencing_grant=sequencing_grant,
            execution_grant=execution_grant,
        )
        assert_av1_v4r3_execution_claim_chain(
            execution_claim=execution_claim,
            execution_grant=execution_grant,
            sequencing_grant=sequencing_grant,
        )
    except (
        AV1V4R3RunnerAdmissionError,
        AV1V4R3ExecutionGrantError,
        AV1V4R3OrdinalWindowError,
        AV1V4R3ExecutionPreflightError,
    ) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission predecessor is invalid"
        ) from exc
    claim = object_dict(execution_claim)
    grant = object_dict(execution_grant)
    sequencing = object_dict(sequencing_grant)
    preflight_payload = object_dict(preflight)
    _assert_claim_grant_bindings(claim, grant, sequencing)
    readiness = _readiness_for_ordinal(preflight_payload, grant["ordinal"])
    if (
        readiness.get("asset_id") != grant.get("asset_id")
        or readiness.get("closure_id") != grant.get("closure_id")
        or readiness.get("invocation_sha256") != grant.get("invocation_sha256")
        or readiness.get("transform_plan_id") != transform_plan_id
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission frozen invocation does not match"
        )
    runtime_payload_bindings = _runtime_payload_bindings(
        asset_id=str(grant["asset_id"]),
        production_stream_plan=production_stream_plan,
        stream_budget_ledger=stream_budget_ledger,
        returned_stream_budget_ledger=returned_stream_budget_ledger,
    )
    instances = dict(instance_path_hmac_ids)
    _assert_runtime_bindings(
        source_path_hmac_id=source_path_hmac_id,
        instance_path_hmac_ids=instances,
        quality_temp_hmac_id=quality_temp_hmac_id,
        quality_temp_key_id=quality_temp_key_id,
        path_privacy_key_id=str(grant["path_privacy_key_id"]),
        transform_plan_id=transform_plan_id,
        **runtime_payload_bindings,
    )
    payload: dict[str, Any] = {
        "schema": AV1_V4R3_RUNNER_ADMISSION_SCHEMA,
        "schema_version": AV1_V4R3_RUNNER_ADMISSION_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_RUNNER_ADMISSION_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "state": AV1_V4R3_RUNNER_ADMISSION_STATE,
        "owner_principal": grant["owner_principal"],
        "admitted_at": admitted_at,
        "execution_claim_id": claim["claim_id"],
        "execution_claim_payload_sha256": claim["payload_sha256"],
        "execution_grant_id": grant["grant_id"],
        "execution_grant_payload_sha256": grant["payload_sha256"],
        "sequencing_grant_id": sequencing["grant_id"],
        "sequencing_grant_payload_sha256": sequencing["payload_sha256"],
        "plan_id": grant["plan_id"],
        "plan_payload_sha256": grant["plan_payload_sha256"],
        "preflight_id": grant["preflight_id"],
        "preflight_payload_sha256": grant["preflight_payload_sha256"],
        "ordinal": grant["ordinal"],
        "asset_id": grant["asset_id"],
        "closure_id": grant["closure_id"],
        "invocation_sha256": grant["invocation_sha256"],
        "source_path_hmac_id": source_path_hmac_id,
        "instance_path_hmac_ids": instances,
        "quality_temp_hmac_id": quality_temp_hmac_id,
        "quality_temp_key_id": quality_temp_key_id,
        "path_privacy_key_id": grant["path_privacy_key_id"],
        "transform_plan_id": transform_plan_id,
        **runtime_payload_bindings,
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload, "admission_id", "av1v4r3admit")
    assert_av1_v4r3_runner_admission_chain(
        plan=plan,
        preflight=preflight,
        sequencing_grant=sequencing_grant,
        execution_grant=execution_grant,
        execution_claim=execution_claim,
        runner_admission=bound,
        production_stream_plan=production_stream_plan,
        stream_budget_ledger=stream_budget_ledger,
        returned_stream_budget_ledger=returned_stream_budget_ledger,
    )
    return bound


def assert_av1_v4r3_runner_admission(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ADMISSION_ALLOWED_KEYS:
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 runner admission shape is invalid")
    _assert_common_non_authorizing(materialized)
    expected = {
        "schema": AV1_V4R3_RUNNER_ADMISSION_SCHEMA,
        "schema_version": AV1_V4R3_RUNNER_ADMISSION_SCHEMA_VERSION,
        "contract_version": AV1_V4R3_RUNNER_ADMISSION_CONTRACT_VERSION,
        "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        "state": AV1_V4R3_RUNNER_ADMISSION_STATE,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission binding is invalid"
        )
    _assert_exact_integers(
        materialized,
        {
            "schema_version": AV1_V4R3_RUNNER_ADMISSION_SCHEMA_VERSION,
            "protocol_version": AV1_V4_R3_PROTOCOL_VERSION,
            "manifest_revision": AV1_V4_R3_MANIFEST_REVISION,
        },
    )
    _assert_chain_identifiers(materialized, admission=True)
    _assert_runtime_bindings(
        source_path_hmac_id=str(materialized["source_path_hmac_id"]),
        instance_path_hmac_ids=object_dict(materialized["instance_path_hmac_ids"]),
        quality_temp_hmac_id=str(materialized["quality_temp_hmac_id"]),
        quality_temp_key_id=str(materialized["quality_temp_key_id"]),
        path_privacy_key_id=str(materialized["path_privacy_key_id"]),
        production_source_id=str(materialized["production_source_id"]),
        production_stream_plan_id=str(materialized["production_stream_plan_id"]),
        production_stream_plan_payload_sha256=str(
            materialized["production_stream_plan_payload_sha256"]
        ),
        stream_budget_ledger_id=str(materialized["stream_budget_ledger_id"]),
        stream_budget_ledger_payload_sha256=str(
            materialized["stream_budget_ledger_payload_sha256"]
        ),
        ledger_closure_id=str(materialized["ledger_closure_id"]),
        size_goal_id=str(materialized["size_goal_id"]),
        transform_plan_id=str(materialized["transform_plan_id"]),
        returned_stream_budget_ledger_id=str(
            materialized["returned_stream_budget_ledger_id"]
        ),
        returned_stream_budget_ledger_payload_sha256=str(
            materialized["returned_stream_budget_ledger_payload_sha256"]
        ),
    )
    if (
        materialized["returned_stream_budget_ledger_id"]
        != materialized["stream_budget_ledger_id"]
        or materialized["returned_stream_budget_ledger_payload_sha256"]
        != materialized["stream_budget_ledger_payload_sha256"]
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger binding is invalid"
        )
    _parse_timestamp(materialized.get("admitted_at"), "admitted_at")
    admission_id = str(materialized.get("admission_id") or "")
    if not _ADMISSION_ID_RE.fullmatch(admission_id) or admission_id != _identity_id(
        materialized, "admission_id", "av1v4r3admit"
    ):
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 runner admission ID is invalid")
    _assert_payload_digest(materialized, "runner admission")


def assert_av1_v4r3_runner_admission_chain(
    *,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    runner_admission: Mapping[str, Any],
    production_stream_plan: Mapping[str, Any],
    stream_budget_ledger: Mapping[str, Any],
    returned_stream_budget_ledger: Mapping[str, Any],
) -> None:
    try:
        assert_av1_v4r3_execution_grant_chain(
            plan=plan,
            preflight=preflight,
            sequencing_grant=sequencing_grant,
            execution_grant=execution_grant,
        )
        assert_av1_v4r3_execution_claim_chain(
            execution_claim=execution_claim,
            execution_grant=execution_grant,
            sequencing_grant=sequencing_grant,
        )
        assert_av1_v4r3_execution_preflight(preflight)
        assert_av1_v4r3_runner_admission(runner_admission)
    except (
        AV1V4R3RunnerAdmissionError,
        AV1V4R3ExecutionGrantError,
        AV1V4R3ExecutionPreflightError,
    ) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission chain is invalid"
        ) from exc
    claim = object_dict(execution_claim)
    grant = object_dict(execution_grant)
    sequencing = object_dict(sequencing_grant)
    preflight_payload = object_dict(preflight)
    admission = object_dict(runner_admission)
    readiness = _readiness_for_ordinal(preflight_payload, grant["ordinal"])
    runtime_payload_bindings = _runtime_payload_bindings(
        asset_id=str(grant["asset_id"]),
        production_stream_plan=production_stream_plan,
        stream_budget_ledger=stream_budget_ledger,
        returned_stream_budget_ledger=returned_stream_budget_ledger,
    )
    bindings = (
        (admission.get("execution_claim_id"), claim.get("claim_id")),
        (
            admission.get("execution_claim_payload_sha256"),
            claim.get("payload_sha256"),
        ),
        (admission.get("execution_grant_id"), grant.get("grant_id")),
        (
            admission.get("execution_grant_payload_sha256"),
            grant.get("payload_sha256"),
        ),
        (admission.get("sequencing_grant_id"), sequencing.get("grant_id")),
        (
            admission.get("sequencing_grant_payload_sha256"),
            sequencing.get("payload_sha256"),
        ),
        (admission.get("plan_id"), grant.get("plan_id")),
        (admission.get("plan_payload_sha256"), grant.get("plan_payload_sha256")),
        (admission.get("preflight_id"), preflight_payload.get("preflight_id")),
        (
            admission.get("preflight_payload_sha256"),
            preflight_payload.get("payload_sha256"),
        ),
        (admission.get("owner_principal"), grant.get("owner_principal")),
        (admission.get("ordinal"), grant.get("ordinal")),
        (admission.get("asset_id"), readiness.get("asset_id")),
        (admission.get("closure_id"), readiness.get("closure_id")),
        (admission.get("invocation_sha256"), readiness.get("invocation_sha256")),
        (admission.get("path_privacy_key_id"), grant.get("path_privacy_key_id")),
        (admission.get("transform_plan_id"), readiness.get("transform_plan_id")),
        (admission.get("size_goal_id"), readiness.get("size_goal_id")),
        (admission.get("ledger_closure_id"), readiness.get("ledger_closure_id")),
        *(
            (admission.get(field), value)
            for field, value in runtime_payload_bindings.items()
        ),
    )
    if any(left != right for left, right in bindings):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission predecessor binding is invalid"
        )
    admitted_at = _parse_timestamp(admission["admitted_at"], "admitted_at")
    claimed_at = _parse_timestamp(claim["claimed_at"], "claimed_at")
    if admitted_at < claimed_at:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission precedes the execution claim"
        )
    _assert_sequencing_admission_time(
        sequencing, str(admission["admitted_at"]), "admitted_at"
    )
    try:
        assert_av1_v4r3_execution_grant_active(
            grant, as_of=str(admission["admitted_at"])
        )
    except AV1V4R3ExecutionGrantError as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission is outside the execution grant"
        ) from exc


def serialize_av1_v4r3_execution_claim(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_execution_claim(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_execution_claim(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r3_execution_claim, "execution claim")


def serialize_av1_v4r3_runner_admission(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r3_runner_admission(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r3_runner_admission(data: bytes) -> dict[str, Any]:
    return _deserialize(data, assert_av1_v4r3_runner_admission, "runner admission")


def _assert_grant_sequencing_bindings(
    grant: Mapping[str, Any], sequencing: Mapping[str, Any]
) -> None:
    pairs = (
        (grant.get("sequencing_grant_id"), sequencing.get("grant_id")),
        (
            grant.get("sequencing_grant_payload_sha256"),
            sequencing.get("payload_sha256"),
        ),
        (grant.get("plan_id"), sequencing.get("plan_id")),
        (grant.get("plan_payload_sha256"), sequencing.get("plan_payload_sha256")),
        (grant.get("ordinal"), sequencing.get("ordinal")),
    )
    if any(left != right for left, right in pairs):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 execution sequencing binding is invalid"
        )


def _assert_claim_grant_bindings(
    claim: Mapping[str, Any], grant: Mapping[str, Any], sequencing: Mapping[str, Any]
) -> None:
    _assert_grant_sequencing_bindings(grant, sequencing)
    pairs = (
        (claim.get("execution_grant_id"), grant.get("grant_id")),
        (claim.get("execution_grant_payload_sha256"), grant.get("payload_sha256")),
        (claim.get("sequencing_grant_id"), sequencing.get("grant_id")),
        (
            claim.get("sequencing_grant_payload_sha256"),
            sequencing.get("payload_sha256"),
        ),
        (claim.get("owner_principal"), grant.get("owner_principal")),
        (claim.get("plan_id"), grant.get("plan_id")),
        (claim.get("plan_payload_sha256"), grant.get("plan_payload_sha256")),
        (claim.get("preflight_id"), grant.get("preflight_id")),
        (
            claim.get("preflight_payload_sha256"),
            grant.get("preflight_payload_sha256"),
        ),
        (claim.get("ordinal"), grant.get("ordinal")),
        (claim.get("asset_id"), grant.get("asset_id")),
        (claim.get("closure_id"), grant.get("closure_id")),
        (claim.get("invocation_sha256"), grant.get("invocation_sha256")),
    )
    if any(left != right for left, right in pairs):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission claim binding is invalid"
        )


def _assert_sequencing_admission_time(
    sequencing_grant: Mapping[str, Any], value: str, label: str
) -> None:
    current = _parse_timestamp(value, label)
    opens = _parse_timestamp(
        sequencing_grant["admission_opens_at"], "sequencing admission_opens_at"
    )
    closes = _parse_timestamp(
        sequencing_grant["admission_closes_at"], "sequencing admission_closes_at"
    )
    if not opens <= current < closes:
        raise AV1V4R3RunnerAdmissionError(
            f"AV1 v4 r3 runner {label} is outside the sequencing admission window"
        )


def _readiness_for_ordinal(
    preflight: Mapping[str, Any], ordinal: int
) -> dict[str, Any]:
    for item in object_list(preflight.get("ordinal_readiness")):
        readiness = object_dict(item)
        if readiness.get("ordinal") == ordinal:
            return readiness
    raise AV1V4R3RunnerAdmissionError(
        "AV1 v4 r3 runner admission readiness is unavailable"
    )


def _assert_runtime_bindings(**values: Any) -> None:
    patterns = {
        "source_path_hmac_id": _SOURCE_PATH_ID_RE,
        "quality_temp_hmac_id": _QUALITY_TEMP_ID_RE,
        "quality_temp_key_id": _QUALITY_KEY_ID_RE,
        "path_privacy_key_id": _PATH_KEY_ID_RE,
        "production_source_id": _SOURCE_ID_RE,
        "production_stream_plan_id": _STREAM_PLAN_ID_RE,
        "stream_budget_ledger_id": _LEDGER_ID_RE,
        "ledger_closure_id": _LEDGER_CLOSURE_ID_RE,
        "size_goal_id": _SIZE_GOAL_ID_RE,
        "transform_plan_id": _TRANSFORM_PLAN_ID_RE,
        "returned_stream_budget_ledger_id": _LEDGER_ID_RE,
    }
    if any(
        not pattern.fullmatch(str(values.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission runtime identity is invalid"
        )
    for field in (
        "production_stream_plan_payload_sha256",
        "stream_budget_ledger_payload_sha256",
        "returned_stream_budget_ledger_payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(values.get(field) or "")):
            raise AV1V4R3RunnerAdmissionError(
                "AV1 v4 r3 runner admission runtime digest is invalid"
            )
    instances = object_dict(values.get("instance_path_hmac_ids"))
    if set(instances) != AV1_V4R3_INSTANCE_PATH_ROLES or any(
        not _INSTANCE_PATH_ID_RE.fullmatch(str(value or ""))
        for value in instances.values()
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission instance identity is invalid"
        )


def _runtime_payload_bindings(
    *,
    asset_id: str,
    production_stream_plan: Mapping[str, Any],
    stream_budget_ledger: Mapping[str, Any],
    returned_stream_budget_ledger: Mapping[str, Any],
) -> dict[str, str]:
    plan = _assert_stream_plan_payload(production_stream_plan)
    ledger = _assert_stream_budget_ledger_payload(
        asset_id=asset_id,
        stream_plan=plan,
        payload=stream_budget_ledger,
    )
    returned_ledger = _assert_stream_budget_ledger_payload(
        asset_id=asset_id,
        stream_plan=plan,
        payload=returned_stream_budget_ledger,
    )
    try:
        if canonical_json_bytes(ledger) != canonical_json_bytes(returned_ledger):
            raise AV1V4R3RunnerAdmissionError(
                "AV1 v4 r3 runner admission returned ledger does not match"
            )
        plan_digest = f"sha256:{stable_json_hash(plan)}"
        ledger_digest = f"sha256:{stable_json_hash(ledger)}"
    except (TypeError, ValueError) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission runtime payload is not canonical"
        ) from exc
    closure = av1_v4_r3_stream_ledger_closure_payload(asset_id)
    size_goal = av1_v4_r3_resolved_size_goal_payload(asset_id)
    return {
        "production_source_id": str(plan["source_id"]),
        "production_stream_plan_id": str(plan["plan_id"]),
        "production_stream_plan_payload_sha256": plan_digest,
        "stream_budget_ledger_id": str(ledger["ledger_id"]),
        "stream_budget_ledger_payload_sha256": ledger_digest,
        "ledger_closure_id": str(closure["ledger_closure_id"]),
        "size_goal_id": str(size_goal["size_goal_id"]),
        "returned_stream_budget_ledger_id": str(returned_ledger["ledger_id"]),
        "returned_stream_budget_ledger_payload_sha256": ledger_digest,
    }


def _assert_stream_plan_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    plan = object_dict(payload)
    if set(plan) != _STREAM_PLAN_KEYS or type(plan.get("schema_version")) is not int:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission stream plan shape is invalid"
        )
    if plan["schema_version"] != 1:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission stream plan version is invalid"
        )
    if (
        not _SOURCE_ID_RE.fullmatch(str(plan.get("source_id") or ""))
        or not _SHA256_RE.fullmatch(str(plan.get("policy_hash") or ""))
        or not isinstance(plan.get("source_fingerprint"), str | type(None))
        or not isinstance(plan.get("output_container"), str)
        or type(plan.get("attachments_known")) is not bool
        or type(plan.get("copy_unknown_attachments")) is not bool
        or not isinstance(plan.get("streams"), list)
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission stream plan binding is invalid"
        )
    try:
        parsed_plan = ProductionStreamPlan.from_payload(plan)
    except (StreamPlanIdentityError, TypeError, ValueError) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission stream plan identity is invalid"
        ) from exc
    if not _canonical_equal(parsed_plan.to_payload(), plan):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission stream plan is not canonical"
        )
    return plan


def _assert_stream_budget_ledger_payload(
    *,
    asset_id: str,
    stream_plan: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    ledger = object_dict(payload)
    if set(ledger) != _LEDGER_KEYS or type(ledger.get("schema_version")) is not int:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger shape is invalid"
        )
    if ledger["schema_version"] != 1 or not _canonical_equal(
        ledger.get("stream_plan"), stream_plan
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger stream plan is invalid"
        )
    source = object_dict(ledger.get("source"))
    if set(source) != _LEDGER_SOURCE_KEYS:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger source shape is invalid"
        )
    closure = av1_v4_r3_stream_ledger_closure_payload(asset_id)
    size_goal = av1_v4_r3_resolved_size_goal_payload(asset_id)
    actual_size_goal = object_dict(ledger.get("size_goal"))
    expected_size_goal = size_goal_from_policy(
        dict(AV1_V4_R3_FULL_VIDEO_POLICY)
    ).resolve(float(size_goal["source_duration_seconds"]))
    if not _canonical_equal(actual_size_goal, expected_size_goal.to_payload()):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission size goal binding is invalid"
        )
    critical_bindings = (
        (source.get("source_id"), stream_plan.get("source_id")),
        (source.get("source_fingerprint"), stream_plan.get("source_fingerprint")),
        (source.get("source_size_bytes"), closure["source_media_bytes"]),
        (
            source.get("source_video_bitrate_bps"),
            closure["source_video_bitrate_bps"],
        ),
        (source.get("duration_seconds"), closure["source_duration_seconds"]),
        (ledger.get("policy_hash"), stream_plan.get("policy_hash")),
        (stream_plan.get("output_container"), closure["output_container"]),
        (
            object_dict(ledger.get("totals")).get("total_target_bytes"),
            closure["total_target_bytes"],
        ),
    )
    if any(not _canonical_equal(left, right) for left, right in critical_bindings):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger closure binding is invalid"
        )
    try:
        parsed_ledger = StreamBudgetLedger.from_payload(ledger)
    except (
        StreamBudgetIdentityError,
        StreamPlanIdentityError,
        TypeError,
        ValueError,
    ) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger identity is invalid"
        ) from exc
    if not _canonical_equal(parsed_ledger.to_payload(), ledger):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner admission ledger is not canonical"
        )
    return ledger


def _assert_common_non_authorizing(payload: Mapping[str, Any]) -> None:
    _assert_no_private_text(payload)
    if any(
        payload.get(field) is not False
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact cannot authorize activity"
        )
    ordinal = payload.get("ordinal")
    if type(ordinal) is not int or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact ordinal is invalid"
        )
    owner = payload.get("owner_principal")
    if not isinstance(owner, str) or not owner.strip() or len(owner) > 128:
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 runner artifact owner is invalid")
    if payload.get("asset_id") not in AV1_VALIDATION_V4_SOURCE_IDS:
        raise AV1V4R3RunnerAdmissionError("AV1 v4 r3 runner artifact asset is invalid")


def _assert_exact_integers(
    payload: Mapping[str, Any], expected: Mapping[str, int]
) -> None:
    if any(
        type(payload.get(field)) is not int or payload.get(field) != value
        for field, value in expected.items()
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact integer binding is invalid"
        )


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        return canonical_json_bytes(left) == canonical_json_bytes(right)
    except (TypeError, ValueError) as exc:
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact contains non-canonical data"
        ) from exc


def _assert_chain_identifiers(
    payload: Mapping[str, Any], *, admission: bool = False
) -> None:
    patterns = {
        "execution_grant_id": _EXECUTION_GRANT_ID_RE,
        "sequencing_grant_id": _SEQUENCING_GRANT_ID_RE,
        "plan_id": _PLAN_ID_RE,
        "preflight_id": _PREFLIGHT_ID_RE,
        "closure_id": _CLOSURE_ID_RE,
    }
    if admission:
        patterns["execution_claim_id"] = _CLAIM_ID_RE
    if any(
        not pattern.fullmatch(str(payload.get(field) or ""))
        for field, pattern in patterns.items()
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact identity is invalid"
        )
    digest_fields = [
        "payload_sha256",
        "execution_grant_payload_sha256",
        "sequencing_grant_payload_sha256",
        "plan_payload_sha256",
        "preflight_payload_sha256",
        "invocation_sha256",
    ]
    if admission:
        digest_fields.append("execution_claim_payload_sha256")
    if any(
        not _SHA256_RE.fullmatch(str(payload.get(field) or ""))
        for field in digest_fields
    ):
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact digest identity is invalid"
        )


def _assert_payload_digest(payload: Mapping[str, Any], label: str) -> None:
    without_sha = {
        key: value for key, value in payload.items() if key != "payload_sha256"
    }
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R3RunnerAdmissionError(f"AV1 v4 r3 {label} digest is invalid")


def _bind_identity(
    payload: Mapping[str, Any], id_field: str, prefix: str
) -> dict[str, Any]:
    bound = dict(payload)
    bound[id_field] = _identity_id(bound, id_field, prefix)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _identity_id(payload: Mapping[str, Any], id_field: str, prefix: str) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {id_field, "payload_sha256"}
    }
    return f"{prefix}_{stable_json_hash(semantic)[:32]}"


def _deserialize(data: bytes, assertion: Any, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
        canonical = canonical_json_bytes(payload) + b"\n"
    except (TypeError, UnicodeDecodeError, ValueError) as exc:
        raise AV1V4R3RunnerAdmissionError(f"AV1 v4 r3 {label} is unreadable") from exc
    if not isinstance(payload, dict) or data != canonical:
        raise AV1V4R3RunnerAdmissionError(f"AV1 v4 r3 {label} bytes are not canonical")
    assertion(payload)
    return payload


def _parse_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3RunnerAdmissionError(f"AV1 v4 r3 runner {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3RunnerAdmissionError(
            f"AV1 v4 r3 runner {label} is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1V4R3RunnerAdmissionError(
            f"AV1 v4 r3 runner {label} must use canonical UTC seconds"
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
        raise AV1V4R3RunnerAdmissionError(
            "AV1 v4 r3 runner artifact exposes private text"
        )
