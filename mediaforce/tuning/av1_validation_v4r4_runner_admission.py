"""Non-authorizing runner admission contract for AV1 v4 revision 4."""

from __future__ import annotations

from collections.abc import Mapping
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
    AV1_V4R4_POLICY_VALUES,
    AV1_V4R4_POLICY_VALUES_SHA256,
    AV1_V4R4_PROTOCOL_VERSION,
    assert_av1_v4r4_runtime_policy,
    av1_v4r4_identity_domain,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
    AV1V4R4ExecutionAuthorityError,
    assert_av1_v4r4_execution_claim,
    assert_av1_v4r4_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    assert_av1_v4r4_ordinal_registry_claim,
    assert_av1_v4r4_ordinal_registry_grant,
    assert_av1_v4r4_ordinal_registry_plan,
)


AV1_V4R4_RUNNER_ADMISSION_SCHEMA = "mediaforce.av1_cold_start_v4r4_runner_admission"
AV1_V4R4_RUNNER_ADMISSION_SCHEMA_VERSION = 1
AV1_V4R4_RUNNER_ADMISSION_CONTRACT_VERSION = "av1v4r4runneradmit1"

_ADMISSION_KEYS = {
    "schema",
    "schema_version",
    "contract_version",
    "protocol_version",
    "manifest_revision",
    "experiment_id",
    "manifest_id",
    "manifest_payload_sha256",
    "runner_admission_id",
    "payload_sha256",
    "plan_id",
    "plan_payload_sha256",
    "sequencing_grant_id",
    "sequencing_grant_payload_sha256",
    "sequencing_claim_id",
    "sequencing_claim_payload_sha256",
    "execution_grant_id",
    "execution_grant_payload_sha256",
    "execution_claim_id",
    "execution_claim_payload_sha256",
    "invocation_sha256",
    "ordinal",
    "asset_id",
    "content_class",
    "role",
    "configuration",
    "policy_values_sha256",
    "runtime_policy",
    "stream_budget_ledger_identity",
    "production_stream_plan_identity",
} | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
_RUNTIME_POLICY_KEYS = {
    "metric_name",
    "metric_target",
    "minimum_metric_score",
    "relax_step",
    "sample_projection_tolerance_percent",
    "final_output_tolerance_percent",
    "source_cap_percent",
    "total_target_bytes",
    "source_cap_total_bytes",
}
_LEDGER_ID_RE = re.compile(r"sb1_[0-9a-f]{32}\Z")
_STREAM_PLAN_ID_RE = re.compile(r"sp1_[0-9a-f]{32}\Z")

_ADMISSION_ID_RE = re.compile(r"av1v4r4runneradmit_[0-9a-f]{32}\Z")
_PUBLIC_ID_RE = re.compile(r"av1v4r4[a-z0-9_]*_[0-9a-f]{32,64}\Z")
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1V4R4RunnerAdmissionError(ValueError):
    """Raised when an r4 runner admission contract is invalid."""


def build_av1_v4r4_runner_admission(
    *,
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    invocation_sha256: str,
    stream_budget_ledger: Mapping[str, Any],
    production_stream_plan: Mapping[str, Any],
    metric_name: str,
    metric_target: float,
    minimum_metric_score: float,
    relax_step: float,
    sample_projection_tolerance_percent: int,
    final_output_tolerance_percent: int,
    source_cap_percent: int,
    total_target_bytes: int,
    source_cap_total_bytes: int,
) -> dict[str, Any]:
    plan_payload = dict(plan)
    seq_grant_payload = dict(sequencing_grant)
    seq_claim_payload = dict(sequencing_claim)
    exec_grant_payload = dict(execution_grant)
    exec_claim_payload = dict(execution_claim)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_grant(seq_grant_payload)
    assert_av1_v4r4_ordinal_registry_claim(seq_claim_payload)
    try:
        assert_av1_v4r4_execution_grant(exec_grant_payload)
        assert_av1_v4r4_execution_claim(exec_claim_payload)
    except AV1V4R4ExecutionAuthorityError as exc:
        raise AV1V4R4RunnerAdmissionError(
            "AV1 v4 r4 runner admission chain binding is invalid"
        ) from exc
    ordinal = exec_claim_payload["ordinal"]
    layout = _layout_for_ordinal(ordinal)
    runtime_policy = {
        "metric_name": metric_name,
        "metric_target": metric_target,
        "minimum_metric_score": minimum_metric_score,
        "relax_step": relax_step,
        "sample_projection_tolerance_percent": sample_projection_tolerance_percent,
        "final_output_tolerance_percent": final_output_tolerance_percent,
        "source_cap_percent": source_cap_percent,
        "total_target_bytes": total_target_bytes,
        "source_cap_total_bytes": source_cap_total_bytes,
    }
    payload: dict[str, Any] = {
        "schema": AV1_V4R4_RUNNER_ADMISSION_SCHEMA,
        "schema_version": AV1_V4R4_RUNNER_ADMISSION_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_RUNNER_ADMISSION_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "sequencing_grant_id": seq_grant_payload["grant_id"],
        "sequencing_grant_payload_sha256": seq_grant_payload["payload_sha256"],
        "sequencing_claim_id": seq_claim_payload["claim_id"],
        "sequencing_claim_payload_sha256": seq_claim_payload["payload_sha256"],
        "execution_grant_id": exec_grant_payload["execution_grant_id"],
        "execution_grant_payload_sha256": exec_grant_payload["payload_sha256"],
        "execution_claim_id": exec_claim_payload["execution_claim_id"],
        "execution_claim_payload_sha256": exec_claim_payload["payload_sha256"],
        "invocation_sha256": invocation_sha256,
        "ordinal": ordinal,
        "asset_id": layout["asset_id"],
        "content_class": layout["content_class"],
        "role": layout["role"],
        "configuration": layout["configuration"],
        "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
        "runtime_policy": runtime_policy,
        "stream_budget_ledger_identity": _stream_budget_ledger_identity(
            stream_budget_ledger,
        ),
        "production_stream_plan_identity": _production_stream_plan_identity(
            production_stream_plan,
        ),
        **{field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS},
    }
    bound = _bind_identity(payload)
    assert_av1_v4r4_runner_admission(bound)
    return bound


