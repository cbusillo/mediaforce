import json
from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from sqlalchemy import delete, select, update

from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.core.db import reset_engine_cache
from mediaforce.core.process_control import ProcessCancelledError
from sqlalchemy.exc import IntegrityError

from test_target_default_context import _context
from mediaforce.cli import _main, build_parser
from mediaforce.core.db import DBClient, open_db, open_readonly_db
from mediaforce.encoding import staging as staging_module
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.core.db_tables import (
    library_items,
    staged_artifacts,
    target_production_outcomes,
)
from mediaforce.tuning.content_intent_observations import (
    _rehash_observation,
    append_content_intent_boundary_observation,
    withdraw_content_intent_boundary_observation,
)
from mediaforce.tuning.production_lineage import (
    OUTPUT_FINGERPRINT_ALGORITHM,
    attach_target_lineage,
    capture_production_identity,
    completed_target_lineage,
    encode_lineage_identity,
    lineage_object,
    production_item_identity,
    seal_lineage,
    validated_target_identity,
)
from mediaforce.tuning.production_outcomes import (
    append_target_production_outcome,
    load_target_production_report,
    prepare_target_production_outcome,
)
from mediaforce.web.runtime.folder_actions import _production_approval_contract


def _admission(
    connection: DBClient,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    item, calibration, row = _context()
    goal = {
        "target_size_bytes": 150_000_000,
        "final_lower_bound_bytes": 142_500_000,
        "final_upper_bound_bytes": 157_500_000,
    }
    item["resolved_operator_intent"] = {
        "size_goal": goal,
        "request": {"size_goal": {"mode": "absolute", "value_bytes": 150_000_000}},
    }
    calibration["sample_item"]["resolved_operator_intent"] = deepcopy(
        item["resolved_operator_intent"]
    )
    calibration.update(
        accepted_at=row["recorded_at"],
        accepted_draft_hash="draft-1",
        accepted_policy_hash="approved-policy",
        accepted_sample_job_id=calibration["job_id"],
    )
    append_content_intent_boundary_observation(connection, _rehash_observation(row))
    manifest = {"run_id": "run-1", "items": [deepcopy(item)]}
    attach_target_lineage(
        connection,
        manifest=manifest,
        calibration=calibration,
        advice_state={
            "content_intent_boundary_observation": {
                "observation_id": row["observation_id"]
            }
        },
        approval_contract=_production_approval_contract(calibration),
    )
    assert "target_lineage" in manifest["items"][0]
    return manifest["items"][0], calibration, row, manifest


def _seed(
    connection: DBClient,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    item, _, row, _ = _admission(connection)
    compat = lineage_object(row["compatibility_json"])
    toolchain = {
        key: compat[key]
        for key in (
            "encoder",
            "encoder_version",
            "encoder_runtime_version",
            "encoder_runtime_signature_id",
            "quality_tool",
            "quality_tool_version",
            "metric_runtime_signature_id",
        )
    }
    toolchain["status"] = "available"
    identity = {
        "status": "available",
        "item_identity": production_item_identity(item),
        "source_fingerprint": row["source_fingerprint"],
        "content_fingerprint": row["content_fingerprint"],
        "toolchain": toolchain,
    }
    context = {
        **compat,
        "metric": row["quality_metric"],
        "target": row["quality_target"],
        "encoder_parameters": ":".join(compat["encoder_parameters"]),
    }
    lineage = completed_target_lineage(
        item=item,
        manifest_run_id="run-1",
        index=0,
        before=identity,
        after=deepcopy(identity),
        context=context,
        chosen_crf=31.0,
    )
    assert lineage is not None
    stage = {
        "library_item_id": 1,
        "manifest_run_id": "run-1",
        "item_index": 0,
        "encode_job_id": "encode-1",
        "encode_started_at": "2026-07-26T21:00:01+00:00",
        "encode_completed_at": "2026-07-26T21:01:00+00:00",
        "staged_at": "2026-07-26T21:01:00+00:00",
        "validated_at": "2026-07-26T21:02:00+00:00",
        "promoted_at": "2026-07-26T21:03:00+00:00",
        "updated_at": "2026-07-26T21:03:00+00:00",
        "staging_path": "/stage/episode.mkv",
        "staging_fingerprint": "stage-stat-fingerprint",
        "staging_size_bytes": 150_000_000,
        "target_lineage_json": lineage,
        "chosen_crf": 31.0,
        "quality_metric": "VMAF",
        "quality_target": 95.0,
        "quality_score": 95.4,
        "encode_command_json": '["ffmpeg", "-crf", "31"]',
    }
    validation = {
        "passed": True,
        "staged_size_bytes": 150_000_000,
        "final_size_goal": {
            "lower_bound_bytes": 142_500_000,
            "upper_bound_bytes": 157_500_000,
            "accepted_under_target": False,
        },
        "target_lineage_identity": {
            "algorithm": OUTPUT_FINGERPRINT_ALGORITHM,
            "content_fingerprint": "output-content",
            "size_bytes": 150_000_000,
            "mtime_ns": 1234,
            "item_identity": production_item_identity(item),
            "lineage_hash": lineage_object(lineage)["payload_sha256"],
            "encode_identity": encode_lineage_identity(stage),
        },
    }
    stage["validation_json"] = json.dumps(validation)
    promoted = {
        "promoted_at": stage["promoted_at"],
        "promoted_path": "/library/episode.mkv",
        "promoted_content_fingerprint": "output-content",
        "promoted_size_bytes": 150_000_000,
        "promoted_mtime_ns": 1234,
    }
    connection.execute(
        library_items.insert().values(
            id=1,
            source_path=promoted["promoted_path"],
            rel_path=item["rel_path"],
            media_root="tv",
            parent_dir="tv/Futurama/Season 8",
            file_name="episode.mkv",
            container="mkv",
            size_bytes=150_000_000,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            attachment_summary_json="[]",
            mtime_ns=1234,
            fingerprint="promoted-stat-fingerprint",
            content_version_fingerprint="output-content",
            content_version_changed_at=stage["promoted_at"],
            status="promoted",
            last_scan_id="scan-1",
            discovered_at=stage["encode_started_at"],
            last_seen_at=stage["promoted_at"],
            updated_at=stage["promoted_at"],
        )
    )
    connection.execute(staged_artifacts.insert().values(**stage))
    return item, row, stage, promoted


def _record(
    connection: DBClient,
    item: dict[str, Any],
    stage: dict[str, Any],
    promoted: dict[str, Any],
) -> None:
    connection.execute(
        update(staged_artifacts)
        .where(staged_artifacts.c.library_item_id == 1)
        .values(**stage)
    )
    append_target_production_outcome(
        connection, prepared=prepare_target_production_outcome(stage, item), **promoted
    )


def test_exact_production_receipt_is_eligible_but_never_adopts(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.db"
    with open_db(db_path) as connection:
        item, row, stage, promoted = _seed(connection)
        _record(connection, item, stage, promoted)
        connection.commit()
    before = db_path.read_bytes()
    with open_readonly_db(db_path) as connection:
        report = load_target_production_report(
            connection, observation_id=row["observation_id"]
        )
        assert report["eligible_outcome_count"] == 1, report
        assert report["adoption_enabled"] is False
        assert report["fingerprint_algorithm"] == OUTPUT_FINGERPRINT_ALGORITHM
        assert "not a full-file digest" in report["identity_limitation"]
    assert db_path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("crf", "production_crf_differs_from_approved_sample"),
        ("under_target", "production_not_strictly_in_band"),
        ("over_target", "production_not_strictly_in_band"),
        ("validation_failed", "production_validation_not_passed"),
        ("quality_floor", "production_quality_floor_not_met"),
        ("output_replaced", "validated_output_identity_mismatch"),
        ("source_changed", "production_source_changed"),
        ("toolchain_changed", "source_or_toolchain_changed_during_encode"),
        ("toolchain_missing", "production_toolchain_unavailable"),
        ("compatibility_changed", "production_compatibility_mismatch"),
        ("timestamp", "production_timestamp_order_invalid"),
    ],
)
def test_invalid_production_is_retained_but_excluded(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, row, stage, promoted = _seed(connection)
        lineage, validation = (
            lineage_object(stage["target_lineage_json"]),
            lineage_object(stage["validation_json"]),
        )
        if mutation == "crf":
            stage["chosen_crf"] = 32
        elif mutation in {"under_target", "over_target"}:
            promoted["promoted_size_bytes"] = (
                140_000_000 if mutation == "under_target" else 160_000_000
            )
            validation["final_size_goal"]["accepted_under_target"] = (
                mutation == "under_target"
            )
        elif mutation == "validation_failed":
            validation["passed"] = False
        elif mutation == "quality_floor":
            stage["quality_score"] = 91
        elif mutation == "output_replaced":
            promoted["promoted_content_fingerprint"] = "other-output"
        elif mutation == "source_changed":
            for name in ("identity_before", "identity_after"):
                lineage[name]["content_fingerprint"] = "changed-source"
        elif mutation == "toolchain_changed":
            lineage["identity_after"]["toolchain"]["encoder_version"] = "changed"
        elif mutation == "toolchain_missing":
            for name in ("identity_before", "identity_after"):
                lineage[name]["toolchain"] = {"status": "unavailable"}
        elif mutation == "compatibility_changed":
            lineage["compatibility"]["encoder_version"] = "changed"
        elif mutation == "timestamp":
            promoted["promoted_at"] = "2026-07-25T00:00:00+00:00"
        lineage = seal_lineage(lineage)
        validation["target_lineage_identity"]["lineage_hash"] = lineage[
            "payload_sha256"
        ]
        stage["target_lineage_json"], stage["validation_json"] = (
            json.dumps(lineage),
            json.dumps(validation),
        )
        _record(connection, item, stage, promoted)
        report = load_target_production_report(
            connection, observation_id=row["observation_id"]
        )
        assert report["eligible_outcome_count"] == 0
        assert report["outcomes"][0]["reason"] == reason


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("withdrawal", "boundary_superseded_or_withdrawn"),
        ("content", "current_output_changed"),
        ("reencode", "production_artifact_replaced"),
        ("lineage_cleared", "production_artifact_changed"),
    ],
)
def test_later_changes_revoke_eligibility_without_rewriting_receipt(
    tmp_path: Path, mutation: str, reason: str
) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, row, stage, promoted = _seed(connection)
        _record(connection, item, stage, promoted)
        before = connection.execute(
            select(target_production_outcomes.c.payload_json)
        ).scalar_one()
        if mutation == "withdrawal":
            append_content_intent_boundary_observation(
                connection,
                withdraw_content_intent_boundary_observation(
                    _rehash_observation(row),
                    reason_code="operator_withdrawal",
                    recorded_at="2026-07-27T00:00:00+00:00",
                ),
            )
        elif mutation == "content":
            connection.execute(
                update(library_items)
                .where(library_items.c.id == 1)
                .values(content_version_fingerprint="changed")
            )
        elif mutation == "reencode":
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == 1)
                .values(encode_job_id="encode-2")
            )
        else:
            connection.execute(
                update(staged_artifacts)
                .where(staged_artifacts.c.library_item_id == 1)
                .values(target_lineage_json=None)
            )
        report = load_target_production_report(
            connection, observation_id=row["observation_id"]
        )
        assert report["outcomes"][0]["reason"] == reason
        assert (
            connection.execute(
                select(target_production_outcomes.c.payload_json)
            ).scalar_one()
            == before
        )


