from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaforce.advisor import apply_seed_policy, request_note_tuning
from mediaforce.config import ConfigPaths, HarnessConfig
from mediaforce.db import open_db
from mediaforce.tuning_memory import promote_learning_artifact, record_tuning_session, retrieve_learning_context


class TuningRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.config = HarnessConfig(
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

    def test_request_note_tuning_uses_structured_runtime_path_and_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "summary": "Lower XPSNR slightly to save more space.",
                    "diagnosis": "Current draft is conservative for clean 1080p TV.",
                    "confidence": "medium",
                    "evidence_checked": ["runtime_toolbelt.recent_sample_result", "retrieved_memory[0]"],
                    "suggested_follow_up": None,
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

        def fake_run(cmd, check, capture_output, text, timeout, cwd):
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

    def test_request_note_tuning_blocks_failed_self_check(self) -> None:
        responses = [
            json.dumps(
                {
                    "summary": "Crank grain up.",
                    "diagnosis": "Trying to protect quality.",
                    "confidence": "low",
                    "evidence_checked": ["runtime_toolbelt.current_policy_focus"],
                    "suggested_follow_up": None,
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

        def fake_run(cmd, check, capture_output, text, timeout, cwd):
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

    def test_learning_artifact_round_trip(self) -> None:
        sample_item = {
            "rel_path": "tv/Suits/Season 5/Episode.mkv",
            "video_codec": "h264",
            "recommendation": "priority_encode",
            "resolved_policy": {"video": {"quality_metric": "xpsnr"}},
        }
        response = {
            "summary": "Lower XPSNR slightly for clean TV episodes.",
            "diagnosis": "The season calibrates cleanly and can save more space.",
            "confidence": "medium",
            "evidence_checked": ["runtime_toolbelt.recent_sample_result"],
            "prompt_version": "tune-v2",
            "proposed_policy": {"video": {"target_xpsnr": 34.5}},
            "applied_policy": {"video": {"target_xpsnr": 34.5}},
            "self_check": {"status": "pass", "summary": "Looks safe.", "issues": []},
            "raw": "{}",
        }
        with open_db(self.config.paths.db_path) as connection:
            session_id = record_tuning_session(
                connection,
                prefix="tv/suits/season-5",
                note="Try slightly smaller.",
                response=response,
                applied_policy=response["applied_policy"],
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
                applied_policy=response["applied_policy"],
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

    def test_apply_seed_policy_ignores_null_tunable_fields(self) -> None:
        base_policy = {
            "video": {
                "target_vmaf": 95.0,
                "min_target_vmaf": 93.0,
                "target_xpsnr": 35.5,
                "min_target_xpsnr": 34.5,
                "max_encoded_percent": 55,
                "default_grain": 0,
            },
            "audio": {"surround_5_1_opus_bitrate": "224k"},
        }
        proposed_policy = {
            "video": {
                "default_grain": 1,
                "target_vmaf": None,
                "min_target_vmaf": None,
                "target_xpsnr": None,
                "min_target_xpsnr": None,
                "max_encoded_percent": None,
            },
            "audio": {"surround_5_1_opus_bitrate": None},
        }
        updated_policy, applied = apply_seed_policy(base_policy, proposed_policy)
        self.assertEqual(applied, {"video": {"default_grain": 1}})
        self.assertEqual(updated_policy["video"]["default_grain"], 1)
        self.assertEqual(updated_policy["video"]["target_vmaf"], 95.0)
        self.assertEqual(updated_policy["video"]["max_encoded_percent"], 55)
        self.assertEqual(updated_policy["audio"]["surround_5_1_opus_bitrate"], "224k")


if __name__ == "__main__":
    unittest.main()
