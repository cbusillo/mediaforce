import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient, open_db, open_readonly_db
from mediaforce.core.db_tables import (
    content_intent_boundary_observations,
    library_items,
    staged_artifacts,
)
from mediaforce.tuning.content_intent_backfill import backfill_content_intent_approvals
from mediaforce.tuning.content_intent_observations import record_visual_content_intent_observation
import test_content_intent_observations as boundary_fixtures

ACCEPTED_AT = "2026-07-26T21:00:00+00:00"
PREFIX = "tv/Futurama/Season 8"


def _approval_files(
        web_dir: Path,
        slug: str,
        *,
        library_item_id: int = 1,
        job_id: str = "sample-1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Write the calibration and advice files that an approval leaves behind."""
    sample_item, calibration = boundary_fixtures.ContentIntentObservationTests()._review_payload(
        library_item_id=library_item_id,
        rel_path=f"{PREFIX}/Episode {library_item_id:02d}.mkv",
        content_fingerprint=f"content-episode-{library_item_id:02d}",
        job_id=job_id,
    )
    calibration.update(
        sample_item=sample_item,
        accepted_at=ACCEPTED_AT,
        accepted_sample_job_id=job_id,
        accepted_draft_hash="draft-1",
        draft_hash="draft-1",
    )
    advice = {
        "operator_approved_at": ACCEPTED_AT,
        "content_intent_boundary_observation": {
            "status": "excluded",
            "observation_id": None,
            "exclusion_reason": "sample_measurement_missing",
        },
        "quality_risk_records": [
            {
                "kind": "post_test",
                "verdict": "approved",
                "tags": ["other"],
                "evidence_ids": ["ev1_fingerprint"],
                "moment_indexes": [1],
                "sample_job_id": job_id,
                "prefix": PREFIX,
                "created_at": ACCEPTED_AT,
            }
        ],
    }
    _write(web_dir, slug, calibration, advice)
    return calibration, advice


def _write(web_dir: Path, slug: str, calibration: dict[str, Any], advice: dict[str, Any]) -> None:
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / f"{slug}.json").write_text(json.dumps(calibration))
    (web_dir / f"{slug}.advice.json").write_text(json.dumps(advice))


def _rows(connection: DBClient) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(select(content_intent_boundary_observations)).mappings()]


def _publish_without_lineage(connection: DBClient, library_item_id: int) -> None:
    connection.execute(
        library_items.insert().values(
            id=library_item_id,
            source_path=f"/library/{library_item_id}.mkv",
            rel_path=f"{PREFIX}/Episode {library_item_id:02d}.mkv",
            media_root="tv",
            parent_dir=PREFIX,
            file_name=f"{library_item_id}.mkv",
            container="mkv",
            size_bytes=1,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            attachment_summary_json="[]",
            mtime_ns=1,
            fingerprint="fingerprint",
            status="promoted",
            last_scan_id="scan-1",
            discovered_at=ACCEPTED_AT,
            last_seen_at=ACCEPTED_AT,
            updated_at=ACCEPTED_AT,
        )
    )
    connection.execute(
        staged_artifacts.insert().values(
            library_item_id=library_item_id,
            staging_path="/stage/episode.mkv",
            promoted_at=ACCEPTED_AT,
            updated_at=ACCEPTED_AT,
        )
    )


