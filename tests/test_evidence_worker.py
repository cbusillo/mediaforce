import json
import sqlite3
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from sqlalchemy import select, update

from mediaforce.cli import main as cli_main
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.core.process_control import ProcessCancelledError
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, analyze_cadence
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.library.evidence_queue import cancel_evidence_queue, claim_next_evidence_work, \
    evidence_queue_summary, mark_evidence_attempt_started, pause_evidence_queue, recover_evidence_queue, \
    queue_decision_evidence_work, resume_evidence_queue, start_evidence_work
from mediaforce.library.evidence_state import EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, \
    load_library_item_evidence_states, rebuild_library_item_evidence_states, sync_library_item_evidence_state
from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once
from mediaforce.web.runtime.decision_evidence import cadence_evidence_blocker, cadence_safety_partition


class EvidenceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        self.media_root = self.project_root / "tv"
        self.media_root.mkdir()
        self.config = MediaforceConfig(
            raw={
                "media": {
                    "libraries": [
                        {
                            "key": "tv",
                            "path": str(self.media_root),
                            "type": "tv",
                            "availability": "production",
                        }
                    ]
                }
            },
            paths=ConfigPaths(
                project_root=self.project_root,
                config_path=self.project_root / "config.toml",
                db_path=self.project_root / "state" / "library.sqlite3",
                run_manifest_dir=self.project_root / "state" / "runs",
                web_state_dir=self.project_root / "state" / "web",
                review_dir=self.project_root / "state" / "review",
                runtime_settings_path=self.project_root / "state" / "settings.json",
            ),
        )

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_explicit_scope_queues_only_noncurrent_independent_kinds(self) -> None:
        item_id, _path = self._insert_item(
            1,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=None,
        )

        with open_db(self.config.paths.db_path) as connection:
            summary = start_evidence_work(connection, self.config, "tv/show/item-1.mkv")
            rows = connection.execute(
                select(
                    library_item_evidence_state.c.evidence_kind,
                    library_item_evidence_state.c.work_status,
                ).where(library_item_evidence_state.c.library_item_id == item_id)
            ).mappings().fetchall()

        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["status"], "paused")
        self.assertTrue(summary["is_paused"])
        self.assertEqual(summary["scope"]["work_limit"], 25)
        self.assertEqual(
            [(row["evidence_kind"], row["work_status"]) for row in rows],
            [(CADENCE_EVIDENCE_KIND, None), (MEDIA_FINGERPRINT_EVIDENCE_KIND, "queued")],
        )

    def test_claim_is_single_concurrency_and_pause_prevents_new_claim(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(connection, self.config, "tv")
            pause_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            paused_claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            first_claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)
        with open_db(self.config.paths.db_path) as connection:
            second_claim = claim_next_evidence_work(connection, worker_id="worker-b", lease_seconds=30)

        self.assertIsNone(paused_claim)
        self.assertIsNotNone(first_claim)
        self.assertIsNone(second_claim)

    def test_batch_limit_caps_root_scope(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        self._insert_item(2, cadence_summary_json=None, media_fingerprint_json=None)

        with open_db(self.config.paths.db_path) as connection:
            summary = start_evidence_work(connection, self.config, "tv", limit=1)

        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["counts"], {"queued": 1})
        self.assertEqual(summary["scope"]["work_limit"], 1)

    def test_fingerprint_preparation_starts_with_bounded_technical_representatives(self) -> None:
        item_ids = [
            self._insert_item(
                index,
                cadence_summary_json=self._cadence_summary_json(),
                media_fingerprint_json=None,
            )[0]
            for index in range(1, 4)
        ]

        with open_db(self.config.paths.db_path) as connection:
            summary = start_evidence_work(
                connection,
                self.config,
                "tv/show",
                evidence_kinds=[MEDIA_FINGERPRINT_EVIDENCE_KIND],
                limit=25,
            )
            queued_rows = connection.execute(
                select(
                    library_item_evidence_state.c.library_item_id,
                    library_item_evidence_state.c.work_priority,
                    library_item_evidence_state.c.work_reason,
                ).where(
                    library_item_evidence_state.c.library_item_id.in_(item_ids),
                    library_item_evidence_state.c.evidence_kind == MEDIA_FINGERPRINT_EVIDENCE_KIND,
                    library_item_evidence_state.c.work_status == "queued",
                )
            ).mappings().fetchall()

        acquisition = summary["scope"]["fingerprint_acquisition"]
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(acquisition["phase"], "technical_representatives")
        self.assertEqual(acquisition["candidate_count"], 1)
        self.assertEqual(acquisition["intentionally_unqueued_count"], 2)
        self.assertEqual(
            [
                (row["work_priority"], row["work_reason"])
                for row in queued_rows
            ],
            [(70, "representative_technical_coverage")],
        )

    def test_decision_evidence_prepares_only_the_affected_item(self) -> None:
        first_item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        second_item_id, _path = self._insert_item(2, cadence_summary_json=None, media_fingerprint_json=None)

        with open_db(self.config.paths.db_path) as connection:
            prepared = queue_decision_evidence_work(
                connection,
                self.config,
                "tv/show/item-2.mkv",
                library_item_ids=[second_item_id],
                evidence_kind=CADENCE_EVIDENCE_KIND,
                work_reason="sample_safety",
            )
            rows = connection.execute(
                select(
                    library_item_evidence_state.c.library_item_id,
                    library_item_evidence_state.c.evidence_kind,
                    library_item_evidence_state.c.work_status,
                    library_item_evidence_state.c.work_priority,
                    library_item_evidence_state.c.work_reason,
                )
                .where(library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND)
                .order_by(library_item_evidence_state.c.library_item_id)
            ).mappings().fetchall()

        self.assertEqual(prepared["required_count"], 1)
        self.assertEqual(prepared["work"]["status"], "paused")
        self.assertEqual(prepared["work"]["item_count"], 1)
        self.assertEqual(
            [dict(row) for row in rows],
            [
                {
                    "library_item_id": first_item_id,
                    "evidence_kind": CADENCE_EVIDENCE_KIND,
                    "work_status": None,
                    "work_priority": 100,
                    "work_reason": None,
                },
                {
                    "library_item_id": second_item_id,
                    "evidence_kind": CADENCE_EVIDENCE_KIND,
                    "work_status": "queued",
                    "work_priority": 0,
                    "work_reason": "sample_safety",
                },
            ],
        )

    def test_cadence_decision_blocker_queues_only_missing_safety_evidence(self) -> None:
        first_item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        second_item_id, _path = self._insert_item(
            2,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=None,
        )

        with open_db(self.config.paths.db_path) as connection:
            blocker = cadence_evidence_blocker(
                connection,
                self.config,
                "tv/show",
                library_item_ids=[first_item_id, second_item_id],
                decision_label="queueing production",
                work_reason="encode_safety",
            )
            rows = connection.execute(
                select(
                    library_item_evidence_state.c.library_item_id,
                    library_item_evidence_state.c.work_status,
                    library_item_evidence_state.c.work_reason,
                )
                .where(library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND)
                .order_by(library_item_evidence_state.c.library_item_id)
            ).mappings().fetchall()

        self.assertEqual(blocker["code"], "cadence_analysis_required")
        self.assertEqual(blocker["affected_item_count"], 1)
        self.assertEqual(blocker["next_route"], "/ops")
        self.assertEqual(
            [dict(row) for row in rows],
            [
                {
                    "library_item_id": first_item_id,
                    "work_status": "queued",
                    "work_reason": "encode_safety",
                },
                {
                    "library_item_id": second_item_id,
                    "work_status": None,
                    "work_reason": None,
                },
            ],
        )

    def test_cadence_decision_blocker_does_not_requeue_measured_blocked_cadence(self) -> None:
        item_id, _path = self._insert_item(
            1,
            cadence_summary_json=self._blocked_cadence_summary_json(),
            media_fingerprint_json=None,
        )

        with open_db(self.config.paths.db_path) as connection:
            blocker = cadence_evidence_blocker(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                library_item_ids=[item_id],
                decision_label="queueing this sample",
                work_reason="sample_safety",
            )
            row = connection.execute(
                select(
                    library_item_evidence_state.c.work_status,
                    library_item_evidence_state.c.work_reason,
                ).where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
            ).mappings().one()

        self.assertEqual(blocker["code"], "cadence_unresolved")
        self.assertEqual(blocker["affected_item_count"], 1)
        self.assertEqual(dict(row), {"work_status": None, "work_reason": None})

    def test_cadence_safety_partition_separates_cleared_blocked_and_required_items(self) -> None:
        cleared_item_id, _path = self._insert_item(
            1,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=None,
        )
        blocked_item_id, _path = self._insert_item(
            2,
            cadence_summary_json=self._blocked_cadence_summary_json(),
            media_fingerprint_json=None,
        )
        required_item_id, _path = self._insert_item(
            3,
            cadence_summary_json=None,
            media_fingerprint_json=None,
        )

        with open_db(self.config.paths.db_path) as connection:
            partition = cadence_safety_partition(
                connection,
                library_item_ids=[cleared_item_id, blocked_item_id, required_item_id],
                synchronize=True,
            )

        self.assertEqual(partition.cleared_item_ids, frozenset({cleared_item_id}))
        self.assertEqual(partition.blocked_item_ids, frozenset({blocked_item_id}))
        self.assertEqual(partition.evidence_required_item_ids, frozenset({required_item_id}))
        self.assertEqual(
            partition.excluded_item_ids,
            frozenset({blocked_item_id, required_item_id}),
        )

    def test_cadence_safety_partition_projects_missing_state_without_writing_on_read(self) -> None:
        item_id, _path = self._insert_item(
            1,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=None,
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                library_item_evidence_state.delete().where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
            )
            partition = cadence_safety_partition(
                connection,
                library_item_ids=[item_id],
                synchronize=False,
            )
            persisted_row = connection.execute(
                select(library_item_evidence_state.c.library_item_id).where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
            ).fetchone()

        self.assertEqual(partition.cleared_item_ids, frozenset({item_id}))
        self.assertIsNone(persisted_row)

    def test_decision_evidence_is_claimed_before_manual_backfill(self) -> None:
        first_item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        second_item_id, _path = self._insert_item(2, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            queue_decision_evidence_work(
                connection,
                self.config,
                "tv/show/item-2.mkv",
                library_item_ids=[second_item_id],
                evidence_kind=CADENCE_EVIDENCE_KIND,
                work_reason="encode_safety",
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)

        self.assertIsNotNone(claim)
        self.assertEqual(claim.library_item_id, second_item_id)
        self.assertNotEqual(claim.library_item_id, first_item_id)

    def test_decision_request_does_not_reset_same_source_failure_budget(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            source_fingerprint = connection.execute(
                select(library_items.c.content_version_fingerprint).where(library_items.c.id == item_id)
            ).scalar_one()
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
                .values(
                    work_batch_id="old-batch",
                    work_status="failed",
                    work_source_fingerprint=source_fingerprint,
                    attempt_count=3,
                    last_error="decoder failed",
                )
            )
            prepared = queue_decision_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                library_item_ids=[item_id],
                evidence_kind=CADENCE_EVIDENCE_KIND,
                work_reason="sample_safety",
            )
            row = connection.execute(
                select(
                    library_item_evidence_state.c.work_status,
                    library_item_evidence_state.c.work_priority,
                    library_item_evidence_state.c.attempt_count,
                    library_item_evidence_state.c.last_error,
                ).where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
            ).mappings().one()

        self.assertEqual(prepared["work"]["status"], "idle")
        self.assertEqual(prepared["prepared_count"], 0)
        self.assertEqual(prepared["terminal_failure_count"], 1)
        self.assertEqual(
            dict(row),
            {
                "work_status": "failed",
                "work_priority": 0,
                "attempt_count": 3,
                "last_error": "decoder failed",
            },
        )

        with open_db(self.config.paths.db_path) as connection:
            blocker = cadence_evidence_blocker(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                library_item_ids=[item_id],
                decision_label="queueing this sample",
                work_reason="sample_safety",
            )

        self.assertEqual(blocker["code"], "cadence_analysis_failed")
        self.assertEqual(blocker["affected_item_count"], 1)

    def test_zero_candidate_batch_completes_without_claims(self) -> None:
        self._insert_item(
            1,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=self._fingerprint_summary_json(),
        )

        with open_db(self.config.paths.db_path) as connection:
            summary = start_evidence_work(connection, self.config, "tv/show/item-1.mkv")

        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["item_count"], 0)
        self.assertEqual(summary["remaining_count"], 0)

    def test_restart_recovery_requeues_owned_work(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)
        with open_db(self.config.paths.db_path) as connection:
            recovered = recover_evidence_queue(connection, reclaim_all_running=True)
            summary = evidence_queue_summary(connection)

        self.assertIsNotNone(claim)
        self.assertEqual(recovered, 1)
        self.assertEqual(summary["counts"], {"queued": 1})

    def test_expired_lease_recovers_without_forcing_live_claims(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        claimed_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(
                connection,
                worker_id="worker-a",
                lease_seconds=10,
                now=claimed_at,
            )
        with open_db(self.config.paths.db_path) as connection:
            live_recovery = recover_evidence_queue(
                connection,
                now=claimed_at + timedelta(seconds=5),
            )
            expired_recovery = recover_evidence_queue(
                connection,
                now=claimed_at + timedelta(seconds=11),
            )

        self.assertIsNotNone(claim)
        self.assertEqual(live_recovery, 0)
        self.assertEqual(expired_recovery, 1)

    def test_classification_only_work_is_claimed_before_media_analysis(self) -> None:
        analysis_item_id, _path = self._insert_item(
            1,
            cadence_summary_json=None,
            media_fingerprint_json=self._fingerprint_summary_json(),
        )
        classification_item_id, _path = self._insert_item(
            2,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=self._fingerprint_summary_json(),
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == classification_item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
                .values(policy_hash="sha256:old-policy")
            )
            rebuild_library_item_evidence_states(connection, library_item_ids=[classification_item_id])
            start_evidence_work(
                connection,
                self.config,
                "tv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)

        self.assertIsNotNone(claim)
        self.assertNotEqual(analysis_item_id, classification_item_id)
        self.assertEqual(claim.library_item_id, classification_item_id)
        self.assertEqual(claim.evidence_state, EVIDENCE_STATE_CLASSIFICATION_REQUIRED)

    def test_attempt_start_rejects_lost_claim_ownership(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)
        self.assertIsNotNone(claim)

        with open_db(self.config.paths.db_path) as connection:
            active_claim = mark_evidence_attempt_started(
                connection,
                replace(claim, worker_id="worker-b"),
            )
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]

        self.assertIsNone(active_claim)
        self.assertEqual(state["attempt_count"], 0)

    def test_policy_only_reclassification_uses_no_media_analyzer(self) -> None:
        item_id, _path = self._insert_item(
            1,
            cadence_summary_json=self._cadence_summary_json(),
            media_fingerprint_json=self._fingerprint_summary_json(),
        )
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
                .values(policy_hash="sha256:old-policy")
            )
            rebuild_library_item_evidence_states(connection, library_item_ids=[item_id])
            states = load_library_item_evidence_states(connection, item_id)
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        analyze_mock = Mock(side_effect=AssertionError("unexpected media analysis"))

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(analyze_mock),
        )

        with open_db(self.config.paths.db_path) as connection:
            refreshed = load_library_item_evidence_states(connection, item_id)
            summary = evidence_queue_summary(connection)
        self.assertEqual(states[CADENCE_EVIDENCE_KIND]["state"], EVIDENCE_STATE_CLASSIFICATION_REQUIRED)
        self.assertTrue(processed)
        analyze_mock.assert_not_called()
        self.assertEqual(refreshed[CADENCE_EVIDENCE_KIND]["state"], EVIDENCE_STATE_CURRENT)
        self.assertEqual(summary["status"], "completed")

    def test_concurrently_resolved_evidence_skips_media_analysis(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == item_id)
                .values(cadence_summary_json=self._cadence_summary_json())
            )
            item = connection.execute(
                select(library_items).where(library_items.c.id == item_id)
            ).mappings().one()
            sync_library_item_evidence_state(
                connection,
                item,
                CADENCE_EVIDENCE_KIND,
                preserve_source_identity=False,
                preserve_policy_identity=False,
            )
        analyze_mock = Mock(side_effect=AssertionError("unexpected media analysis"))

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(analyze_mock),
        )

        with open_db(self.config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
        self.assertTrue(processed)
        analyze_mock.assert_not_called()
        self.assertEqual(state["work_status"], "completed")
        self.assertEqual(state["attempt_count"], 0)

    def test_analysis_runs_without_sqlite_write_transaction_and_commits_current_evidence(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)

        def analyze_without_lock(
                _path: Path,
                _kind: str,
                *,
                process_controller: object,
        ) -> dict[str, object]:
            writer = sqlite3.connect(self.config.paths.db_path, timeout=0.1)
            try:
                writer.execute("BEGIN IMMEDIATE")
                writer.commit()
            finally:
                writer.close()
            return json.loads(self._cadence_summary_json())

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(analyze_without_lock),
        )

        with open_db(self.config.paths.db_path) as connection:
            states = load_library_item_evidence_states(connection, item_id)
            item = connection.execute(
                select(library_items.c.cadence_summary_json).where(library_items.c.id == item_id)
            ).scalar_one()
            summary = evidence_queue_summary(connection)
        self.assertTrue(processed)
        self.assertEqual(states[CADENCE_EVIDENCE_KIND]["state"], EVIDENCE_STATE_CURRENT)
        self.assertEqual(states[CADENCE_EVIDENCE_KIND]["attempt_count"], 0)
        self.assertEqual(item, self._cadence_summary_json())
        self.assertEqual(summary["status"], "completed")

    def test_source_identity_change_discards_completed_analysis(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)

        def change_catalog_source_identity(
                _path: Path,
                _kind: str,
                *,
                process_controller: object,
        ) -> dict[str, object]:
            with open_db(self.config.paths.db_path) as connection:
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == item_id)
                    .values(content_version_fingerprint="changed-after-claim")
                )
            return json.loads(self._cadence_summary_json())

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(change_catalog_source_identity),
        )

        with open_db(self.config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
            canonical_json = connection.execute(
                select(library_items.c.cadence_summary_json).where(library_items.c.id == item_id)
            ).scalar_one()
        self.assertTrue(processed)
        self.assertIsNone(canonical_json)
        self.assertEqual(state["work_status"], "failed")
        self.assertIn("Source changed", state["last_error"])

    def test_unavailable_source_does_not_consume_attempt(self) -> None:
        item_id, source_path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        source_path.unlink()
        source_path.parent.rmdir()
        self.media_root.rmdir()
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(Mock(side_effect=AssertionError("unexpected media analysis"))),
        )

        with open_db(self.config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
        self.assertTrue(processed)
        self.assertEqual(state["work_status"], "waiting_source")
        self.assertEqual(state["attempt_count"], 0)

    def test_missing_file_in_available_root_fails_without_retry_loop(self) -> None:
        item_id, source_path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        source_path.unlink()
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        analyze_mock = Mock(side_effect=AssertionError("unexpected media analysis"))

        processed = process_evidence_queue_once(
            config_path=self.config.paths.config_path,
            deps=self._deps(analyze_mock),
        )

        with open_db(self.config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
        self.assertTrue(processed)
        analyze_mock.assert_not_called()
        self.assertEqual(state["work_status"], "failed")
        self.assertEqual(state["attempt_count"], 0)

    def test_failures_back_off_then_become_terminal(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        deps = self._deps(Mock(side_effect=RuntimeError("fixture failure")), max_attempts=2)

        process_evidence_queue_once(config_path=self.config.paths.config_path, deps=deps)
        with open_db(self.config.paths.db_path) as connection:
            first = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
            connection.execute(
                update(library_item_evidence_state)
                .where(
                    library_item_evidence_state.c.library_item_id == item_id,
                    library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                )
                .values(retry_not_before="2000-01-01T00:00:00+00:00")
            )
        process_evidence_queue_once(config_path=self.config.paths.config_path, deps=deps)

        with open_db(self.config.paths.db_path) as connection:
            second = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
            summary = evidence_queue_summary(connection)
        self.assertEqual(first["work_status"], "retry_wait")
        self.assertEqual(first["attempt_count"], 1)
        self.assertEqual(second["work_status"], "failed")
        self.assertEqual(second["attempt_count"], 2)
        self.assertEqual(summary["status"], "completed_with_errors")

    def test_cancel_terminates_active_claim_and_restores_attempt(self) -> None:
        item_id, _path = self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        analyzer_started = threading.Event()

        def cancellable_analyzer(
                _path: Path,
                _kind: str,
                *,
                process_controller: object,
        ) -> dict[str, object]:
            analyzer_started.set()
            while True:
                process_controller.throw_if_cancelled()
                time.sleep(0.005)

        worker = threading.Thread(
            target=process_evidence_queue_once,
            kwargs={
                "config_path": self.config.paths.config_path,
                "deps": self._deps(cancellable_analyzer, heartbeat_seconds=0.01),
            },
        )
        worker.start()
        self.assertTrue(analyzer_started.wait(timeout=2.0))
        with open_db(self.config.paths.db_path) as connection:
            cancel_evidence_queue(connection)
        worker.join(timeout=2.0)

        with open_db(self.config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
            summary = evidence_queue_summary(connection)
        self.assertFalse(worker.is_alive())
        self.assertEqual(state["work_status"], "cancelled")
        self.assertEqual(state["attempt_count"], 0)
        self.assertEqual(summary["status"], "cancelled")

    def test_cancel_requested_batch_cannot_be_resumed(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with open_db(self.config.paths.db_path) as connection:
            start_evidence_work(
                connection,
                self.config,
                "tv/show/item-1.mkv",
                evidence_kinds=[CADENCE_EVIDENCE_KIND],
            )
            resume_evidence_queue(connection)
        with open_db(self.config.paths.db_path) as connection:
            claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=30)
        self.assertIsNotNone(claim)
        with open_db(self.config.paths.db_path) as connection:
            cancelling = cancel_evidence_queue(connection)
            resumed = resume_evidence_queue(connection)

        self.assertEqual(cancelling["status"], "cancel_requested")
        self.assertEqual(resumed["status"], "cancel_requested")
        self.assertTrue(resumed["cancel_requested"])

    def test_cli_starts_paused_batch_and_runs_without_outer_db_transaction(self) -> None:
        self._insert_item(1, cadence_summary_json=None, media_fingerprint_json=None)
        with patch("mediaforce.cli.load_config", return_value=self.config), patch(
            "mediaforce.cli.purge_transient_artifacts"
        ), patch("builtins.print") as print_line:
            exit_code = cli_main([
                "--config",
                str(self.config.paths.config_path),
                "evidence",
                "start",
                "tv/show/item-1.mkv",
                "--kind",
                CADENCE_EVIDENCE_KIND,
                "--limit",
                "1",
            ])
        payload = json.loads(str(print_line.call_args.args[0]))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "paused")
        self.assertEqual(payload["item_count"], 1)

        worker_summary = {"status": "paused", "processed_count": 0, "stop_reason": "blocked"}
        with patch("mediaforce.cli.load_config", return_value=self.config), patch(
            "mediaforce.cli.purge_transient_artifacts"
        ), patch(
            "mediaforce.cli.run_evidence_queue_until_blocked",
            return_value=worker_summary,
        ) as run_worker, patch(
            "mediaforce.cli.open_db",
            side_effect=AssertionError("run command must not hold an outer database transaction"),
        ), patch("builtins.print"):
            exit_code = cli_main([
                "--config",
                str(self.config.paths.config_path),
                "evidence",
                "run",
                "--max-items",
                "1",
            ])

        self.assertEqual(exit_code, 0)
        run_worker.assert_called_once_with(
            config_path=self.config.paths.config_path,
            config=self.config,
            max_work_items=1,
            max_seconds=None,
        )

    def _insert_item(
            self,
            index: int,
            *,
            cadence_summary_json: str | None,
            media_fingerprint_json: str | None,
    ) -> tuple[int, Path]:
        source_path = self.media_root / "show" / f"item-{index}.mkv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(f"fixture-{index}".encode())
        stat_result = source_path.stat()
        content_fingerprint = content_version_fingerprint(source_path, stat_result)
        now = "2026-07-19T12:00:00+00:00"
        with open_db(self.config.paths.db_path) as connection:
            result = connection.execute(
                library_items.insert().values(
                    source_path=str(source_path),
                    rel_path=f"tv/show/item-{index}.mkv",
                    media_root="tv",
                    parent_dir="tv/show",
                    file_name=f"item-{index}.mkv",
                    container=".mkv",
                    size_bytes=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    fingerprint=file_fingerprint(source_path, stat_result, 60.0),
                    duration_seconds=60.0,
                    video_codec="h264",
                    audio_track_count=1,
                    subtitle_track_count=0,
                    english_audio_count=1,
                    english_subtitle_count=0,
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    cadence_summary_json=cadence_summary_json,
                    media_fingerprint_json=media_fingerprint_json,
                    content_version_changed_at=now,
                    content_version_fingerprint=content_fingerprint,
                    status="discovered",
                    priority_score=0,
                    last_scan_id="fixture",
                    discovered_at=now,
                    last_seen_at=now,
                    updated_at=now,
                )
            )
            item_id = int(result.inserted_primary_key[0])
            rebuild_library_item_evidence_states(connection, library_item_ids=[item_id])
        return item_id, source_path

    def _deps(
            self,
            analyzer: object,
            *,
            max_attempts: int = 3,
            heartbeat_seconds: float = 0.05,
    ) -> EvidenceWorkerDeps:
        return EvidenceWorkerDeps(
            load_config=lambda _path: self.config,
            analyze_evidence=analyzer,
            logger=Mock(),
            lease_seconds=1,
            heartbeat_seconds=heartbeat_seconds,
            retry_base_delay_seconds=1,
            retry_max_delay_seconds=2,
            max_attempts=max_attempts,
            source_retry_delay_seconds=1,
        )

    @staticmethod
    def _cadence_summary_json() -> str:
        return json.dumps(
            analyze_cadence(
                Path("unused.mkv"),
                video_stream={
                    "field_order": "progressive",
                    "avg_frame_rate": "24/1",
                    "r_frame_rate": "24/1",
                    "time_base": "1/1000",
                },
                duration_seconds=60.0,
            ),
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _blocked_cadence_summary_json(cls) -> str:
        payload = json.loads(cls._cadence_summary_json())
        payload["decision"]["status"] = "blocked"
        payload["decision"]["reason"] = "fixture cadence requires operator review"
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _fingerprint_summary_json() -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "analysis": {
                    "sampled_frames": 120,
                    "coverage": 1.0,
                    "ranges": [],
                    "aggregate": {"dark_frame_fraction": 0.05},
                    "audio_probe": {"track_count": 0},
                    "tool": {
                        "name": "mediaforce.media_fingerprint",
                        "version": "1",
                        "ffmpeg_version": "ffmpeg fixture",
                    },
                },
                "decision": {"status": "measured"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )


if __name__ == "__main__":
    unittest.main()
