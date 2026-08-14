import hashlib
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.library.staged_integrity import MAX_DETAIL_PAGE_SIZE, staged_integrity_report
from mediaforce.web.runtime.folder_actions import promote_folder_outputs_action


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
            tracked = self._insert_item(connection, "tv/Show/Season 1/Tracked.mkv", status="promoted")
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
        self.assertEqual(report.counts["partial_or_temporary"], 1)
        self.assertFalse(report.discovery_truncated)

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

    def test_tv_season_rejects_partial_promotion(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            valid_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="planned")
            stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready")
            self._insert_artifact(connection, valid_id, stage, passed=True)

        promoted = Mock(return_value=[Path("unused")])
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [self._manifest_item(stage, "tv/Show/Season 1/One.mkv")],
            promote_manifest_items_fn=promoted,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "season_promotion_incomplete")
        self.assertIn("season_staged_integrity_not_started", {blocker["code"] for blocker in result["blockers"]})
        promoted.assert_not_called()

    def test_tv_season_blocks_active_descendant_job_even_when_root_job_completed(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready")
            self._insert_artifact(connection, item_id, stage, passed=True)
            self._insert_encode_job(
                connection,
                job_id="root-complete",
                prefix="tv/Show/Season 1",
                status="completed",
                updated_at="2026-08-14T12:00:00+00:00",
            )
            self._insert_encode_job(
                connection,
                job_id="episode-running",
                prefix="tv/Show/Season 1/Episode 1",
                status="running",
                updated_at="2026-08-14T11:00:00+00:00",
            )

        promoted = Mock(return_value=[Path("unused")])
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(stage, "tv/Show/Season 1/One.mkv")
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertFalse(result["ok"])
        self.assertIn("season_active_encode_job", {blocker["code"] for blocker in result["blockers"]})
        promoted.assert_not_called()

    def test_tv_season_fails_closed_when_policy_gate_is_unavailable(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_item(connection, "tv/Show/Season 1/One.mkv", status="validated")
            second_id = self._insert_item(connection, "tv/Show/Season 1/Two.mkv", status="validated")
            first_stage = self._write_stage("tv/Show/Season 1/One.mkv", b"ready-one")
            second_stage = self._write_stage("tv/Show/Season 1/Two.mkv", b"ready-two")
            self._insert_artifact(connection, first_id, first_stage, passed=True)
            self._insert_artifact(connection, second_id, second_stage, passed=True)

        promoted = Mock(return_value=[Path("one"), Path("two")])
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

    def test_tv_season_requires_one_approved_policy(self) -> None:
        first_policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 93}}
        second_policy = {"video": {"encoder": "libsvtav1", "target_vmaf": 91}}
        manifest_path = self.root / "runs" / "mixed-policy.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "items": [
                {"resolved_policy": first_policy},
                {"resolved_policy": second_policy},
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

        promoted = Mock(return_value=[Path("one"), Path("two")])
        result = promote_folder_outputs_action(
            self.config,
            "tv/Show/Season 1",
            load_calibration_state_fn=lambda _config, _prefix: {
                "accepted_policy_hash": self._policy_hash(first_policy),
            },
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [
                self._manifest_item(first_stage, "tv/Show/Season 1/One.mkv"),
                self._manifest_item(second_stage, "tv/Show/Season 1/Two.mkv"),
            ],
            promote_manifest_items_fn=promoted,
        )

        self.assertFalse(result["ok"])
        blocker_codes = {blocker["code"] for blocker in result["blockers"]}
        self.assertIn("season_policy_mixed", blocker_codes)
        self.assertIn("season_policy_not_approved", blocker_codes)
        promoted.assert_not_called()

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

        promoted = Mock(return_value=[Path("one"), Path("two")])
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

        for prefix, stage in (("movies/Film.mkv", movie_stage), ("other/Loose.mkv", other_stage)):
            promoted = Mock(return_value=[Path("promoted")])
            result = promote_folder_outputs_action(
                self.config,
                prefix,
                load_folder_staged_items_fn=lambda *_args, stage=stage, prefix=prefix, **_kwargs: [
                    self._manifest_item(stage, prefix),
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

    def _insert_artifact(
            self,
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
    ) -> None:
        connection.execute(encode_jobs.insert().values(
            job_id=job_id,
            prefix=prefix,
            job_kind="folder",
            status=status,
            manifest_path="/tmp/web-smoke-manifest.json",
            item_count=1,
            host_json="{}",
            last_host_json="{}",
            created_at=updated_at,
            updated_at=updated_at,
        ))

    def _manifest_item(self, stage: Path, rel_path: str) -> dict[str, object]:
        return {
            "source_path": str(self.root / "source" / rel_path),
            "staging_path": str(stage),
            "rel_path": rel_path,
        }

    @staticmethod
    def _policy_hash(policy: dict[str, object]) -> str:
        encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:16]
