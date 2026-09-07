import argparse
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch

import pytest
import test_content_intent_observations as boundary_fixtures

from mediaforce.cli import _main, build_parser
from mediaforce.core.db import open_db
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.compression_intent import CompressionIntentLevel
from mediaforce.tuning.content_intent_observations import (
    _rehash_observation,
    append_content_intent_boundary_observation,
    correct_content_intent_boundary_observation,
    withdraw_content_intent_boundary_observation,
)
from mediaforce.tuning.target_defaults import target_default_report


def boundary(
        source: int = 1,
        *,
        sample: int = 1,
        size: int = 130_000_000,
        duration: float = 2700,
        folder: str = "tv/Futurama/Season 8",
        verdict: Literal["approved", "rejected"] = "approved",
        traits: list[str] | None = None,
        intent_level: CompressionIntentLevel = "perceptual_floor",
        exact_scope: bool = False,
) -> dict[str, Any]:
    fixtures = boundary_fixtures.ContentIntentObservationTests()
    item, calibration = fixtures._review_payload(
        library_item_id=source,
        rel_path=f"{folder}/Episode {source}.mkv",
        content_fingerprint=f"content-{source}",
        job_id=f"job-{source}-{sample}",
        artifact_fingerprint=f"artifact-{source}-{sample}",
        boundary_size_bytes=size,
        intent_level=intent_level,
    )
    if traits is not None:
        item["media_fingerprint_decision"] = {
            **object_dict(item["media_fingerprint_decision"]), "traits": traits,
        }
    observation = fixtures._observation(
        item, calibration, verdict=verdict,
        prefix=str(item["rel_path"]) if exact_scope else folder,
    )
    values = observation.values()
    values["duration_seconds"] = duration
    return _rehash_observation(values).values()


def test_one_approval_stays_an_observation_without_a_default() -> None:
    row = boundary()
    report = target_default_report([row], observation_id=row["observation_id"])
    assert report.proposed_scope is None
    assert report.reference_target_bytes == 150_000_000
    assert report.scopes[0].reason == "insufficient_independent_approved_artifacts"
    assert report.scopes[1].reason == "insufficient_independent_approved_sources"
    assert report.to_payload()["mode"] == "review_only"


def test_repeated_item_approvals_use_total_bytes_and_never_crf() -> None:
    rows = [boundary(sample=1, size=130_000_000), boundary(sample=2, size=140_000_000)]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.proposed_scope == "item"
    assert report.proposed_bytes_per_45_minutes == 130_000_000
    for row in rows:
        assessment = json.loads(row["assessment_json"])
        assessment["chosen_crf"] = 10
        row["assessment_json"] = json.dumps(assessment)
    changed = [_rehash_observation(row).values() for row in rows]
    assert target_default_report(changed, observation_id=changed[0]["observation_id"]).proposed_bytes_per_45_minutes == 130_000_000


def test_repeated_same_artifact_does_not_unlock_an_item_default() -> None:
    rows = [boundary(sample=index) for index in range(3)]
    for row in rows:
        row["artifact_fingerprint"] = "one-reviewed-artifact"
    rows = [_rehash_observation(row).values() for row in rows]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.proposed_scope is None
    assert report.scopes[0].approved_artifact_count == 1


def test_folder_default_normalizes_duration_and_weights_sources_equally() -> None:
    rows = [
        boundary(1, size=65_000_000, duration=1350),
        boundary(2, size=132_000_000),
        boundary(3, size=268_000_000, duration=5400),
    ]
    # Many reviews of one source cannot pull the conservative proposal down.
    rows.extend(boundary(1, sample=index, size=65_000_000, duration=1350) for index in range(2, 10))
    report = target_default_report(rows, observation_id=rows[1]["observation_id"])
    assert report.proposed_scope == "folder"
    assert report.proposed_bytes_per_45_minutes == 134_000_000
    assert report.scopes[1].approved_source_count == 3


def test_rejections_normalize_before_conflict_detection() -> None:
    rows = [boundary(sample=1), boundary(sample=2), boundary(sample=3, size=70_000_000, duration=1350, verdict="rejected")]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.proposed_scope is None
    assert report.fallback_reason == "item_boundary_conflict"


def test_folder_cannot_override_a_known_item_rejection() -> None:
    rows = [boundary(1, size=150_000_000, verdict="rejected")]
    rows.extend(boundary(source) for source in range(2, 5))
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.proposed_scope is None
    assert report.scopes[1].reason == "conflicting_boundaries"


def test_unstable_sources_do_not_create_a_folder_default() -> None:
    rows = [boundary(source, size=size) for source, size in enumerate((100_000_000, 130_000_000, 200_000_000), 1)]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.scopes[1].reason == "unstable_boundary_dispersion"
    assert report.proposed_scope is None


def test_an_unusual_item_approval_needs_repeated_stable_boundaries() -> None:
    rows = [boundary(sample=1, size=60_000_000), boundary(sample=2, size=130_000_000)]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.scopes[0].reason == "unstable_boundary_dispersion"
    assert report.proposed_scope is None


def test_approval_below_quality_floor_cannot_supply_a_default() -> None:
    rows = [boundary(sample=1), boundary(sample=2)]
    rows[1]["quality_floor_met"] = False
    rows[1] = _rehash_observation(rows[1]).values()
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.scopes[0].approved_artifact_count == 1
    assert report.proposed_scope is None


