import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import encode_jobs, item_events, library_items, staged_artifacts
from mediaforce.encoding.staging import partial_output_path
from mediaforce.web.runtime.child_recovery import apply_child_recovery, preview_child_recovery
from mediaforce.web.runtime.encode_runtime import sync_encode_job_parent


NOW = "2026-09-09T12:00:00+00:00"
RECOVERED_AT = "2026-09-09T12:05:00+00:00"
APPROVAL = {"policy_hash": "approved-policy", "target": "production"}


class ChildRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_root = self.root / "source" / "tv"
        self.source_root.mkdir(parents=True)
        (self.root / "staging").mkdir()
        self.manifest_path = self.root / "runs" / "folder.json"
        self.manifest_path.parent.mkdir(parents=True)
        self.config = MediaforceConfig(
            raw={
                "media": {
                    "source_roots": {"tv": str(self.source_root)},
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                    "output_container": "mkv",
                }
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

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_apply_requeues_exact_ids_without_row_churn_and_retains_history(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=3)
            before = self._raw_jobs(connection)
            token = self._preview(connection, ["child-0", "child-2"])["token"]
            connection.commit()

            result = self._apply(connection, ["child-0", "child-2"], token)
            after = self._raw_jobs(connection)

            self.assertEqual(result["child_ids"], ["child-0", "child-2"])
            self.assertEqual(set(before), set(after))
            self.assertEqual(before["parent"], after["parent"])
            self.assertEqual(before["child-1"], after["child-1"])
            for child_id in ("child-0", "child-2"):
                row = after[child_id]
                self.assertEqual(row["status"], "queued")
                self.assertEqual(row["attempt_count"], before[child_id]["attempt_count"])
                self.assertEqual(row["host_cooldown_until"], before[child_id]["host_cooldown_until"])
                self.assertIsNone(row["leased_at"])
                self.assertIsNone(row["lease_expires_at"])
                self.assertIsNone(row["heartbeat_at"])
                self.assertIsNone(row["worker_id"])
            events = connection.execute(select(item_events).order_by(item_events.c.library_item_id)).mappings().all()
            self.assertEqual([event["event_type"] for event in events], ["targeted_child_recovery"] * 2)
            details = [json.loads(str(event["details_json"])) for event in events]
            self.assertEqual([detail["previous_attempt_count"] for detail in details], [3, 5])
            self.assertTrue(all(detail["previous_failure_kind"] == "host_configuration" for detail in details))

    def test_apply_is_all_or_nothing_when_parent_sync_fails(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=2)
            before = self._raw_jobs(connection)
            token = self._preview(connection, ["child-0", "child-1"])["token"]
            connection.commit()
            def fail_second(_connection: DBClient, _child: dict[str, Any]) -> None:
                raise RuntimeError("sync failed")

            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                self._apply(connection, ["child-0", "child-1"], token, sync_parent=fail_second)
            self.assertEqual(self._raw_jobs(connection), before)
            self.assertEqual(connection.execute(select(item_events)).all(), [])

    def test_apply_integrates_with_parent_aggregation_and_persists_audit(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=2, statuses=["needs_attention", "completed"])
            token = self._preview(connection, ["child-0"])["token"]
            connection.commit()

            self._apply(
                connection,
                ["child-0"],
                token,
                sync_parent=lambda current_connection, child: sync_encode_job_parent(
                    current_connection, child, SimpleNamespace(now_iso=lambda: RECOVERED_AT),
                ),
            )

            self.assertEqual(self._job(connection, "parent")["status"], "queued")
            event = connection.execute(select(item_events)).mappings().one()
            self.assertEqual(event["event_type"], "targeted_child_recovery")
            self.assertEqual(json.loads(str(event["details_json"]))["job_id"], "child-0")

    def test_apply_commits_on_context_exit_and_repeated_apply_rejects(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=1)
            token = self._preview(connection, ["child-0"])["token"]
            connection.commit()
            self._apply(connection, ["child-0"], token)
            self.assertTrue(connection.in_transaction())
        with open_db(self.config.paths.db_path) as connection:
            self.assertEqual(self._job(connection, "child-0")["status"], "queued")
            self.assertEqual(len(connection.execute(select(item_events)).all()), 1)
            connection.commit()
            self._assert_http(409, lambda: self._apply(connection, ["child-0"], token))
        with open_db(self.config.paths.db_path) as connection:
            self.assertEqual(self._job(connection, "child-0")["status"], "queued")
            self.assertEqual(len(connection.execute(select(item_events)).all()), 1)

    def test_apply_rejects_stale_selected_child_token(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=2)
            token = self._preview(connection, ["child-0"])["token"]
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "child-0").values(attempt_count=99))
            connection.commit()
            self._assert_http(409, lambda: self._apply(connection, ["child-0"], token))
            self.assertEqual(self._job(connection, "child-0")["status"], "needs_attention")

    def test_running_sibling_heartbeat_does_not_stale_selected_token(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=2, statuses=["needs_attention", "running"])
            token = self._preview(connection, ["child-0"])["token"]
            connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "child-1").values(
                heartbeat_at="2026-09-09T12:04:00+00:00", lease_expires_at="2026-09-09T12:10:00+00:00",
                updated_at="2026-09-09T12:04:00+00:00",
            ))
            connection.commit()
            result = self._apply(connection, ["child-0"], token)
            self.assertTrue(result["ok"])
            self.assertEqual(self._job(connection, "child-1")["status"], "running")

    def test_preview_accepts_one_selection_from_parent_with_more_than_100_children(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=101, statuses=["needs_attention"] + ["failed"] * 100)
            preview = self._preview(connection, ["child-0"])
            self.assertEqual(preview["child_ids"], ["child-0"])

    def test_explicit_nonempty_unique_child_ids_are_required(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=1)
            for child_ids in ([], [""], ["child-0", "child-0"]):
                with self.subTest(child_ids=child_ids):
                    self._assert_http(400, lambda child_ids=child_ids: self._preview(connection, child_ids))

    def test_invalid_manifest_indexes_fail_closed(self) -> None:
        invalid_indexes: tuple[Any, ...] = (None, [], [True], ["0"], [-1], [1], [0, 0])
        for indexes in invalid_indexes:
            with self.subTest(indexes=indexes), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=1, indexes=[indexes])
                self._assert_http(409, lambda: self._preview(connection, ["child-0"]))
            self._reset_database()

    def test_overlapping_selected_and_active_ownership_is_rejected(self) -> None:
        scenarios = [
            (["needs_attention", "failed"], [[0], [0]], ["child-0", "child-1"]),
            (["needs_attention", "running"], [[0], [0]], ["child-0"]),
        ]
        for statuses, indexes, selected in scenarios:
            with self.subTest(statuses=statuses), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=2, statuses=statuses, indexes=indexes, item_count=1)
                self._assert_http(409, lambda: self._preview(connection, selected))
            self._reset_database()

    def test_ineligible_failure_kinds_and_statuses_are_rejected_deterministically(self) -> None:
        cases = (("failed", "unknown"), ("failed", "deterministic_search_failure"), ("completed", "host_configuration"))
        for status, failure_kind in cases:
            with self.subTest(status=status, failure_kind=failure_kind), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=1, statuses=[status], failure_kinds=[failure_kind])
                self._assert_http(409, lambda: self._preview(connection, ["child-0"]))
            self._reset_database()

    def test_selected_child_with_any_lease_ownership_is_rejected(self) -> None:
        ownership = ("process_pid", "leased_at", "lease_expires_at", "heartbeat_at", "worker_id")
        for field in ownership:
            with self.subTest(field=field), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=1)
                value: Any = 4000 if field == "process_pid" else ("worker-0" if field == "worker_id" else NOW)
                connection.execute(encode_jobs.update().where(encode_jobs.c.job_id == "child-0").values(**{field: value}))
                self._assert_http(409, lambda: self._preview(connection, ["child-0"]))
            self._reset_database()

    def test_existing_final_and_partial_outputs_are_rejected_including_episode_20(self) -> None:
        for partial in (False, True):
            with self.subTest(partial=partial), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=1, rel_paths=["Show/Season 01/Show - S01E20.mkv"])
                manifest = self._manifest()
                output = Path(manifest["items"][0]["staging_path"])
                target = partial_output_path(output) if partial else output
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"unfinished")
                self._assert_http(409, lambda: self._preview(connection, ["child-0"]))
                self.assertTrue(target.is_file())
            self._reset_database()

    def test_same_size_source_mtime_change_stales_preview_and_apply(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=1)
            token = self._preview(connection, ["child-0"])["token"]
            source = Path(self._manifest()["items"][0]["source_path"])
            stat = source.stat()
            os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            connection.commit()
            self._assert_http(409, lambda: self._apply(connection, ["child-0"], token))
            self.assertEqual(self._job(connection, "child-0")["status"], "needs_attention")

    def test_staged_artifact_blocks_recovery_without_mutation(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_ids = self._seed(connection, count=1)
            connection.execute(staged_artifacts.insert().values(
                library_item_id=item_ids[0], staging_path=str(self.root / "staging/already.mkv"), updated_at=NOW,
            ))
            before = self._raw_jobs(connection)
            self._assert_http(409, lambda: self._preview(connection, ["child-0"]))
            self.assertEqual(self._raw_jobs(connection), before)

    def test_approval_and_candidate_drift_stale_the_token(self) -> None:
        for drift in ("approval", "candidate"):
            with self.subTest(drift=drift), open_db(self.config.paths.db_path) as connection:
                self._seed(connection, count=1)
                token = self._preview(connection, ["child-0"])["token"]
                connection.commit()
                approval = ({"policy_hash": "changed"} if drift == "approval" else APPROVAL)
                candidate = ({"eligible": True, "generation": 2} if drift == "candidate" else None)
                self._assert_http(
                    409,
                    lambda: self._apply(connection, ["child-0"], token, approval=approval, candidate=candidate),
                )
                self.assertEqual(self._job(connection, "child-0")["status"], "needs_attention")
            self._reset_database()

    def test_candidate_blocker_rejects_the_whole_selected_set(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._seed(connection, count=2)

            def blocked(_connection: DBClient, _parent: dict[str, Any], items: list[dict[str, Any]]) -> Mapping[str, Any]:
                raise HTTPException(status_code=409, detail=f"blocked {len(items)} selected items")

            self._assert_http(409, lambda: self._preview(connection, ["child-0", "child-1"], candidate_fn=blocked))
            self.assertEqual([self._job(connection, child)["status"] for child in ("child-0", "child-1")],
                             ["needs_attention", "needs_attention"])

    def _seed(
            self,
            connection: DBClient,
            *,
            count: int,
            statuses: list[str] | None = None,
            failure_kinds: list[str] | None = None,
            indexes: list[Any] | None = None,
            item_count: int | None = None,
            rel_paths: list[str] | None = None,
    ) -> list[int]:
        actual_item_count = count if item_count is None else item_count
        item_ids: list[int] = []
        items: list[dict[str, Any]] = []
        for index in range(actual_item_count):
            rel_path = (rel_paths or [])[index] if rel_paths and index < len(rel_paths) else f"Show/Season 01/Episode {index + 1:03}.mkv"
            source = self.source_root / rel_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(f"source-{index}".encode())
            stat = source.stat()
            result = connection.execute(library_items.insert().values(
                source_path=str(source), rel_path=f"tv/{rel_path}", media_root="tv", parent_dir=str(Path(rel_path).parent),
                file_name=source.name, container=".mkv", size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                fingerprint=f"fingerprint-{index}", audio_summary_json="[]", subtitle_summary_json="[]",
                last_scan_id="scan-test", discovered_at=NOW, last_seen_at=NOW, updated_at=NOW,
            ))
            item_id = int(result.inserted_primary_key[0])
            item_ids.append(item_id)
            items.append({
                "library_item_id": item_id, "source_path": str(source), "source_size_bytes": stat.st_size,
                "source_rel_path": f"tv/{rel_path}", "source_fingerprint": f"fingerprint-{index}",
                "staging_path": str(self.root / "staging" / rel_path),
            })
        self.manifest_path.write_text(json.dumps({
            "selection": {"production_approval_contract": APPROVAL}, "items": items,
        }), encoding="utf-8")
        connection.execute(encode_jobs.insert().values(
            job_id="parent", prefix="tv/Show", job_kind="folder", status="running",
            manifest_path=str(self.manifest_path), item_count=actual_item_count, host_json="{}", last_host_json="{}",
            created_at=NOW, updated_at=NOW,
        ))
        for index in range(count):
            child_indexes = indexes[index] if indexes is not None else [index % actual_item_count]
            connection.execute(encode_jobs.insert().values(
                job_id=f"child-{index}", prefix="tv/Show", job_kind="shard", parent_job_id="parent",
                status=(statuses or ["needs_attention"] * count)[index], manifest_path=str(self.manifest_path),
                manifest_indexes_json=json.dumps(child_indexes) if child_indexes is not None else None,
                item_count=1, host_json=json.dumps({"key": "bad-host"}), last_host_json=json.dumps({"key": "bad-host"}),
                process_pid=None, error=f"failure {index}", attempt_count=3 + index,
                leased_at=None, lease_expires_at=None, heartbeat_at=None, worker_id=None,
                retry_not_before=NOW, waiting_reason="waiting", terminal_reason="terminal",
                last_failure_kind=(failure_kinds or ["host_configuration"] * count)[index], last_failure_at=NOW,
                host_cooldown_until="2026-09-09T13:00:00+00:00", progress_json=json.dumps({"history": [index]}),
                created_at=f"{NOW}-{index:03}", finished_at=NOW, updated_at=NOW,
            ))
        return item_ids

    def _preview(
            self,
            connection: DBClient,
            child_ids: list[str],
            *,
            approval: Mapping[str, Any] = APPROVAL,
            candidate: Mapping[str, Any] | None = None,
            candidate_fn: Any = None,
    ) -> dict[str, Any]:
        eligibility = candidate_fn or (lambda _connection, _parent, _items: candidate or {"eligible": True, "generation": 1})
        return preview_child_recovery(
            connection, self.config, "parent", child_ids=child_ids,
            approval_contract=lambda _parent, _manifest: approval, candidate_eligibility=eligibility,
        )

    def _apply(
            self,
            connection: DBClient,
            child_ids: list[str],
            token: str,
            *,
            approval: Mapping[str, Any] = APPROVAL,
            candidate: Mapping[str, Any] | None = None,
            sync_parent: Any = None,
    ) -> dict[str, Any]:
        return apply_child_recovery(
            connection, self.config, "parent", child_ids=child_ids, expected_token=token,
            approval_contract=lambda _parent, _manifest: approval,
            candidate_eligibility=lambda _connection, _parent, _items: candidate or {"eligible": True, "generation": 1},
            sync_parent=sync_parent or (lambda _connection, _child: None), now_iso=lambda: RECOVERED_AT,
        )

    def _manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def _assert_http(status_code: int, action: Any) -> None:
        with unittest.TestCase().assertRaises(HTTPException) as raised:
            action()
        if raised.exception.status_code != status_code:
            raise AssertionError(f"expected HTTP {status_code}, got {raised.exception.status_code}: {raised.exception.detail}")

    @staticmethod
    def _raw_jobs(connection: DBClient) -> dict[str, dict[str, Any]]:
        rows = connection.execute(select(encode_jobs).order_by(encode_jobs.c.job_id)).mappings().all()
        return {str(row["job_id"]): dict(row) for row in rows}

    @staticmethod
    def _job(connection: DBClient, job_id: str) -> dict[str, Any]:
        row = connection.execute(select(encode_jobs).where(encode_jobs.c.job_id == job_id)).mappings().one()
        return dict(row)

    def _reset_database(self) -> None:
        reset_engine_cache()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.config.paths.db_path}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