def test_backfill_records_saved_approvals_and_skips_with_reasons(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    _approval_files(web_dir, "recordable", library_item_id=1, job_id="sample-1")
    replaced, replaced_advice = _approval_files(web_dir, "replaced", library_item_id=2, job_id="sample-2")
    replaced["job_id"] = "newer-sample"
    _write(web_dir, "replaced", replaced, replaced_advice)
    unreviewable, unreviewable_advice = _approval_files(web_dir, "no-review-media", library_item_id=3, job_id="sample-3")
    unreviewable["boundary_review_media_ready"] = False
    _write(web_dir, "no-review-media", unreviewable, unreviewable_advice)
    legacy, legacy_advice = _approval_files(web_dir, "legacy", library_item_id=4, job_id="sample-4")
    legacy_advice["quality_risk_records"] = []
    _write(web_dir, "legacy", legacy, legacy_advice)
    unapproved, unapproved_advice = _approval_files(web_dir, "unapproved", library_item_id=5, job_id="sample-5")
    del unapproved_advice["operator_approved_at"]
    _write(web_dir, "unapproved", unapproved, unapproved_advice)
    db_path = tmp_path / "library.sqlite3"
    with open_db(db_path) as connection:
        _publish_without_lineage(connection, 1)

    with open_readonly_db(db_path) as connection:
        dry_run = backfill_content_intent_approvals(connection, web_dir, apply=False).to_payload()
        assert _rows(connection) == []
    assert dry_run["approvals_seen"] == 4
    assert dry_run["boundary_observations"] == {
        "recorded": 0,
        "would_record": 1,
        "already_recorded": 0,
        "skipped": {
            "approval_record_missing": 1,
            "calibration_replaced_since_approval": 1,
            "encoded_review_media_unavailable": 1,
        },
    }
    assert dry_run["production_outcomes"] == {
        "recorded": 0,
        "skipped": {"encode_identity_not_captured": 1},
    }

    with open_db(db_path) as connection:
        applied = backfill_content_intent_approvals(connection, web_dir, apply=True).to_payload()
    assert applied["boundary_observations"]["recorded"] == 1
    assert applied["boundary_observations"]["skipped"] == dry_run["boundary_observations"]["skipped"]
    with open_readonly_db(db_path) as connection:
        rows = _rows(connection)
    assert len(rows) == 1
    assert rows[0]["library_item_id"] == 1
    assert rows[0]["verdict"] == "acceptable"
    assert rows[0]["recorded_at"] == ACCEPTED_AT
    assert rows[0]["sampled_clip_bytes"] is None
    assert json.loads(rows[0]["provenance_json"])["recorded_by"] == "approval_file_backfill"
    recorded_entry = next(entry for entry in applied["entries"] if entry["status"] == "recorded")
    assert recorded_entry["observation_id"] == rows[0]["observation_id"]


def test_backfill_is_idempotent_and_respects_native_rows(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    _approval_files(web_dir, "backfilled", library_item_id=1, job_id="sample-1")
    native, _ = _approval_files(web_dir, "native", library_item_id=2, job_id="sample-2")
    db_path = tmp_path / "library.sqlite3"
    with open_db(db_path) as connection:
        native_result = record_visual_content_intent_observation(
            connection,
            prefix=PREFIX,
            sample_item=deepcopy(native["sample_item"]),
            calibration=native,
            verdict="approved",
            concern_tags=["other"],
            evidence_ids=["ev1_fingerprint"],
            moment_indexes=[1],
            recorded_at=ACCEPTED_AT,
        )
        assert native_result.status == "inserted"
        first = backfill_content_intent_approvals(connection, web_dir, apply=True).to_payload()
    with open_db(db_path) as connection:
        second = backfill_content_intent_approvals(connection, web_dir, apply=True).to_payload()
        rows = _rows(connection)

    assert first["boundary_observations"]["recorded"] == 1
    assert first["boundary_observations"]["already_recorded"] == 1
    assert second["boundary_observations"]["recorded"] == 0
    assert second["boundary_observations"]["already_recorded"] == 2
    assert len(rows) == 2
    native_entry = next(entry for entry in second["entries"] if entry["advice_file"] == "native.advice.json")
    assert native_entry["observation_id"] == native_result.observation_id
    assert {entry["observation_id"] for entry in second["entries"]} == {row["observation_id"] for row in rows}


def test_backfill_reports_the_current_revision_of_a_corrected_series(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    calibration, _ = _approval_files(web_dir, "corrected", library_item_id=1, job_id="sample-1")
    db_path = tmp_path / "library.sqlite3"
    with open_db(db_path) as connection:
        for verdict, tags in (("approved", ["other"]), ("rejected", ["motion_breakup"])):
            latest = record_visual_content_intent_observation(
                connection,
                prefix=PREFIX,
                sample_item=deepcopy(calibration["sample_item"]),
                calibration=calibration,
                verdict=verdict,
                concern_tags=tags,
                evidence_ids=["ev1_fingerprint"],
                moment_indexes=[1],
                recorded_at=ACCEPTED_AT,
            )
        assert latest.status == "corrected"

    with open_readonly_db(db_path) as connection:
        report = backfill_content_intent_approvals(connection, web_dir, apply=False).to_payload()

    assert report["boundary_observations"]["already_recorded"] == 1
    assert report["entries"][0]["observation_id"] == latest.observation_id
