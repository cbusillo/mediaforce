import json
from pathlib import Path
import tempfile
import unittest

from mediaforce.core.config import load_config
from mediaforce.encoding.bakeoff import build_bakeoff_plan, write_bakeoff_plan


def _manifest() -> dict[str, object]:
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
        self.assertEqual(item["target_size_bytes"], 314_572_800)
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

    def test_build_bakeoff_plan_can_limit_engines(self) -> None:
        config = load_config(Path("config/defaults.toml"))
        plan = build_bakeoff_plan(config, _manifest(), indexes=[0], engines=["av1an"])

        self.assertEqual([engine["key"] for engine in plan["items"][0]["engines"]], ["av1an"])

    def test_write_bakeoff_plan_creates_parent_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "nested" / "plan.json"
            write_bakeoff_plan({"ok": True}, output_path)

            self.assertEqual(json.loads(output_path.read_text()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