def test_receipts_are_append_only_idempotent_and_transactional(tmp_path: Path) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, row, stage, promoted = _seed(connection)
        _record(connection, item, stage, promoted)
        _record(connection, item, stage, promoted)
        assert len(connection.execute(select(target_production_outcomes)).all()) == 1
        connection.commit()
        for statement in (
            update(target_production_outcomes).values(recorded_at="changed"),
            delete(target_production_outcomes),
        ):
            with pytest.raises(IntegrityError, match="append-only"):
                connection.execute(statement)
            connection.rollback()
        with pytest.raises(ValueError, match="different immutable evidence"):
            append_target_production_outcome(
                connection,
                prepared=prepare_target_production_outcome(stage, item),
                **{**promoted, "promoted_mtime_ns": 999},
            )
        connection.rollback()
        append_target_production_outcome(
            connection,
            prepared=prepare_target_production_outcome(
                {**stage, "encode_job_id": "encode-2"}, item
            ),
            **promoted,
        )
        connection.rollback()
        assert len(connection.execute(select(target_production_outcomes)).all()) == 1


@pytest.mark.parametrize(
    "mutation",
    ["sibling", "duplicate", "unapproved", "changed_policy", "stale_boundary"],
)
def test_admission_never_inherits_or_reuses_invalid_approval(
    tmp_path: Path, mutation: str
) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, calibration, row, manifest = _admission(connection)
        if mutation == "sibling":
            manifest["items"][0]["library_item_id"] = 2
        elif mutation == "duplicate":
            manifest["items"].append(deepcopy(item))
        elif mutation == "unapproved":
            calibration["accepted_sample_job_id"] = "other-job"
        elif mutation == "changed_policy":
            manifest["items"][0]["resolved_policy"]["video"]["preset"] = 6
        else:
            append_content_intent_boundary_observation(
                connection,
                withdraw_content_intent_boundary_observation(
                    _rehash_observation(row),
                    reason_code="operator_withdrawal",
                    recorded_at="2026-07-27T00:00:00+00:00",
                ),
            )
        attach_target_lineage(
            connection,
            manifest=manifest,
            calibration=calibration,
            advice_state={
                "content_intent_boundary_observation": {
                    "observation_id": row["observation_id"]
                }
            },
            approval_contract=_production_approval_contract(calibration),
        )
        assert all("target_lineage" not in candidate for candidate in manifest["items"])