def assert_av1_v4r4_runner_admission(payload: Mapping[str, Any]) -> None:
    materialized = object_dict(payload)
    if set(materialized) != _ADMISSION_KEYS:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission shape is invalid")
    expected_common = {
        "schema": AV1_V4R4_RUNNER_ADMISSION_SCHEMA,
        "schema_version": AV1_V4R4_RUNNER_ADMISSION_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_RUNNER_ADMISSION_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    }
    if any(materialized.get(key) != value for key, value in expected_common.items()):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission binding is invalid")
    ordinal = materialized.get("ordinal")
    layout = _layout_for_ordinal(ordinal)
    if (
        materialized.get("asset_id") != layout["asset_id"]
        or materialized.get("content_class") != layout["content_class"]
        or materialized.get("role") != layout["role"]
        or materialized.get("configuration") != layout["configuration"]
        or materialized.get("policy_values_sha256") != AV1_V4R4_POLICY_VALUES_SHA256
    ):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ordinal binding is invalid")
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission cannot confer authority")
    for key in (
        "plan_payload_sha256",
        "sequencing_grant_payload_sha256",
        "sequencing_claim_payload_sha256",
        "execution_grant_payload_sha256",
        "execution_claim_payload_sha256",
    ):
        if not _SHA256_RE.fullmatch(str(materialized.get(key) or "")):
            raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission chain digest is invalid")
    if not _SHA256_RE.fullmatch(str(materialized.get("invocation_sha256") or "")):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission invocation digest is invalid")
    _assert_ledger_identity(materialized.get("stream_budget_ledger_identity"))
    _assert_stream_plan_identity(materialized.get("production_stream_plan_identity"))
    _assert_runtime_policy(materialized.get("runtime_policy"), layout)
    _assert_no_private_text(materialized)
    _assert_identity(materialized)


def assert_av1_v4r4_runner_admission_chain(
    *,
    admission: Mapping[str, Any],
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
) -> None:
    admission_payload = dict(admission)
    plan_payload = dict(plan)
    seq_grant_payload = dict(sequencing_grant)
    seq_claim_payload = dict(sequencing_claim)
    exec_grant_payload = dict(execution_grant)
    exec_claim_payload = dict(execution_claim)
    assert_av1_v4r4_runner_admission(admission_payload)
    assert_av1_v4r4_ordinal_registry_plan(plan_payload)
    assert_av1_v4r4_ordinal_registry_grant(seq_grant_payload)
    assert_av1_v4r4_ordinal_registry_claim(seq_claim_payload)
    try:
        assert_av1_v4r4_execution_grant(exec_grant_payload)
        assert_av1_v4r4_execution_claim(exec_claim_payload)
    except AV1V4R4ExecutionAuthorityError as exc:
        raise AV1V4R4RunnerAdmissionError(
            "AV1 v4 r4 runner admission chain binding is invalid"
        ) from exc
    expected = {
        "plan_id": plan_payload["plan_id"],
        "plan_payload_sha256": plan_payload["payload_sha256"],
        "sequencing_grant_id": seq_grant_payload["grant_id"],
        "sequencing_grant_payload_sha256": seq_grant_payload["payload_sha256"],
        "sequencing_claim_id": seq_claim_payload["claim_id"],
        "sequencing_claim_payload_sha256": seq_claim_payload["payload_sha256"],
        "execution_grant_id": exec_grant_payload["execution_grant_id"],
        "execution_grant_payload_sha256": exec_grant_payload["payload_sha256"],
        "execution_claim_id": exec_claim_payload["execution_claim_id"],
        "execution_claim_payload_sha256": exec_claim_payload["payload_sha256"],
        "ordinal": exec_claim_payload["ordinal"],
    }
    if any(admission_payload.get(key) != value for key, value in expected.items()):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission chain binding is invalid")
    if (
        seq_grant_payload["ordinal"] != expected["ordinal"]
        or seq_claim_payload["ordinal"] != expected["ordinal"]
        or exec_grant_payload["ordinal"] != expected["ordinal"]
    ):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ordinal chain is invalid")


