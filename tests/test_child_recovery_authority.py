from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from mediaforce.web.runtime import folder_actions


def test_recovery_requires_current_approval_and_matching_manifest() -> None:
    config = Mock()
    calibration = {"accepted_sample_job_id": "sample", "accepted_policy_hash": "policy", "sample_item": {
        "resolved_operator_intent": {"request": {"compression_intent": "balanced"}},
    }}
    contract = folder_actions._production_approval_contract(calibration)
    kwargs = dict(load_calibration_state=lambda *_: calibration,
                  review_gate=lambda _: {"can_confirm_full": True}, load_advice_state=lambda *_: {})
    with patch.object(folder_actions, "production_action_blocker", return_value=None), \
            patch.object(folder_actions, "build_quality_risk_contract", return_value={}), \
            patch.object(folder_actions, "_quality_risk_blocking_reason", return_value=None):
        assert folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
            "selection": {"production_approval_contract": contract},
        }, **kwargs) == contract
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
                "selection": {"production_approval_contract": {**contract, "sample_job_id": "old"}},
            }, **kwargs)
        kwargs["review_gate"] = lambda _: {"can_confirm_full": False}
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
                "selection": {"production_approval_contract": contract},
            }, **kwargs)


@pytest.mark.parametrize("eligible,cleared", [(False, {1}), (True, set())])
def test_recovery_rechecks_policy_and_cadence_without_writes(eligible: bool, cleared: set[int]) -> None:
    decision = SimpleNamespace(item_id=1, eligible=eligible, override_applied=False, hold_reasons=[])
    with patch.object(folder_actions, "project_candidates", return_value=[decision]), \
            patch.object(folder_actions, "cadence_safety_partition", return_value=SimpleNamespace(cleared_item_ids=cleared)) as cadence:
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [{"library_item_id": 1}])
        if eligible:
            assert cadence.call_args.kwargs["synchronize"] is False


def test_recovery_only_reuses_original_in_scope_override() -> None:
    decision = SimpleNamespace(item_id=1, eligible=True, override_applied=True, hold_reasons=[], is_current_season=False)
    item = {"library_item_id": 1, "selection_provenance": {
        "manual_override": True, "override_applied": True, "season_prefix": "tv/Show/Season 1", "is_current_season": False,
    }}
    with patch.object(folder_actions, "project_candidates", return_value=[decision]) as candidates, \
            patch.object(folder_actions, "cadence_safety_partition", return_value=SimpleNamespace(cleared_item_ids={1})):
        evidence = folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert evidence["cadence_cleared"] == [1]
        assert candidates.call_args.kwargs["manual_override_prefixes"] == {"tv/Show/Season 1"}
        item["selection_provenance"]["season_prefix"] = "tv/Other/Season 1"
        folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert candidates.call_args.kwargs["manual_override_prefixes"] == set()


def test_recovery_refuses_new_lifecycle_holds_despite_original_override() -> None:
    decision = SimpleNamespace(item_id=1, eligible=True, override_applied=True,
                               hold_reasons=[SimpleNamespace(code="new_hold")], is_current_season=False)
    item = {"library_item_id": 1, "selection_provenance": {
        "manual_override": True, "override_applied": True, "season_prefix": "tv/Show/Season 1",
        "hold_reasons": [{"code": "old_hold"}], "is_current_season": False,
    }}
    with patch.object(folder_actions, "project_candidates", return_value=[decision]):
        with pytest.raises(HTTPException) as exc:
            folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert "original lifecycle override" in exc.value.detail