def test_unlinked_production_does_not_probe_or_gain_a_receipt(tmp_path: Path) -> None:
    with patch(
        "mediaforce.tuning.production_lineage.quality_toolchain_identity"
    ) as probe:
        assert (
            capture_production_identity(
                {},
                tmp_path / "missing",
                quality_metric="VMAF",
                host=None,
                process_controller=None,
            )
            is None
        )
        probe.assert_not_called()
    with open_db(tmp_path / "evidence.db") as connection:
        append_target_production_outcome(
            connection,
            prepared=prepare_target_production_outcome({}, {}),
            promoted_at="now",
            promoted_path="path",
            promoted_content_fingerprint="fingerprint",
            promoted_size_bytes=1,
            promoted_mtime_ns=1,
        )
        assert connection.execute(select(target_production_outcomes)).all() == []


def test_validation_identity_is_based_on_current_repaired_bytes(tmp_path: Path) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, _, stage, _ = _seed(connection)
        path = tmp_path / "synthetic-bytes"
        path.write_bytes(b"original test bytes")
        before = validated_target_identity(stage, item, path)
        path.write_bytes(b"repaired fixture")
        after = validated_target_identity(stage, item, path)
        assert before is not None and after is not None
        assert before["content_fingerprint"] != after["content_fingerprint"]
        assert after["size_bytes"] == len(b"repaired fixture")
        assert after["encode_identity"] == encode_lineage_identity(stage)


