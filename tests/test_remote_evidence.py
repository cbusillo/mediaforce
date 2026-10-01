import json
import subprocess
import tempfile
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint
from mediaforce.encoding import fingerprint
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, analyze_cadence
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND, MEDIA_FINGERPRINT_TOOL_VERSION, \
    analyze_media_fingerprint, media_fingerprint_manifest_payload, media_fingerprint_staleness
from mediaforce.encoding.remote_media import RemoteMediaCommands
from mediaforce.hosts.types import FFMPEG_MISSING_ISSUE
from mediaforce.library.evidence_hosts import EVIDENCE_HOST_WAIT_REASON, select_evidence_host
from mediaforce.library.background_work import list_evidence_backlog
from mediaforce.library.evidence_queue import claim_next_evidence_work, evidence_queue_summary, \
    resume_evidence_queue, start_evidence_work
from mediaforce.library.evidence_state import EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_STATE_CURRENT, \
    EVIDENCE_REASON_TOOL_CHANGED, load_library_item_evidence_states, project_evidence_state, \
    rebuild_library_item_evidence_states
from mediaforce.library.evidence_worker import EvidenceWorkerDeps, process_evidence_queue_once
from mediaforce.library.probe import probe_evidence

REMOTE_FFMPEG_VERSION = "ffmpeg version 7.1-encode-computer"
CONTROLLER_FFMPEG_VERSION = "ffmpeg version 6.0-controller"
PROGRESSIVE_IDET_STDERR = (
    "Repeated Fields: Neither: 200 Top: 0 Bottom: 0\n"
    "Multi frame detection: TFF: 0 BFF: 0 Progressive: 200 Undetermined: 0\n"
)
PROBE_JSON = json.dumps(
    {
        "streams": [
            {
                "codec_type": "video",
                "field_order": "unknown",
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30000/1001",
                "time_base": "1/1000",
            }
        ],
        "format": {"duration": "60.0"},
    }
)


def _host(key: str, label: str, *, priority: int, **extra: Any) -> dict[str, Any]:
    return {
        "host": key,
        "label": label,
        "mode": "ssh",
        "priority": priority,
        "capabilities": ["encode_queue"],
        "source_roots": {"tv": f"/Volumes/{label}/tv"},
        **extra,
    }


def _row(host: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "key": host["host"],
        "label": host["label"],
        "available": True,
        "capabilities": list(host["capabilities"]),
        "issues": [],
        "schedule_open": True,
        **overrides,
    }


