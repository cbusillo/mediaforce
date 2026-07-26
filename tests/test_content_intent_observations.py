import copy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import Mock, patch

from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DatabaseError

from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.core.db_tables import content_intent_boundary_observations
from mediaforce.core.evidence import stable_source_id
from mediaforce.tuning.compression_intent import CompressionIntentV1
from mediaforce.tuning.content_intent_observations import (
    ContentIntentBoundaryCompatibilityV1,
    ContentIntentBoundaryObservation,
    ContentIntentObservationConflictError,
    append_content_intent_boundary_observation,
    build_content_intent_boundary_compatibility,
    build_visual_content_intent_observation,
    content_intent_stream_plan_id,
    correct_content_intent_boundary_observation,
    load_current_content_intent_boundary_observations,
    record_visual_content_intent_observation,
    replay_content_intent_personalization,
    summarize_content_intent_boundary,
    withdraw_content_intent_boundary_observation,
)
from mediaforce.tuning.size_goals import SizeGoalIntent
from mediaforce.tuning.stream_budget import resolve_stream_budget_ledger
from mediaforce.web.runtime.calibration_runtime import (
    _content_intent_compatibility_payload,
    _review_artifact_fingerprint,
    _stable_quality_toolchain_identity,
)
from mediaforce.web.runtime.folder_ai_tuning import _persist_quality_risk_feedback
from mediaforce.web.runtime.folder_state import FolderStateDeps, load_calibration_state
from mediaforce.web.runtime.folder_tuning_advice import calibration_draft_hash


class ContentIntentObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_context = open_db(Path(self.temp_dir.name) / "library.sqlite3")
        self.connection = self.db_context.__enter__()

    def tearDown(self) -> None:
        self.db_context.__exit__(None, None, None)
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_visual_approval_is_structured_append_only_and_idempotent(self) -> None:
        sample_item, calibration = self._review_payload()
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=["softness_detail_loss"],
            evidence_ids=["ev1_fingerprint", "ev1_cadence"],
            moment_indexes=[2, 1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )

        self.assertIsNone(result.exclusion_reason)
        assert result.observation is not None
        observation = result.observation
        self.assertEqual(observation.observation_kind, "visual_approval")
        self.assertEqual(observation.verdict, "acceptable")
        self.assertEqual(observation.boundary_kind, "upper_bound")
        self.assertEqual(observation.boundary_size_bytes, 130_000_000)
        self.assertEqual(observation.authoritative_anchor_bytes, 150_000_000)
        self.assertEqual(observation.direction, "smaller")
        self.assertEqual(json.loads(observation.content_traits_json), ["animation_cues", "dark_luma"])
        self.assertTrue(append_content_intent_boundary_observation(self.connection, observation))
        self.assertFalse(append_content_intent_boundary_observation(self.connection, observation))
        row = self.connection.execute(select(content_intent_boundary_observations)).mappings().one()
        self.assertEqual(row["payload_sha256"], observation.payload_sha256)
        self.assertNotIn("operator_note", row["assessment_json"])

    def test_visual_rejection_runtime_records_current_boundary(self) -> None:
        sample_item, calibration = self._review_payload()
        calibration["sample_item"] = copy.deepcopy(sample_item)
        calibration["review_moments"] = [
            {"timestamp_seconds": 90.0, "risk_tags": ["motion_breakup"]}
        ]
        live_sample_item = copy.deepcopy(sample_item)
        live_sample_item.update(
            {
                "library_item_id": 99,
                "rel_path": "tv/Futurama/Season 8/Replacement.mkv",
                "source_fingerprint": "replacement-source",
                "content_version_fingerprint": "replacement-content",
                "duration_seconds": 1_350.0,
            }
        )
        live_sample_item["representative_source_id"] = stable_source_id(live_sample_item)
        config = Mock()
        config.paths.db_path = Path(self.temp_dir.name) / "library.sqlite3"
        deps = Mock()
        deps.now_iso.return_value = "2026-07-26T21:00:00+00:00"
        deps.load_advice_state.return_value = {}
        deps.load_boundary_calibration_state.return_value = calibration

        state = _persist_quality_risk_feedback(
            config,
            deps,
            prefix="tv/Futurama/Season 8",
            note="Motion breakup is visible in the reviewed sample.",
            sample_item=live_sample_item,
            current_policy=calibration["policy"],
            calibration=calibration,
            operator_request={
                "evidence_authority": "rejected_visual_result",
                "quality_risk_tags": ["motion_breakup"],
                "quality_risk_details": "Motion breakup is visible in the reviewed sample.",
            },
            latest_failed_sample_job=None,
        )

        self.assertEqual(state["content_intent_boundary_observation"]["status"], "inserted")
        deps.save_advice_state.assert_called_once()
        row = self.connection.execute(select(content_intent_boundary_observations)).mappings().one()
        self.assertEqual(row["verdict"], "unacceptable")
        self.assertEqual(row["source_event_id"], calibration["job_id"])
        self.assertEqual(row["library_item_id"], sample_item["library_item_id"])
        self.assertEqual(row["content_fingerprint"], sample_item["content_version_fingerprint"])

    def test_same_review_reassessment_appends_a_correction(self) -> None:
        sample_item, calibration = self._review_payload()
        approval = record_visual_content_intent_observation(
            self.connection,
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=["other"],
            evidence_ids=["ev1_fingerprint", "ev1_cadence"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        rejection = record_visual_content_intent_observation(
            self.connection,
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="rejected",
            concern_tags=["motion_breakup"],
            evidence_ids=["ev1_fingerprint", "ev1_cadence"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )

        self.assertEqual(approval.status, "inserted")
        self.assertEqual(rejection.status, "corrected")
        rows = list(self.connection.execute(select(content_intent_boundary_observations)).mappings())
        self.assertEqual(len(rows), 2)
        self.assertGreater(str(rows[1]["recorded_at"]), str(rows[0]["recorded_at"]))
        state = replay_content_intent_personalization(
            rows,
            source_id=str(rows[0]["source_id"]),
            content_id=str(rows[0]["content_id"]),
            prefix=str(rows[0]["prefix"]),
            content_profile_id=str(rows[0]["content_profile_id"]),
            intent_semantic_id=str(rows[0]["intent_semantic_id"]),
            compatibility_key=str(rows[0]["compatibility_key"]),
        )
        self.assertEqual(state.item_boundary.status, "unacceptable_only")

    def test_calibration_freezes_toolchain_compatibility_and_review_artifact(self) -> None:
        sample_item, _ = self._review_payload()
        stream_budget = sample_item["stream_budget_ledger"]
        compatibility = _content_intent_compatibility_payload(
            sample_item=sample_item,
            video_policy=sample_item["resolved_policy"]["video"],
            stream_budget=stream_budget,
            output_dimensions=(1920, 1080),
            effective_preset=4,
            video_filter=None,
            encoder_parameters=["tune=0", "film-grain=8", "film-grain-denoise=0"],
            toolchain_identity={
                "status": "available",
                "encoder": "libsvtav1",
                "encoder_version": "SVT-AV1 Encoder Lib v4.2.0",
                "encoder_runtime_version": "ffmpeg version 8.0",
                "encoder_runtime_signature_id": "erti1_test",
                "quality_tool": "ab-av1",
                "quality_tool_version": "ab-av1 0.11.3",
                "metric_runtime_signature_id": "qmri1_test",
            },
            quality_metric="VMAF",
            quality_target=95.0,
            target_size_trace={"quality_floor": {"minimum": 92.0}},
        )
        self.assertIsNotNone(compatibility)
        assert compatibility is not None
        self.assertEqual(compatibility["encoder_version"], "SVT-AV1 Encoder Lib v4.2.0")
        self.assertEqual(compatibility["encoder_runtime_version"], "ffmpeg version 8.0")
        self.assertEqual(compatibility["stream_plan_id"], content_intent_stream_plan_id(stream_budget))

        first_path = Path(self.temp_dir.name) / "preview-1.mkv"
        second_path = Path(self.temp_dir.name) / "preview-2.mkv"
        first_path.write_bytes(b"first preview")
        second_path.write_bytes(b"second preview")
        clips = [
            SimpleNamespace(
                output_path=first_path,
                timestamp_seconds=30.0,
                duration_seconds=8.0,
            ),
            SimpleNamespace(
                output_path=second_path,
                timestamp_seconds=90.0,
                duration_seconds=8.0,
            ),
        ]
        first_fingerprint = _review_artifact_fingerprint(clips)
        self.assertEqual(first_fingerprint, _review_artifact_fingerprint(clips))
        self.assertEqual(first_fingerprint, _review_artifact_fingerprint(list(reversed(clips))))
        second_path.write_bytes(b"changed preview")
        self.assertNotEqual(first_fingerprint, _review_artifact_fingerprint(clips))

    def test_calibration_load_revalidates_encoded_preview_bytes(self) -> None:
        preview_path = Path(self.temp_dir.name) / "preview.mkv"
        source_path = Path(self.temp_dir.name) / "source.mkv"
        preview_path.write_bytes(b"a" * (1024 * 1024))
        source_path.write_bytes(b"source review bytes")
        preview_clip = SimpleNamespace(
            output_path=preview_path,
            timestamp_seconds=30.0,
            duration_seconds=8.0,
        )
        source_clip = SimpleNamespace(
            output_path=source_path,
            timestamp_seconds=30.0,
            duration_seconds=8.0,
        )
        artifact_fingerprint = _review_artifact_fingerprint([preview_clip], [source_clip])
        assert artifact_fingerprint is not None
        calibration_path = Path(self.temp_dir.name) / "calibration.json"
        calibration_path.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "review_artifact_fingerprint": artifact_fingerprint,
                    "preview_clips": [
                        {
                            "path": str(preview_path),
                            "timestamp_seconds": 30.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                    "source_clips": [
                        {
                            "path": str(source_path),
                            "timestamp_seconds": 30.0,
                            "duration_seconds": 8.0,
                        }
                    ],
                }
            )
        )
        deps = FolderStateDeps(
            review_file_from_url=lambda _config, value: Path(value),
            load_advice_state=lambda *_args: None,
            calibration_draft_hash=lambda _payload: "draft",
            tuning_policy_focus=lambda *_args: {},
            pending_proposal_trace_public_view=lambda *_args: {},
        )

        loaded = load_calibration_state(
            deps,
            Mock(),
            "tv/show",
            calibration_path,
            verify_boundary_artifact=True,
        )
        assert loaded is not None
        self.assertTrue(loaded["boundary_review_media_ready"])
        self.assertEqual(loaded["current_review_artifact_fingerprint"], artifact_fingerprint)

        with preview_path.open("r+b") as handle:
            handle.seek(200_000)
            handle.write(b"changed")
        changed = load_calibration_state(
            deps,
            Mock(),
            "tv/show",
            calibration_path,
            verify_boundary_artifact=True,
        )
        assert changed is not None
        self.assertFalse(changed["boundary_review_media_ready"])
        self.assertNotEqual(changed["current_review_artifact_fingerprint"], artifact_fingerprint)

    def test_boundary_verification_fields_do_not_change_the_reviewed_draft(self) -> None:
        _, calibration = self._review_payload()
        baseline = calibration_draft_hash(calibration)
        verified = {
            **calibration,
            "boundary_review_media_ready": False,
            "current_review_artifact_fingerprint": "cira1_rechecked",
        }

        self.assertEqual(calibration_draft_hash(verified), baseline)

    def test_toolchain_change_during_measurement_excludes_compatibility(self) -> None:
        before = {
            "schema_version": 1,
            "status": "available",
            "encoder": "libsvtav1",
            "encoder_version": "SVT-AV1 Encoder Lib v4.2.0",
            "encoder_runtime_version": "ffmpeg version 8.1.2",
            "encoder_runtime_signature_id": "erti1_before",
            "quality_tool": "ab-av1",
            "quality_tool_version": "ab-av1 0.11.3",
            "metric_runtime_signature_id": "qmri1_before",
        }
        after = {**before, "encoder_runtime_signature_id": "erti1_after"}

        changed = _stable_quality_toolchain_identity(before, after)

        self.assertEqual(changed["status"], "unavailable")
        self.assertEqual(changed["reason"], "toolchain_changed_during_measurement")
        stable = _stable_quality_toolchain_identity(before, {**before, "metric_runtime_signature_id": "qmri1_after"})
        self.assertEqual(stable["metric_runtime_signature_id"], "qmri1_after")

    def test_same_review_identity_with_different_payload_conflicts(self) -> None:
        sample_item, calibration = self._review_payload()
        first = self._observation(sample_item, calibration, concern_tags=["softness_detail_loss"])
        conflicting = self._observation(sample_item, calibration, concern_tags=["motion_breakup"])

        self.assertEqual(first.series_id, conflicting.series_id)
        self.assertNotEqual(first.observation_id, conflicting.observation_id)
        self.assertTrue(append_content_intent_boundary_observation(self.connection, first))
        with self.assertRaises(ContentIntentObservationConflictError):
            append_content_intent_boundary_observation(self.connection, conflicting)

    def test_unconfirmed_and_incomplete_evidence_is_not_persisted(self) -> None:
        sample_item, calibration = self._review_payload()
        sample_item["compression_intent"] = CompressionIntentV1(
            level="legacy_unconfirmed",
            source="legacy",
            confirmed=False,
        ).to_payload()
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(result.exclusion_reason, "compression_intent_unconfirmed")

        sample_item, calibration = self._review_payload()
        sample_item.pop("content_version_fingerprint")
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(result.exclusion_reason, "content_version_fingerprint_missing")
        self.assertEqual(
            self.connection.scalar(select(content_intent_boundary_observations.c.observation_id)),
            None,
        )

    def test_source_and_policy_identity_must_match_frozen_evidence(self) -> None:
        sample_item, calibration = self._review_payload()
        sample_item["representative_source_id"] = "src1_wrong"
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="rejected",
            concern_tags=["motion_breakup"],
            evidence_ids=["ev1_fingerprint"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(result.exclusion_reason, "source_identity_mismatch")

        sample_item, calibration = self._review_payload()
        sample_item["stream_budget_ledger"]["policy_hash"] = "sha256:stale"
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="rejected",
            concern_tags=["motion_breakup"],
            evidence_ids=["ev1_fingerprint"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(result.exclusion_reason, "stream_budget_identity_invalid")

    def test_corrections_are_linear_and_time_travel_resolves_prior_revision(self) -> None:
        sample_item, calibration = self._review_payload()
        original = self._observation(sample_item, calibration)
        correction = correct_content_intent_boundary_observation(
            original,
            verdict="unacceptable",
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="operator_corrected_review",
            recorded_at="2026-07-26T21:05:00+00:00",
        )
        self.assertTrue(append_content_intent_boundary_observation(self.connection, original))
        self.assertTrue(append_content_intent_boundary_observation(self.connection, correction))

        current = load_current_content_intent_boundary_observations(self.connection)
        prior = load_current_content_intent_boundary_observations(
            self.connection,
            recorded_before="2026-07-26T21:05:00+00:00",
        )
        self.assertEqual(current[0]["observation_id"], correction.observation_id)
        self.assertEqual(current[0]["verdict"], "unacceptable")
        self.assertEqual(prior[0]["observation_id"], original.observation_id)

        fork = correct_content_intent_boundary_observation(
            original,
            verdict="acceptable",
            personalization_eligible=False,
            exclusion_reason="review_invalidated",
            reason_code="second_correction",
            recorded_at="2026-07-26T21:06:00+00:00",
        )
        with self.assertRaises(ContentIntentObservationConflictError):
            append_content_intent_boundary_observation(self.connection, fork)

        with self.assertRaisesRegex(ValueError, "timestamps must advance"):
            correct_content_intent_boundary_observation(
                correction,
                verdict="acceptable",
                personalization_eligible=True,
                exclusion_reason=None,
                reason_code="non_monotonic_correction",
                recorded_at=correction.recorded_at,
            )

    def test_replay_collapses_superseded_and_withdrawn_revisions(self) -> None:
        sample_item, calibration = self._review_payload()
        original = self._observation(sample_item, calibration)
        correction = correct_content_intent_boundary_observation(
            original,
            verdict="unacceptable",
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="operator_corrected_review",
            recorded_at="2026-07-26T21:05:00+00:00",
        )
        corrected_state = replay_content_intent_personalization(
            [original.values(), correction.values()],
            source_id=original.source_id,
            content_id=original.content_id,
            prefix=original.prefix,
            content_profile_id=original.content_profile_id,
            intent_semantic_id=original.intent_semantic_id,
            compatibility_key=original.compatibility_key,
        )
        self.assertEqual(corrected_state.item_boundary.status, "unacceptable_only")

        withdrawn = withdraw_content_intent_boundary_observation(
            original,
            reason_code="operator_withdrew_review",
            recorded_at="2026-07-26T21:06:00+00:00",
        )
        withdrawn_state = replay_content_intent_personalization(
            [original.values(), withdrawn.values()],
            source_id=original.source_id,
            content_id=original.content_id,
            prefix=original.prefix,
            content_profile_id=original.content_profile_id,
            intent_semantic_id=original.intent_semantic_id,
            compatibility_key=original.compatibility_key,
        )
        self.assertEqual(withdrawn_state.item_boundary.status, "empty")

    def test_withdrawal_removes_current_evidence_without_deleting_history(self) -> None:
        sample_item, calibration = self._review_payload()
        original = self._observation(sample_item, calibration)
        withdrawn = withdraw_content_intent_boundary_observation(
            original,
            reason_code="operator_withdrew_review",
            recorded_at="2026-07-26T21:05:00+00:00",
        )
        append_content_intent_boundary_observation(self.connection, original)
        append_content_intent_boundary_observation(self.connection, withdrawn)

        self.assertEqual(load_current_content_intent_boundary_observations(self.connection), [])
        current = load_current_content_intent_boundary_observations(
            self.connection,
            include_withdrawn=True,
        )
        self.assertEqual(current[0]["disposition"], "withdrawn")
        self.assertEqual(
            self.connection.scalar(select(func.count()).select_from(content_intent_boundary_observations)),
            2,
        )

    def test_database_rejects_updates_and_deletes(self) -> None:
        sample_item, calibration = self._review_payload()
        observation = self._observation(sample_item, calibration)
        append_content_intent_boundary_observation(self.connection, observation)
        self.connection.commit()

        with self.assertRaises(DatabaseError):
            self.connection.execute(
                text(
                    "UPDATE content_intent_boundary_observations "
                    "SET measured_quality_score = 99 WHERE observation_id = :id"
                ),
                {"id": observation.observation_id},
            )
        self.connection.rollback()
        with self.assertRaises(DatabaseError):
            self.connection.execute(
                text("DELETE FROM content_intent_boundary_observations WHERE observation_id = :id"),
                {"id": observation.observation_id},
            )

    def test_database_rejects_correction_measurement_mutation(self) -> None:
        sample_item, calibration = self._review_payload()
        original = self._observation(sample_item, calibration)
        correction = correct_content_intent_boundary_observation(
            original,
            verdict="unacceptable",
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="operator_corrected_review",
            recorded_at="2026-07-26T21:05:00+00:00",
        )
        append_content_intent_boundary_observation(self.connection, original)
        self.connection.commit()
        mutated_values = correction.values()
        mutated_values["boundary_size_bytes"] = correction.boundary_size_bytes + 1

        with self.assertRaises(DatabaseError):
            self.connection.execute(
                content_intent_boundary_observations.insert().values(**mutated_values)
            )
        self.connection.rollback()

        mutated_values = correction.values()
        assessment = json.loads(str(mutated_values["assessment_json"]))
        assessment["chosen_crf"] = 37.0
        mutated_values["assessment_json"] = json.dumps(
            assessment,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.assertRaises(DatabaseError):
            self.connection.execute(
                content_intent_boundary_observations.insert().values(**mutated_values)
            )

    def test_boundary_summary_detects_one_sided_bounded_and_conflicting_evidence(self) -> None:
        sample_item, calibration = self._review_payload(boundary_size_bytes=130_000_000)
        approval = self._observation(sample_item, calibration)
        summary = summarize_content_intent_boundary([approval.values()])
        self.assertEqual(summary.status, "acceptable_only")
        self.assertEqual(summary.acceptable_upper_bound_bytes, 130_000_000)

        rejected_item, rejected_calibration = self._review_payload(
            job_id="sample-rejected",
            artifact_fingerprint="cira1_rejected",
            boundary_size_bytes=110_000_000,
        )
        rejection = self._observation(rejected_item, rejected_calibration, verdict="rejected")
        summary = summarize_content_intent_boundary([approval.values(), rejection.values()])
        self.assertEqual(summary.status, "bounded")
        self.assertEqual(summary.unacceptable_lower_bound_bytes, 110_000_000)

        conflicting_item, conflicting_calibration = self._review_payload(
            job_id="sample-conflict",
            artifact_fingerprint="cira1_conflict",
            boundary_size_bytes=150_000_000,
        )
        conflict = self._observation(conflicting_item, conflicting_calibration, verdict="rejected")
        summary = summarize_content_intent_boundary([approval.values(), conflict.values()])
        self.assertEqual(summary.status, "conflicting")

    def test_replay_keeps_item_rejections_non_actionable_for_broader_scopes(self) -> None:
        first_item, first_calibration = self._review_payload(boundary_size_bytes=130_000_000)
        first = self._observation(first_item, first_calibration)
        second_item, second_calibration = self._review_payload(
            library_item_id=2,
            rel_path="tv/Futurama/Season 8/Episode 02.mkv",
            content_fingerprint="content-episode-02",
            job_id="sample-2",
            artifact_fingerprint="cira1_sample_2",
            boundary_size_bytes=150_000_000,
        )
        second = self._observation(second_item, second_calibration, verdict="rejected")
        observations = [first.values(), second.values()]
        state = replay_content_intent_personalization(
            observations,
            source_id=first.source_id,
            content_id=first.content_id,
            prefix=first.prefix,
            content_profile_id=first.content_profile_id,
            intent_semantic_id=first.intent_semantic_id,
            compatibility_key=first.compatibility_key,
        )

        self.assertEqual(state.item_boundary.status, "acceptable_only")
        self.assertTrue(state.model_compatibility_id.startswith("cimc1_"))
        self.assertTrue(state.cohorts[0].actionable)
        self.assertEqual(state.cohorts[0].unacceptable_count, 0)
        self.assertFalse(state.cohorts[1].actionable)
        self.assertEqual(state.cohorts[1].unacceptable_count, 1)

    def test_replay_requires_independent_sources_before_broader_personalization(self) -> None:
        observations: list[dict[str, object]] = []
        first = None
        for index in range(1, 4):
            sample_item, calibration = self._review_payload(
                library_item_id=index,
                rel_path=f"tv/Futurama/Season 8/Episode {index:02d}.mkv",
                content_fingerprint=f"content-episode-{index:02d}",
                job_id=f"sample-{index}",
                artifact_fingerprint=f"cira1_sample_{index}",
                boundary_size_bytes=129_000_000 + index * 1_000_000,
            )
            observation = self._observation(sample_item, calibration)
            first = first or observation
            observations.append(observation.values())
        assert first is not None

        state = replay_content_intent_personalization(
            observations,
            source_id=first.source_id,
            content_id=first.content_id,
            prefix=first.prefix,
            content_profile_id=first.content_profile_id,
            intent_semantic_id=first.intent_semantic_id,
            compatibility_key=first.compatibility_key,
        )

        self.assertEqual(state.cohorts[1].source_count, 3)
        self.assertEqual(state.cohorts[1].confidence, "moderate")
        self.assertTrue(state.cohorts[1].actionable)
        self.assertTrue(state.cohorts[2].actionable)
        self.assertTrue(state.cohorts[3].actionable)

    def test_replay_does_not_mix_distinct_content_feature_profiles(self) -> None:
        first_item, first_calibration = self._review_payload()
        first = self._observation(first_item, first_calibration)
        second_item, second_calibration = self._review_payload(
            library_item_id=2,
            rel_path="tv/Futurama/Season 8/Episode 02.mkv",
            content_fingerprint="content-episode-02",
            job_id="sample-2",
            artifact_fingerprint="cira1_sample_2",
        )
        second_item["media_fingerprint_decision"]["traits"] = ["high_motion", "high_texture"]
        second = self._observation(second_item, second_calibration, verdict="rejected")

        state = replay_content_intent_personalization(
            [first.values(), second.values()],
            source_id=first.source_id,
            content_id=first.content_id,
            prefix=first.prefix,
            content_profile_id=first.content_profile_id,
            intent_semantic_id=first.intent_semantic_id,
            compatibility_key=first.compatibility_key,
        )

        self.assertEqual(state.cohorts[1].observation_count, 1)
        self.assertEqual(state.cohorts[1].unacceptable_count, 0)

    def test_exact_item_boundary_survives_content_profile_reclassification(self) -> None:
        sample_item, calibration = self._review_payload()
        observation = self._observation(sample_item, calibration)
        reclassified_item, reclassified_calibration = self._review_payload(
            job_id="sample-reclassified",
            artifact_fingerprint="cira1_reclassified",
        )
        reclassified_item["media_fingerprint_decision"]["traits"] = ["high_motion"]
        reclassified = self._observation(reclassified_item, reclassified_calibration)

        state = replay_content_intent_personalization(
            [observation.values()],
            source_id=observation.source_id,
            content_id=observation.content_id,
            prefix=observation.prefix,
            content_profile_id=reclassified.content_profile_id,
            intent_semantic_id=observation.intent_semantic_id,
            compatibility_key=observation.compatibility_key,
        )

        self.assertEqual(state.item_boundary.status, "acceptable_only")
        self.assertEqual(state.cohorts[1].observation_count, 0)

    def test_repeated_item_evidence_does_not_unlock_broader_cohorts(self) -> None:
        observations: list[dict[str, object]] = []
        first = None
        for index in range(1, 5):
            sample_item, calibration = self._review_payload(
                job_id=f"sample-{index}",
                artifact_fingerprint=f"cira1_sample_{index}",
                boundary_size_bytes=129_000_000 + index * 1_000_000,
            )
            observation = self._observation(sample_item, calibration)
            first = first or observation
            observations.append(observation.values())
        assert first is not None

        state = replay_content_intent_personalization(
            observations,
            source_id=first.source_id,
            content_id=first.content_id,
            prefix=first.prefix,
            content_profile_id=first.content_profile_id,
            intent_semantic_id=first.intent_semantic_id,
            compatibility_key=first.compatibility_key,
        )

        self.assertEqual(state.cohorts[1].source_count, 1)
        self.assertFalse(state.cohorts[1].actionable)
        self.assertFalse(state.cohorts[2].actionable)
        self.assertFalse(state.cohorts[3].actionable)

    def test_replay_ignores_rows_with_invalid_payload_hashes(self) -> None:
        sample_item, calibration = self._review_payload()
        observation = self._observation(sample_item, calibration)
        tampered = observation.values()
        tampered["payload_sha256"] = "0" * 64

        state = replay_content_intent_personalization(
            [tampered],
            source_id=observation.source_id,
            content_id=observation.content_id,
            prefix=observation.prefix,
            content_profile_id=observation.content_profile_id,
            intent_semantic_id=observation.intent_semantic_id,
            compatibility_key=observation.compatibility_key,
        )

        self.assertEqual(state.item_boundary.status, "empty")

    def test_stale_stream_budget_and_compatibility_are_mechanically_excluded(self) -> None:
        sample_item, calibration = self._review_payload()
        sample_item["source_size_bytes"] = 900_000_000
        stale_budget = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(stale_budget.exclusion_reason, "stream_budget_identity_invalid")

        sample_item, calibration = self._review_payload()
        calibration["sample_result"]["quality_target"] = 94.0
        stale_compatibility = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(stale_compatibility.exclusion_reason, "compatibility_quality_mismatch")

        sample_item, calibration = self._review_payload()
        calibration["current_review_artifact_fingerprint"] = "cira1_changed"
        stale_artifact = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(stale_artifact.exclusion_reason, "review_artifact_stale")

    def test_audio_only_rejection_is_not_an_av1_boundary(self) -> None:
        sample_item, calibration = self._review_payload()
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="rejected",
            concern_tags=["audio_quality_layout"],
            evidence_ids=["ev1_fingerprint"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )

        self.assertEqual(result.exclusion_reason, "visual_rejection_reason_missing")

    def test_concurrent_same_review_decisions_serialize_without_forking(self) -> None:
        db_path = Path(self.temp_dir.name) / "concurrent.sqlite3"
        with open_db(db_path):
            pass
        sample_item, calibration = self._review_payload()
        barrier = threading.Barrier(2)

        def record(verdict: Literal["approved", "rejected"]) -> str:
            barrier.wait()
            with open_db(db_path) as connection:
                result = record_visual_content_intent_observation(
                    connection,
                    prefix="tv/Futurama/Season 8",
                    sample_item=sample_item,
                    calibration=calibration,
                    verdict=verdict,
                    concern_tags=["motion_breakup"] if verdict == "rejected" else ["other"],
                    evidence_ids=["ev1_fingerprint", "ev1_cadence"],
                    moment_indexes=[1],
                    recorded_at="2026-07-26T21:00:00+00:00",
                )
            return result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = set(executor.map(record, ("approved", "rejected")))

        self.assertIn("inserted", statuses)
        self.assertTrue(statuses.intersection({"corrected", "existing"}))
        with open_db(db_path) as connection:
            rows = list(connection.execute(select(content_intent_boundary_observations)).mappings())
        self.assertLessEqual(len(rows), 2)
        self.assertEqual(len({int(row["revision"]) for row in rows}), len(rows))

    def test_concurrent_corrections_reload_the_winning_revision(self) -> None:
        db_path = Path(self.temp_dir.name) / "concurrent-corrections.sqlite3"
        sample_item, calibration = self._review_payload()
        with open_db(db_path) as connection:
            first = record_visual_content_intent_observation(
                connection,
                prefix="tv/Futurama/Season 8",
                sample_item=sample_item,
                calibration=calibration,
                verdict="approved",
                concern_tags=["other"],
                evidence_ids=["ev1_fingerprint", "ev1_cadence"],
                moment_indexes=[1],
                recorded_at="2026-07-26T21:00:00+00:00",
            )
        self.assertEqual(first.status, "inserted")
        barrier = threading.Barrier(2)

        def correct(payload: tuple[Literal["approved", "rejected"], str]) -> str:
            verdict, concern = payload
            barrier.wait()
            with open_db(db_path) as connection:
                result = record_visual_content_intent_observation(
                    connection,
                    prefix="tv/Futurama/Season 8",
                    sample_item=sample_item,
                    calibration=calibration,
                    verdict=verdict,
                    concern_tags=[concern],
                    evidence_ids=["ev1_fingerprint", "ev1_cadence"],
                    moment_indexes=[1],
                    recorded_at="2026-07-26T21:00:00+00:00",
                )
            return result.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(
                executor.map(
                    correct,
                    (
                        ("rejected", "motion_breakup"),
                        ("approved", "softness_detail_loss"),
                    ),
                )
            )

        self.assertEqual(statuses, ["corrected", "corrected"])
        with open_db(db_path) as connection:
            rows = list(
                connection.execute(
                    select(content_intent_boundary_observations)
                    .order_by(content_intent_boundary_observations.c.revision)
                ).mappings()
            )
        self.assertEqual([int(row["revision"]) for row in rows], [0, 1, 2])
        self.assertEqual(len({str(row["supersedes_observation_id"]) for row in rows[1:]}), 2)

    def test_correction_rebases_after_a_second_append_conflict(self) -> None:
        sample_item, calibration = self._review_payload()
        original = self._observation(sample_item, calibration)
        append_content_intent_boundary_observation(self.connection, original)
        rival = correct_content_intent_boundary_observation(
            original,
            verdict="acceptable",
            personalization_eligible=True,
            exclusion_reason=None,
            reason_code="concurrent_operator_reassessment",
            recorded_at="2026-07-26T21:00:01+00:00",
            concern_tags=["softness_detail_loss"],
            moment_indexes=[1],
        )
        append_attempts = 0

        def racing_append(connection: DBClient, observation: ContentIntentBoundaryObservation) -> bool:
            nonlocal append_attempts
            append_attempts += 1
            if append_attempts == 2:
                append_content_intent_boundary_observation(self.connection, rival)
                raise ContentIntentObservationConflictError("simulated concurrent correction")
            return append_content_intent_boundary_observation(connection, observation)

        with patch(
            "mediaforce.tuning.content_intent_observations.append_content_intent_boundary_observation",
            side_effect=racing_append,
        ):
            result = record_visual_content_intent_observation(
                self.connection,
                prefix="tv/Futurama/Season 8",
                sample_item=sample_item,
                calibration=calibration,
                verdict="rejected",
                concern_tags=["motion_breakup"],
                evidence_ids=["ev1_fingerprint", "ev1_cadence"],
                moment_indexes=[1],
                recorded_at="2026-07-26T21:00:00+00:00",
            )

        self.assertEqual(result.status, "corrected")
        rows = list(
            self.connection.execute(
                select(content_intent_boundary_observations)
                .order_by(content_intent_boundary_observations.c.revision)
            ).mappings()
        )
        self.assertEqual([int(row["revision"]) for row in rows], [0, 1, 2])
        self.assertEqual(rows[-1]["verdict"], "unacceptable")
        self.assertEqual(rows[-1]["recorded_at"], "2026-07-26T21:00:02+00:00")

    def test_stopped_or_unreviewable_calibration_cannot_become_boundary_evidence(self) -> None:
        sample_item, calibration = self._review_payload()
        calibration["review_media_ready"] = False
        stopped = build_visual_content_intent_observation(
            prefix="tv/Big Brother/Season 27",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(stopped.exclusion_reason, "review_media_unavailable")

        sample_item, calibration = self._review_payload()
        calibration["mode"] = "full"
        unsupported = build_visual_content_intent_observation(
            prefix="tv/Big Brother/Season 27",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(unsupported.exclusion_reason, "unsupported_calibration_mode")

    def test_content_traits_remain_multi_label_and_unknown_is_excluded(self) -> None:
        expected_traits = [
            "animation_cues",
            "dark_luma",
            "high_motion",
            "high_texture",
            "likely_film_grain",
            "uncertain_noise_mix",
        ]
        sample_item, calibration = self._review_payload()
        sample_item["media_fingerprint_decision"]["traits"] = list(reversed(expected_traits))
        observation = self._observation(sample_item, calibration)
        self.assertEqual(json.loads(observation.content_traits_json), expected_traits)

        sample_item, calibration = self._review_payload()
        sample_item["media_fingerprint_decision"] = {
            "status": "unknown",
            "traits": ["unknown"],
            "evidence_id": "ev1_unknown",
        }
        unknown = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict="approved",
            concern_tags=[],
            evidence_ids=[],
            moment_indexes=[],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertEqual(unknown.exclusion_reason, "content_fingerprint_unmeasured")

    def test_compatibility_is_versioned_and_excludes_size_and_crf(self) -> None:
        first = self._compatibility()
        second = self._compatibility()
        changed_runtime = self._compatibility(encoder_runtime_version="ffmpeg version changed")
        changed_encoder_build = self._compatibility(encoder_runtime_signature_id="erti1_changed")
        changed_metric_runtime = self._compatibility(metric_runtime_signature_id="qmri1_changed")
        self.assertEqual(first.compatibility_key, second.compatibility_key)
        self.assertNotEqual(first.compatibility_key, changed_runtime.compatibility_key)
        self.assertNotEqual(first.compatibility_key, changed_encoder_build.compatibility_key)
        self.assertNotEqual(first.compatibility_key, changed_metric_runtime.compatibility_key)
        payload = first.to_payload()
        self.assertNotIn("target_size_bytes", payload)
        self.assertNotIn("chosen_crf", payload)

    def test_stream_plan_compatibility_excludes_source_measurements(self) -> None:
        first = {
            "stream_plan": {
                "schema_version": 1,
                "plan_id": "sp1_first",
                "source_id": "src1_first",
                "source_fingerprint": "fingerprint-first",
                "policy_hash": "sha256:first",
                "output_container": "mkv",
                "attachments_known": True,
                "copy_unknown_attachments": False,
                "streams": [
                    {
                        "kind": "audio",
                        "source_index": 1,
                        "source_codec": "eac3",
                        "action": "transcode",
                        "output_codec": "opus",
                        "codec_argument": "libopus",
                        "output_bitrate_bps": 256_000,
                        "output_bitrate_text": "256k",
                        "channels": 6,
                        "language": "eng",
                        "default": True,
                        "forced": False,
                        "source_bitrate_bps": 640_000,
                        "source_duration_seconds": 2_700.0,
                        "source_size_bytes": 216_000_000,
                        "file_name": "episode-one.eac3",
                        "mime_type": "audio/eac3",
                    }
                ],
            }
        }
        second = copy.deepcopy(first)
        second_plan = second["stream_plan"]
        second_plan.update(
            {
                "plan_id": "sp1_second",
                "source_id": "src1_second",
                "source_fingerprint": "fingerprint-second",
                "policy_hash": "sha256:second",
            }
        )
        second_stream = second_plan["streams"][0]
        second_stream.update(
            {
                "source_index": 8,
                "source_bitrate_bps": 768_000,
                "source_duration_seconds": 5_400.0,
                "source_size_bytes": 518_400_000,
                "file_name": "episode-two.eac3",
            }
        )

        self.assertEqual(
            content_intent_stream_plan_id(first),
            content_intent_stream_plan_id(second),
        )
        second_stream["output_bitrate_bps"] = 320_000
        second_stream["output_bitrate_text"] = "320k"
        self.assertNotEqual(
            content_intent_stream_plan_id(first),
            content_intent_stream_plan_id(second),
        )

    def test_migration_downgrade_drops_empty_table_and_refuses_observation_loss(self) -> None:
        empty_path = Path(self.temp_dir.name) / "empty-downgrade.sqlite3"
        with open_db(empty_path):
            pass
        reset_engine_cache()
        with _alembic_script_location() as script_location:
            empty_config = _alembic_config(empty_path, script_location)
            command.downgrade(empty_config, "20260724_0018")
            with sqlite3.connect(empty_path) as raw_connection:
                table = raw_connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = 'content_intent_boundary_observations'"
                ).fetchone()
            self.assertIsNone(table)
            command.upgrade(empty_config, "head")

        populated_path = Path(self.temp_dir.name) / "populated-downgrade.sqlite3"
        sample_item, calibration = self._review_payload()
        observation = self._observation(sample_item, calibration)
        with open_db(populated_path) as connection:
            append_content_intent_boundary_observation(connection, observation)
        reset_engine_cache()
        with _alembic_script_location() as script_location:
            populated_config = _alembic_config(populated_path, script_location)
            with self.assertRaisesRegex(RuntimeError, "Cannot remove immutable"):
                command.downgrade(populated_config, "20260724_0018")

    def _observation(
            self,
            sample_item: dict[str, object],
            calibration: dict[str, object],
            *,
            verdict: Literal["approved", "rejected"] = "approved",
            concern_tags: list[str] | None = None,
    ) -> ContentIntentBoundaryObservation:
        result = build_visual_content_intent_observation(
            prefix="tv/Futurama/Season 8",
            sample_item=sample_item,
            calibration=calibration,
            verdict=verdict,
            concern_tags=concern_tags or ["softness_detail_loss"],
            evidence_ids=["ev1_cadence", "ev1_fingerprint"],
            moment_indexes=[1],
            recorded_at="2026-07-26T21:00:00+00:00",
        )
        self.assertIsNone(result.exclusion_reason)
        assert result.observation is not None
        return result.observation

    def _review_payload(
            self,
            *,
            library_item_id: int = 1,
            rel_path: str = "tv/Futurama/Season 8/Episode 01.mkv",
            content_fingerprint: str = "content-episode-01",
            job_id: str = "sample-1",
            artifact_fingerprint: str = "cira1_sample_1",
            boundary_size_bytes: int = 130_000_000,
    ) -> tuple[dict[str, object], dict[str, object]]:
        intent = CompressionIntentV1(level="perceptual_floor", source="operator", confirmed=True)
        policy: dict[str, object] = {
            "video": {
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "preset": 4,
                "default_grain": 8,
                "grain_denoise": 0,
                "quality_metric": "vmaf",
                "target_vmaf": 95.0,
                "min_target_vmaf": 92.0,
                "compression_intent_schema_version": 1,
                "compression_intent": "perceptual_floor",
                "compression_intent_source": "operator",
                "compression_intent_confirmed": True,
            },
            "audio": {"mode": "copy"},
            "subtitle": {"mode": "copy"},
        }
        sample_item: dict[str, object] = {
            "library_item_id": library_item_id,
            "rel_path": rel_path,
            "source_fingerprint": f"source-{library_item_id}",
            "content_version_fingerprint": content_fingerprint,
            "source_size_bytes": 800_000_000,
            "video_bitrate": 2_000_000,
            "duration_seconds": 2_700.0,
            "output_container": "mkv",
            "resolved_policy": policy,
            "compression_intent": intent.to_payload(),
            "cadence_summary": {
                "probe": {"average_frame_rate": "24000/1001"},
            },
            "cadence_decision": {
                "classification": "progressive",
                "transform": "none",
                "evidence_id": "ev1_cadence",
            },
            "media_fingerprint_decision": {
                "status": "measured",
                "confidence": 0.94,
                "traits": ["animation_cues", "dark_luma"],
                "evidence_id": "ev1_fingerprint",
            },
            "audio_summary": [],
            "subtitle_summary": [],
            "attachment_summary": [],
        }
        sample_item["representative_source_id"] = stable_source_id(sample_item)
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=150_000_000,
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10.0,
            final_output_tolerance_percent=5.0,
            source="test",
        ).resolve(2_700.0)
        stream_budget = resolve_stream_budget_ledger(
            sample_item,
            resolved_size_goal=size_goal,
            prefer_persisted=False,
        ).to_payload()
        sample_item["stream_budget_ledger"] = stream_budget
        stream_plan_id = content_intent_stream_plan_id(stream_budget)
        assert stream_plan_id is not None
        compatibility = self._compatibility(stream_plan_id=stream_plan_id)
        calibration: dict[str, object] = {
            "job_id": job_id,
            "mode": "sample",
            "action": "ai_tune",
            "review_media_ready": True,
            "boundary_review_media_ready": True,
            "policy": policy,
            "review_artifact_fingerprint": artifact_fingerprint,
            "current_review_artifact_fingerprint": artifact_fingerprint,
            "sample_result": {
                "chosen_crf": 31.0,
                "quality_metric": "VMAF",
                "quality_target": 95.0,
                "quality_score": 95.4,
                "predicted_video_size_bytes": boundary_size_bytes - 8_000_000,
                "predicted_total_size_bytes": boundary_size_bytes,
                "sampled_clip_bytes": 12_000_000,
                "review_artifact_size_bytes": 18_000_000,
                "target_size_trace": {
                    "quality_floor": {"minimum": 92.0},
                },
                "content_intent_compatibility": compatibility.to_payload(),
            },
        }
        return sample_item, calibration

    def _compatibility(
            self,
            *,
            stream_plan_id: str = "cisp1_stream",
            encoder_runtime_version: str = "ffmpeg version 8.0",
            encoder_runtime_signature_id: str = "erti1_test",
            metric_runtime_signature_id: str = "qmri1_test",
    ) -> ContentIntentBoundaryCompatibilityV1:
        return build_content_intent_boundary_compatibility(
            encoder="libsvtav1",
            encoder_version="SVT-AV1 Encoder Lib v4.2.0",
            encoder_runtime_version=encoder_runtime_version,
            encoder_runtime_signature_id=encoder_runtime_signature_id,
            quality_tool="ab-av1",
            quality_tool_version="ab-av1 0.11.3",
            metric_runtime_signature_id=metric_runtime_signature_id,
            preset=4,
            pixel_format="yuv420p10le",
            encoder_parameters=["tune=0", "film-grain=8", "film-grain-denoise=0"],
            output_width=1920,
            output_height=1080,
            frame_rate="24000/1001",
            cadence_transform="none",
            video_filter=None,
            output_container="mkv",
            stream_plan_id=stream_plan_id,
            measurement_basis="sample_projection",
            quality_metric="VMAF",
            quality_target=95.0,
            minimum_quality_score=92.0,
        )