def av1_v4r4_runner_stream_budget_ledger_identity(
    stream_budget_ledger: Mapping[str, Any],
) -> dict[str, Any]:
    return _stream_budget_ledger_identity(stream_budget_ledger)


def av1_v4r4_runner_production_stream_plan_identity(
    production_stream_plan: Mapping[str, Any],
) -> dict[str, Any]:
    return _production_stream_plan_identity(production_stream_plan)


def serialize_av1_v4r4_runner_admission(payload: Mapping[str, Any]) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_v4r4_runner_admission(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def deserialize_av1_v4r4_runner_admission(data: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission bytes are unreadable") from exc
    if not isinstance(payload, dict) or data != canonical_json_bytes(payload) + b"\n":
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission bytes are not canonical")
    assert_av1_v4r4_runner_admission(payload)
    return payload


def _bind_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    bound = dict(payload)
    bound["runner_admission_id"] = "av1v4r4runneradmit_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("runner-admission"), "payload": bound}
    )[:32]
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    return json.loads(canonical_json_bytes(bound))


def _assert_identity(payload: Mapping[str, Any]) -> None:
    semantic = {
        key: value
        for key, value in dict(payload).items()
        if key not in {"runner_admission_id", "payload_sha256"}
    }
    expected_id = "av1v4r4runneradmit_" + stable_json_hash(
        {"domain": av1_v4r4_identity_domain("runner-admission"), "payload": semantic}
    )[:32]
    if not _ADMISSION_ID_RE.fullmatch(str(payload.get("runner_admission_id") or "")) or payload.get("runner_admission_id") != expected_id:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ID is invalid")
    without_sha = {key: value for key, value in dict(payload).items() if key != "payload_sha256"}
    if payload.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission digest is invalid")


def _assert_runtime_policy(value: Any, layout: Mapping[str, Any]) -> None:
    policy = object_dict(value)
    if set(policy) != _RUNTIME_POLICY_KEYS:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission runtime policy shape is invalid")
    try:
        assert_av1_v4r4_runtime_policy(
            metric_name=policy["metric_name"],
            metric_target=policy["metric_target"],
            minimum_metric_score=policy["minimum_metric_score"],
            relax_step=policy["relax_step"],
            sample_projection_tolerance_percent=policy["sample_projection_tolerance_percent"],
            final_output_tolerance_percent=policy["final_output_tolerance_percent"],
            source_cap_percent=policy["source_cap_percent"],
        )
    except Exception as exc:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission runtime policy drifted") from exc
    if (
        policy["total_target_bytes"] != layout["target_size_bytes"]
        or policy["source_cap_total_bytes"] != layout["source_cap_total_bytes"]
        or policy["metric_name"] != AV1_V4R4_POLICY_VALUES["quality_metric"]
    ):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission runtime policy ordinal binding is invalid")


def _stream_budget_ledger_identity(stream_budget_ledger: Mapping[str, Any]) -> dict[str, Any]:
    ledger = json.loads(canonical_json_bytes(stream_budget_ledger))
    ledger_id = str(object_dict(ledger).get("ledger_id") or "")
    return {
        "ledger_id": ledger_id,
        "payload_sha256": f"sha256:{stable_json_hash(ledger)}",
    }


def _production_stream_plan_identity(production_stream_plan: Mapping[str, Any]) -> dict[str, Any]:
    stream_plan = json.loads(canonical_json_bytes(production_stream_plan))
    plan_id = str(object_dict(stream_plan).get("plan_id") or "")
    return {
        "stream_plan_id": plan_id,
        "payload_sha256": f"sha256:{stable_json_hash(stream_plan)}",
    }


def _assert_ledger_identity(value: Any) -> None:
    identity = object_dict(value)
    if set(identity) != {"ledger_id", "payload_sha256"}:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ledger identity shape is invalid")
    if not _LEDGER_ID_RE.fullmatch(str(identity.get("ledger_id") or "")) or not _SHA256_RE.fullmatch(str(identity.get("payload_sha256") or "")):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ledger identity is invalid")


def _assert_stream_plan_identity(value: Any) -> None:
    identity = object_dict(value)
    if set(identity) != {"stream_plan_id", "payload_sha256"}:
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission stream plan identity shape is invalid")
    if not _STREAM_PLAN_ID_RE.fullmatch(str(identity.get("stream_plan_id") or "")) or not _SHA256_RE.fullmatch(str(identity.get("payload_sha256") or "")):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission stream plan identity is invalid")


def _layout_for_ordinal(ordinal: Any) -> dict[str, Any]:
    layout = av1_v4r4_ordinal_layout()
    if type(ordinal) is not int or not 1 <= ordinal <= len(layout):
        raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission ordinal is invalid")
    return layout[ordinal - 1]


def _assert_no_private_text(value: Any) -> None:
    if isinstance(value, str):
        if av1_validation_v4_contains_private_text(value):
            raise AV1V4R4RunnerAdmissionError("AV1 v4 r4 runner admission contains machine-local text")
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _assert_no_private_text(str(key))
            _assert_no_private_text(child)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_private_text(child)
