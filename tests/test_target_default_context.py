from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import test_content_intent_observations as boundary_fixtures

from mediaforce.core.db import open_db, open_readonly_db
from mediaforce.tuning.content_intent_observations import (
    _rehash_observation,
    append_content_intent_boundary_observation,
    correct_content_intent_boundary_observation,
    withdraw_content_intent_boundary_observation,
)
from mediaforce.tuning.target_default_context import build_target_default_evidence
from mediaforce.web.runtime.calibration_runtime import _stored_sample_item_payload


def _context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    fixtures = boundary_fixtures.ContentIntentObservationTests()
    item, calibration = fixtures._review_payload()
    observation = fixtures._observation(item, calibration)
    calibration["sample_item"] = _stored_sample_item_payload(deepcopy(item))
    return item, calibration, observation.values()


def _payload(
        db_path: Path,
        item: dict[str, Any],
        calibration: dict[str, Any],
        row: dict[str, Any],
        *,
        observation_id: str | None = None,
) -> dict[str, Any]:
    with open_db(db_path) as connection:
        append_content_intent_boundary_observation(connection, _rehash_observation(row))
        connection.commit()
        return build_target_default_evidence(
            connection,
            budget_item=item,
            calibration=calibration,
            advice_state={
                "content_intent_boundary_observation": {
                    "observation_id": observation_id or row["observation_id"],
                },
            },
        )


def test_current_exact_item_context_exposes_review_only_fallback(tmp_path: Path) -> None:
    item, calibration, row = _context()
    evidence = _payload(tmp_path / "evidence.sqlite3", item, calibration, row)

    assert evidence["status"] == "available"
    assert evidence["reason"] is None
    assert evidence["report"]["mode"] == "review_only"
    assert evidence["report"]["proposed_scope"] is None
    assert evidence["report"]["fallback_reason"] == "no_supported_default_keep_reference_target"


def test_context_read_does_not_mutate_database(tmp_path: Path) -> None:
    item, calibration, row = _context()
    db_path = tmp_path / "readonly.sqlite3"
    with open_db(db_path) as connection:
        append_content_intent_boundary_observation(connection, _rehash_observation(row))
        connection.commit()
    before = db_path.read_bytes()
    with open_readonly_db(db_path) as connection:
        evidence = build_target_default_evidence(
            connection,
            budget_item=item,
            calibration=calibration,
            advice_state={
                "content_intent_boundary_observation": {
                    "observation_id": row["observation_id"],
                },
            },
        )
    assert evidence["status"] == "available"
    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("rel_path", "tv/Futurama/Season 8/Other.mkv", "source_rel_path_mismatch"),
        ("library_item_id", 9, "source_id_mismatch"),
        ("source_fingerprint", "changed-source", "source_fingerprint_mismatch"),
        ("content_version_fingerprint", "changed-content", "content_version_mismatch"),
        (
            "media_fingerprint_decision",
            {"status": "measured", "traits": ["changed"]},
            "content_profile_mismatch",
        ),
        ("resolved_policy", {"video": {"preset": 7}}, "policy_hash_mismatch"),
        (
            "compression_intent",
            {"schema_version": 1, "level": "transparent", "source": "operator", "confirmed": True},
            "intent_mismatch",
        ),
    ],
)
def test_current_binding_drift_is_unavailable(
        tmp_path: Path,
        field: str,
        value: Any,
        reason: str,
) -> None:
    item, calibration, row = _context()
    item[field] = value

    evidence = _payload(tmp_path / f"{field}.sqlite3", item, calibration, row)

    assert evidence == {
        "schema_version": 1,
        "status": "unavailable",
        "reason": reason,
        "report": None,
    }


def test_compatibility_and_stream_ledger_drift_are_unavailable(tmp_path: Path) -> None:
    item, calibration, row = _context()
    calibration["sample_result"]["content_intent_compatibility"]["encoder_runtime_version"] = "changed"
    evidence = _payload(tmp_path / "compatibility.sqlite3", item, calibration, row)
    assert evidence["status"] == "unavailable"
    assert evidence["reason"] == "compatibility_mismatch"

    item, calibration, row = _context()
    item["stream_budget_ledger"] = deepcopy(item["stream_budget_ledger"])
    item["stream_budget_ledger"]["policy_hash"] = "stale"
    evidence = _payload(tmp_path / "ledger.sqlite3", item, calibration, row)
    assert evidence["status"] == "unavailable"
    assert evidence["reason"] == "stream_ledger_missing"


@pytest.mark.parametrize(
    ("field", "reason"),
    [("job_id", "calibration_job_mismatch"), ("review_artifact_fingerprint", "calibration_artifact_mismatch")],
)
def test_newer_calibration_cannot_reuse_an_old_reference(
        tmp_path: Path,
        field: str,
        reason: str,
) -> None:
    item, calibration, row = _context()
    calibration[field] = f"new-{field}"

    evidence = _payload(tmp_path / f"{field}.sqlite3", item, calibration, row)

    assert evidence["status"] == "unavailable"
    assert evidence["reason"] == reason


def test_missing_corrected_and_withdrawn_evidence_is_unavailable(tmp_path: Path) -> None:
    item, calibration, row = _context()
    missing = _payload(
        tmp_path / "missing.sqlite3",
        item,
        calibration,
        row,
        observation_id="missing",
    )
    assert missing["reason"] == "boundary_observation_missing"

    db_path = tmp_path / "superseded.sqlite3"
    with open_db(db_path) as connection:
        append_content_intent_boundary_observation(connection, _rehash_observation(row))
        corrected = correct_content_intent_boundary_observation(
            boundary_fixtures.ContentIntentObservationTests()._observation(item, calibration),
            verdict="unacceptable",
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="corrected_review",
            recorded_at="2026-07-27T21:00:00+00:00",
        )
        append_content_intent_boundary_observation(connection, corrected)
        corrected_evidence = build_target_default_evidence(
            connection,
            budget_item=item,
            calibration=calibration,
            advice_state={"content_intent_boundary_observation": {"observation_id": row["observation_id"]}},
        )
        assert corrected_evidence["status"] == "unavailable"
        assert corrected_evidence["reason"] == "boundary_observation_not_current"

        withdrawn = withdraw_content_intent_boundary_observation(
            corrected,
            reason_code="withdrawn_review",
            recorded_at="2026-07-28T21:00:00+00:00",
        )
        append_content_intent_boundary_observation(connection, withdrawn)
        withdrawn_evidence = build_target_default_evidence(
            connection,
            budget_item=item,
            calibration=calibration,
            advice_state={"content_intent_boundary_observation": {"observation_id": withdrawn.observation_id}},
        )
        assert withdrawn_evidence["status"] == "unavailable"
        assert withdrawn_evidence["reason"] == "boundary_observation_withdrawn"