def test_proposal_rounds_up_and_snapshot_is_order_independent() -> None:
    rows = [boundary(sample=1, duration=2701), boundary(sample=2, duration=2701)]
    forward = target_default_report(rows, observation_id=rows[0]["observation_id"])
    backward = target_default_report(list(reversed(rows)), observation_id=rows[0]["observation_id"])
    assert forward.to_payload() == backward.to_payload()
    assert forward.proposed_bytes_per_45_minutes == 129_951_870


def test_content_class_requires_cross_folder_approvals_and_rejections() -> None:
    reference = boundary()
    rows = [reference] + [boundary(source, folder=f"tv/Show {source % 3}") for source in range(2, 10)]
    report = target_default_report(rows, observation_id=reference["observation_id"])
    assert report.scopes[2].reason == "insufficient_rejected_sources"
    rows.extend(boundary(source, sample=2, size=100_000_000, folder=f"tv/Show {source % 3}", verdict="rejected") for source in range(2, 5))
    report = target_default_report(rows, observation_id=reference["observation_id"])
    assert report.proposed_scope == "content_class"
    assert report.scopes[2].confidence == "high"
    assert report.scopes[2].approved_source_count == 8
    assert report.scopes[2].rejected_source_count == 3


def test_exact_item_review_prefixes_do_not_count_as_independent_folders() -> None:
    reference = boundary(exact_scope=True)
    rows = [reference] + [boundary(source, folder="tv/Other/Season 1", exact_scope=True) for source in range(2, 10)]
    rows.extend(boundary(source, sample=2, size=100_000_000, folder="tv/Other/Season 1", verdict="rejected", exact_scope=True) for source in range(2, 5))
    report = target_default_report(rows, observation_id=reference["observation_id"])
    assert report.folder_prefix == "tv/Futurama/Season 8"
    assert report.scopes[2].approved_folder_count == 1
    assert report.scopes[2].reason == "insufficient_cross_folder_evidence"
    assert report.proposed_scope is None


def test_exact_item_reviews_from_same_folder_can_supply_folder_evidence() -> None:
    rows = [boundary(source, exact_scope=True) for source in (1, 2, 3)]
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.proposed_scope == "folder"
    assert report.scopes[1].approved_source_count == 3


@pytest.mark.parametrize("variant", ["traits", "intent", "ineligible", "toolchain"])
def test_incompatible_or_ineligible_rows_cannot_unlock_defaults(variant: str) -> None:
    reference = boundary()
    rows = [reference]
    for source in (2, 3):
        row = boundary(
            source,
            traits=["grain_noise"] if variant == "traits" else None,
            intent_level="balanced" if variant == "intent" else "perceptual_floor",
        )
        if variant == "ineligible":
            row["personalization_eligible"] = False
            row["exclusion_reason"] = "excluded"
        if variant == "toolchain":
            compatibility = json.loads(row["compatibility_json"])
            compatibility["encoder_runtime_signature_id"] = "other-runtime"
            row["compatibility_json"] = json.dumps(compatibility)
        rows.append(_rehash_observation(row).values())
    report = target_default_report(rows, observation_id=reference["observation_id"])
    assert report.proposed_scope is None


def test_tampered_evidence_cannot_unlock_default() -> None:
    rows = [boundary(source) for source in (1, 2, 3)]
    rows[1]["boundary_size_bytes"] += 1
    report = target_default_report(rows, observation_id=rows[0]["observation_id"])
    assert report.scopes[1].approved_source_count == 2
    assert report.proposed_scope is None


def test_correction_and_withdrawal_replace_original_votes() -> None:
    first, second = boundary(sample=1), boundary(sample=2)
    correction = correct_content_intent_boundary_observation(
        _rehash_observation(second), verdict="unacceptable",
        personalization_eligible=True, exclusion_reason=None,
        reason_code="visual_artifacts", recorded_at="2026-07-27T21:00:00+00:00",
    )
    rows = [first, second, correction.values()]
    report = target_default_report(rows, observation_id=first["observation_id"])
    assert report.fallback_reason == "item_boundary_conflict"
    withdrawn = withdraw_content_intent_boundary_observation(
        correction, reason_code="wrong_review", recorded_at="2026-07-28T21:00:00+00:00",
    )
    report = target_default_report([*rows, withdrawn.values()], observation_id=first["observation_id"])
    assert report.scopes[0].approved_artifact_count == 1
    assert report.scopes[0].rejected_source_count == 0
    with pytest.raises(ValueError, match="current eligible"):
        target_default_report(rows, observation_id=second["observation_id"])


def test_cli_report_uses_readonly_database_without_runtime_mutations(tmp_path: Path) -> None:
    row = boundary()
    db_path = tmp_path / "evidence.sqlite3"
    with open_db(db_path) as connection:
        append_content_intent_boundary_observation(connection, _rehash_observation(row))
        connection.commit()
    before = db_path.read_bytes()
    config = SimpleNamespace(paths=SimpleNamespace(db_path=db_path))
    output = StringIO()
    args = build_parser().parse_args(["target-defaults", row["observation_id"]])
    with patch("mediaforce.cli._run_locked_command", side_effect=AssertionError("runtime mutation")), redirect_stdout(output):
        assert _main(args, config) == 0  # type: ignore[arg-type]
    assert json.loads(output.getvalue())["mode"] == "review_only"
    assert db_path.read_bytes() == before
    output = StringIO()
    with redirect_stdout(output):
        assert _main(argparse.Namespace(command="target-defaults", observation_id="missing"), config) == 2  # type: ignore[arg-type]
    assert "error" in json.loads(output.getvalue())
