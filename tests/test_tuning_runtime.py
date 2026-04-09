import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.routing import APIRoute
from sqlalchemy import func
from sqlalchemy import select

from mediaforce.advisor import (
    RUN_VERDICT_PROMPT_VERSION,
    SEED_PROMPT_VERSION,
    SeedPolicyResponse,
    _build_prompt,
    _build_tune_prompt,
    _extract_seed_payload,
    _memory_disabled_code_args,
    _policy_response_schema,
    _build_run_verdict_prompt,
    _try_load_first_json_object,
    _build_seed_prompt,
    apply_seed_policy,
    request_seed_policy,
    request_tuning_advice,
    request_note_tuning,
    request_run_verdict,
)
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import calibration_jobs
from mediaforce.core.db_tables import learning_artifacts
from mediaforce.core.db_tables import tuning_sessions
from mediaforce.tuning.tuning_memory import (
    promote_learning_artifact,
    record_tuning_session,
    record_visual_approval_artifact,
    retrieve_learning_context,
)
from mediaforce.web.runtime.archive_cleanup import archive_cleanup_summary, clear_archive_cleanup_action
from mediaforce.web.app import (
    _advice_file,
    _build_seed_policy_payload,
    _maybe_seed_baseline_policy,
    _build_tuning_runtime_toolbelt,
    _calibration_file,
    _clear_folder_tuning_state,
    _multimodal_review_pack_public_view,
    _operator_requested_experiment,
    _planned_audio_review_context,
    _proposal_file,
    _review_pack_dir,
    _upsert_override,
)


class TuningRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = MediaforceConfig(
            raw={
                "state": {},
                "media": {
                    "source_roots": {"tv": str(self.root / "source" / "tv")},
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                },
                "remote_hosts": [],
            },
            paths=ConfigPaths(
                project_root=self.root,
                config_path=self.root / "config.toml",
                db_path=self.root / "library.sqlite3",
                run_manifest_dir=self.root / "runs",
                web_state_dir=self.root / "web",
                review_dir=self.root / "review",
                runtime_settings_path=self.root / "runtime.json",
            ),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _capture_subprocess_commands(self, response_body: str) -> tuple[list[list[str]], object]:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            return type("Result", (), {"returncode": 0, "stdout": response_body, "stderr": ""})()

        return commands, fake_run

    def _assert_structured_subprocess_call(self, commands: list[list[str]]) -> None:
        self.assertTrue(commands)
        self.assertEqual(commands[0][1:7], _memory_disabled_code_args())

    def test_request_note_tuning_uses_structured_runtime_path_and_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I can make it smaller without obvious blockiness, so I trimmed the quality target a bit.",
                    "request_disposition": "honored",
                    "summary": "Lower XPSNR slightly to save more space.",
                    "diagnosis": "Current draft is conservative for clean 1080p TV.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.recent_sample_result", "retrieved_memory[0]"],
                    "suggested_follow_up": None,
                    "feasibility_note": None,
                    "policy": {"video": {"target_xpsnr": 34.5, "max_encoded_percent": 50}},
                }
            ),
            json.dumps(
                {
                    "status": "pass",
                    "summary": "The proposal is consistent with the operator request and bounded evidence.",
                    "issues": [],
                }
            ),
        ]
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-5",
                    "operator_note": "Try slightly smaller without obvious blockiness.",
                    "runtime_toolbelt": {"recent_sample_result": {"quality_score": 95.0}},
                    "retrieved_memory": [{"title": "Prior Suits note"}],
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.toolbelt_used, ["recent_sample_result"])
        assert response.self_check is not None
        assert response.proposed_policy is not None
        self.assertEqual(response.self_check["status"], "pass")
        self.assertEqual(response.proposed_policy["video"]["target_xpsnr"], 34.5)
        self.assertEqual(response.request_disposition, "honored")
        self.assertIn("smaller", response.request_response)
        self.assertTrue(commands)
        self.assertEqual(commands[0][1:7], _memory_disabled_code_args())

    def test_archive_cleanup_summary_counts_files_and_size(self) -> None:
        archive_root = self.config.archive_root
        first = archive_root / "tv/show/episode-1.mkv"
        second = archive_root / "movies/demo.mp4"
        first.parent.mkdir(parents=True, exist_ok=True)
        second.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"abc")
        second.write_bytes(b"12345")

        summary = archive_cleanup_summary(self.config)

        self.assertTrue(summary["has_cleanup"])
        self.assertEqual(summary["file_count"], 2)
        self.assertEqual(summary["total_size_bytes"], 8)

    def test_clear_archive_cleanup_action_removes_files_and_prunes_directories(self) -> None:
        archive_root = self.config.archive_root
        archived = archive_root / "tv/show/episode-1.mkv"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text("backup")

        result = clear_archive_cleanup_action(self.config)

        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_count"], 1)
        self.assertFalse(archived.exists())
        self.assertFalse((archive_root / "tv/show").exists())
        self.assertTrue(archive_root.exists())
        self.assertEqual(result["archive_cleanup"]["file_count"], 0)

    def test_archive_cleanup_route_passes_transcode_root_to_action(self) -> None:
        from mediaforce.web import app as web_app

        captured: list[str | None] = []

        def fake_clear_archive_cleanup_action(
                _config: MediaforceConfig,
                *,
                transcode_root: str | None = None,
        ) -> dict[str, object]:
            captured.append(transcode_root)
            return {"ok": True, "transcode_root": transcode_root}

        with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                "mediaforce.web.app.purge_transient_artifacts"
        ), patch("mediaforce.web.app._start_calibration_queue_worker"), patch(
            "mediaforce.web.app._start_encode_queue_worker"
        ), patch("mediaforce.web.app._refresh_host_status_cache", return_value=[]), patch(
            "mediaforce.web.app.clear_archive_cleanup_action",
            side_effect=fake_clear_archive_cleanup_action,
        ):
            app = web_app.create_app(self.root / "config.toml")
            route = next(
                route
                for route in app.routes
                if isinstance(route, APIRoute) and route.path == "/api/archive-cleanup/clear"
            )

            class _FakeRequest:
                def __init__(self, payload: dict[str, str]) -> None:
                    self._payload = payload

                async def json(self) -> dict[str, str]:
                    return self._payload

            response = asyncio.run(cast(APIRoute, route).endpoint(_FakeRequest({"transcode_root": "/Volumes/media/transcode-alt"})))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads(response.body),
            {"ok": True, "transcode_root": "/Volumes/media/transcode-alt"},
        )
        self.assertEqual(captured, ["/Volumes/media/transcode-alt"])

    def test_request_tuning_advice_reads_last_message_output(self) -> None:
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            output_index = cmd.index("--output-last-message") + 1
            Path(cmd[output_index]).write_text("Recommendation\nTry one smaller sample.")
            return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_tuning_advice(
                project_root=self.root,
                payload={"folder": "tv/House/Season 2", "sample_result": {"quality_score": 91.4}},
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.summary, "Recommendation")
        self.assertIn("smaller sample", response.raw)
        self.assertTrue(commands)
        self.assertEqual(commands[0][1:7], _memory_disabled_code_args())

    def test_request_note_tuning_uses_multimodal_exec_when_review_pack_present(self) -> None:
        image_path = self.root / "review-pack.png"
        image_path.write_bytes(b"png")
        commands: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(kwargs, dict)
            commands.append(cmd)
            if "exec" in cmd:
                output_index = cmd.index("--output-last-message") + 1
                Path(cmd[output_index]).write_text(
                    json.dumps(
                        {
                            "request_response": "The attached review pack still looks clean enough to push a bit smaller.",
                            "request_disposition": "honored_with_risk",
                            "summary": "Use the review pack to justify one smaller pass.",
                            "diagnosis": "The attached source-versus-draft contact sheets do not show obvious new damage on the sampled moments.",
                            "confidence": "medium",
                            "evidence_checked": ["multimodal_review_pack.artifacts[0]",
                                                 "runtime_toolbelt.review_media_context"],
                            "suggested_follow_up": "If the next draft softens faces or dark scenes, stop there.",
                            "feasibility_note": None,
                            "policy": {"video": {"target_vmaf": 88.5, "max_crf": 41}},
                        }
                    )
                )
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type(
                "Result",
                (),
                {
                    "returncode": 0,
                    "stdout": json.dumps(
                        {
                            "status": "pass",
                            "summary": "The proposal is consistent with the multimodal evidence.",
                            "issues": [],
                        }
                    ),
                    "stderr": "",
                },
            )()

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "operator_note": "It still looks good in the review pack. Can we push a little more?",
                    "runtime_toolbelt": {
                        "review_media_context": {"review_media_ready": True, "moment_count": 2}
                    },
                    "multimodal_review_pack": {
                        "artifacts": [{"label": "Moment 1", "detail": "Top source, bottom draft"}],
                        "images": [str(image_path)],
                    },
                },
            )

        self.assertTrue(response.ok)
        assert response.proposed_policy is not None
        self.assertEqual(response.proposed_policy["video"]["target_vmaf"], 88.5)
        self.assertTrue(commands)
        self.assertIn("exec", commands[0])
        self.assertIn("--image", commands[0])
        self.assertEqual(commands[0][1:7], _memory_disabled_code_args())

    def test_request_note_tuning_blocks_failed_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "request_response": "I do not have enough evidence to justify that much extra grain.",
                    "request_disposition": "rejected",
                    "summary": "Crank grain up.",
                    "diagnosis": "Trying to protect quality.",
                    "confidence": "low",
                    "evidence_checked": ["runtime_toolbelt.current_policy_focus"],
                    "suggested_follow_up": None,
                    "feasibility_note": None,
                    "policy": {"video": {"default_grain": 20}},
                }
            ),
            json.dumps(
                {
                    "status": "fail",
                    "summary": "The proposal is too aggressive for the stated goal.",
                    "issues": ["Default grain increase is not supported by the supplied evidence."],
                }
            ),
        ]

        def fake_run(cmd: list[str], **kwargs: object) -> object:
            self.assertIsInstance(cmd, list)
            self.assertIsInstance(kwargs, dict)
            stdout = responses.pop(0)
            return type("Result", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_note_tuning(
                project_root=self.root,
                payload={
                    "folder": "tv/suits/season-5",
                    "operator_note": "Go smaller, but keep it clean.",
                    "runtime_toolbelt": {"current_policy_focus": {"video": {"default_grain": 0}}},
                    "retrieved_memory": [],
                },
            )

        self.assertFalse(response.ok)
        self.assertIsNone(response.proposed_policy)
        assert response.self_check is not None
        self.assertEqual(response.self_check["status"], "fail")

    def test_request_run_verdict_uses_structured_runtime_path(self) -> None:
        response_body = json.dumps(
            {
                "summary": "This aggressive sample landed much smaller and still looks usable on the checked moments.",
                "outcome": "acceptable_experiment",
                "confidence": "medium",
                "next_step": "Approve it if the remaining review moments still look clean.",
                "evidence_checked": ["sample_result", "operator_request"],
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_run_verdict(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "sample_result": {"quality_metric": "VMAF", "quality_score": 91.4},
                    "operator_request": {"metric": "vmaf", "target": 90.0},
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.prompt_version, RUN_VERDICT_PROMPT_VERSION)
        self.assertEqual(response.outcome, "acceptable_experiment")
        self.assertEqual(response.next_step, "Approve it if the remaining review moments still look clean.")
        self._assert_structured_subprocess_call(commands)

    def test_request_seed_policy_uses_structured_runner(self) -> None:
        response_body = json.dumps(
            {
                "request_response": "I would keep the first pass close to base and only lean slightly smaller.",
                "request_disposition": "softened",
                "summary": "Keep the base policy close and only lean a touch smaller.",
                "diagnosis": "The note asks for a tighter encode than a cold-start draft can justify.",
                "confidence": "medium",
                "evidence_checked": ["base_policy", "class_signals"],
                "suggested_follow_up": "Run one sample first, then ask for a tighter size target if it still looks roomy.",
                "feasibility_note": "Cold-start size budgets are rough until a measured sample exists.",
                "policy": {"video": {}}
            }
        )
        commands, fake_run = self._capture_subprocess_commands(response_body)

        with patch("mediaforce.advisor.subprocess.run", side_effect=fake_run):
            response = request_seed_policy(
                project_root=self.root,
                payload={
                    "folder": "tv/house/season-2",
                    "base_policy": {"video": {"target_vmaf": 94.5}},
                },
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.request_disposition, "softened")
        self.assertIn("first pass", response.request_response)
        self._assert_structured_subprocess_call(commands)

    def test_operator_requested_experiment_detects_literal_vmaf_target(self) -> None:
        request = _operator_requested_experiment("I want to try 85 VMAF on this show.")

        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["min_target_vmaf"], 83.0)

    def test_operator_requested_experiment_accepts_vmaf_of_phrasing(self) -> None:
        request = _operator_requested_experiment("Try a VMAF of 85 for this sample.")

        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)

    def test_operator_requested_experiment_detects_size_budget_request(self) -> None:
        request = _operator_requested_experiment(
            "I want to aim for 200MB per episode while losing as little fidelity as possible.",
            {
                "source_size_bytes": 4_815_446_620,
                "duration_seconds": 2660.352,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "size_budget")
        self.assertEqual(request["budget_label"], "200 MB per episode")
        self.assertEqual(request["feasibility"], "unreasonable")
        self.assertTrue(request["requires_confirmation"])
        self.assertAlmostEqual(request["estimated_source_percent"], 4.36, places=2)

    def test_operator_requested_experiment_detects_combined_budget_and_vmaf_request(self) -> None:
        request = _operator_requested_experiment(
            "I really want 200MB and VMAF of around 85.",
            {
                "source_size_bytes": 4_480_523_243,
                "duration_seconds": 2645.248,
                "audio_summary": [{"channels": 6}],
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
        )

        assert request is not None
        self.assertEqual(request["request_type"], "combined_experiment")
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["budget_label"], "200 MB per episode")
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["max_encoded_percent"], 10)

    def test_build_seed_policy_payload_carries_requested_experiment(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 2",
            user_note="Can we try to target 85 VMAF instead? Will that help?",
            base_policy={"video": {"target_vmaf": 90.0, "min_target_vmaf": 88.0}},
            sample_item={
                "rel_path": "tv/House/Season 2/House.S02E06.mkv",
                "source_size_bytes": 4_815_446_620,
                "video_codec": "h264",
                "video_bitrate": None,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2660.352,
                "audio_summary": [],
                "subtitle_summary": [],
                "recommendation": None,
                "recommendation_reason": None,
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 100_570_881_417,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": ["Season 2"],
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
        )

        request = payload["requested_experiment"]
        assert request is not None
        self.assertEqual(request["metric"], "vmaf")
        self.assertEqual(request["target"], 85.0)
        self.assertEqual(request["applied_policy"]["video"]["target_vmaf"], 85.0)

    def test_build_seed_policy_payload_marks_repeated_operator_confirmation(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 5",
            user_note="I really want 200MB and VMAF of around 85.",
            base_policy={"video": {"target_vmaf": 94.5, "min_target_vmaf": 93.0, "max_encoded_percent": 75}},
            sample_item={
                "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                "source_size_bytes": 4_480_523_243,
                "video_codec": "h264",
                "video_bitrate": None,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2645.248,
                "audio_summary": [{"channels": 6}],
                "subtitle_summary": [],
                "recommendation": None,
                "recommendation_reason": None,
                "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 76_892_875_827,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": ["Season 5"],
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
            recent_sessions_payload=[
                {
                    "note": "Try to hit around 200MB an episode. VMAF of around 85.",
                    "request_disposition": "softened",
                    "summary": "Too aggressive for a safe cold start.",
                    "created_at": "2026-04-04T01:29:02+00:00",
                }
            ],
        )

        repeat_signal = payload["operator_repeat_signal"]
        assert repeat_signal is not None
        self.assertEqual(repeat_signal["repeat_count"], 2)
        self.assertEqual(repeat_signal["previous_softened_count"], 1)

    def test_maybe_seed_baseline_policy_honors_repeated_explicit_request(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            record_tuning_session(
                connection,
                prefix="tv/House/Season 5",
                note="Try to hit around 200MB an episode. VMAF of around 85.",
                response={
                    "summary": "Too aggressive for a safe cold start.",
                    "diagnosis": "Prior request was softened.",
                    "confidence": "medium",
                    "raw": json.dumps({"request_disposition": "softened"}),
                },
                applied_policy={},
                toolbelt={},
                created_at="2026-04-04T01:29:02+00:00",
            )

            mocked_response = SeedPolicyResponse(
                ok=True,
                summary="Mildly smaller first-pass draft.",
                raw=json.dumps({"request_disposition": "softened"}),
                prompt_version=SEED_PROMPT_VERSION,
                diagnosis="The request is too aggressive for a cold start.",
                confidence="medium",
                evidence_checked=["base_policy"],
                suggested_follow_up="Measure one sample first.",
                request_disposition="softened",
                request_response="I softened that request.",
                feasibility_note="Cold-start budgets are rough.",
                proposed_policy={"video": {"target_vmaf": 94.0}},
            )

            with patch(
                "mediaforce.web.runtime.folder_tuning_advice.inspect_prefix",
                return_value={
                    "item_count": 24,
                    "total_size_bytes": 76_892_875_827,
                    "statuses": {"discovered": 24},
                    "video_codecs": {"h264": 24},
                    "audio_codecs": {"eac3:6": 24},
                    "seasons": ["Season 5"],
                },
            ), patch(
                "mediaforce.web.runtime.folder_tuning_advice.request_seed_policy",
                return_value=mocked_response,
            ):
                metadata = _maybe_seed_baseline_policy(
                    config=self.config,
                    prefix="tv/House/Season 5",
                    action="baseline",
                    user_note="I really want 200MB and VMAF of around 85.",
                    base_policy={"video": {"target_vmaf": 94.5, "min_target_vmaf": 93.0, "max_encoded_percent": 75}},
                    sample_item={
                        "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                        "source_size_bytes": 4_480_523_243,
                        "video_codec": "h264",
                        "video_bitrate": None,
                        "width": 1920,
                        "height": 1080,
                        "duration_seconds": 2645.248,
                        "audio_summary": [{"channels": 6}],
                        "subtitle_summary": [],
                        "resolved_policy": {"audio": {"surround_5_1_opus_bitrate": "224k"}},
                    },
                    existing_calibration=None,
                    connection=connection,
                )

        assert metadata is not None
        job_fields = metadata["job_fields"]
        self.assertEqual(job_fields["seed_request_disposition"], "honored_with_risk")
        self.assertEqual(job_fields["seed_applied_policy"]["video"]["target_vmaf"], 85.0)
        self.assertEqual(job_fields["seed_applied_policy"]["video"]["max_encoded_percent"], 10)

    def test_clear_folder_tuning_state_removes_thread_and_artifacts(self) -> None:
        prefix = "tv/House/Season 2"
        review_root = self.config.paths.review_dir / "run-123"
        preview_path = review_root / "item-00" / "encoded-01.mp4"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text("preview")
        review_pack_dir = _review_pack_dir(self.config, prefix, "request-1")
        review_pack_image = review_pack_dir / "review-video-moment-01.png"
        review_pack_image.parent.mkdir(parents=True, exist_ok=True)
        review_pack_image.write_text("pack")
        calibration_path = _calibration_file(self.config, prefix)
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_text(
            json.dumps(
                {
                    "mode": "sample",
                    "job_id": "job-1",
                    "preview_clips": [{"path": "/review-media/run-123/item-00/encoded-01.mp4"}],
                    "source_clips": [],
                    "compare_clips": [],
                }
            )
        )
        _advice_file(self.config, prefix).write_text(json.dumps({"summary": "Bench summary"}))
        _proposal_file(self.config, prefix).write_text(json.dumps({"proposal_id": "prop-1"}))

        with open_db(self.config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix=prefix,
                note="try smaller",
                response={
                    "summary": "Draft smaller",
                    "diagnosis": "Need a leaner sample",
                    "confidence": "medium",
                    "evidence_checked": [],
                    "prompt_version": "tune-v2",
                    "raw": "{}",
                },
                applied_policy={"video": {"target_vmaf": 90.0}},
                toolbelt={},
                created_at="2026-03-30T00:00:00+00:00",
            )
            artifact_path = self.root / "learned-memory" / "artifact.md"
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text("artifact")
            connection.execute(
                learning_artifacts.insert().values(
                    artifact_id="artifact-1",
                    session_id=session_id,
                    prefix=prefix,
                    title="artifact",
                    artifact_path=str(artifact_path),
                    summary="summary",
                    tags_json="[]",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="job-1",
                    prefix=prefix,
                    status="failed",
                    lane="sample",
                    action="ai_tune",
                    host_json="{}",
                    notes="note",
                    policy_json="{}",
                    sample_item_json="{}",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )

            result = _clear_folder_tuning_state(connection, config=self.config, prefix=prefix)

            self.assertTrue(result["ok"])
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(tuning_sessions).where(tuning_sessions.c.prefix == prefix)
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    select(func.count()).select_from(calibration_jobs).where(calibration_jobs.c.prefix == prefix)
                ).scalar_one(),
                0,
            )

        self.assertFalse(calibration_path.exists())
        self.assertFalse(_advice_file(self.config, prefix).exists())
        self.assertFalse(_proposal_file(self.config, prefix).exists())
        self.assertFalse(preview_path.exists())
        self.assertFalse(review_root.exists())
        self.assertFalse(review_pack_image.exists())
        self.assertFalse(review_pack_dir.exists())
        self.assertFalse(_review_pack_dir(self.config, prefix).exists())
        self.assertFalse((self.root / "learned-memory" / "artifact.md").exists())

    def test_multimodal_review_pack_public_view_uses_review_media_urls(self) -> None:
        pack_dir = _review_pack_dir(self.config, "tv/House/Season 2", "request-1")
        image_path = pack_dir / "review-video-moment-01.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_text("png")

        public_view = _multimodal_review_pack_public_view(
            self.config,
            {
                "artifacts": [
                    {
                        "kind": "video_contact_sheet",
                        "label": "Review moment 1",
                        "detail": "Top row source, bottom row preview.",
                    }
                ],
                "images": [str(image_path)],
                "audio_plan": {"summary": "Primary track eac3 is planned for Opus at 224k."},
            },
        )

        assert public_view is not None
        self.assertEqual(public_view["artifact_count"], 1)
        self.assertEqual(public_view["artifacts"][0]["image_url"],
                         f"/review-media/{image_path.relative_to(self.config.paths.review_dir).as_posix()}")
        self.assertEqual(
            public_view["audio_plan"]["summary"],
            "Primary track eac3 is planned for Opus at 224k.",
        )

    def test_review_pack_dir_is_unique_per_request(self) -> None:
        first = _review_pack_dir(self.config, "tv/House/Season 2", "request-1")
        second = _review_pack_dir(self.config, "tv/House/Season 2", "request-2")

        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, second.parent)
        self.assertEqual(first.parent, _review_pack_dir(self.config, "tv/House/Season 2"))

    def test_clear_folder_tuning_state_refuses_active_job(self) -> None:
        prefix = "tv/House/Season 2"
        with open_db(self.config.paths.db_path) as connection:
            connection.execute(
                calibration_jobs.insert().values(
                    job_id="job-active",
                    prefix=prefix,
                    status="queued",
                    lane="sample",
                    action="ai_tune",
                    host_json="{}",
                    notes="note",
                    policy_json="{}",
                    sample_item_json="{}",
                    created_at="2026-03-30T00:00:00+00:00",
                    updated_at="2026-03-30T00:00:00+00:00",
                )
            )

            result = _clear_folder_tuning_state(connection, config=self.config, prefix=prefix)

            self.assertFalse(result["ok"])
            self.assertIn("still active", result["message"])

    def test_learning_artifact_round_trip(self) -> None:
        sample_item = {
            "rel_path": "tv/Suits/Season 5/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "xpsnr"}},
        }
        applied_policy = {"video": {"target_xpsnr": 34.5}}
        response: dict[str, object] = {
            "summary": "Lower XPSNR slightly for clean TV episodes.",
            "diagnosis": "The season calibrates cleanly and can save more space.",
            "confidence": "medium",
            "evidence_checked": ["runtime_toolbelt.recent_sample_result"],
            "prompt_version": "tune-v2",
            "proposed_policy": {"video": {"target_xpsnr": 34.5}},
            "applied_policy": applied_policy,
            "self_check": {"status": "pass", "summary": "Looks safe.", "issues": []},
            "raw": "{}",
        }
        with open_db(self.config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix="tv/suits/season-5",
                note="Try slightly smaller.",
                response=response,
                applied_policy=applied_policy,
                toolbelt={"recent_sample_result": {"quality_score": 95.0}},
                created_at="2026-03-25T00:00:00+00:00",
            )
            artifact = promote_learning_artifact(
                connection,
                self.config,
                session_id=session_id,
                prefix="tv/suits/season-5",
                note="Try slightly smaller.",
                sample_item=sample_item,
                response=response,
                applied_policy=applied_policy,
                created_at="2026-03-25T00:00:00+00:00",
            )
            self.assertIsNotNone(artifact)
            context = retrieve_learning_context(
                connection,
                prefix="tv/suits/season-8",
                sample_item=sample_item,
                note="Need slightly smaller output.",
            )

        assert artifact is not None
        self.assertTrue(Path(artifact["artifact_path"]).exists())
        self.assertTrue(context)
        self.assertIn("Lower XPSNR slightly", context[0]["summary"])

    def test_record_visual_approval_artifact_creates_retrievable_memory(self) -> None:
        sample_item = {
            "rel_path": "tv/House/Season 2/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "vmaf"}},
        }
        calibration = {
            "job_id": "job-123",
            "policy": {"video": {"target_vmaf": 90.0, "min_target_vmaf": 88.0}},
            "sample_result": {"quality_metric": "VMAF", "quality_score": 91.4, "chosen_crf": 38.0},
        }

        with open_db(self.config.paths.db_path) as connection:
            artifact = record_visual_approval_artifact(
                connection,
                self.config,
                prefix="tv/House/Season 2",
                note="Looks good after review.",
                sample_item=sample_item,
                calibration=calibration,
                run_verdict={"summary": "Aggressive but acceptable for this show."},
                created_at="2026-03-29T23:45:00+00:00",
            )
            context = retrieve_learning_context(
                connection,
                prefix="tv/House/Season 2",
                sample_item=sample_item,
                note="Need a smaller approved draft.",
            )

        assert artifact is not None
        self.assertEqual(artifact["sample_job_id"], "job-123")
        self.assertTrue(Path(artifact["artifact_path"]).exists())
        self.assertTrue(context)
        self.assertIn("Aggressive but acceptable", context[0]["summary"])

    def test_apply_seed_policy_ignores_null_tunable_fields(self) -> None:
        base_policy = {
            "video": {
                "preset": 4,
                "target_vmaf": 95.0,
                "min_target_vmaf": 93.0,
                "target_xpsnr": 35.5,
                "min_target_xpsnr": 34.5,
                "max_encoded_percent": 55,
                "default_grain": 0,
            },
            "audio": {"surround_5_1_opus_bitrate": "224k", "keep_languages": ["eng"]},
            "subtitle": {"prefer_text": True},
        }
        proposed_policy = {
            "video": {
                "default_grain": 1,
                "preset": None,
                "target_vmaf": None,
                "min_target_vmaf": None,
                "target_xpsnr": None,
                "min_target_xpsnr": None,
                "max_encoded_percent": None,
            },
            "audio": {"surround_5_1_opus_bitrate": None, "keep_languages": None},
            "subtitle": {"prefer_text": None},
        }
        updated_policy, applied = apply_seed_policy(base_policy, proposed_policy)
        self.assertEqual(applied, {"video": {"default_grain": 1}})
        self.assertEqual(updated_policy["video"]["default_grain"], 1)
        self.assertEqual(updated_policy["video"]["target_vmaf"], 95.0)
        self.assertEqual(updated_policy["video"]["preset"], 4)
        self.assertEqual(updated_policy["video"]["max_encoded_percent"], 55)
        self.assertEqual(updated_policy["audio"]["surround_5_1_opus_bitrate"], "224k")
        self.assertEqual(updated_policy["audio"]["keep_languages"], ["eng"])
        self.assertTrue(updated_policy["subtitle"]["prefer_text"])

    def test_apply_seed_policy_supports_full_encode_policy_surface(self) -> None:
        base_policy = {
            "video": {
                "encoder": "libsvtav1",
                "pixel_format": "yuv420p10le",
                "preset": 4,
                "crf_search": True,
                "quality_metric": "auto",
                "target_vmaf": 95.0,
                "min_target_vmaf": 93.0,
                "target_xpsnr": 41.0,
                "min_target_xpsnr": 35.0,
                "sample_every": "8m",
                "sample_duration": "20s",
                "min_crf": 18,
                "max_crf": 38,
                "max_encoded_percent": 80,
                "default_grain": 8,
                "grain_denoise": 0,
                "thorough": True,
            },
            "audio": {
                "keep_languages": ["eng"],
                "copy_codecs": ["aac", "opus"],
                "convert_to_opus_codecs": ["ac3", "eac3"],
                "stereo_opus_bitrate": "128k",
                "surround_5_1_opus_bitrate": "256k",
                "surround_7_1_opus_bitrate": "320k",
            },
            "subtitle": {
                "keep_languages": ["eng"],
                "prefer_text": True,
                "keep_forced": True,
                "default_mode": "first_english",
            },
        }
        proposed_policy = {
            "video": {
                "preset": 6,
                "sample_every": "5m",
                "min_crf": 20,
                "max_crf": 36,
                "default_grain": 5,
                "thorough": False,
            },
            "audio": {
                "keep_languages": ["eng", "jpn"],
                "surround_5_1_opus_bitrate": "224k",
            },
            "subtitle": {
                "prefer_text": False,
                "default_mode": "none",
            },
        }

        updated_policy, applied = apply_seed_policy(base_policy, proposed_policy, mode="tune")

        self.assertEqual(updated_policy["video"]["preset"], 6)
        self.assertEqual(updated_policy["video"]["sample_every"], "5m")
        self.assertEqual(updated_policy["video"]["min_crf"], 20)
        self.assertEqual(updated_policy["video"]["max_crf"], 36)
        self.assertEqual(updated_policy["video"]["default_grain"], 5)
        self.assertFalse(updated_policy["video"]["thorough"])
        self.assertEqual(updated_policy["audio"]["keep_languages"], ["eng", "jpn"])
        self.assertEqual(updated_policy["audio"]["surround_5_1_opus_bitrate"], "224k")
        self.assertFalse(updated_policy["subtitle"]["prefer_text"])
        self.assertEqual(updated_policy["subtitle"]["default_mode"], "none")
        self.assertIn("subtitle", applied)

    def test_seed_prompt_adds_class_guardrails(self) -> None:
        prompt = _build_seed_prompt({"folder": "tv/House/Season 5"})

        self.assertEqual(SEED_PROMPT_VERSION, "seed-v4")
        self.assertIn("cold-start guess", prompt)
        self.assertIn("Teach media-class taste", prompt)
        self.assertIn("Do not chase dramatic savings", prompt)
        self.assertIn("operator_repeat_signal", prompt)
        self.assertIn("clean 1080p catalog TV", prompt)
        self.assertIn("request_response", prompt)
        self.assertIn("honored_with_risk", prompt)

    def test_extract_seed_payload_recovers_json_object_from_wrapped_text(self) -> None:
        raw = "Here you go:\n```json\n{\"summary\":\"safe\",\"policy\":{\"video\":{}}}\n```"

        payload = _extract_seed_payload(raw)

        self.assertEqual(payload["summary"], "safe")
        self.assertEqual(payload["policy"], {"video": {}})

    def test_try_load_first_json_object_skips_leading_text(self) -> None:
        raw = "noise before {\"status\":\"pass\",\"summary\":\"ok\",\"issues\":[]} trailing"

        payload = _try_load_first_json_object(raw)

        assert isinstance(payload, dict)
        self.assertEqual(payload["status"], "pass")

    def test_policy_response_schema_tracks_policy_shape(self) -> None:
        schema = _policy_response_schema(
            {
                "video": {"target_vmaf": 95.0, "thorough": True},
                "audio": {"keep_languages": ["eng"], "stereo_opus_bitrate": "128k"},
                "subtitle": {"prefer_text": True},
            }
        )

        self.assertEqual(schema["required"], ["video", "audio", "subtitle"])
        self.assertEqual(schema["properties"]["video"]["properties"]["target_vmaf"]["type"], ["number", "null"])
        self.assertEqual(schema["properties"]["video"]["properties"]["thorough"]["type"], ["boolean", "null"])
        self.assertEqual(schema["properties"]["audio"]["properties"]["keep_languages"]["type"], ["array", "null"])

    def test_generic_and_verdict_prompts_embed_context_and_shape(self) -> None:
        generic_prompt = _build_prompt({"folder": "tv/House/Season 2", "sample": {"score": 91.4}})
        verdict_prompt = _build_run_verdict_prompt({"folder": "tv/House/Season 2", "sample_result": {"quality_score": 91.4}})

        self.assertIn("Recommendation, Why, Setting changes, Audio/Subtitles notes", generic_prompt)
        self.assertIn("tv/House/Season 2", generic_prompt)
        self.assertIn("acceptable_experiment", verdict_prompt)
        self.assertIn("sample_result", verdict_prompt)

    def test_tune_prompt_mentions_review_media_conversation(self) -> None:
        prompt = _build_tune_prompt(
            {
                "folder": "tv/House/Season 2",
                "operator_note": "What do you think of the current review clips?",
                "runtime_toolbelt": {
                    "review_media_context": {
                        "review_media_ready": True,
                        "moment_count": 3,
                        "moments": [{"moment": 1, "timestamp_seconds": 89.0}],
                    }
                },
            }
        )

        self.assertIn("review_media_context", prompt)
        self.assertIn("current audio tradeoff", prompt)
        self.assertIn("never pretend", prompt)

    def test_tune_prompt_summarizes_multimodal_review_pack_without_paths(self) -> None:
        prompt = _build_tune_prompt(
            {
                "folder": "tv/House/Season 2",
                "operator_note": "What do you think of these artifacts?",
                "multimodal_review_pack": {
                    "artifacts": [{"label": "Moment 1", "detail": "Source on top, draft on bottom"}],
                    "images": ["/tmp/private-artifact.png"],
                },
            }
        )

        self.assertIn("multimodal_review_pack", prompt)
        self.assertIn("image_count", prompt)
        self.assertNotIn("/tmp/private-artifact.png", prompt)

    def test_build_tuning_runtime_toolbelt_summarizes_review_media(self) -> None:
        toolbelt = _build_tuning_runtime_toolbelt(
            sample_item={
                "rel_path": "tv/House/Season 2/House.S02E06.mkv",
                "source_size_bytes": 4_815_446_620,
                "resolved_policy": {},
            },
            current_policy={"video": {"target_vmaf": 89.0}},
            calibration={
                "sample_result": {"quality_score": 90.7, "quality_target": 89.0},
                "review_media_ready": True,
                "source_clips": [
                    {
                        "path": "/review-media/run-123/item-00/source-01.mp4",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                        "size_bytes": 27_525_266,
                    }
                ],
                "preview_clips": [
                    {
                        "path": "/review-media/run-123/item-00/encoded-01.mp4",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                        "size_bytes": 2_461_753,
                    }
                ],
                "compare_clips": [
                    {
                        "path": "/review-media/run-123/item-00/compare-01.mkv",
                        "timestamp_seconds": 89.0,
                        "duration_seconds": 8.0,
                    }
                ],
            },
            metric_support={"vmaf": True, "xpsnr": True},
        )

        review_media_context = toolbelt.get("review_media_context")
        assert isinstance(review_media_context, dict)
        self.assertTrue(review_media_context["review_media_ready"])
        self.assertEqual(review_media_context["moment_count"], 1)
        self.assertEqual(review_media_context["moments"][0]["preview_clip_size_bytes"], 2_461_753)
        self.assertEqual(review_media_context["moments"][0]["compare_clip_path"],
                         "/review-media/run-123/item-00/compare-01.mkv")

    def test_planned_audio_review_context_marks_copy_vs_transcode(self) -> None:
        transcode_context = _planned_audio_review_context(
            sample_item={"audio_summary": [{"codec_name": "eac3", "channels": 6, "language": "eng"}]},
            current_policy={
                "audio": {
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                    "surround_5_1_opus_bitrate": "224k",
                }
            },
        )
        copy_context = _planned_audio_review_context(
            sample_item={"audio_summary": [{"codec_name": "aac", "channels": 2, "language": "eng"}]},
            current_policy={
                "audio": {
                    "copy_codecs": ["aac", "opus"],
                    "convert_to_opus_codecs": ["eac3"],
                    "stereo_opus_bitrate": "128k",
                }
            },
        )

        self.assertEqual(transcode_context["action"], "libopus")
        self.assertEqual(transcode_context["target_bitrate"], "224k")
        self.assertEqual(copy_context["action"], "copy")
        self.assertIsNone(copy_context["target_bitrate"])

    def test_build_seed_policy_payload_surfaces_class_signals(self) -> None:
        payload = _build_seed_policy_payload(
            prefix="tv/House/Season 5",
            user_note="",
            base_policy={
                "video": {
                    "target_vmaf": 95.0,
                    "min_target_vmaf": 93.0,
                    "target_xpsnr": 41.0,
                    "min_target_xpsnr": 35.0,
                    "max_encoded_percent": 80,
                    "default_grain": 8,
                    "encoder": "libsvtav1",
                },
                "audio": {
                    "surround_5_1_opus_bitrate": "256k",
                    "copy_codecs": ["aac", "opus"],
                },
            },
            sample_item={
                "rel_path": "tv/House/Season 5/House.S05E22.mkv",
                "source_size_bytes": 4_700_000_000,
                "video_codec": "h264",
                "video_bitrate": 11_000_000,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 2645.248,
                "audio_summary": [{"codec_name": "eac3", "channels": 6}],
                "subtitle_summary": [{"codec_name": "subrip", "language": "eng"}],
                "recommendation": "priority_encode",
                "recommendation_reason": "Large H.264 season.",
            },
            summary={
                "item_count": 24,
                "total_size_bytes": 112_800_000_000,
                "statuses": {"discovered": 24},
                "video_codecs": {"h264": 24},
                "audio_codecs": {"eac3:6": 24},
                "seasons": {"Season 5": 24},
                "suggested_override": {
                    "video": {
                        "default_grain": 0,
                        "target_xpsnr": 35.5,
                        "min_target_xpsnr": 34.5,
                        "max_encoded_percent": 55,
                    },
                    "audio": {"surround_5_1_opus_bitrate": "224k"},
                    "planning": {"extra_score": 18},
                    "reason": [
                        "Mostly H.264, so this folder is a strong AV1 candidate.",
                        "Clean catalog TV usually does not need synthetic grain and can tolerate a lower XPSNR floor.",
                    ],
                },
            },
            metric_support={"vmaf": True, "xpsnr": True, "ssim": True, "psnr": True},
        )

        self.assertEqual(payload["sample_item"]["resolution_tier"], "1080p")
        self.assertEqual(payload["class_signals"]["collection_shape"], "tv_season")
        self.assertEqual(payload["preferred_metric"], "vmaf")
        self.assertEqual(payload["base_policy"]["audio"]["surround_5_1_opus_bitrate"], "256k")
        self.assertEqual(payload["base_policy"]["video"]["encoder"], "libsvtav1")
        self.assertEqual(payload["summary"]["suggested_override"]["policy_focus"]["audio"]["surround_5_1_opus_bitrate"],
                         "224k")
        self.assertIn(
            "Sample codec matches the folder majority codec (h264).",
            payload["class_signals"]["positive_signals"],
        )
        self.assertIn(
            "This first-pass seed is only a bounded starting point; measured calibration should confirm any lean move.",
            payload["class_signals"]["caution_flags"],
        )

    def test_shutdown_cleanup_cancels_managed_processes(self) -> None:
        from mediaforce.web import app as web_app

        calibration_controller = web_app.ManagedProcessController()
        encode_controller = web_app.ManagedProcessController()

        original_map = dict(web_app.CALIBRATION_QUEUE_PROCESSES)
        original_encode = web_app.ENCODE_QUEUE_PROCESS
        try:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()
            web_app.CALIBRATION_QUEUE_PROCESSES["job-1"] = calibration_controller
            web_app.ENCODE_QUEUE_PROCESS = encode_controller

            with patch("mediaforce.web.app.load_config", return_value=self.config), patch(
                    "mediaforce.web.app._start_calibration_queue_worker"
            ), patch("mediaforce.web.app._start_encode_queue_worker"), patch(
                "mediaforce.web.app._refresh_host_status_cache", return_value=[]
            ):
                app = web_app.create_app(self.root / "config.toml")
            import asyncio

            async def _exercise_lifespan() -> None:
                async with app.router.lifespan_context(app):
                    return None

            asyncio.run(_exercise_lifespan())

            self.assertTrue(calibration_controller.cancelled)
            self.assertTrue(encode_controller.cancelled)
        finally:
            web_app.CALIBRATION_QUEUE_PROCESSES.clear()
            web_app.CALIBRATION_QUEUE_PROCESSES.update(original_map)
            web_app.ENCODE_QUEUE_PROCESS = original_encode

    def test_upsert_override_replaces_existing_matching_block(self) -> None:
        override_file = self.root / "overrides.toml"
        override_file.write_text(
            """[[overrides]]
path_prefix = "tv/House/Season 2"
note = "Old note"

[overrides.video]
target_xpsnr = 33.0

[[overrides]]
path_prefix = "tv/Other"
note = "Keep me"
"""
        )

        _upsert_override(
            override_file,
            "tv/House/Season 2",
            {"video": {"target_xpsnr": 34.5}, "audio": {"bitrate_kbps": 224}},
        )

        updated = override_file.read_text()
        self.assertEqual(updated.count('path_prefix = "tv/House/Season 2"'), 1)
        self.assertIn('target_xpsnr = 34.5', updated)
        self.assertIn('bitrate_kbps = 224', updated)
        self.assertIn('path_prefix = "tv/Other"', updated)
        self.assertNotIn('target_xpsnr = 33.0', updated)

    def test_upsert_override_renders_nested_planning_tables_as_valid_toml(self) -> None:
        override_file = self.root / "overrides.toml"

        _upsert_override(
            override_file,
            "tv/House/Season 2",
            {
                "planning": {
                    "bucket_thresholds": {"priority_encode": 70, "review_encode": 35},
                    "codec_bonus": {"av1": -45, "default": 18, "h264": 40},
                }
            },
        )

        updated = override_file.read_text()
        self.assertIn("bucket_thresholds = { priority_encode = 70, review_encode = 35 }", updated)
        self.assertIn("codec_bonus = { av1 = -45, default = 18, h264 = 40 }", updated)


if __name__ == "__main__":
    unittest.main()
