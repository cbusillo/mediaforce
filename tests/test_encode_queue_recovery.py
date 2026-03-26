from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from media_harness.config import ConfigPaths, HarnessConfig
from media_harness.db import open_db
from media_harness.encode_queue import load_encode_job, load_queue_state, save_encode_job, save_queue_state
from media_harness.quality import QualitySearchResult, SampleEncodeResult
from media_harness import execution, remote
from media_harness.remote import HostStatus
from media_harness.review import CompareClip, EncodedPreviewClip
from media_harness.web import app as web_app


class EncodeQueueRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = self._build_config()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_restart_recovery_requeues_running_job_and_cleans_transients(self) -> None:
        source_path = self._create_source_file("episode-a.mkv")
        staging_path = self._staging_path("episode-a.mkv")
        partial_path = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.write_text("staged")
        partial_path.write_text("partial")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoded")
            self._write_manifest(
                "manifest-a.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._insert_staged_artifact(connection, item_id, staging_path)
            self._save_job(
                connection,
                job_id="job-restart",
                manifest_name="manifest-a.json",
                host={"key": "remote-a", "label": "Remote A", "mode": "ssh"},
                status="running",
                attempt_count=1,
                lease_expires_at="2026-03-25T00:00:00+00:00",
            )
            state = load_queue_state(connection)
            state.update({"active_job_id": "job-restart", "stop_requested": True, "updated_at": web_app._now_iso()})
            save_queue_state(connection, state)

            web_app._recover_encode_queue(connection, self.config)

            job = load_encode_job(connection, "job-restart")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "retry_backoff")
            self.assertEqual(job["last_failure_kind"], "worker_restart")
            self.assertEqual(job["last_host"]["key"], "remote-a")
            self.assertFalse(staging_path.exists())
            self.assertFalse(partial_path.exists())
            stage_row = connection.execute(
                "SELECT promoted_at FROM staged_artifacts WHERE library_item_id = ?",
                (item_id,),
            ).fetchone()
            self.assertIsNone(stage_row)
            item_status = connection.execute(
                "SELECT status FROM library_items WHERE id = ?",
                (item_id,),
            ).fetchone()[0]
            self.assertEqual(item_status, "planned")
            queue_state = load_queue_state(connection)
            self.assertIsNone(queue_state["active_job_id"])
            self.assertFalse(queue_state["stop_requested"])

    def test_stale_lease_reconciler_requeues_running_job(self) -> None:
        source_path = self._create_source_file("episode-b.mkv")
        staging_path = self._staging_path("episode-b.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-b.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-stale",
                manifest_name="manifest-b.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
                lease_expires_at="2000-01-01T00:00:00+00:00",
            )

            web_app._reconcile_encode_jobs(connection, self.config)

            job = load_encode_job(connection, "job-stale")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "retry_backoff")
            self.assertEqual(job["last_failure_kind"], "stale_lease")
            self.assertIn("stale worker lease", str(job["waiting_reason"]))

    def test_retry_backoff_promotes_job_to_queued_when_ready(self) -> None:
        source_path = self._create_source_file("episode-c.mkv")
        staging_path = self._staging_path("episode-c.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path)
            self._write_manifest(
                "manifest-c.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-backoff",
                manifest_name="manifest-c.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="retry_backoff",
                attempt_count=1,
                retry_not_before="2000-01-01T00:00:00+00:00",
                waiting_reason="retrying",
            )

            web_app._reconcile_encode_jobs(connection, self.config)

            job = load_encode_job(connection, "job-backoff")
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["status"], "queued")
            self.assertIsNone(job["retry_not_before"])
            self.assertIsNone(job["waiting_reason"])

    def test_host_selection_prefers_other_encode_capable_host_during_cooldown(self) -> None:
        job = {
            "last_host": {"key": "remote-a", "label": "Remote A", "host": "remote-a"},
            "host_cooldown_until": "2999-01-01T00:00:00+00:00",
        }
        statuses = [
            {
                "key": "remote-a",
                "label": "Remote A",
                "priority": 90,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "remote-b",
                "label": "Remote B",
                "priority": 70,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": True,
            },
            {
                "key": "calibration-only",
                "label": "Calibration Only",
                "priority": 100,
                "capabilities": ["sample_calibration"],
                "available": True,
                "active_encode_count": 0,
                "max_parallel_encodes": 1,
                "queue_active": False,
            },
        ]
        with open_db(self.config.paths.db_path) as connection:
            with patch("media_harness.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, job)
        self.assertIsNotNone(host_payload)
        assert host_payload is not None
        self.assertEqual(host_payload["key"], "remote-b")
        self.assertIsNone(waiting_reason)

    def test_deterministic_failure_moves_job_to_needs_attention(self) -> None:
        source_path = self._create_source_file("episode-d.mkv")
        staging_path = self._staging_path("episode-d.mkv")

        with open_db(self.config.paths.db_path) as connection:
            item_id = self._insert_library_item(connection, source_path, status="encoding")
            self._write_manifest(
                "manifest-d.json",
                [{"library_item_id": item_id, "staging_path": str(staging_path)}],
            )
            self._save_job(
                connection,
                job_id="job-deterministic",
                manifest_name="manifest-d.json",
                host={"key": "local", "label": "Local", "mode": "local"},
                status="running",
                attempt_count=1,
            )

            job = load_encode_job(connection, "job-deterministic")
            assert job is not None
            web_app._transition_encode_job_failure(
                connection,
                self.config,
                job,
                failure_kind="deterministic",
                error_message="ffmpeg encode failed: invalid stream mapping",
            )

            updated = load_encode_job(connection, "job-deterministic")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated["status"], "needs_attention")
            self.assertEqual(updated["terminal_reason"], "deterministic")

    def test_runtime_settings_payload_normalizes_capabilities(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "repo_path": "/srv/media-harness",
                    "wake_mac": "aa:bb:cc:dd:ee:ff",
                    "priority": "20",
                    "max_parallel_encodes": "3",
                    "schedule_profile": "late_night",
                    "capabilities": "encode_queue, sample_calibration, encode_queue",
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[
                {
                    "key": "late_night",
                    "label": "Late Night",
                    "mode": "night",
                    "timezone": "local",
                    "start_hour": "23",
                    "end_hour": "6",
                }
            ],
        )
        self.assertEqual(
            payload["remote_hosts"][0]["capabilities"],
            ["encode_queue", "sample_calibration"],
        )
        self.assertEqual(payload["remote_hosts"][0]["max_parallel_encodes"], 3)
        self.assertEqual(payload["remote_hosts"][0]["schedule_profile"], "late_night")
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["key"], "late_night")
        self.assertEqual(payload["encode_queue"]["schedule_profiles"][0]["timezone"], "host_local")

    def test_runtime_settings_payload_defaults_host_schedule_to_always(self) -> None:
        payload = web_app._build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[
                {
                    "label": "Remote A",
                    "host": "remote-a",
                    "priority": "20",
                    "max_parallel_encodes": "2",
                    "capabilities": ["encode_queue"],
                }
            ],
            transcode_root=str(self.root / "staging"),
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )
        self.assertEqual(payload["remote_hosts"][0]["schedule_profile"], "always")

    def test_runtime_settings_payload_rejects_unknown_host_schedule_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown schedule profile"):
            web_app._build_runtime_settings_payload(
                libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
                remote_hosts=[
                    {
                        "label": "Remote A",
                        "host": "remote-a",
                        "priority": "20",
                        "max_parallel_encodes": "2",
                        "schedule_profile": "missing_profile",
                        "capabilities": "encode_queue",
                    }
                ],
                transcode_root=str(self.root / "staging"),
                encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
                schedule_profiles=[],
            )

    def test_settings_helpers_tolerate_malformed_runtime_values(self) -> None:
        self.config.raw["media"]["source_roots"] = ["not-a-mapping"]
        self.config.raw["media"]["staging_root"] = {"bad": "shape"}
        self.config.raw["remote_hosts"] = ["not-a-host-row"]

        library_rows = web_app._settings_library_rows_for_config(self.config)
        remote_rows = web_app._settings_remote_rows_for_config(self.config)
        transcode_root = web_app._settings_transcode_root_value(self.config)
        archive_root = web_app._settings_archive_root(transcode_root)
        host_statuses = web_app._safe_collect_host_statuses(self.config)

        self.assertEqual(library_rows[0]["key"], "")
        self.assertEqual(library_rows[0]["path"], "")
        self.assertEqual(remote_rows[0]["host"], "")
        self.assertEqual(transcode_root, "")
        self.assertEqual(archive_root, "")
        self.assertEqual(len(host_statuses), 1)
        self.assertEqual(host_statuses[0].key, "host-status-error")
        self.assertFalse(host_statuses[0].available)

    def test_settings_rows_map_legacy_default_schedule_to_always(self) -> None:
        self.config.raw["remote_hosts"] = [
            {
                "host": "remote-a",
                "label": "Remote A",
                "schedule_profile": "default",
                "capabilities": ["encode_queue", "sample_calibration"],
            }
        ]

        remote_rows = web_app._settings_remote_rows_for_config(self.config)

        self.assertEqual(remote_rows[0]["schedule_profile"], "always")
        self.assertEqual(remote_rows[0]["capabilities"], ["encode_queue", "sample_calibration"])

    def test_scheduler_uses_host_local_time_for_windows(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "night", "start_hour": 22, "end_hour": 6, "timezone": "host_local"}
        )
        now = web_app._parse_iso("2026-03-25T07:30:00+00:00")

        self.assertTrue(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": -420},
            )
        )
        self.assertFalse(
            web_app._scheduler_allows_encode_run(
                policy,
                now=now,
                host_payload={"utc_offset_minutes": 120},
            )
        )

    def test_scheduler_host_local_time_falls_back_to_runner_timezone_without_remote_offset(self) -> None:
        policy = web_app._normalize_encode_queue_scheduler(
            {"mode": "night", "start_hour": 22, "end_hour": 6, "timezone": "host_local"}
        )
        now = web_app._parse_iso("2026-03-25T07:30:00+00:00")
        assert now is not None
        local_hour = now.astimezone().hour
        expected = local_hour >= 22 or local_hour < 6

        self.assertEqual(
            web_app._scheduler_allows_encode_run(policy, now=now, host_payload={}),
            expected,
        )

    def test_project_env_loader_sets_defaults_without_overriding_shell_env(self) -> None:
        env_path = Path.home() / "Developer" / "claude-local-machine" / "projects" / "media-encoding" / ".env"
        with patch.object(web_app, "DEFAULT_CONFIG_PATH", env_path.parent / "config" / "defaults.toml"):
            with patch.object(Path, "exists", autospec=True) as exists_mock:
                exists_mock.side_effect = lambda path: path == env_path
                with patch.object(Path, "read_text", autospec=True, return_value=(
                    "MEDIA_HARNESS_WEB_HOST=0.0.0.0\n"
                    "MEDIA_HARNESS_WEB_PORT=8777\n"
                    "MEDIA_HARNESS_WEB_RELOAD=true\n"
                )):
                    with patch.dict(os.environ, {"MEDIA_HARNESS_WEB_PORT": "9999"}, clear=True):
                        web_app._load_project_env_file()
                        self.assertEqual(os.environ["MEDIA_HARNESS_WEB_HOST"], "0.0.0.0")
                        self.assertEqual(os.environ["MEDIA_HARNESS_WEB_PORT"], "9999")
                        self.assertEqual(os.environ["MEDIA_HARNESS_WEB_RELOAD"], "true")

    def test_default_web_host_and_reload_use_neutral_fallbacks(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(web_app._default_web_host(), "127.0.0.1")
            self.assertFalse(web_app._default_web_reload_enabled())

    def test_explicit_env_controls_host_and_reload(self) -> None:
        with patch.dict(os.environ, {"MEDIA_HARNESS_WEB_HOST": "0.0.0.0", "MEDIA_HARNESS_WEB_RELOAD": "true"}, clear=True):
            self.assertEqual(web_app._default_web_host(), "0.0.0.0")
            self.assertTrue(web_app._default_web_reload_enabled())

    def test_parse_project_env_value_strips_matching_quotes(self) -> None:
        self.assertEqual(web_app._parse_project_env_value('"0.0.0.0"'), "0.0.0.0")
        self.assertEqual(web_app._parse_project_env_value("'8777'"), "8777")
        self.assertEqual(web_app._parse_project_env_value("true"), "true")

    def test_resolve_sample_host_maps_legacy_local_key_to_self_host(self) -> None:
        statuses = [
            HostStatus(
                key="cbusillo@localhost",
                label="Chris-Studio",
                mode="ssh",
                priority=100,
                capabilities=["encode_queue", "sample_calibration"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("media_harness.web.app.collect_host_statuses", return_value=statuses):
            host = web_app._resolve_sample_host(self.config, "local")
        self.assertEqual(host.key, "cbusillo@localhost")

    def test_resolve_sample_host_accepts_remote_sample_host(self) -> None:
        statuses = [
            HostStatus(
                key="cbusillo@m1-mini",
                label="M1 mini",
                mode="ssh",
                priority=80,
                capabilities=["sample_calibration"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("media_harness.web.app.collect_host_statuses", return_value=statuses):
            host = web_app._resolve_sample_host(self.config, "cbusillo@m1-mini")
        self.assertEqual(host.key, "cbusillo@m1-mini")

    def test_resolve_sample_host_rejects_non_sample_host(self) -> None:
        statuses = [
            HostStatus(
                key="remote-a",
                label="Remote A",
                mode="ssh",
                priority=80,
                capabilities=["encode_queue"],
                available=True,
                message="Mounted and ready",
                missing_paths=[],
                repo_path=str(self.root),
            )
        ]
        with patch("media_harness.web.app.collect_host_statuses", return_value=statuses):
            with self.assertRaises(HTTPException) as exc_info:
                web_app._resolve_sample_host(self.config, "remote-a")
        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertEqual(exc_info.exception.detail, "Unknown sampled calibration host")

    @patch("media_harness.web.app.generate_compare_clips_from_previews")
    @patch("media_harness.web.app.encode_preview_clips")
    @patch("media_harness.web.app.recommend_review_timestamps")
    @patch("media_harness.web.app.run_sample_encode")
    @patch("media_harness.web.app.search_quality_for_source")
    def test_run_sampled_calibration_passes_remote_host_to_remote_work(
        self,
        search_quality_mock,
        sample_encode_mock,
        recommend_timestamps_mock,
        encode_preview_mock,
        compare_preview_mock,
    ) -> None:
        source_path = self._create_source_file("episode-remote.mkv")
        preview_path = self.root / "review" / "remote-run" / "item-00" / "encoded-01-12m-00s.mkv"
        compare_path = self.root / "review" / "remote-run" / "item-00" / "compare-01-12m-00s.mkv"
        host = {"key": "cbusillo@m1-mini", "label": "M1 mini", "mode": "ssh"}
        policy = {
            "video": {
                "quality_metric": "auto",
                "preset": 6,
                "pixel_format": "yuv420p10le",
                "sample_every": "10m",
                "sample_duration": "30s",
                "encoder": "libsvtav1",
                "default_grain": 0,
                "grain_denoise": 0,
            },
            "audio": {
                "copy_codecs": ["aac"],
                "convert_to_opus_codecs": ["ac3", "dca"],
                "stereo_opus_bitrate": "160k",
                "surround_5_1_opus_bitrate": "320k",
                "surround_7_1_opus_bitrate": "448k",
            },
            "subtitle": {"prefer_text": True},
        }
        sample_item = {
            "source_path": str(source_path),
            "rel_path": "tv/show/episode-remote.mkv",
            "source_size_bytes": 1_000_000,
            "duration_seconds": 3600.0,
            "resolved_policy": policy,
            "audio_summary": [
                {
                    "index": 1,
                    "codec_name": "aac",
                    "channels": 2,
                    "language": "eng",
                    "default": 1,
                    "bit_rate": 192_000,
                }
            ],
            "subtitle_summary": [],
        }

        search_quality_mock.return_value = QualitySearchResult(
            crf=28.0,
            metric="VMAF",
            target=95.0,
            score=95.2,
            stdout="quality ok",
        )
        sample_encode_mock.return_value = SampleEncodeResult(
            metric="VMAF",
            score=95.2,
            predicted_encode_percent=55.0,
            predicted_encode_seconds=120.0,
            predicted_encode_size_bytes=550_000,
            stdout="sample ok",
        )
        recommend_timestamps_mock.return_value = [720.0]
        encode_preview_mock.return_value = [
            EncodedPreviewClip(
                output_path=preview_path,
                timestamp_seconds=720.0,
                duration_seconds=8.0,
                size_bytes=123_456,
            )
        ]
        compare_preview_mock.return_value = [
            CompareClip(
                output_path=compare_path,
                timestamp_seconds=720.0,
                duration_seconds=8.0,
            )
        ]

        payload, _ = web_app._run_sampled_calibration(
            config=self.config,
            prefix="tv/show",
            action="baseline",
            host_data=host,
            notes="Prefer a smaller file if it still looks clean.",
            policy=policy,
            seed_metadata=None,
            sample_item=sample_item,
            calibration_run_id="remote-run",
        )

        search_quality_mock.assert_called_once_with(source_path, policy["video"], host=host)
        sample_encode_mock.assert_called_once()
        self.assertEqual(sample_encode_mock.call_args.kwargs["host"], host)
        encode_preview_mock.assert_called_once()
        self.assertEqual(encode_preview_mock.call_args.kwargs["host"], host)
        self.assertEqual(payload["host"], host)
        self.assertEqual(payload["compare_clips"][0]["path"], "/review-media/remote-run/item-00/compare-01-12m-00s.mkv")

    def test_collect_host_statuses_uses_configured_hosts_only(self) -> None:
        configured_hosts = [
            {"host": "cbusillo@localhost", "label": "Chris-Studio"},
            {"host": "cbusillo@example-host", "label": "Remote"},
        ]
        self.config.raw["remote_hosts"] = configured_hosts
        with patch("media_harness.remote._remote_host_status", side_effect=lambda config, host: HostStatus(
            key=str(host["host"]),
            label=str(host["label"]),
            mode="ssh",
            priority=0,
            capabilities=["encode_queue"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=None,
        )):
            statuses = remote.collect_host_statuses(self.config)
        self.assertEqual([status.key for status in statuses], ["cbusillo@localhost", "cbusillo@example-host"])

    def test_remote_host_status_requires_ab_av1_for_sample_hosts(self) -> None:
        host = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["sample_calibration"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_libvmaf|1",
                "tool|ffmpeg_xpsnr|1",
                "tool|ffmpeg_libsvtav1|1",
                "tool|ab_av1|0",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
            "media_harness.remote._run_remote_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("media_harness.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.AB_AV1_MISSING_ISSUE, status.issues)
        self.assertEqual(status.message, "Needs remote setup")

    def test_remote_host_status_requires_metric_and_av1_support_for_sample_hosts(self) -> None:
        host = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["sample_calibration"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|1",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
            "media_harness.remote._run_remote_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("media_harness.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertFalse(status.available)
        self.assertIn(remote.SAMPLE_METRIC_MISSING_ISSUE, status.issues)
        self.assertIn(remote.SAMPLE_AV1_ENCODER_MISSING_ISSUE, status.issues)

    def test_remote_host_status_allows_encode_only_hosts_without_ab_av1(self) -> None:
        host = {
            "host": "cbusillo@encode-host",
            "label": "Encode Host",
            "capabilities": ["encode_queue"],
        }
        stdout = "\n".join(
            [
                f"path|{self.config.staging_root}|1",
                f"path|{self.config.archive_root}|1",
                "tool|xcode_clt|1",
                "tool|brew|1",
                "tool|ffmpeg|1",
                "tool|ffmpeg_libvmaf|0",
                "tool|ffmpeg_xpsnr|0",
                "tool|ffmpeg_libsvtav1|0",
                "tool|ab_av1|0",
                "time|utc_offset|+0000",
                "repo|exists|1",
            ]
        )
        with patch(
            "media_harness.remote._run_remote_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout=stdout, stderr=""),
        ), patch("media_harness.remote._learn_remote_wake_mac"):
            status = remote._remote_host_status(self.config, host)
        self.assertTrue(status.available)
        self.assertNotIn(remote.AB_AV1_MISSING_ISSUE, status.issues)

    def test_finish_remote_host_prepare_installs_ab_av1_for_sample_hosts(self) -> None:
        host = {
            "host": "cbusillo@sample-host",
            "label": "Sample Host",
            "capabilities": ["encode_queue", "sample_calibration"],
        }
        ready_status = HostStatus(
            key="cbusillo@sample-host",
            label="Sample Host",
            mode="ssh",
            priority=0,
            capabilities=["encode_queue", "sample_calibration"],
            available=True,
            message="Mounted and ready",
            missing_paths=[],
            repo_path=None,
        )
        with patch(
            "media_harness.remote._run_remote_ssh",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_remote_ssh_mock, patch("media_harness.remote._remote_host_status", return_value=ready_status):
            result = remote._finish_remote_host_prepare(self.config, host, prep_steps=[])
        self.assertTrue(result.ok)
        self.assertIn("Installed ffmpeg-full with Homebrew for sampled calibration hosts when required.", result.performed_steps)
        self.assertIn("Installed ab-av1 with Homebrew for sampled calibration if it was missing.", result.performed_steps)
        prep_script = run_remote_ssh_mock.call_args.args[3]
        self.assertIn("install ffmpeg-full", prep_script)
        self.assertIn('install ab-av1', prep_script)

    def test_run_encode_command_remote_prefers_ffmpeg_full_path(self) -> None:
        ffmpeg_cmd = ["/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg", "-hide_banner", "-i", "/tmp/in.mkv", "/tmp/out.mkv"]
        temp_output = self.root / "staging" / "episode.partial.mkv"
        staging_path = self.root / "staging" / "episode.mkv"
        with patch(
            "media_harness.execution.run_command",
            return_value=subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="", stderr=""),
        ) as run_command_mock:
            execution._run_encode_command(
                ffmpeg_cmd=ffmpeg_cmd,
                temp_output=temp_output,
                staging_path=staging_path,
                overwrite=False,
                process_controller=None,
                host={"key": "cbusillo@sample-host", "mode": "ssh"},
            )
        ssh_cmd = run_command_mock.call_args.args[0]
        self.assertEqual(ssh_cmd[0], "ssh")
        self.assertIn("/opt/homebrew/opt/ffmpeg-full/bin", ssh_cmd[-1])

    def test_select_encode_host_respects_parallel_limit(self) -> None:
        statuses = [
            {
                "key": "cbusillo@localhost",
                "label": "Chris-Studio",
                "priority": 100,
                "capabilities": ["encode_queue"],
                "available": True,
                "active_encode_count": 1,
                "max_parallel_encodes": 1,
                "queue_active": False,
            }
        ]
        with open_db(self.config.paths.db_path) as connection:
            with patch("media_harness.web.app._host_runtime_rows", return_value=statuses):
                host_payload, waiting_reason = web_app._select_encode_host(connection, self.config, {"bypass_schedule": False})
        self.assertIsNone(host_payload)
        self.assertEqual(waiting_reason, "waiting for host capacity to free up")

    def _build_config(self) -> HarnessConfig:
        paths = ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
        )
        raw = {
            "state": {"cleanup": {"transient_artifact_retention_days": 14}},
            "media": {
                "source_roots": {"tv": str(self.root / "source" / "tv")},
                "staging_root": str(self.root / "staging"),
                "archive_root": str(self.root / "archive"),
            },
            "remote_hosts": [],
            "encode_queue": {
                "scheduler": {
                    "mode": "anytime",
                    "start_hour": 22,
                    "end_hour": 8,
                    "timezone": "local",
                }
            },
        }
        return HarnessConfig(raw=raw, paths=paths)

    def _create_source_file(self, name: str) -> Path:
        path = self.root / "source" / "tv" / "show" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("source")
        return path

    def _staging_path(self, name: str) -> Path:
        return self.root / "staging" / "tv" / "show" / name

    def _insert_library_item(self, connection, source_path: Path, *, status: str = "planned") -> int:
        now = web_app._now_iso()
        connection.execute(
            """
            INSERT INTO library_items(
                source_path, rel_path, media_root, parent_dir, file_name, container,
                size_bytes, mtime_ns, fingerprint, duration_seconds, video_codec,
                audio_summary_json, subtitle_summary_json, last_scan_id, discovered_at,
                last_seen_at, updated_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_path),
                f"tv/show/{source_path.name}",
                "tv",
                "tv/show",
                source_path.name,
                ".mkv",
                1024,
                1,
                f"fingerprint-{source_path.name}",
                60.0,
                "h264",
                "[]",
                "[]",
                "scan-1",
                now,
                now,
                now,
                status,
            ),
        )
        return int(connection.execute("SELECT id FROM library_items ORDER BY id DESC LIMIT 1").fetchone()[0])

    def _insert_staged_artifact(self, connection, library_item_id: int, staging_path: Path) -> None:
        connection.execute(
            "INSERT INTO staged_artifacts(library_item_id, staging_path, updated_at) VALUES (?, ?, ?)",
            (library_item_id, str(staging_path), web_app._now_iso()),
        )

    def _write_manifest(self, name: str, items: list[dict[str, object]]) -> Path:
        path = self.root / "runs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"items": items}))
        return path

    def _save_job(
        self,
        connection,
        *,
        job_id: str,
        manifest_name: str,
        host: dict[str, object],
        status: str,
        attempt_count: int,
        lease_expires_at: str | None = None,
        retry_not_before: str | None = None,
        waiting_reason: str | None = None,
    ) -> None:
        manifest_path = self.root / "runs" / manifest_name
        now = web_app._now_iso()
        save_encode_job(
            connection,
            {
                "job_id": job_id,
                "prefix": "tv/show",
                "status": status,
                "manifest_path": str(manifest_path),
                "item_count": 1,
                "saved_profile_path": None,
                "host": host,
                "last_host": {},
                "notes": "",
                "bypass_schedule": False,
                "attempt_count": attempt_count,
                "process_pid": 111 if status == "running" else None,
                "error": None,
                "leased_at": now if status == "running" else None,
                "lease_expires_at": lease_expires_at,
                "heartbeat_at": now if status == "running" else None,
                "worker_id": "test-worker" if status == "running" else None,
                "retry_not_before": retry_not_before,
                "waiting_reason": waiting_reason,
                "terminal_reason": None,
                "last_failure_kind": None,
                "last_failure_at": None,
                "host_cooldown_until": None,
                "created_at": now,
                "started_at": now if status == "running" else None,
                "finished_at": None,
                "updated_at": now,
            },
        )


if __name__ == "__main__":
    unittest.main()