@pytest.mark.parametrize("fail_receipt", [False, True])
def test_real_promotion_transaction_owns_receipt_and_rollback(
    tmp_path: Path, fail_receipt: bool
) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, _, stage, _ = _seed(connection)
        source = tmp_path / item["rel_path"]
        output = tmp_path / "stage" / "output.mkv"
        source.parent.mkdir(parents=True)
        output.parent.mkdir()
        source.write_bytes(b"original fixture")
        output.write_bytes(b"encoded fixture")
        item.update(source_path=str(source), media_root="tv")
        stage.update(staging_path=str(output), promoted_at=None, promoted_path=None)
        connection.execute(
            update(staged_artifacts)
            .where(staged_artifacts.c.library_item_id == 1)
            .values(**stage)
        )
        connection.execute(
            update(library_items)
            .where(library_items.c.id == 1)
            .values(status="validated", source_path=str(source))
        )
        connection.commit()
        config = SimpleNamespace(
            output_container="mkv",
            archive_root=tmp_path / "archive",
            source_root_map={"tv": tmp_path / "tv"},
        )
        probe = SimpleNamespace(
            duration_seconds=2700,
            video_codec="av1",
            video_bitrate=1000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=0,
            subtitle_track_count=0,
            english_audio_count=0,
            english_subtitle_count=0,
            default_audio_language=None,
            default_subtitle_language=None,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            attachment_summary_json="[]",
        )

        def append_or_fail(*args: Any, **kwargs: Any) -> None:
            append_target_production_outcome(*args, **kwargs)
            if fail_receipt:
                raise RuntimeError("receipt transaction failed")

        with patch.object(
            staging_module,
            "append_target_production_outcome",
            side_effect=append_or_fail,
        ):
            kwargs = dict(
                force=False,
                probe_media=lambda _path: probe,
                file_fingerprint=lambda *_args: "output-stat",
                timestamp=lambda: "2026-07-26T21:03:00+00:00",
                record_event=lambda *_args: None,
                active_reserve_waiting_reason=lambda *_args: None,
                reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                    allowed=True, waiting_reason=None, required_by_volume={}
                ),
            )
            if fail_receipt:
                with pytest.raises(RuntimeError, match="receipt transaction failed"):
                    staging_module.promote_one_item(connection, config, item, **kwargs)
            else:
                staging_module.promote_one_item(connection, config, item, **kwargs)
        receipts = connection.execute(select(target_production_outcomes)).all()
        assert len(receipts) == (0 if fail_receipt else 1)
        if fail_receipt:
            assert source.read_bytes() == b"original fixture"
            assert output.read_bytes() == b"encoded fixture"
            assert (
                connection.execute(
                    select(library_items.c.status).where(library_items.c.id == 1)
                ).scalar_one()
                == "validated"
            )
        else:
            assert source.read_bytes() == b"encoded fixture"
            assert not output.exists()