class FakeEncodeComputers:
    """Answers remote commands the way ffmpeg and ffprobe would on an encode computer."""

    def __init__(self, *, idet_result: subprocess.CompletedProcess[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.idet_result = idet_result

    def __call__(
            self,
            host: dict[str, object],
            command: list[str],
            timeout: int,
            input_text: str | None = None,
            process_controller: ManagedProcessController | None = None,
    ) -> subprocess.CompletedProcess[str]:
        _ = input_text
        self.calls.append(
            {"host": host["host"], "command": command, "timeout": timeout, "process_controller": process_controller}
        )
        if command == ["ffmpeg", "-version"]:
            return subprocess.CompletedProcess(command, 0, f"{REMOTE_FFMPEG_VERSION}\nbuilt with clang\n", "")
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(command, 0, PROBE_JSON, "")
        if "idet" in command:
            return self.idet_result or subprocess.CompletedProcess(command, 0, "", PROGRESSIVE_IDET_STDERR)
        raise AssertionError(f"unexpected remote command: {command}")

    def measurement_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["command"] != ["ffmpeg", "-version"]]


class RemoteEvidenceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(self.temp_dir.name)
        self.media_root = self.project_root / "tv"
        self.media_root.mkdir()
        self.mini = _host("m1-mini.example", "M1 mini", priority=5)
        self.mbp = _host("m2-mbp.example", "M2 MBP", priority=1)

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_measures_on_the_highest_priority_encode_computer_using_its_path(self) -> None:
        config = self._config([self.mbp, self.mini])
        item_id = self._prepare_cadence_item(config)
        remote = FakeEncodeComputers()

        with self._no_local_media_commands(), patch("mediaforce.encoding.remote_media.run_remote_command", remote):
            processed = process_evidence_queue_once(
                config_path=config.paths.config_path,
                deps=self._deps(config, probe_evidence, host_rows=[_row(self.mbp), _row(self.mini)]),
            )

        state, summary = self._stored(config, item_id)
        measurement_calls = remote.measurement_calls()
        self.assertTrue(processed)
        self.assertEqual({call["host"] for call in remote.calls}, {"m1-mini.example"})
        self.assertEqual(measurement_calls[0]["command"][0], "ffprobe")
        self.assertEqual(measurement_calls[0]["command"][-1], "/Volumes/M1 mini/tv/show/item-1.mkv")
        self.assertEqual(len(measurement_calls), 4)
        for call in measurement_calls:
            self.assertIsInstance(call["process_controller"], ManagedProcessController)
        self.assertEqual(state["state"], EVIDENCE_STATE_CURRENT)
        self.assertEqual(state["work_status"], "completed")
        self.assertEqual(
            summary["analysis"]["tool"],
            {
                "name": "mediaforce.ffmpeg_idet",
                "version": "1",
                "ffmpeg_version": REMOTE_FFMPEG_VERSION,
                "host": "m1-mini.example",
                "host_label": "M1 mini",
            },
        )

    def test_remote_idet_commands_are_the_local_commands(self) -> None:
        remote = FakeEncodeComputers()
        local_commands: list[list[str]] = []

        def local_run_command(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            local_commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", PROGRESSIVE_IDET_STDERR)

        stream = json.loads(PROBE_JSON)["streams"][0]
        with patch("mediaforce.encoding.cadence.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.cadence._ffmpeg_version",
            return_value=CONTROLLER_FFMPEG_VERSION,
        ), patch("mediaforce.encoding.cadence.run_command", local_run_command):
            local = analyze_cadence(Path("/media/tv/a.mkv"), video_stream=stream, duration_seconds=60.0)
        with patch("mediaforce.encoding.remote_media.run_remote_command", remote):
            measured_remotely = analyze_cadence(
                Path("/media/tv/a.mkv"),
                video_stream=stream,
                duration_seconds=60.0,
                command_runner=RemoteMediaCommands(self.mini),
            )

        self.assertEqual([call["command"] for call in remote.measurement_calls()], local_commands)
        self.assertEqual(measured_remotely["decision"], local["decision"])

    def test_remote_fingerprint_commands_are_the_local_commands(self) -> None:
        local_commands: list[list[str]] = []
        remote_commands: list[list[str]] = []

        class FinishedProcess:
            pid = 4321
            returncode = 0

            def __init__(self, command: list[str], **_kwargs: object) -> None:
                local_commands.append(command)

            @staticmethod
            def communicate(timeout: float | None = None) -> tuple[str, str]:
                _ = timeout
                return "", ""

        def remote_run(
                _host: dict[str, object],
                command: list[str],
                _timeout: int,
                input_text: str | None = None,
                process_controller: ManagedProcessController | None = None,
        ) -> subprocess.CompletedProcess[str]:
            _ = input_text, process_controller
            if command == ["ffmpeg", "-version"]:
                return subprocess.CompletedProcess(command, 0, REMOTE_FFMPEG_VERSION, "")
            remote_commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        arguments: dict[str, Any] = {
            "video_stream": {"codec_type": "video"},
            "audio_streams": [{"codec_type": "audio", "channels": 2}],
            "duration_seconds": 600.0,
        }
        with patch("mediaforce.encoding.fingerprint.ffmpeg_binary", return_value="ffmpeg"), patch(
            "mediaforce.encoding.fingerprint._ffmpeg_version",
            return_value=CONTROLLER_FFMPEG_VERSION,
        ), patch("mediaforce.encoding.fingerprint.subprocess.Popen", FinishedProcess):
            analyze_media_fingerprint(Path("/media/tv/a.mkv"), **arguments)
        with patch("mediaforce.encoding.remote_media.run_remote_command", remote_run):
            summary = analyze_media_fingerprint(
                Path("/media/tv/a.mkv"),
                command_runner=RemoteMediaCommands(self.mini),
                **arguments,
            )

        self.assertTrue(local_commands)
        self.assertEqual(remote_commands, local_commands)
        self.assertEqual(summary["analysis"]["tool"]["ffmpeg_version"], REMOTE_FFMPEG_VERSION)
        self.assertEqual(summary["analysis"]["tool"]["host"], "m1-mini.example")

    def test_waits_without_measuring_when_no_encode_computer_can_take_the_work(self) -> None:
        config = self._config([self.mini])
        item_id = self._prepare_cadence_item(config)
        analyzer = Mock(side_effect=AssertionError("must not measure on the controller"))
        remote = Mock(side_effect=AssertionError("must not reach an unavailable computer"))

        with self._no_local_media_commands(), patch("mediaforce.encoding.remote_media.run_remote_command", remote):
            processed = process_evidence_queue_once(
                config_path=config.paths.config_path,
                deps=self._deps(config, analyzer, host_rows=[_row(self.mini, available=False)]),
            )

        state, _summary = self._stored(config, item_id)
        self.assertTrue(processed)
        analyzer.assert_not_called()
        remote.assert_not_called()
        self.assertEqual(state["work_status"], "waiting_host")
        self.assertEqual(state["last_error"], EVIDENCE_HOST_WAIT_REASON)
        self.assertEqual(state["attempt_count"], 0)

    def test_work_waiting_for_an_encode_computer_stays_queued_and_returns_after_its_delay(self) -> None:
        config = self._config([self.mini])
        item_id = self._prepare_cadence_item(config)
        process_evidence_queue_once(
            config_path=config.paths.config_path,
            deps=self._deps(config, Mock(), host_rows=[_row(self.mini, available=False)]),
        )

        with open_db(config.paths.db_path) as connection:
            summary = evidence_queue_summary(connection)
            backlog = list_evidence_backlog(connection, work_status="waiting_host")
            early_claim = claim_next_evidence_work(connection, worker_id="worker-a", lease_seconds=5)
        with open_db(config.paths.db_path) as connection:
            later_claim = claim_next_evidence_work(
                connection,
                worker_id="worker-a",
                lease_seconds=5,
                now=datetime.now(UTC) + timedelta(minutes=5),
            )

        self.assertEqual(summary["status"], "queued")
        self.assertEqual(summary["remaining_count"], 1)
        self.assertEqual([row["library_item_id"] for row in backlog["rows"]], [item_id])
        self.assertIsNone(early_claim)
        assert later_claim is not None
        self.assertEqual(later_claim.library_item_id, item_id)

    def test_lost_connection_waits_for_the_computer_instead_of_failing_the_file(self) -> None:
        config = self._config([self.mini])
        item_id = self._prepare_cadence_item(config)
        remote = FakeEncodeComputers(
            idet_result=subprocess.CompletedProcess(
                ["ffmpeg"],
                255,
                "",
                "client_loop: send disconnect: Broken pipe\n",
            )
        )

        with self._no_local_media_commands(), patch("mediaforce.encoding.remote_media.run_remote_command", remote):
            process_evidence_queue_once(
                config_path=config.paths.config_path,
                deps=self._deps(config, probe_evidence, host_rows=[_row(self.mini)]),
            )

        state, summary = self._stored(config, item_id)
        self.assertIsNone(summary)
        self.assertEqual(state["work_status"], "waiting_host")
        self.assertEqual(state["last_error"], EVIDENCE_HOST_WAIT_REASON)
        self.assertEqual(state["attempt_count"], 0)

    def test_without_encode_computers_measures_locally_as_before(self) -> None:
        config = self._config([])
        item_id = self._prepare_cadence_item(config)
        calls: list[tuple[Path, dict[str, object]]] = []

        def analyzer(path: Path, _kind: str, **kwargs: object) -> dict[str, object]:
            calls.append((path, kwargs))
            return json.loads(_progressive_cadence_summary_json())

        host_rows = Mock(side_effect=AssertionError("no host check without encode computers"))
        processed = process_evidence_queue_once(
            config_path=config.paths.config_path,
            deps=self._deps(config, analyzer, host_rows=host_rows),
        )

        state, _summary = self._stored(config, item_id)
        self.assertTrue(processed)
        self.assertEqual([path for path, _kwargs in calls], [self.media_root / "show" / "item-1.mkv"])
        self.assertEqual(set(calls[0][1]), {"process_controller"})
        self.assertEqual(state["state"], EVIDENCE_STATE_CURRENT)

    def _config(self, remote_hosts: list[dict[str, Any]]) -> MediaforceConfig:
        return MediaforceConfig(
            raw={
                "media": {
                    "libraries": [
                        {"key": "tv", "path": str(self.media_root), "type": "tv", "availability": "production"}
                    ]
                },
                "remote_hosts": remote_hosts,
            },
            paths=ConfigPaths(
                project_root=self.project_root,
                config_path=self.project_root / "config.toml",
                db_path=self.project_root / "state" / "library.sqlite3",
                run_manifest_dir=self.project_root / "state" / "runs",
                web_state_dir=self.project_root / "state" / "web",
                review_dir=self.project_root / "state" / "review",
                runtime_settings_path=self.project_root / "state" / "settings.json",
                runtime_reservation_dir=self.project_root / "runtime-reservations",
            ),
        )

    def _prepare_cadence_item(self, config: MediaforceConfig) -> int:
        source_path = self.media_root / "show" / "item-1.mkv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"fixture")
        stat_result = source_path.stat()
        now = "2026-09-30T12:00:00+00:00"
        with open_db(config.paths.db_path) as connection:
            result = connection.execute(
                library_items.insert().values(
                    source_path=str(source_path),
                    rel_path="tv/show/item-1.mkv",
                    media_root="tv",
                    parent_dir="tv/show",
                    file_name="item-1.mkv",
                    container=".mkv",
                    size_bytes=stat_result.st_size,
                    mtime_ns=stat_result.st_mtime_ns,
                    fingerprint=file_fingerprint(source_path, stat_result, 60.0),
                    duration_seconds=60.0,
                    video_codec="h264",
                    audio_track_count=0,
                    subtitle_track_count=0,
                    english_audio_count=0,
                    english_subtitle_count=0,
                    audio_summary_json="[]",
                    subtitle_summary_json="[]",
                    content_version_changed_at=now,
                    content_version_fingerprint=content_version_fingerprint(source_path, stat_result),
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
            start_evidence_work(connection, config, "tv/show/item-1.mkv", evidence_kinds=[CADENCE_EVIDENCE_KIND])
            resume_evidence_queue(connection)
        return item_id

    @staticmethod
    def _deps(config: MediaforceConfig, analyzer: object, *, host_rows: object) -> EvidenceWorkerDeps:
        return EvidenceWorkerDeps(
            load_config=lambda _path: config,
            analyze_evidence=analyzer,  # type: ignore[arg-type]
            logger=Mock(),
            lease_seconds=5,
            heartbeat_seconds=0.05,
            source_retry_delay_seconds=1,
            evidence_host_rows=host_rows if callable(host_rows) else (lambda _config: host_rows),  # type: ignore[arg-type]
        )

    @staticmethod
    def _stored(config: MediaforceConfig, item_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
        with open_db(config.paths.db_path) as connection:
            state = load_library_item_evidence_states(connection, item_id)[CADENCE_EVIDENCE_KIND]
            summary_json = connection.execute(
                select(library_items.c.cadence_summary_json).where(library_items.c.id == item_id)
            ).scalar_one()
        return dict(state), json.loads(summary_json) if summary_json else None

    @staticmethod
    def _no_local_media_commands() -> ExitStack:
        refuse = Mock(side_effect=AssertionError("media command ran on the controller"))
        stack = ExitStack()
        for target in (
                "mediaforce.library.probe.run_command",
                "mediaforce.encoding.cadence.run_command",
                "mediaforce.encoding.cadence._ffmpeg_version",
        ):
            stack.enter_context(patch(target, refuse))
        return stack


class EvidenceHostSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mini = _host("m1-mini.example", "M1 mini", priority=5)
        self.mbp = _host("m2-mbp.example", "M2 MBP", priority=1)
        self.config = MediaforceConfig(
            raw={
                "media": {"libraries": [{"key": "tv", "path": "/media/tv", "type": "tv", "availability": "production"}]},
                "remote_hosts": [self.mbp, self.mini],
            },
            paths=Mock(),
        )

    def test_skips_computers_that_cannot_take_encode_work_now(self) -> None:
        ineligible_rows = {
            "unavailable": _row(self.mini, available=False),
            "schedule closed": _row(self.mini, schedule_open=False),
            "encode work turned off": _row(self.mini, capabilities=["sample_calibration"]),
            "no ffmpeg": _row(self.mini, issues=[FFMPEG_MISSING_ISSUE]),
        }
        for reason, mini_row in ineligible_rows.items():
            with self.subTest(reason):
                selected = select_evidence_host(self.config, [mini_row, _row(self.mbp)], media_root="tv")
                self.assertEqual(selected, self.mbp)

    def test_respects_the_libraries_a_computer_is_allowed(self) -> None:
        self.mini["allowed_libraries"] = ["movies"]

        self.assertEqual(select_evidence_host(self.config, [_row(self.mini), _row(self.mbp)], media_root="tv"), self.mbp)

    def test_streaming_computers_and_the_controller_never_measure(self) -> None:
        self.mini["media_access"] = "stream"
        self.mbp["mode"] = "local"

        self.assertIsNone(select_evidence_host(self.config, [_row(self.mini), _row(self.mbp)], media_root="tv"))


class RemoteEvidenceFreshnessTests(unittest.TestCase):
    def test_result_from_another_computer_is_not_stale_for_its_ffmpeg_build(self) -> None:
        summary = self._remote_fingerprint_summary()
        evidence, _decision = media_fingerprint_manifest_payload(
            summary,
            source_id="src1",
            source_fingerprint="fingerprint-1",
        )
        assert evidence is not None

        with patch("mediaforce.encoding.fingerprint._ffmpeg_version", return_value=CONTROLLER_FFMPEG_VERSION):
            staleness = media_fingerprint_staleness(evidence, source_id="src1", source_fingerprint="fingerprint-1")
        projection = project_evidence_state(
            MEDIA_FINGERPRINT_EVIDENCE_KIND,
            json.dumps(summary),
            source_fingerprint="fingerprint-1",
        )

        self.assertEqual(staleness, {"stale": False, "reasons": []})
        self.assertEqual(projection.state, EVIDENCE_STATE_CURRENT)
        self.assertEqual(projection.analyzer_runtime_version, REMOTE_FFMPEG_VERSION)

    def test_a_new_analyzer_version_still_makes_remote_evidence_stale(self) -> None:
        summary = self._remote_fingerprint_summary()
        summary["analysis"]["tool"]["version"] = f"{MEDIA_FINGERPRINT_TOOL_VERSION}-previous"
        evidence, _decision = media_fingerprint_manifest_payload(
            summary,
            source_id="src1",
            source_fingerprint="fingerprint-1",
        )
        assert evidence is not None

        staleness = media_fingerprint_staleness(evidence, source_id="src1", source_fingerprint="fingerprint-1")
        projection = project_evidence_state(
            MEDIA_FINGERPRINT_EVIDENCE_KIND,
            json.dumps(summary),
            source_fingerprint="fingerprint-1",
        )

        self.assertEqual(staleness["reasons"], ["tool_changed"])
        self.assertEqual(projection.state, EVIDENCE_STATE_ANALYSIS_REQUIRED)
        self.assertEqual(projection.reason, EVIDENCE_REASON_TOOL_CHANGED)

    @staticmethod
    def _remote_fingerprint_summary() -> dict[str, Any]:
        runner = Mock(spec=RemoteMediaCommands)
        runner.tool_lineage.return_value = {
            "ffmpeg_version": REMOTE_FFMPEG_VERSION,
            "host": "m1-mini.example",
            "host_label": "M1 mini",
        }
        analysis_frames = "\n".join(
            f"frame:{index} pts:{index}\nlavfi.signalstats.YAVG=60.0\nlavfi.signalstats.YDIF=2.0"
            for index in range(60)
        )
        runner.run.return_value = subprocess.CompletedProcess([], 0, analysis_frames, "")
        summary = fingerprint.analyze_media_fingerprint(
            Path("/Volumes/M1 mini/tv/a.mkv"),
            video_stream={"codec_type": "video"},
            audio_streams=[],
            duration_seconds=600.0,
            command_runner=runner,
        )
        assert summary["decision"]["status"] == "measured", summary["decision"]
        return summary


def _progressive_cadence_summary_json() -> str:
    return json.dumps(
        analyze_cadence(
            Path("unused.mkv"),
            video_stream={"field_order": "progressive", "avg_frame_rate": "24/1", "r_frame_rate": "24/1"},
            duration_seconds=60.0,
        )
    )


if __name__ == "__main__":
    unittest.main()
