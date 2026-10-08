import hashlib
import json
import tempfile
import unittest
from collections.abc import Collection
from typing import Any
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import calibration_jobs, encode_jobs, item_events, library_items, run_manifests, staged_artifacts
from mediaforce.library.staged_integrity import (
    MAX_DETAIL_PAGE_SIZE,
    CheckedStagedOutputUnavailable,
    checked_staged_output,
    integrity_disposition_blocks_promotion,
    staged_integrity_report,
)
from mediaforce.encoding.staging import FAR_BELOW_PREDICTION_CHECK, FINAL_SIZE_GOAL_CHECK
from mediaforce.execution import PromotionResult
from mediaforce.web.runtime.folder_actions import promote_folder_outputs_action
from mediaforce.web.runtime import folder_actions, size_held
from mediaforce.web.runtime.size_held import decide_size_held_file, staged_remake_records


class StagedIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = self._config()

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_classifier_reports_every_disposition_without_mutating_rows(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            promotable = self._insert_item(connection, "tv/Show/Season 1/Promotable.mkv", status="validated")
            self._insert_item(connection, "tv/Show/Season 1/Tracked.mkv", status="promoted")
            unvalidated = self._insert_item(connection, "tv/Show/Season 1/Unvalidated.mkv", status="encoded")
            validation_failed = self._insert_item(connection, "tv/Show/Season 1/Failed.mkv", status="encoded")
            missing = self._insert_item(connection, "tv/Show/Season 1/Missing.mkv", status="encoded")
            drifted = self._insert_item(connection, "tv/Show/Season 1/Drifted.mkv", status="validated")
            remote = self._insert_item(connection, "tv/Show/Season 1/Remote.mkv", status="validated")
            self._insert_item(connection, "tv/Show/Season 1/NotStarted.mkv", status="planned")

            promotable_stage = self._write_stage("tv/Show/Season 1/Promotable.mkv", b"promotable")
            unvalidated_stage = self._write_stage("tv/Show/Season 1/Unvalidated.mkv", b"unvalidated")
            failed_stage = self._write_stage("tv/Show/Season 1/Failed.mkv", b"failed")
            drifted_stage = self._write_stage("tv/Show/Season 1/Drifted.mkv", b"new-content")
            remote_stage = self.root / "remote-staging" / "tv/Show/Season 1/Remote.mkv"
            self._insert_artifact(connection, promotable, promotable_stage, passed=True)
            self._insert_artifact(connection, unvalidated, unvalidated_stage)
            self._insert_artifact(connection, validation_failed, failed_stage, passed=False)
            self._insert_artifact(connection, missing, self.root / "staging/tv/Show/Season 1/Missing.mkv")
            self._insert_artifact(connection, drifted, drifted_stage, passed=True, size_bytes=1, mtime_ns=1)
            self._insert_artifact(
                connection,
                remote,
                remote_stage,
                passed=True,
                encode_host_key="remote-a",
                encode_media_access="stream",
            )
            before = connection.execute(
                staged_artifacts.select().order_by(staged_artifacts.c.library_item_id)
            ).mappings().all()

            self._write_stage("tv/Show/Season 1/Orphan.mkv", b"orphan")
            self._write_stage("tv/Show/Season 1/Partial.partial.mkv", b"partial")
            self._write_stage("tv/Show/Season 1/.retained-480ee39f9a56-20260914.mkv", b"retained")
            self._write_stage("tv/Show/Season 1/.hidden-working.mkv", b"hidden")
            report = staged_integrity_report(
                connection,
                self.config,
                "tv/Show/Season 1",
                discover=True,
            )
            after = connection.execute(
                staged_artifacts.select().order_by(staged_artifacts.c.library_item_id)
            ).mappings().all()

        self.assertEqual(before, after)
        self.assertEqual(report.counts["promotable"], 1)
        self.assertEqual(report.counts["tracked"], 1)
        self.assertEqual(report.counts["unvalidated"], 1)
        self.assertEqual(report.counts["validation_failed"], 1)
        self.assertEqual(report.counts["missing"], 1)
        self.assertEqual(report.counts["drifted"], 1)
        self.assertEqual(report.counts["remote_only_or_unreachable"], 1)
        self.assertEqual(report.counts["not_started"], 1)
        self.assertEqual(report.counts["orphaned"], 1)
        self.assertEqual(report.counts["partial_or_temporary"], 2)
        self.assertEqual(report.counts["retained"], 1)
        self.assertFalse(integrity_disposition_blocks_promotion("retained"))
        self.assertTrue(integrity_disposition_blocks_promotion("partial_or_temporary"))
        self.assertFalse(report.discovery_truncated)

    @staticmethod
    def _held_validation(*, other_failure: str | None = None) -> str:
        checks = [{"passed": False, "message": FAR_BELOW_PREDICTION_CHECK}]
        if other_failure:
            checks.append({"passed": False, "message": other_failure})
        return json.dumps({
            "passed": False,
            "checks": checks,
            "size_prediction": {
                "predicted_bytes": 100, "source": "sample", "actual_bytes": 50, "ratio": 0.5,
                "threshold": 0.7, "owner_kept_at": None, "held": True,
            },
        })

    def _held_file(self, connection: DBClient, name: str, **artifact: object) -> tuple[int, Path]:
        rel_path = f"tv/Show/Season 1/{name}"
        item_id = self._insert_item(connection, rel_path, status="encoded")
        stage = self._write_stage(rel_path, b"small")
        self._insert_artifact(connection, item_id, stage, **artifact)
        connection.execute(
            staged_artifacts.update()
            .where(staged_artifacts.c.library_item_id == item_id)
            .values(validation_json=self._held_validation(), validated_at=datetime.now(tz=UTC).isoformat())
        )
        return item_id, stage

    def test_a_file_held_only_for_its_size_gets_its_own_state_with_both_sizes(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._held_file(connection, "Held.mkv")
            other = self._insert_item(connection, "tv/Show/Season 1/AlsoBroken.mkv", status="encoded")
            self._insert_artifact(connection, other, self._write_stage("tv/Show/Season 1/AlsoBroken.mkv", b"x"))
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == other)
                .values(validation_json=self._held_validation(other_failure="staged duration closely matches the source"))
            )
            report = staged_integrity_report(connection, self.config, "tv/Show/Season 1", discover=False)

        records = {record.rel_path: record for record in report.records}
        held = records["tv/Show/Season 1/Held.mkv"]
        self.assertEqual(held.disposition, "size_held")
        self.assertEqual(held.to_payload()["size_prediction"]["predicted_bytes"], 100)
        self.assertEqual(records["tv/Show/Season 1/AlsoBroken.mkv"].disposition, "validation_failed")
        self.assertTrue(integrity_disposition_blocks_promotion("size_held"))

    def test_keeping_a_held_file_records_the_owner_and_checks_only_that_file(self) -> None:
        checked: list[tuple[str, list[int]]] = []
        with open_db(self.config.paths.db_path) as connection:
            item_id, _stage = self._held_file(connection, "Keep.mkv")

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=True, now_iso=lambda: "2026-09-30T12:00:00+00:00",
            validate_items=lambda prefix, ids: checked.append((prefix, list(ids))) or {"ok": True, "validated_count": 1},
            queue_items=lambda *_args: self.fail("keeping must not queue anything"),
        )
        with open_db(self.config.paths.db_path) as connection:
            stored = json.loads(str(connection.execute(
                select(staged_artifacts.c.validation_json).where(staged_artifacts.c.library_item_id == item_id)
            ).scalar_one()))

        self.assertTrue(result["ok"], result)
        self.assertIn("can be replaced now", result["message"])
        self.assertEqual(checked, [("tv/Show/Season 1", [item_id])])
        self.assertEqual(stored["size_prediction"]["owner_kept_at"], "2026-09-30T12:00:00+00:00")
        self.assertFalse(stored["size_prediction"]["held"])

    def test_making_a_held_file_again_removes_it_and_queues_only_that_file_in_its_run_mode(self) -> None:
        manifest_path = self.root / "runs" / "manifest-held.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"selection": {"queue_mode": "season_override"}, "items": []}))
        queued: list[tuple[str, str, list[int]]] = []
        with open_db(self.config.paths.db_path) as connection:
            item_id, stage = self._held_file(connection, "Remake.mkv", manifest_path=manifest_path, item_index=0)

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "2026-09-30T12:00:00+00:00",
            validate_items=lambda *_args: self.fail("making it again must not check the old file"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        with open_db(self.config.paths.db_path) as connection:
            artifact = connection.execute(
                select(staged_artifacts.c.library_item_id).where(staged_artifacts.c.library_item_id == item_id)
            ).first()
            status = connection.execute(select(library_items.c.status).where(library_items.c.id == item_id)).scalar_one()

        self.assertTrue(result["ok"], result)
        self.assertFalse(stage.exists())
        self.assertIsNone(artifact)
        self.assertEqual(status, "planned")
        self.assertEqual(queued, [("tv/Show/Season 1", "season_override", [item_id])])

    def test_making_a_held_file_again_waits_while_its_run_is_still_active(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id, stage = self._held_file(connection, "Busy.mkv")
            connection.execute(
                encode_jobs.insert().values(
                    job_id="active-run", prefix="tv/Show/Season 1", status="running", job_kind="shard", host_json="{}",
                    manifest_path=str(self.root / "runs" / "active.json"), item_count=1,
                    created_at=datetime.now(tz=UTC).isoformat(), updated_at=datetime.now(tz=UTC).isoformat(),
                )
            )

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("nothing to check"),
            queue_items=lambda *_args: self.fail("nothing may be queued while the run is active"),
        )

        self.assertFalse(result["ok"])
        self.assertIn("still compressing", result["message"])
        self.assertTrue(stage.exists())

    def test_promotion_refuses_a_held_file_even_when_its_stored_record_says_passed(self) -> None:
        from mediaforce.encoding.staging import PromotionWaiting, staged_size_prediction

        stored = {"passed": True, "target_size_trace": {"selected_candidate": {"predicted_whole_episode_bytes": 1_000}}}
        self.assertTrue(staged_size_prediction(stored, 500)["held"])
        with open_db(self.config.paths.db_path) as connection:
            rel_path = "tv/Show/Season 1/Skipped Check.mkv"
            item_id = self._insert_item(connection, rel_path, status="validated")
            stage = self._write_stage(rel_path, b"x" * 500)
            self._insert_artifact(connection, item_id, stage)
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == item_id)
                .values(validation_json=json.dumps(stored))
            )
            source = self.root / "library" / rel_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"original")
            from mediaforce.execution import promote_one_item

            with self.assertRaises(PromotionWaiting):
                promote_one_item(
                    connection, self.config,
                    {"library_item_id": item_id, "source_path": str(source), "rel_path": rel_path,
                     "source_size_bytes": 10_000},
                    force=False,
                )
        self.assertTrue(source.exists())

    def test_a_file_that_is_not_held_gets_no_size_decision(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/Fine.mkv", status="encoded")
            self._insert_artifact(connection, item_id, self._write_stage("tv/Show/Season 1/Fine.mkv", b"ok"), passed=True)

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=True, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("nothing to check"),
            queue_items=lambda *_args: self.fail("nothing to queue"),
        )

        self.assertFalse(result["ok"])

    def _final_size_file(self, *, extra_failure: bool = False) -> tuple[int, Path, Path]:
        from mediaforce.core.evidence import stable_json_hash
        request = {"size_goal": {"mode": "normalized", "value_mb": 300, "reference_runtime_minutes": 45}}
        contract = {"schema_version": 1, "sample_job_id": "old-sample", "policy_hash": "old-policy",
                    "operator_intent": request, "operator_intent_hash": f"sha256:{stable_json_hash(request)}"}
        manifest = self.root / "runs" / "remake.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({
            "selection": {"queue_mode": "older_seasons", "production_approval_contract": contract,
                          "media_scope": {"prefix": "tv/Show"}},
            "items": [{"duration_seconds": 2700}, {"duration_seconds": 438.058}],
        }))
        checks = [{"passed": False, "message": FINAL_SIZE_GOAL_CHECK}]
        if extra_failure:
            checks.append({"passed": False, "message": "staged duration closely matches the source"})
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/TooLarge.mkv", status="encoded")
            stage = self._write_stage("tv/Show/Season 1/TooLarge.mkv", b"compressed")
            self._insert_artifact(connection, item_id, stage, manifest_path=manifest, item_index=1)
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
                encode_job_id="finished-show", validation_json=json.dumps({"passed": False, "checks": checks,
                    "final_size_goal": {"target_size_bytes": 48_673_111}})))
            connection.execute(encode_jobs.insert().values(
                job_id="finished-show", prefix="tv/Show", status="completed", job_kind="folder", host_json="{}",
                manifest_path=str(manifest), item_count=2, created_at="now", updated_at="now"))
        return item_id, stage, manifest

    @staticmethod
    def _new_remake_approval(*, sample: str = "new-sample", value_mb: float = 220) -> dict[str, object]:
        from mediaforce.core.evidence import stable_json_hash
        request = {"size_goal": {"mode": "normalized", "value_mb": value_mb, "reference_runtime_minutes": 45}}
        return {"schema_version": 1, "sample_job_id": sample, "policy_hash": "new-policy",
                "operator_intent": request, "operator_intent_hash": f"sha256:{stable_json_hash(request)}"}

    def test_final_size_remake_requires_new_sample_and_goal_before_removal(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        self._assert_remake_available(item_id)
        for approval in (None, self._new_remake_approval(sample="old-sample"), self._new_remake_approval(value_mb=300)):
            with self.subTest(approval=approval):
                result = decide_size_held_file(
                    self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
                    validate_items=lambda *_args: self.fail("no validation"),
                    queue_items=lambda *_args: self.fail("no queue before a fresh approval"),
                    current_approval=lambda _prefix: approval,
                )
                self.assertFalse(result["ok"])
                self.assertTrue(stage.exists())
                with open_db(self.config.paths.db_path) as connection:
                    self.assertIsNotNone(connection.execute(select(staged_artifacts).where(
                        staged_artifacts.c.library_item_id == item_id)).first())

    def test_final_size_remake_queues_one_file_at_the_show_scope(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        queued: list[tuple[str, str, list[int]]] = []
        with open_db(self.config.paths.db_path) as connection:
            other_id, other_stage = self._held_file(connection, "Untouched.mkv")
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no old-file validation"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertFalse(stage.exists())
        self.assertTrue(other_stage.exists())
        self.assertEqual((self.root / "source/tv/Show/Season 1/TooLarge.mkv").read_bytes(), b"source")
        self.assertEqual(queued, [("tv/Show", "older_seasons", [item_id])])
        with open_db(self.config.paths.db_path) as connection:
            self.assertIsNotNone(connection.execute(select(staged_artifacts).where(
                staged_artifacts.c.library_item_id == other_id)).first())

    def test_batch_remake_queues_compatible_files_once_and_preserves_excluded_siblings(self) -> None:
        size_id, size_stage, manifest = self._final_size_file()
        with open_db(self.config.paths.db_path) as connection:
            history_id = self._insert_item(connection, "tv/Show/Season 1/History.mkv", status="validated")
            history_stage = self._write_stage("tv/Show/Season 1/History.mkv", b"history")
            self._insert_artifact(connection, history_id, history_stage, passed=True, manifest_path=manifest, item_index=0)
            blocked_id, blocked_stage = self._held_file(connection, "Unavailable.mkv", manifest_path=manifest)
            (self.root / "source/tv/Show/Season 1/Unavailable.mkv").unlink()
            foreign_id = self._insert_item(connection, "tv/Other/Season 1/Other.mkv", status="validated")
            foreign_stage = self._write_stage("tv/Other/Season 1/Other.mkv", b"other")
            self._insert_artifact(connection, foreign_id, foreign_stage, passed=True, manifest_path=manifest)
        queued: list[tuple[str, str, list[int]]] = []
        result = decide_size_held_file(
            self.config, "tv/Show", [size_id, history_id, blocked_id, foreign_id, size_id], keep=False,
            now_iso=lambda: "now", validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(queued, [("tv/Show", "older_seasons", [size_id, history_id])])
        self.assertEqual(result["queued_library_item_ids"], [size_id, history_id])
        self.assertEqual({file["library_item_id"] for file in result["left_out"]}, {blocked_id, foreign_id})
        self.assertIn("Restore access", result["left_out"][0]["reason"])
        self.assertFalse(size_stage.exists())
        self.assertFalse(history_stage.exists())
        self.assertEqual(blocked_stage.read_bytes(), b"small")
        self.assertEqual(foreign_stage.read_bytes(), b"other")
        with open_db(self.config.paths.db_path) as connection:
            statuses = dict(connection.execute(select(library_items.c.id, library_items.c.status)).all())
            self.assertEqual(statuses[size_id], "planned")
            self.assertEqual(statuses[history_id], "planned")
            self.assertEqual(statuses[blocked_id], "encoded")
            decisions = connection.execute(select(item_events.c.library_item_id).where(
                item_events.c.event_type == "owner_size_held_decision")).scalars().all()
            self.assertEqual(decisions, [size_id, history_id])
        for name in ("TooLarge.mkv", "History.mkv"):
            self.assertEqual((self.root / "source/tv/Show/Season 1" / name).read_bytes(), b"source")

    def test_batch_remake_preserves_completed_recovery_when_later_removal_interrupts(self) -> None:
        first_id, first_stage, manifest = self._final_size_file()
        with open_db(self.config.paths.db_path) as connection:
            second_id, second_stage = self._held_file(connection, "Later.mkv", manifest_path=manifest)
        remove = size_held._remove_finished_output

        def interrupt_later(config: MediaforceConfig, row: Any) -> bool:
            if row["library_item_id"] == second_id:
                raise KeyboardInterrupt("Controller interrupted during the next file")
            return remove(config, row)

        with patch.object(size_held, "_remove_finished_output", side_effect=interrupt_later):
            with self.assertRaisesRegex(KeyboardInterrupt, "Controller interrupted"):
                decide_size_held_file(
                    self.config, "tv/Show", [first_id, second_id], keep=False, now_iso=lambda: "now",
                    validate_items=lambda *_args: self.fail("no validation"),
                    queue_items=lambda *_args: self.fail("interrupted before queueing"),
                    current_approval=lambda _prefix: self._new_remake_approval(),
                )
        self.assertFalse(first_stage.exists())
        self.assertTrue(second_stage.exists())
        with open_db(self.config.paths.db_path) as connection:
            self.assertEqual(connection.execute(select(library_items.c.status).where(
                library_items.c.id == first_id)).scalar_one(), "planned")
            self.assertIsNone(connection.execute(select(staged_artifacts.c.library_item_id).where(
                staged_artifacts.c.library_item_id == first_id)).scalar_one_or_none())
            self.assertEqual(connection.execute(select(item_events.c.library_item_id).where(
                item_events.c.event_type == "owner_size_held_decision")).scalars().all(), [first_id])

    def test_batch_remake_queues_completed_files_despite_one_removal_error(self) -> None:
        first_id, first_stage, manifest = self._final_size_file()
        with open_db(self.config.paths.db_path) as connection:
            failed_id, failed_stage = self._held_file(connection, "Unavailable.mkv", manifest_path=manifest)
            last_id, last_stage = self._held_file(connection, "Later.mkv", manifest_path=manifest)
        remove = size_held._remove_finished_output

        def remove_available(config: MediaforceConfig, row: Any) -> bool:
            if row["library_item_id"] == failed_id:
                raise OSError("Host cleanup unavailable")
            return remove(config, row)

        queue = Mock(return_value={"ok": True})
        with patch.object(size_held, "_remove_finished_output", side_effect=remove_available):
            result = decide_size_held_file(
                self.config, "tv/Show", [first_id, failed_id, last_id], keep=False, now_iso=lambda: "now",
                validate_items=lambda *_args: self.fail("no validation"), queue_items=queue,
                current_approval=lambda _prefix: self._new_remake_approval(),
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["queued_library_item_ids"], [first_id, last_id])
        self.assertEqual(result["removed_library_item_ids"], [first_id, last_id])
        self.assertEqual(result["left_out"][0]["library_item_id"], failed_id)
        self.assertFalse(first_stage.exists())
        self.assertFalse(last_stage.exists())
        self.assertEqual(failed_stage.read_bytes(), b"small")
        with open_db(self.config.paths.db_path) as connection:
            self.assertEqual(connection.execute(select(library_items.c.status).where(
                library_items.c.id == failed_id)).scalar_one(), "encoded")
            self.assertIsNotNone(connection.execute(select(staged_artifacts.c.library_item_id).where(
                staged_artifacts.c.library_item_id == failed_id)).scalar_one_or_none())

    def test_batch_remake_reports_silent_queue_exclusion_without_the_success_copy(self) -> None:
        first_id, _stage, manifest = self._final_size_file()
        with open_db(self.config.paths.db_path) as connection:
            second_id, _second_stage = self._held_file(connection, "Later.mkv", manifest_path=manifest)
        result = decide_size_held_file(
            self.config, "tv/Show", [first_id, second_id], keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda *_args: {"ok": True, "queued_library_item_ids": [first_id], "message": "Queued 1 file."},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["left_out"][0]["library_item_id"], second_id)
        self.assertNotIn("Queued 1 file", result["left_out"][0]["reason"])
        self.assertIn("not accepted", result["left_out"][0]["reason"])

    def test_batch_remake_attempts_other_groups_when_one_queue_raises(self) -> None:
        first_id, first_stage, _manifest = self._final_size_file()
        season_manifest = self.root / "runs/another-scope.json"
        season_manifest.write_text(json.dumps({"selection": {"queue_mode": "season_override",
            "media_scope": {"prefix": "tv/Show/Season 1"}}, "items": [{}]}))
        with open_db(self.config.paths.db_path) as connection:
            second_id, second_stage = self._held_file(connection, "Later.mkv", manifest_path=season_manifest)
        queue = Mock(side_effect=[OSError("Queue storage unavailable"), {"ok": True}])
        result = decide_size_held_file(
            self.config, "tv/Show", [first_id, second_id], keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=queue,
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["queued_library_item_ids"], [second_id])
        self.assertEqual(result["removed_library_item_ids"], [first_id, second_id])
        self.assertEqual(result["left_out"][0]["library_item_id"], first_id)
        self.assertIn("Finished copy removed, but not queued", result["left_out"][0]["reason"])
        self.assertFalse(first_stage.exists())
        self.assertFalse(second_stage.exists())

    def test_queued_followup_remake_is_accepted_but_overlapping_or_unreadable_work_is_preserved(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        queued_manifest = self.root / "runs/queued.json"
        queued_manifest.write_text(json.dumps({"items": [{"library_item_id": item_id + 100}]}))
        with open_db(self.config.paths.db_path) as connection:
            self._insert_encode_job(connection, job_id="queued-run", prefix="tv/Show", status="queued",
                                    updated_at="now", manifest_path=queued_manifest)
        for contents in ({"items": [{"library_item_id": item_id}]}, {}, {"items": [{}]}):
            queued_manifest.write_text(json.dumps(contents))
            result = decide_size_held_file(
                self.config, "tv/Show", [item_id], keep=False, now_iso=lambda: "now",
                validate_items=lambda *_args: self.fail("no validation"),
                queue_items=lambda *_args: self.fail("must preserve"),
                current_approval=lambda _prefix: self._new_remake_approval(),
            )
            self.assertFalse(result["ok"])
            self.assertTrue(stage.exists())
            self.assertIn("queued run", result["message"])
        queued_manifest.write_text(json.dumps({"items": [{"library_item_id": item_id + 100}]}))
        queued = Mock(return_value={"ok": True})
        result = decide_size_held_file(
            self.config, "tv/Show", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=queued,
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        queued.assert_called_once_with("tv/Show", "older_seasons", [item_id])
        self.assertFalse(stage.exists())
        self.assertTrue(manifest.exists())

    def test_batch_remake_reports_queue_failures_after_removal(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        result = decide_size_held_file(
            self.config, "tv/Show", [item_id], keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda *_args: {"ok": False, "message": "Storage is unavailable."},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["removed_library_item_ids"], [item_id])
        self.assertEqual(result["queued_library_item_ids"], [])
        self.assertIn("Finished copy removed, but not queued", result["message"])
        self.assertFalse(stage.exists())

    def test_batch_remake_retains_each_saved_scope_and_mode_and_queue_exclusion(self) -> None:
        size_id, _stage, _manifest = self._final_size_file()
        season_manifest = self.root / "runs/season-remake.json"
        season_manifest.write_text(json.dumps({"selection": {"queue_mode": "season_override",
            "media_scope": {"prefix": "tv/Show/Season 1"}}, "items": [{}]}))
        with open_db(self.config.paths.db_path) as connection:
            season_id, _season_stage = self._held_file(connection, "Season.mkv", manifest_path=season_manifest)
        calls: list[tuple[str, str, list[int]]] = []

        def queue(prefix: str, mode: str, ids: Collection[int]) -> dict[str, Any]:
            calls.append((prefix, mode, list(ids)))
            return {"ok": True, "left_out": [{"library_item_id": season_id, "reason": "Needs a motion check."}]
                    if mode == "season_override" else []}

        result = decide_size_held_file(
            self.config, "tv/Show", [size_id, season_id], keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no old-file check"), queue_items=queue,
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertEqual(calls, [("tv/Show", "older_seasons", [size_id]),
                                 ("tv/Show/Season 1", "season_override", [season_id])])
        self.assertTrue(result["ok"])
        self.assertEqual(result["queued_library_item_ids"], [size_id])
        self.assertEqual(result["left_out"][0]["library_item_id"], season_id)
        self.assertIn("Needs a motion check", result["left_out"][0]["reason"])

    def test_remake_does_not_claim_a_file_the_queue_did_not_accept(self) -> None:
        item_id, _stage, _manifest = self._final_size_file()
        result = decide_size_held_file(
            self.config, "tv/Show", [item_id], keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no old-file check"),
            queue_items=lambda *_args: {"ok": True, "queued_library_item_ids": [],
                                       "message": "The selected file is already queued elsewhere."},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["queued_library_item_ids"], [])
        self.assertEqual(result["removed_library_item_ids"], [item_id])
        self.assertIn("not accepted", result["left_out"][0]["reason"])

    def test_other_validation_failures_and_keep_cannot_bypass_the_size_contract(self) -> None:
        item_id, stage, _manifest = self._final_size_file(extra_failure=True)
        for keep in (False, True):
            result = decide_size_held_file(
                self.config, "tv/Show/Season 1", item_id, keep=keep, now_iso=lambda: "now",
                validate_items=lambda *_args: self.fail("must refuse"), queue_items=lambda *_args: self.fail("must refuse"),
                current_approval=lambda _prefix: self._new_remake_approval(),
            )
            self.assertFalse(result["ok"])
            self.assertTrue(stage.exists())

    def test_missing_settings_history_can_be_remade_but_never_kept(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/Old.mkv", status="validated")
            stage = self._write_stage("tv/Show/Season 1/Old.mkv", b"old")
            manifest = self.root / "runs" / "history.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"selection": {"queue_mode": "folder", "media_scope": {
                "prefix": "tv/Show/Season 1"}}, "items": [{}]}))
            self._insert_artifact(connection, item_id, stage, passed=True, manifest_path=manifest, item_index=0)
            records = staged_remake_records(connection, [{"item_id": item_id}], "tv/Show/Season 1",
                current_approval=lambda _prefix: None)
        self.assertEqual(records[0]["remake"]["reason"], "settings_history")
        self.assertTrue(records[0]["remake"]["blocked_reason"])
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=True, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("must refuse"), queue_items=lambda *_args: self.fail("must refuse"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        queued: list[int] = []

        def queue(_prefix: str, _mode: str, ids: Collection[int]) -> dict[str, bool]:
            queued.extend(ids)
            return {"ok": True}

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no old-file validation"),
            queue_items=queue,
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(queued, [item_id])
        self.assertFalse(stage.exists())

    def test_remake_preserves_the_output_when_the_original_is_unavailable(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        original = self.root / "source/tv/Show/Season 1/TooLarge.mkv"
        original.unlink()
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("Restore access to the original", result["message"])
        self.assertTrue(stage.exists())

    def test_remake_refuses_a_staging_path_that_points_at_the_original(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        original = self.root / "source/tv/Show/Season 1/TooLarge.mkv"
        stage.unlink()
        stage.symlink_to(original)
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(original.read_bytes(), b"source")
        self.assertTrue(stage.is_symlink())

    def test_remake_keeps_show_scope_and_mode_after_its_job_row_was_cleared(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.delete().where(encode_jobs.c.job_id == "finished-show"))
        queued: list[tuple[str, str, list[int]]] = []
        requested_approvals: list[str] = []

        def approval(prefix: str) -> dict[str, object] | None:
            requested_approvals.append(prefix)
            return self._new_remake_approval() if prefix == "tv/Show" else None

        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=approval,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(requested_approvals, ["tv/Show"])
        self.assertEqual(queued, [("tv/Show", "older_seasons", [item_id])])
        self.assertFalse(stage.exists())

    def test_changed_legacy_goal_clears_an_existing_misrecorded_miss_without_erasing_history(self) -> None:
        item_id, _stage, manifest = self._final_size_file()
        payload = json.loads(manifest.read_text())
        payload["selection"].pop("production_approval_contract")
        payload["items"][1]["library_item_id"] = item_id
        manifest.write_text(json.dumps(payload))
        current = self._new_remake_approval()
        analysis = {"kind": "final_size_target_miss", "manifest_index": 1,
                    "target_size_verification": {"target_size_bytes": 48_673_111}}
        items = [{"library_item_id": item_id, "rel_path": "tv/Show/Season 1/TooLarge.mkv"}]
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "finished-show").values(
                progress_json=json.dumps({"failure_analysis": analysis})))
            connection.execute(item_events.insert().values(
                library_item_id=item_id, created_at="before", event_type=folder_actions.FINAL_SIZE_MISS_EVENT,
                details_json=json.dumps({"job_id": "finished-show", "sample_job_id": current["sample_job_id"],
                    "operator_intent_hash": current["operator_intent_hash"], "named": True})))
            self.assertEqual(len(folder_actions._recorded_final_size_miss_left_out(connection, items, current)), 1)
            folder_actions._record_final_size_misses(connection, "tv/Show", current, now="after")
            self.assertEqual(folder_actions._recorded_final_size_miss_left_out(connection, items, current), [])
            folder_actions._record_final_size_misses(connection, "tv/Show", current, now="after-again")
            events = connection.execute(select(item_events.c.event_type).where(
                item_events.c.library_item_id == item_id)).scalars().all()
            self.assertEqual(events, [folder_actions.FINAL_SIZE_MISS_EVENT, folder_actions.FINAL_SIZE_MISS_RECOVERED_EVENT])
            connection.execute(encode_jobs.delete().where(encode_jobs.c.job_id == "finished-show"))
            self.assertEqual(folder_actions._recorded_final_size_miss_left_out(connection, items, current), [])
            self.assertEqual(len(folder_actions._recorded_final_size_miss_left_out(
                connection, items, self._new_remake_approval(sample="another-sample", value_mb=300))), 1)
            self.assertEqual(len(folder_actions._recorded_final_size_miss_left_out(connection, items, None)), 1)

    def test_run_level_size_blocker_preserves_the_finished_file_before_queue_refusal(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        self._assert_remake_available(item_id)
        payload = json.loads(manifest.read_text())
        payload["selection"].pop("production_approval_contract")
        payload["items"][1]["library_item_id"] = item_id
        manifest.write_text(json.dumps(payload))
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "finished-show").values(
                progress_json=json.dumps({"failure_analysis": {"kind": "final_size_target_miss",
                    "target_size_verification": {"target_size_bytes": 48_673_111}}})))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertTrue(stage.exists())
        with open_db(self.config.paths.db_path) as connection:
            self.assertEqual(connection.execute(select(item_events.c.id)).all(), [])
            self.assertIsNotNone(connection.execute(select(staged_artifacts).where(
                staged_artifacts.c.library_item_id == item_id)).first())

    def test_saved_size_miss_preserves_a_file_even_when_its_staged_goal_changed(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        self._assert_remake_available(item_id)
        current = self._new_remake_approval()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(item_events.insert().values(
                library_item_id=item_id, created_at="before", event_type=folder_actions.FINAL_SIZE_MISS_EVENT,
                details_json=json.dumps({"job_id": "cleared-other-run", "sample_job_id": current["sample_job_id"],
                    "operator_intent_hash": current["operator_intent_hash"], "named": True})))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: current,
        )
        self.assertFalse(result["ok"])
        self.assertTrue(stage.exists())

    def test_verified_legacy_recovery_preflight_does_not_write_or_falsely_block(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        payload = json.loads(manifest.read_text())
        payload["selection"].pop("production_approval_contract")
        payload["items"][1]["library_item_id"] = item_id
        manifest.write_text(json.dumps(payload))
        current = self._new_remake_approval()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "finished-show").values(
                progress_json=json.dumps({"failure_analysis": {"kind": "final_size_target_miss", "manifest_index": 1,
                    "target_size_verification": {"target_size_bytes": 48_673_111}}})))
            connection.execute(item_events.insert().values(
                library_item_id=item_id, created_at="before", event_type=folder_actions.FINAL_SIZE_MISS_EVENT,
                details_json=json.dumps({"job_id": "finished-show", "sample_job_id": current["sample_job_id"],
                    "operator_intent_hash": current["operator_intent_hash"], "named": True})))
            self.assertIsNone(folder_actions.staged_requeue_size_blocker(connection, "tv/Show", item_id, current))
            self.assertEqual(len(connection.execute(select(item_events.c.id)).all()), 1)
        self.assertTrue(stage.exists())

    def test_saved_selection_preserves_scope_and_mode_when_manifest_and_job_are_gone(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        selection = json.loads(manifest.read_text())["selection"]
        selection.pop("queue_mode")
        selection.pop("media_scope")
        selection["lifecycle_override"] = {"mode": "older_seasons", "series_prefix": "tv/Show"}
        manifest.unlink()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.delete().where(encode_jobs.c.job_id == "finished-show"))
            connection.execute(run_manifests.insert().values(run_id="saved-run", created_at="before",
                output_path=str(manifest), selection_json=json.dumps(selection), item_count=2))
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
                manifest_run_id="saved-run", validation_json=json.dumps({"passed": True})))
        queued: list[tuple[str, str, list[int]]] = []
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=lambda prefix: self._new_remake_approval() if prefix == "tv/Show" else None,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(queued, [("tv/Show", "older_seasons", [item_id])])
        self.assertFalse(stage.exists())

    def test_legacy_manual_season_override_is_preserved_without_recorded_queue_mode(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        payload = json.loads(manifest.read_text())
        payload["selection"].pop("queue_mode")
        payload["selection"]["media_scope"]["prefix"] = "tv/Show/Season 1"
        payload["items"][1]["selection_provenance"] = {"override_applied": True, "manual_override": True}
        manifest.write_text(json.dumps(payload))
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "finished-show").values(
                prefix="tv/Show/Season 1"))
        queued = []
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"),
            queue_items=lambda prefix, mode, ids: queued.append((prefix, mode, list(ids))) or {"ok": True},
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(queued, [("tv/Show/Season 1", "season_override", [item_id])])
        self.assertFalse(stage.exists())

    def test_missing_legacy_mode_preserves_the_copy_until_its_run_record_is_restored(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        selection = json.loads(manifest.read_text())["selection"]
        selection.pop("queue_mode")
        manifest.unlink()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(run_manifests.insert().values(run_id="saved-run", created_at="before",
                output_path=str(manifest), selection_json=json.dumps(selection), item_count=2))
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
                manifest_run_id="saved-run", validation_json=json.dumps({"passed": True})))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("Restore the saved run settings", result["message"])
        self.assertTrue(stage.exists())

    def test_approved_page_reads_shared_policy_manifest_once(self) -> None:
        policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        manifest = self._write_policy_manifest("approved-page.json", [policy] * 10,
            selection={"production_approval_contract": {"schema_version": 1, "policy_hash": "earlier"}})
        reads: list[Path] = []
        def read(path: Path) -> str:
            reads.append(path)
            with path.open(encoding="utf-8") as stream:
                return stream.read()

        with open_db(self.config.paths.db_path) as connection:
            records = []
            for index in range(10):
                rel_path = f"tv/Show/Season 1/{index}.mkv"
                item_id = self._insert_item(connection, rel_path, status="validated")
                self._insert_artifact(connection, item_id, self._write_stage(rel_path, b"ready"), passed=True,
                                      manifest_path=manifest, item_index=index)
                records.append({"item_id": item_id})
            with patch.object(Path, "read_text", read):
                result = staged_remake_records(connection, records, "tv/Show/Season 1",
                                              current_approval=lambda _prefix: self.fail("already approved"))
        self.assertTrue(all("remake" not in record for record in result))
        self.assertEqual(reads.count(manifest), 1)

    def test_active_sample_preserves_the_finished_file_before_queue_refusal(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        self._assert_remake_available(item_id)
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(calibration_jobs.insert().values(
                job_id="new-sample-running", prefix="tv/Show", status="running", lane="test", action="sample",
                host_json="{}", policy_json="{}", sample_item_json="{}", created_at="now", updated_at="now"))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("sample", result["message"])
        self.assertTrue(stage.exists())

    def test_held_remake_preserves_the_finished_file_without_current_approval(self) -> None:
        manifest = self._write_policy_manifest("held-approval.json", [], selection={"queue_mode": "folder"})
        with open_db(self.config.paths.db_path) as connection:
            item_id, stage = self._held_file(connection, "No approval.mkv", manifest_path=manifest, item_index=0)
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
        )
        self.assertFalse(result["ok"])
        self.assertIn("Approve", result["message"])
        self.assertTrue(stage.exists())

    def test_missing_size_manifest_explains_the_required_recovery_record(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        self._assert_remake_available(item_id)
        selection = json.loads(manifest.read_text())["selection"]
        manifest.unlink()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(run_manifests.insert().values(run_id="saved-run", created_at="before",
                output_path=str(manifest), selection_json=json.dumps(selection), item_count=2))
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
                manifest_run_id="saved-run"))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("Restore the run manifest", result["message"])
        self.assertTrue(stage.exists())

    def _assert_remake_available(self, item_id: int) -> None:
        with open_db(self.config.paths.db_path) as connection:
            page = staged_remake_records(connection, [{"item_id": item_id}], "tv/Show/Season 1",
                current_approval=lambda _prefix: self._new_remake_approval())
        self.assertFalse(page[0]["remake"]["blocked_reason"], page)

    def test_remake_pages_share_reads_and_refresh_on_the_next_request(self) -> None:
        for kind in ("approved", "settings_history", "final_size", "legacy_size"):
            with self.subTest(kind=kind):
                manifest = self.root / "runs" / f"{kind}.json"
                manifest.parent.mkdir(parents=True, exist_ok=True)
                selection = {"queue_mode": "older_seasons", "media_scope": {"prefix": "tv/Show"}}
                if kind in {"approved", "final_size"}:
                    selection["production_approval_contract"] = self._new_remake_approval(
                        sample="old-sample", value_mb=300)
                items = []
                ids = []
                with open_db(self.config.paths.db_path) as connection:
                    for index in range(20):
                        rel_path = f"tv/Show/Season 1/{kind}-{index}.mkv"
                        item_id = self._insert_item(connection, rel_path, status="encoded")
                        ids.append(item_id)
                        stage = self._write_stage(rel_path, b"finished")
                        self._insert_artifact(connection, item_id, stage, passed=True,
                            manifest_path=manifest, item_index=index)
                        item = {"library_item_id": item_id, "duration_seconds": 2700}
                        if kind != "settings_history":
                            item["resolved_policy"] = {"video": {"encoder": "libsvtav1"}}
                        items.append(item)
                        if kind in {"final_size", "legacy_size"}:
                            connection.execute(staged_artifacts.update().where(
                                staged_artifacts.c.library_item_id == item_id).values(validation_json=json.dumps({
                                    "passed": False, "checks": [{"passed": False, "message": FINAL_SIZE_GOAL_CHECK}],
                                    "final_size_goal": {"target_size_bytes": 300_000_000},
                                })))
                    if kind in {"final_size", "legacy_size"}:
                        connection.execute(encode_jobs.insert().values(job_id=f"{kind}-run", prefix="tv/Show",
                            status="failed", job_kind="folder", host_json="{}", manifest_path=str(manifest),
                            item_count=len(items), created_at="now", updated_at="now", progress_json=json.dumps({
                                "failure_analysis": {"kind": "final_size_target_miss", "manifest_index": 0,
                                    "target_size_verification": {"target_size_bytes": 300_000_000}},
                            })))
                manifest.write_text(json.dumps({"selection": selection, "items": items}))
                reads = []
                original_read = Path.read_text

                def counted_read(path: Path, *args: object, **kwargs: Any) -> str:
                    if path == manifest:
                        reads.append(path)
                    return original_read(path, *args, **kwargs)

                def page() -> list[dict[str, Any]]:
                    with open_db(self.config.paths.db_path) as connection:
                        return staged_remake_records(connection, [{"item_id": item_id} for item_id in ids],
                            "tv/Show/Season 1", current_approval=lambda _prefix: self._new_remake_approval())

                with patch.object(Path, "read_text", counted_read):
                    records = page()
                self.assertEqual(len(reads), 1)
                if kind == "approved":
                    self.assertTrue(all("remake" not in record for record in records))
                else:
                    self.assertTrue(all(not record["remake"]["blocked_reason"] for record in records), records)

                manifest.write_text("broken json")
                reads.clear()
                with patch.object(Path, "read_text", counted_read):
                    records = page()
                self.assertEqual(len(reads), 1)
                self.assertTrue(all("Restore the saved run settings" in record["remake"]["blocked_reason"]
                                    for record in records), records)
                manifest.write_text(json.dumps({"selection": selection, "items": items}))
                records = page()
                self.assertTrue(all(not record.get("remake", {}).get("blocked_reason") for record in records), records)

    def test_missing_run_context_preserves_the_output_until_its_record_is_restored(self) -> None:
        item_id, stage, manifest = self._final_size_file()
        manifest.unlink()
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(encode_jobs.delete().where(encode_jobs.c.job_id == "finished-show"))
            connection.execute(staged_artifacts.update().where(staged_artifacts.c.library_item_id == item_id).values(
                validation_json=json.dumps({"passed": True})))
        result = decide_size_held_file(
            self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
            validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
            current_approval=lambda _prefix: self._new_remake_approval(),
        )
        self.assertFalse(result["ok"])
        self.assertIn("Restore the saved run settings", result["message"])
        self.assertTrue(stage.exists())

    def test_partial_cleanup_failure_does_not_remove_the_finished_output(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        attempted: list[Path] = []

        def remove(path: Path, **_kwargs: object) -> bool:
            attempted.append(path)
            if path != stage:
                return False
            path.unlink()
            return True

        with patch("mediaforce.web.runtime.size_held.remove_stale_staging_path", side_effect=remove):
            result = decide_size_held_file(
                self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
                validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
                current_approval=lambda _prefix: self._new_remake_approval(),
            )
        self.assertFalse(result["ok"])
        self.assertTrue(stage.exists())
        self.assertNotIn(stage, attempted)

    def test_failed_removal_retains_record_and_does_not_queue(self) -> None:
        item_id, stage, _manifest = self._final_size_file()
        with patch("mediaforce.web.runtime.size_held.remove_stale_staging_path", return_value=False):
            result = decide_size_held_file(
                self.config, "tv/Show/Season 1", item_id, keep=False, now_iso=lambda: "now",
                validate_items=lambda *_args: self.fail("no validation"), queue_items=lambda *_args: self.fail("no queue"),
                current_approval=lambda _prefix: self._new_remake_approval(),
            )
        self.assertFalse(result["ok"])
        self.assertTrue(stage.exists())
        with open_db(self.config.paths.db_path) as connection:
            self.assertIsNotNone(connection.execute(select(staged_artifacts).where(
                staged_artifacts.c.library_item_id == item_id)).first())

    def test_remote_only_is_distinct_from_missing(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            remote = self._insert_item(connection, "tv/Show/Season 1/Remote.mkv", status="encoded")
            missing = self._insert_item(connection, "tv/Show/Season 1/Missing.mkv", status="encoded")
            self._insert_artifact(
                connection,
                remote,
                self.root / "remote-staging/tv/Show/Season 1/Remote.mkv",
                encode_host_key="remote-a",
                encode_media_access="stream",
            )
            self._insert_artifact(
                connection,
                missing,
                self.root / "staging/tv/Show/Season 1/Missing.mkv",
            )
            report = staged_integrity_report(connection, self.config, "tv/Show/Season 1", discover=False)

        by_path = {record.rel_path: record.disposition for record in report.records}
        self.assertEqual(by_path["tv/Show/Season 1/Remote.mkv"], "remote_only_or_unreachable")
        self.assertEqual(by_path["tv/Show/Season 1/Missing.mkv"], "missing")

    def test_detail_page_is_bounded(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            for index in range(MAX_DETAIL_PAGE_SIZE + 2):
                self._insert_item(connection, f"tv/Show/Season 1/Episode {index}.mkv", status="planned")
            report = staged_integrity_report(connection, self.config, "tv/Show/Season 1", discover=False)

        payload = report.detail_payload(offset=0, limit=MAX_DETAIL_PAGE_SIZE + 20)
        self.assertEqual(payload["limit"], MAX_DETAIL_PAGE_SIZE)
        self.assertEqual(len(payload["records"]), MAX_DETAIL_PAGE_SIZE)
        self.assertEqual(payload["next_offset"], MAX_DETAIL_PAGE_SIZE)

    def test_database_truncation_marks_discovery_incomplete(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="planned")
            self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="planned")
            report = staged_integrity_report(
                connection,
                self.config,
                "tv/Show/Season 1",
                discover=True,
                record_limit=1,
            )

        self.assertTrue(report.database_truncated)
        self.assertTrue(report.discovery_requested)
        self.assertTrue(report.discovery_truncated)
        self.assertEqual(report.discovery_entries_scanned, 0)

    def test_discovery_ignores_sidecars_and_hidden_directories(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Planned.mkv", status="planned")
            orphan = self._write_stage("tv/Show/Season 1/Orphan.mkv", b"orphan")
            sidecar = self._write_stage("tv/Show/Season 1/Orphan.srt", b"subtitle")
            hidden = self._write_stage("tv/Show/Season 1/.sync/Hidden.mkv", b"hidden")
            report = staged_integrity_report(
                connection,
                self.config,
                "tv/Show/Season 1",
                discover=True,
            )

        discovered_paths = {record.staging_path for record in report.records if record.item_id is None}
        self.assertIn(str(orphan.resolve()), discovered_paths)
        self.assertNotIn(str(sidecar.resolve()), discovered_paths)
        self.assertNotIn(str(hidden.resolve()), discovered_paths)

    def test_validated_item_without_artifact_is_missing(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Validated.mkv", status="validated")
            report = staged_integrity_report(connection, self.config, "tv/Show/Season 1", discover=False)

        self.assertEqual(report.records[0].disposition, "missing")

    def test_checked_staged_output_returns_exact_validated_movie_identity(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "movies/Ready/Feature.mp4", status="validated")
            stage = self._write_stage("movies/Ready/Feature.mkv", b"checked movie output")
            self._insert_artifact(connection, item_id, stage, passed=True)

            output = checked_staged_output(
                connection,
                self.config,
                "movies/Ready/Feature.mp4",
            )

        self.assertEqual(output.path, stage.resolve())
        self.assertEqual(output.size_bytes, stage.stat().st_size)
        self.assertEqual(output.mtime_ns, stage.stat().st_mtime_ns)

    def test_checked_staged_output_fails_closed_for_drift_and_destination_conflict(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            drifted_id = self._insert_item(connection, "movies/Drifted/Feature.mp4", status="validated")
            drifted_stage = self._write_stage("movies/Drifted/Feature.mkv", b"changed")
            self._insert_artifact(connection, drifted_id, drifted_stage, passed=True, size_bytes=1)
            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "changed after validation"):
                checked_staged_output(connection, self.config, "movies/Drifted/Feature.mp4")

            conflict_id = self._insert_item(connection, "movies/Conflict/Feature.mp4", status="validated")
            conflict_stage = self._write_stage("movies/Conflict/Feature.mkv", b"checked")
            self._insert_artifact(connection, conflict_id, conflict_stage, passed=True)
            conflict_destination = self.root / "source/movies/Conflict/Feature.mkv"
            conflict_destination.write_bytes(b"existing destination")
            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "replacement destination"):
                checked_staged_output(connection, self.config, "movies/Conflict/Feature.mp4")

    def test_checked_staged_output_requires_one_promotable_movie(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            for filename in ("Feature.mkv", "Director Cut.mkv"):
                rel_path = f"movies/Editions/{filename}"
                item_id = self._insert_item(connection, rel_path, status="validated")
                stage = self._write_stage(rel_path, filename.encode())
                self._insert_artifact(connection, item_id, stage, passed=True)

            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "multiple checked outputs"):
                checked_staged_output(connection, self.config, "movies/Editions")

            tv_id = self._insert_item(connection, "tv/Show/Season 1/Episode.mkv", status="validated")
            tv_stage = self._write_stage("tv/Show/Season 1/Episode.mkv", b"episode")
            self._insert_artifact(connection, tv_id, tv_stage, passed=True)
            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "only for movie scopes"):
                checked_staged_output(connection, self.config, "tv/Show/Season 1/Episode.mkv")

    def test_checked_staged_output_blocks_duplicate_destination_conflict(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            for suffix in ("mp4", "mov"):
                rel_path = f"movies/Conflict/Feature.{suffix}"
                item_id = self._insert_item(connection, rel_path, status="validated")
                stage = self._write_stage(f"movies/Conflict/Feature-{suffix}.mkv", suffix.encode())
                self._insert_artifact(connection, item_id, stage, passed=True)

            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "destination conflict"):
                checked_staged_output(connection, self.config, "movies/Conflict")
            with self.assertRaisesRegex(CheckedStagedOutputUnavailable, "destination conflict"):
                checked_staged_output(connection, self.config, "movies/Conflict/Feature.mp4")

    def test_remote_worker_on_shared_root_is_missing_not_unreachable(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/Shared.mkv", status="encoded")
            shared_path = self.root / "staging/tv/Show/Season 1/Shared.mkv"
            shared_path.parent.mkdir(parents=True, exist_ok=True)
            self._insert_artifact(
                connection,
                item_id,
                shared_path,
                encode_host_key="remote-a",
            )
            report = staged_integrity_report(connection, self.config, "tv/Show/Season 1", discover=False)

        self.assertEqual(report.records[0].disposition, "missing")

    def test_discovery_reports_appended_temporary_suffixes(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Planned.mkv", status="planned")
            temporary = self._write_stage("tv/Show/Season 1/Abandoned.mkv.part", b"partial")
            report = staged_integrity_report(
                connection,
                self.config,
                "tv/Show/Season 1",
                discover=True,
            )

        temporary_records = [record for record in report.records if record.staging_path == str(temporary.resolve())]
        self.assertEqual([record.disposition for record in temporary_records], ["partial_or_temporary"])

    def test_tv_season_publishes_a_ready_episode_while_others_wait(self) -> None:
        policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        manifest_path = self._write_policy_manifest("partial.json", [policy])
        with open_db(self.config.paths.db_path) as connection:
            valid_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="planned")
            stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready")
            self._insert_artifact(connection, valid_id, stage, passed=True, manifest_path=manifest_path, item_index=0)

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("one")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, _prefix: {"accepted_policy_hash": self._policy_hash(policy)},
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [self._manifest_item(stage, "tv/Show/Season 1/One.mkv")],
            promote_manifest_items_fn=promoted,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["promoted_count"], 1)
        self.assertIn("season_staged_integrity_not_started", {entry["code"] for entry in result["waiting"]})
        promoted.assert_called_once()

    def test_tv_season_holds_only_the_files_an_active_encode_is_making(self) -> None:
        policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        manifest_path = self._write_policy_manifest("active.json", [policy, policy])
        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            second_id = self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="validated")
            first_stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready-one")
            second_stage = self._write_stage("tv/Show/Season 1/Two.mkv", b"ready-two")
            self._insert_artifact(connection, first_id, first_stage, passed=True, manifest_path=manifest_path, item_index=0)
            self._insert_artifact(connection, second_id, second_stage, passed=True, manifest_path=manifest_path, item_index=1)
            active_manifest = self.root / "runs" / "episode-two-retry.json"
            active_manifest.write_text(json.dumps({"items": [{"library_item_id": second_id}]}))
            self._insert_encode_job(
                connection,
                job_id="episode-two-running",
                prefix="tv/Show/Season 1/Two.mkv",
                status="running",
                updated_at="2026-08-14T11:00:00+00:00",
                manifest_path=active_manifest,
            )

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("one")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, _prefix: {"accepted_policy_hash": self._policy_hash(policy)},
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(first_stage, "tv/Show/Season 1/One.mkv"),
                self._manifest_item(second_stage, "tv/Show/Season 1/Two.mkv"),
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertTrue(result["ok"])
        published = promoted.call_args.args[2]["items"]
        self.assertEqual([item["rel_path"] for item in published], ["tv/Show/Season 1/One.mkv"])
        self.assertIn({"code": "season_active_encode_job", "count": 1, "next_action": "wait_for_encode_job"}, result["waiting"])

    def test_tv_season_fails_closed_when_an_active_encode_cannot_be_read(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready")
            self._insert_artifact(connection, item_id, stage, passed=True)
            self._insert_encode_job(
                connection,
                job_id="unreadable-running",
                prefix="tv/Show/Season 1",
                status="running",
                updated_at="2026-08-14T11:00:00+00:00",
            )

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("unused")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, _prefix: {"accepted_policy_hash": "unused"},
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [self._manifest_item(stage, "tv/Show/Season 1/One.mkv")],
            promote_manifest_items_fn=promoted,
        )

        self.assertFalse(result["ok"])
        self.assertIn("season_active_encode_unreadable", {blocker["code"] for blocker in result["blockers"]})
        promoted.assert_not_called()

    def test_tv_season_fails_closed_when_policy_gate_is_unavailable(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            second_id = self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="validated")
            first_stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready-one")
            second_stage = self._write_stage("tv/Show/Season 1/Two.mkv", b"ready-two")
            self._insert_artifact(connection, first_id, first_stage, passed=True)
            self._insert_artifact(connection, second_id, second_stage, passed=True)

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("one"), Path("two")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(first_stage, "tv/Show/Season 1/One.mkv"),
                self._manifest_item(second_stage, "tv/Show/Season 1/Two.mkv"),
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertFalse(result["ok"])
        self.assertIn("season_policy_gate_unavailable", {blocker["code"] for blocker in result["blockers"]})
        promoted.assert_not_called()

    def test_tv_season_publishes_only_files_made_under_an_approved_policy(self) -> None:
        approved_policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        other_policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 91}}
        unapproved_manifest = self._write_policy_manifest("unapproved.json", [approved_policy, other_policy])
        approved_run_manifest = self._write_policy_manifest(
            "approved-run.json",
            [other_policy],
            selection={"production_approval_contract": {"schema_version": 1, "policy_hash": "earlier"}},
        )
        stages = {}
        with open_db(self.config.paths.db_path) as connection:
            for name, manifest_path, index in (
                    ("One", unapproved_manifest, 0),
                    ("Two", unapproved_manifest, 1),
                    ("Three", approved_run_manifest, 0),
            ):
                rel_path = f"tv/Show/Season 1/{name}.mkv"
                item_id = self._insert_item(connection, rel_path, status="validated")
                stages[rel_path] = self._write_stage(rel_path, name.encode())
                self._insert_artifact(
                    connection,
                    item_id,
                    stages[rel_path],
                    passed=True,
                    manifest_path=manifest_path,
                    item_index=index,
                )

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("one"), Path("three")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, _prefix: {
                "accepted_policy_hash": self._policy_hash(approved_policy),
            },
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(stage, rel_path) for rel_path, stage in stages.items()
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertTrue(result["ok"])
        published = [item["rel_path"] for item in promoted.call_args.args[2]["items"]]
        self.assertEqual(published, ["tv/Show/Season 1/One.mkv", "tv/Show/Season 1/Three.mkv"])
        self.assertIn(
            {"code": "season_policy_not_approved", "count": 1, "next_action": "approve_matching_policy_or_recreate_outputs"},
            result["waiting"],
        )

    def test_tv_season_accepts_matching_policy_from_show_approval(self) -> None:
        policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        manifest_path = self.root / "runs" / "one-policy.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "items": [
                {"resolved_policy": policy},
                {"resolved_policy": policy},
            ]
        }))
        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            second_id = self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="validated")
            first_stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready-one")
            second_stage = self._write_stage("tv/Show/Season 1/Two.mkv", b"ready-two")
            self._insert_artifact(
                connection,
                first_id,
                first_stage,
                passed=True,
                manifest_path=manifest_path,
                item_index=0,
            )
            self._insert_artifact(
                connection,
                second_id,
                second_stage,
                passed=True,
                manifest_path=manifest_path,
                item_index=1,
            )

        promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("one"), Path("two")], held=[]))
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, prefix: (
                {"accepted_policy_hash": self._policy_hash(policy)}
                if prefix == "tv/Show"
                else None
            ),
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(first_stage, "tv/Show/Season 1/One.mkv"),
                self._manifest_item(second_stage, "tv/Show/Season 1/Two.mkv"),
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertTrue(result["ok"])
        promoted.assert_called_once()

    def test_movie_and_exact_file_scopes_remain_item_granular(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            movie_id = self._insert_item(connection, "movies/Film.mkv", status="validated")
            other_id = self._insert_item(connection, "other/Loose.mkv", status="validated")
            movie_stage = self._write_stage("movies/Film.mkv", b"movie")
            other_stage = self._write_stage("other/Loose.mkv", b"other")
            self._insert_artifact(connection, movie_id, movie_stage, passed=True)
            self._insert_artifact(connection, other_id, other_stage, passed=True)

        for item_prefix, item_stage in (("movies/Film.mkv", movie_stage), ("other/Loose.mkv", other_stage)):
            promoted = Mock(return_value=PromotionResult(promoted_paths=[Path("promoted")], held=[]))
            result = promote_folder_outputs_action(
                self.config,
                item_prefix,
                load_folder_staged_items_fn=lambda *_args, item_stage=item_stage, item_prefix=item_prefix, **_kwargs: [
                    self._manifest_item(item_stage, item_prefix),
                ],
                promote_manifest_items_fn=promoted,
            )
            self.assertTrue(result["ok"])
            promoted.assert_called_once()

    def _config(self) -> MediaforceConfig:
        return MediaforceConfig(
            raw={
                "media": {
                    "source_roots": {
                        "tv": str(self.root / "source/tv"),
                        "movies": str(self.root / "source/movies"),
                        "other": str(self.root / "source/other"),
                    },
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                    "output_container": "mkv",
                },
                "remote_hosts": [{"key": "remote-a", "staging_root": str(self.root / "remote-staging")}],
            },
            paths=ConfigPaths(
                project_root=self.root,
                config_path=self.root / "config.toml",
                db_path=self.root / "library.sqlite3",
                run_manifest_dir=self.root / "runs",
                web_state_dir=self.root / "web",
                review_dir=self.root / "review",
                runtime_settings_path=self.root / "runtime.json",
                runtime_reservation_dir=self.root / "reservations",
            ),
        )

    def _insert_item(self, connection: DBClient, rel_path: str, *, status: str) -> int:
        now = datetime.now(tz=UTC).isoformat()
        path = Path(rel_path)
        source = self.root / "source" / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        result = connection.execute(
            library_items.insert().values(
                source_path=str(source),
                rel_path=rel_path,
                media_root=path.parts[0],
                parent_dir=str(path.parent),
                file_name=path.name,
                container=".mkv",
                size_bytes=source.stat().st_size,
                mtime_ns=source.stat().st_mtime_ns,
                fingerprint=f"fingerprint-{path.name}",
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                last_scan_id="scan-test",
                discovered_at=now,
                last_seen_at=now,
                updated_at=now,
                status=status,
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _insert_artifact(
            connection: DBClient,
            item_id: int,
            path: Path,
            *,
            passed: bool | None = None,
            size_bytes: int | None = None,
            mtime_ns: int | None = None,
            encode_host_key: str | None = None,
            encode_media_access: str | None = None,
            manifest_path: Path | None = None,
            item_index: int | None = None,
    ) -> None:
        values: dict[str, object] = {
            "library_item_id": item_id,
            "staging_path": str(path),
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "encode_host_key": encode_host_key,
            "encode_media_access": encode_media_access,
            "manifest_path": str(manifest_path) if manifest_path is not None else None,
            "item_index": item_index,
        }
        if path.exists() and size_bytes is None:
            size_bytes = path.stat().st_size
        if path.exists() and mtime_ns is None:
            mtime_ns = path.stat().st_mtime_ns
        if size_bytes is not None:
            values["staging_size_bytes"] = size_bytes
        if mtime_ns is not None:
            values["staging_mtime_ns"] = mtime_ns
        if passed is not None:
            values["validation_json"] = json.dumps({"passed": passed})
            values["validated_at"] = datetime.now(tz=UTC).isoformat()
        connection.execute(staged_artifacts.insert().values(**values))

    def _write_stage(self, rel_path: str, content: bytes) -> Path:
        path = self.root / "staging" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    @staticmethod
    def _insert_encode_job(
            connection: DBClient,
            *,
            job_id: str,
            prefix: str,
            status: str,
            updated_at: str,
            manifest_path: Path | None = None,
    ) -> None:
        connection.execute(encode_jobs.insert().values(
            job_id=job_id,
            prefix=prefix,
            job_kind="folder",
            status=status,
            manifest_path=str(manifest_path or "/tmp/web-smoke-manifest.json"),
            item_count=1,
            host_json="{}",
            last_host_json="{}",
            created_at=updated_at,
            updated_at=updated_at,
        ))

    def _manifest_item(self, stage: Path, rel_path: str) -> dict[str, object]:
        with open_db(self.config.paths.db_path) as connection:
            item_id = connection.execute(
                select(library_items.c.id).where(library_items.c.rel_path == rel_path)
            ).scalar_one()
        return {
            "library_item_id": item_id,
            "source_path": str(self.root / "source" / rel_path),
            "staging_path": str(stage),
            "rel_path": rel_path,
        }

    def _write_policy_manifest(
            self,
            name: str,
            policies: list[dict[str, object]],
            *,
            selection: dict[str, object] | None = None,
    ) -> Path:
        manifest_path = self.root / "runs" / name
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"items": [{"resolved_policy": policy} for policy in policies]}
        if selection is not None:
            payload["selection"] = selection
        manifest_path.write_text(json.dumps(payload))
        return manifest_path

    @staticmethod
    def _policy_hash(policy: dict[str, object]) -> str:
        encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]