def test_advisory_probe_cancellation_retains_unavailable_evidence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic-source"
    source.write_bytes(b"source fixture")
    with patch(
        "mediaforce.tuning.production_lineage.quality_toolchain_identity",
        side_effect=ProcessCancelledError("cancelled"),
    ):
        identity = capture_production_identity(
            {"target_lineage": {"schema_version": 1}},
            source,
            quality_metric="VMAF",
            host=None,
            process_controller=None,
        )
    assert identity == {
        "status": "unavailable",
        "reason": "production_identity_unavailable",
    }


def test_malformed_capsule_cannot_interrupt_promotion_preparation(
    tmp_path: Path,
) -> None:
    with open_db(tmp_path / "evidence.db") as connection:
        item, _, stage, _ = _seed(connection)
        lineage = lineage_object(stage["target_lineage_json"])
        del lineage["capsule"]["observation_id"]
        lineage["capsule"] = seal_lineage(lineage["capsule"])
        stage["target_lineage_json"] = json.dumps(seal_lineage(lineage))
        assert prepare_target_production_outcome(stage, item) is None


def test_previous_schema_upgrade_preserves_staging_without_fabricating_lineage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "evidence.db"
    with open_db(db_path) as connection:
        _, _, stage, _ = _seed(connection)
    reset_engine_cache()
    with _alembic_script_location() as location:
        command.downgrade(_alembic_config(db_path, location), "20260822_0021")
    with open_db(db_path) as connection:
        upgraded = connection.execute(select(staged_artifacts)).mappings().one()
        assert upgraded["target_lineage_json"] is None
        assert upgraded["validation_json"] == stage["validation_json"]
        assert upgraded["encode_job_id"] == stage["encode_job_id"]
        assert connection.execute(select(target_production_outcomes)).all() == []


