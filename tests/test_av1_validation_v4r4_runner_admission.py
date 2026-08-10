from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

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
    _private_canonical_bytes,
    _sequencing_claim,
    _sequencing_grant,
)
from tests.test_av1_validation_v4r4_ordinal_registry import _publish_plan


def test_runner_admission_binds_all_public_chain_and_policy_values(tmp_path: Path) -> None:
    binding, plan, clock = _publish_plan(tmp_path)
    seq_grant = _sequencing_grant(binding, plan, clock)
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)

    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim)

    assert_av1_v4r4_runner_admission(admission)
    assert_av1_v4r4_runner_admission_chain(
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
    binding, plan, clock = _publish_plan(tmp_path)
    seq_grant = _sequencing_grant(binding, plan, clock)
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim)

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
    binding, plan, clock = _publish_plan(tmp_path)
    seq_grant = _sequencing_grant(binding, plan, clock)
    seq_claim = _sequencing_claim(binding, plan, seq_grant, clock)
    exec_grant = _execution_grant(plan, seq_grant)
    exec_claim = _execution_claim(plan, seq_claim, exec_grant)
    admission = _admission(plan, seq_grant, seq_claim, exec_grant, exec_claim)
    forged_claim = {**exec_claim, "execution_claim_id": "av1v4r4execclaim_" + "0" * 32}

    with pytest.raises(AV1V4R4RunnerAdmissionError, match="chain binding"):
        assert_av1_v4r4_runner_admission_chain(
            admission=admission,
            plan=plan,
            sequencing_grant=seq_grant,
            sequencing_claim=seq_claim,
            execution_grant=exec_grant,
            execution_claim=forged_claim,
        )


def _admission(
    plan: Mapping[str, Any],
    seq_grant: Mapping[str, Any],
    seq_claim: Mapping[str, Any],
    exec_grant: Mapping[str, Any],
    exec_claim: Mapping[str, Any],
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
    return build_av1_v4r4_runner_admission(
        plan=plan,
        sequencing_grant=seq_grant,
        sequencing_claim=seq_claim,
        execution_grant=exec_grant,
        execution_claim=exec_claim,
        invocation_sha256=av1_validation_v4_qualification_search_invocation_sha256(
            source_path=Path("/tmp/mediaforce-v4r4-source.mkv"),
            video_policy=video_policy,
            mode=mode,
            warm_start=warm_start,
            extra_search_kwargs=search_kwargs,
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
