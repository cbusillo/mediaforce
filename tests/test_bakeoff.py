import json
from pathlib import Path
import tempfile
import unittest
from typing import Any

from mediaforce.core.config import load_config
from mediaforce.encoding.bakeoff import build_bakeoff_plan, write_bakeoff_plan


def _manifest() -> dict[str, Any]:
    return {
        "run_id": "test-run",
        "items": [
            {
                "source_path": "/media/tv/Show/Season 1/Episode.mkv",
                "rel_path": "tv/Show/Season 1/Episode.mkv",
                "source_size_bytes": 4_294_967_296,
                "duration_seconds": 2700.0,
                "width": 1920,
                "height": 1080,
                "video_codec": "hevc",
                "source_fingerprint": "fingerprint-1",
                "output_container": "mkv",
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
                "attachment_summary": [],
                "resolved_policy": {
                    "video": {
                        "encoder": "libsvtav1",
                        "pixel_format": "yuv420p10le",
                        "preset": 4,
                        "quality_metric": "vmaf",
                        "target_vmaf": 85.0,
                        "min_target_vmaf": 80.0,
                        "target_xpsnr": 39.0,
                        "min_target_xpsnr": 35.0,
                        "sample_every": "8m",
                        "sample_duration": "20s",
                        "min_crf": 18,
                        "max_crf": 38,
                        "max_encoded_percent": 80,
                        "default_grain": 8,
                        "grain_denoise": 0,
                        "max_height": 1080,
                        "target_size_mb": 300,
                        "target_runtime_minutes": 45,
                        "decision_model": "size_first_review",
                        "quality_engine": "ab_av1_fast_sample",
                    },
                    "audio": {},
                    "subtitle": {},
                    "planning": {},
                },
            }
        ],
    }


class BakeoffPlanTests(unittest.TestCase):
    def test_build_bakeoff_plan_uses_size_first_defaults_and_candidates(self) -> None:
        config = load_config(Path("config/defaults.toml"))
        plan = build_bakeoff_plan(
            config,
            _manifest(),
            indexes=[0],
            output_dir=Path("/tmp/mediaforce-bakeoff"),
        )

        self.assertEqual(plan["decision_model"], "size_first_review")
        self.assertEqual(plan["default_targets"]["target_size_mb"], 300)
        self.assertEqual(plan["default_targets"]["min_target_vmaf"], 80.0)
        item = plan["items"][0]
        self.assertEqual(item["target_size_bytes"], 300_000_000)
        self.assertEqual(item["target_video_size_bytes"], 231_200_000)
        self.assertEqual(item["stream_budget_ledger"]["totals"]["non_video_bytes"], 68_800_000)
        self.assertEqual(item["resolved_operator_intent"]["size_goal"]["mode"], "normalized")
        self.assertEqual(item["duration_seconds"], 2700.0)
        self.assertEqual(item["resolution"], "1920x1080")
        self.assertEqual(item["quality_floor"], {"metric": "vmaf", "target": 85.0, "minimum": 80.0})
        engine_keys = [engine["key"] for engine in item["engines"]]
        self.assertEqual(engine_keys, ["ab-av1", "av1an", "xav", "auto-boost"])
        av1an = item["engines"][1]
        self.assertIn("scene-aware-candidate", av1an["category"])
        self.assertIn("ssimulacra2", av1an["metric_support"])
        self.assertIn("ssimulacra2", av1an["command"])
        self.assertEqual(av1an["command_status"], "template-needs-host-validation")
        self.assertIn("https://rust-av.github.io/Av1an/Features/TargetQuality", av1an["sources"])
        auto_boost = item["engines"][3]
        self.assertEqual(auto_boost["metric_support"], ["script-defined"])

    def test_build_bakeoff_plan_resolves_normalized_target_for_item_runtime(self) -> None:
        config = load_config(Path("config/defaults.toml"))
        manifest = _manifest()
        manifest["items"][0]["duration_seconds"] = 88 * 60

        plan = build_bakeoff_plan(config, manifest, indexes=[0], engines=["auto-boost"])

        item = plan["items"][0]
        self.assertEqual(item["target_size_bytes"], 586_666_667)
        command = item["engines"][0]["command"]
        self.assertEqual(item["target_video_size_bytes"], 454_080_000)
        self.assertEqual(command[command.index("--target-size-mb") + 1], "454.08")

    def test_build_bakeoff_plan_can_limit_engines(self) -> None:
        config = load_config(Path("config/defaults.toml"))
        plan = build_bakeoff_plan(config, _manifest(), indexes=[0], engines=["av1an"])

        self.assertEqual([engine["key"] for engine in plan["items"][0]["engines"]], ["av1an"])

    def test_build_bakeoff_plan_blocks_target_size_engine_when_runtime_is_missing(self) -> None:
        config = load_config(Path("config/defaults.toml"))
        manifest = _manifest()
        manifest["items"][0]["duration_seconds"] = 0

        plan = build_bakeoff_plan(config, manifest, indexes=[0], engines=["auto-boost"])

        item = plan["items"][0]
        self.assertIsNone(item["target_size_bytes"])
        self.assertEqual(item["size_goal_status"], "missing_item_runtime")
        self.assertIn("episode runtime", item["size_goal_issue"])
        engine = item["engines"][0]
        self.assertEqual(engine["command_status"], "blocked-unresolved-size-goal")
        self.assertEqual(engine["command"], [])

    def test_write_bakeoff_plan_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "nested" / "plan.json"
            write_bakeoff_plan({"ok": True}, output_path)

            self.assertEqual(json.loads(output_path.read_text()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
