from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4r4_contract import AV1_V4R4_POLICY_VALUES
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    av1_validation_v4_qualification_search_invocation_sha256,
)
from mediaforce.tuning.av1_validation_v4r4_one_ordinal_runner import (
    _runtime_item,
    _video_policy_for_ordinal,
    _warm_start_for_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_runner_admission import (
    AV1V4R4RunnerAdmissionError,
    assert_av1_v4r4_runner_admission,
    assert_av1_v4r4_runner_admission_chain,
    build_av1_v4r4_runner_admission,
    deserialize_av1_v4r4_runner_admission,
    serialize_av1_v4r4_runner_admission,
)
from mediaforce.encoding.streams import resolve_stream_plan
from mediaforce.tuning.size_goals import SizeGoalIntent
from mediaforce.tuning.stream_budget import build_stream_budget_ledger
from tests.test_av1_validation_v4r4_execution_authority import (
    _execution_claim,
    _execution_grant,
    _prepared_chain,
    _private_canonical_bytes,
    _sequencing_claim,
)


def test_runner_admission_binds_all_public_chain_and_policy_values(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    seq_grant = prepared.sequencing_grant
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant, qualification_request=prepared.request, execution_preflight=prepared.preflight)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)

    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim, qualification_request=prepared.request, execution_preflight=prepared.preflight)

    assert_av1_v4r4_runner_admission(admission)
    assert_av1_v4r4_runner_admission_chain(
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        admission=admission,
        plan=plan,
        sequencing_grant=seq_grant,
        sequencing_claim=seq_claim,
        execution_grant=exec_grant,
        execution_claim=exec_claim,
    )
    assert deserialize_av1_v4r4_runner_admission(
        serialize_av1_v4r4_runner_admission(admission)
    ) == admission
    assert deserialize_av1_v4r4_runner_admission(_private_canonical_bytes(admission)) == admission
    assert all(admission[field] is False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)


def test_runner_admission_rejects_runtime_policy_drift_and_private_text(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    seq_grant = prepared.sequencing_grant
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant, qualification_request=prepared.request, execution_preflight=prepared.preflight)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim, qualification_request=prepared.request, execution_preflight=prepared.preflight)

    drifted = deepcopy(admission)
    drifted["runtime_policy"]["metric_target"] = 84.0
    with pytest.raises(AV1V4R4RunnerAdmissionError, match="drifted"):
        assert_av1_v4r4_runner_admission(drifted)

    bad_digest = deepcopy(admission)
    bad_digest["stream_budget_ledger_identity"]["payload_sha256"] = "not-a-digest"
    with pytest.raises(AV1V4R4RunnerAdmissionError, match="ledger identity"):
        assert_av1_v4r4_runner_admission(bad_digest)

    authorized = deepcopy(admission)
    authorized["runtime_execution_authorized"] = True
    with pytest.raises(AV1V4R4RunnerAdmissionError, match="cannot confer authority"):
        assert_av1_v4r4_runner_admission(authorized)


def test_runner_admission_chain_rejects_forged_execution_claim(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    seq_grant = prepared.sequencing_grant
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant, qualification_request=prepared.request, execution_preflight=prepared.preflight)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim, qualification_request=prepared.request, execution_preflight=prepared.preflight)
    forged_claim = {**exec_claim, "execution_claim_id": "av1v4r4execclaim_" + "0" * 32}

    with pytest.raises(AV1V4R4RunnerAdmissionError, match="chain binding"):
        assert_av1_v4r4_runner_admission_chain(
            qualification_request=prepared.request,
            execution_preflight=prepared.preflight,
            admission=admission,
            plan=plan,
            sequencing_grant=seq_grant,
            sequencing_claim=seq_claim,
            execution_grant=exec_grant,
            execution_claim=forged_claim,
        )


def test_runner_admission_requires_owner_prepared_invocation(tmp_path: Path) -> None:
    prepared = _prepared_chain(tmp_path)
    binding, plan, clock = prepared.binding, prepared.plan, prepared.clock
    seq_grant = prepared.sequencing_grant
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(
        plan,
        seq_grant,
        qualification_request=prepared.request,
        execution_preflight=prepared.preflight,
        prepared_invocation_sha256="sha256:" + "0" * 64,
    )
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)

    with pytest.raises(AV1V4R4RunnerAdmissionError, match="preparation"):
        _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim, qualification_request=prepared.request, execution_preflight=prepared.preflight)


def _admission(
    plan: Mapping[str, Any],
    seq_grant: Mapping[str, Any],
    seq_claim: Mapping[str, Any],
    exec_grant: Mapping[str, Any],
    exec_claim: Mapping[str, Any],
    *,
    qualification_request: Mapping[str, Any] | None = None,
    execution_preflight: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ordinal = int(exec_claim["ordinal"])
    video_policy = _video_policy_for_ordinal(ordinal)
    runtime_item = _runtime_item(ordinal, video_policy)
    stream_plan = resolve_stream_plan(runtime_item)
    ledger = build_stream_budget_ledger(
        runtime_item,
        resolved_size_goal=SizeGoalIntent(
            mode="absolute",
            value_bytes=int(seq_grant["target_size_bytes"]),
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=float(AV1_V4R4_POLICY_VALUES["sample_projection_tolerance_percent"]),
            final_output_tolerance_percent=float(AV1_V4R4_POLICY_VALUES["final_output_tolerance_percent"]),
            source="av1_v4r4_frozen_ordinal_layout",
        ).resolve(float(runtime_item["duration_seconds"])),
        stream_plan=stream_plan,
    )
    warm_start = _warm_start_for_ordinal(ordinal)
    mode = "guided" if warm_start is not None else "baseline"
    search_kwargs = {
        "source_codec": "h264",
        "width": 1920,
        "height": 1080,
        "quality_temp_dir": Path("/tmp/mediaforce-v4r4-quality"),
    }
    call = lambda: build_av1_v4r4_runner_admission(
        qualification_request=qualification_request or {},
        execution_preflight=execution_preflight or {},
        plan=plan,
        sequencing_grant=seq_grant,
        sequencing_claim=seq_claim,
        execution_grant=exec_grant,
        execution_claim=exec_claim,
        invocation_sha256=(
            next(
                item["invocation_sha256"]
                for item in qualification_request["invocation_digests"]
                if item["ordinal"] == ordinal
            )
            if qualification_request is not None
            else str(exec_grant["prepared_invocation_sha256"])
        ),
        stream_budget_ledger=ledger.to_payload(),
        production_stream_plan=stream_plan.to_payload(),
        metric_name="vmaf",
        metric_target=float(AV1_V4R4_POLICY_VALUES["target_vmaf"]),
        minimum_metric_score=float(AV1_V4R4_POLICY_VALUES["min_target_vmaf"]),
        relax_step=float(AV1_V4R4_POLICY_VALUES["target_relax_step_vmaf"]),
        sample_projection_tolerance_percent=int(AV1_V4R4_POLICY_VALUES["sample_projection_tolerance_percent"]),
        final_output_tolerance_percent=int(AV1_V4R4_POLICY_VALUES["final_output_tolerance_percent"]),
        source_cap_percent=int(AV1_V4R4_POLICY_VALUES["max_encoded_percent"]),
        total_target_bytes=int(seq_grant["target_size_bytes"]),
        source_cap_total_bytes=int(seq_grant["source_cap_total_bytes"]),
    )
    if qualification_request is not None and execution_preflight is not None:
        return call()
    with patch(
        "mediaforce.tuning.av1_validation_v4r4_runner_admission.assert_av1_v4r4_execution_preparation_chain"
    ):
        return call()