def test_cli_uses_readonly_database_without_runtime_mutations(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.db"
    with open_db(db_path) as connection:
        item, row, stage, promoted = _seed(connection)
        _record(connection, item, stage, promoted)
        connection.commit()
    before = db_path.read_bytes()
    config = SimpleNamespace(paths=SimpleNamespace(db_path=db_path))
    args = build_parser().parse_args(
        ["target-production-evidence", row["observation_id"]]
    )
    output = StringIO()
    with (
        patch(
            "mediaforce.cli._run_locked_command",
            side_effect=AssertionError("runtime mutation"),
        ),
        redirect_stdout(output),
    ):
        assert _main(args, config) == 0  # type: ignore[arg-type]
    report = json.loads(output.getvalue())
    assert report["mode"] == "evidence_only"
    assert report["eligible_outcome_count"] == 1
    assert report["adoption_enabled"] is False
    assert db_path.read_bytes() == before


def test_persisted_manifest_encode_validate_promote_retains_eligible_lineage(
    tmp_path: Path,
) -> None:
    import subprocess

    from mediaforce import execution
    from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
    from mediaforce.core.models import ProbeSummary
    from mediaforce.encoding.quality import QualitySearchResult
    from mediaforce.library.run_manifests import write_manifest
    from mediaforce.tuning.content_intent_observations import (
        content_intent_stream_plan_id,
    )
    from mediaforce.tuning.size_goals import SizeGoalIntent
    from mediaforce.web.runtime.folder_actions import _calibration_policy_hash
    from test_content_intent_observations import ContentIntentObservationTests
    from test_target_size_production import TargetSizeProductionTests

    harness = TargetSizeProductionTests()
    harness.root = tmp_path
    config = harness._config()
    config.raw["media"]["output_container"] = "mkv"
    config.raw["validation"] = {}
    source = harness._source_file("lineage.mkv")
    with source.open("r+b") as handle:
        handle.truncate(20_000_000)
    output = harness._staging_path("lineage.mkv")
    fixtures = ContentIntentObservationTests()
    sample, calibration = fixtures._review_payload()
    with open_db(config.paths.db_path) as connection:
        item_id = harness._insert_item(connection, source)
        item = harness._manifest_item(item_id, source, output)
        item["media_root"] = "tv"
        item.update(
            {
                key: sample[key]
                for key in (
                    "compression_intent",
                    "cadence_summary",
                    "media_fingerprint_decision",
                )
            }
        )
        item["resolved_policy"]["video"].update(
            target_vmaf=95.0,
            min_target_vmaf=92.0,
            default_grain=8,
            compression_intent="perceptual_floor",
            compression_intent_source="operator",
            compression_intent_confirmed=True,
            compression_intent_schema_version=1,
        )
        item["source_fingerprint"] = file_fingerprint(
            source, source.stat(), item["duration_seconds"]
        )
        item["content_version_fingerprint"] = content_version_fingerprint(
            source, source.stat()
        )
        goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=5_000_000,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10,
            final_output_tolerance_percent=5,
            source="test",
        ).resolve(60)
        item["resolved_operator_intent"] = {
            "size_goal": goal.to_payload(),
            "request": {"size_goal": {"mode": "absolute", "value_bytes": 5_000_000}},
        }
        item["stream_budget_ledger"] = execution.resolve_stream_budget_ledger(
            item,
            resolved_size_goal=goal,
            prefer_persisted=False,
        ).to_payload()
        compat = fixtures._compatibility(
            stream_plan_id=content_intent_stream_plan_id(item["stream_budget_ledger"])
        ).to_payload()
        calibration.update(policy=item["resolved_policy"], sample_item=deepcopy(item))
        calibration["sample_result"].update(
            content_intent_compatibility=compat,
            predicted_video_size_bytes=4_800_000,
            predicted_total_size_bytes=5_000_000,
        )
        row = fixtures._observation(item, calibration).values()
        append_content_intent_boundary_observation(connection, _rehash_observation(row))
        calibration.update(
            accepted_at=row["recorded_at"],
            accepted_draft_hash="approved-draft",
            accepted_sample_job_id=calibration["job_id"],
            accepted_policy_hash=_calibration_policy_hash(calibration),
        )
        manifest = {
            "run_id": "integration-run",
            "created_at": row["recorded_at"],
            "items": [item],
        }
        attach_target_lineage(
            connection,
            manifest=manifest,
            calibration=calibration,
            advice_state={
                "content_intent_boundary_observation": {
                    "observation_id": row["observation_id"]
                }
            },
            approval_contract=_production_approval_contract(calibration),
        )
        assert "target_lineage" in item
        path = write_manifest(connection, config, manifest)
        manifest = json.loads(path.read_text())
        assert manifest["items"][0]["target_lineage"] == item["target_lineage"]
        toolchain = {
            key: compat[key]
            for key in (
                "encoder",
                "encoder_version",
                "encoder_runtime_version",
                "encoder_runtime_signature_id",
                "quality_tool",
                "quality_tool_version",
                "metric_runtime_signature_id",
            )
        }
        toolchain["status"] = "available"
        quality = QualitySearchResult(
            crf=31, metric="VMAF", target=95, score=95.4, stdout="fixture"
        )
        probe = ProbeSummary(
            duration_seconds=60,
            video_codec="av1",
            video_bitrate=600_000,
            width=1920,
            height=1080,
            pix_fmt="yuv420p10le",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            default_audio_language="eng",
            default_subtitle_language=None,
            audio_summary_json='[{"language":"eng"}]',
            subtitle_summary_json="[]",
        )
        encode_command = [
            "ffmpeg",
            "-i",
            str(source),
            "-c:v",
            "libsvtav1",
            "-pix_fmt",
            "yuv420p10le",
            "-preset",
            "4",
            "-svtav1-params",
            "tune=0:film-grain=8:film-grain-denoise=0",
            "-crf",
            "31",
            str(output),
        ]

        def fake_encode(
            *, temp_output: Path, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            temp_output.parent.mkdir(parents=True, exist_ok=True)
            with temp_output.open("wb") as handle:
                handle.truncate(5_000_000)
            return subprocess.CompletedProcess(encode_command, 0, "", "")

        with (
            patch(
                "mediaforce.tuning.production_lineage.quality_toolchain_identity",
                return_value=toolchain,
            ) as identity_probe,
            patch("mediaforce.execution._search_quality", return_value=quality),
            patch(
                "mediaforce.execution._select_streams",
                return_value={"audio_tracks": [], "subtitle_tracks": []},
            ),
            patch(
                "mediaforce.execution._build_ffmpeg_command",
                return_value=encode_command,
            ),
            patch("mediaforce.execution._run_encode_command", side_effect=fake_encode),
            patch("mediaforce.execution.probe_media", return_value=probe),
        ):
            execution.encode_manifest_items(
                connection,
                config,
                path,
                manifest,
                [0],
                False,
                encode_context={"encode_job_id": "integration-encode"},
            )
            assert len(identity_probe.call_args_list) == 2
            assert {
                call.kwargs["quality_metric"].casefold()
                for call in identity_probe.call_args_list
            } == {"vmaf"}
            validation = execution.validate_manifest_items(
                connection, config, manifest, [0]
            )
            assert validation[0]["passed"] is True
            execution.promote_manifest_items(connection, config, manifest, [0], False)
        report = load_target_production_report(
            connection, observation_id=row["observation_id"]
        )
        assert report["eligible_outcome_count"] == 1, report
        assert report["outcomes"][0]["encode_identity"]["item_index"] == 0
        assert report["outcomes"][0]["production_crf"] == 31


def test_migration_cannot_drop_recorded_production_history(tmp_path: Path) -> None:
    db_path = tmp_path / "evidence.db"
    with open_db(db_path) as connection:
        item, _, stage, promoted = _seed(connection)
        _record(connection, item, stage, promoted)
    reset_engine_cache()
    with (
        _alembic_script_location() as location,
        pytest.raises(RuntimeError, match="Cannot remove immutable"),
    ):
        command.downgrade(_alembic_config(db_path, location), "20260822_0021")
    with open_readonly_db(db_path) as connection:
        assert len(connection.execute(select(target_production_outcomes)).all()) == 1


def test_actual_toolchain_probe_normalizes_plan_and_result_metric_names() -> None:
    import subprocess
    from mediaforce.encoding.quality import quality_toolchain_identity

    replies = [
        subprocess.CompletedProcess([], 0, "ab-av1 0.11.3", ""),
        subprocess.CompletedProcess([], 0, "ffmpeg version fixture", ""),
        subprocess.CompletedProcess([], 0, "", "SVT-AV1 Encoder Lib v4.2.0"),
        subprocess.CompletedProcess([], 0, "filter help fixture", ""),
    ]
    with patch(
        "mediaforce.encoding.quality._run_quality_command", side_effect=replies * 2
    ):
        plan_identity = quality_toolchain_identity(quality_metric="vmaf")
        result_identity = quality_toolchain_identity(quality_metric="VMAF")
    assert plan_identity["status"] == "available"
    assert plan_identity == result_identity
