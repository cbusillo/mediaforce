import json
import unittest
from pathlib import Path

from mediaforce.core.config import ConfigPaths, MediaforceConfig, _merge_runtime_settings
from mediaforce.library.planner import build_manifest_item
from mediaforce.tuning.size_goals import (
    DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
    DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
    OperatorIntent,
    ResolutionIntent,
    SizeGoalIntent,
    guided_size_goal_options,
    megabytes_to_bytes,
    operator_intent_from_policy,
    operator_intent_from_request,
)


class SizeGoalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.default_video = {
            "size_goal_schema_version": 1,
            "size_goal_mode": "normalized",
            "size_goal_source": "config_default",
            "target_size_mb": 300,
            "target_size_bytes": 300_000_000,
            "target_runtime_minutes": 45,
            "sample_projection_tolerance_percent": 10,
            "final_output_tolerance_percent": 5,
            "resolution_intent_mode": "max_height",
            "max_height": 1080,
        }

    def test_normalized_default_scales_to_item_runtime(self) -> None:
        intent = operator_intent_from_policy(self.default_video, default_video_policy=self.default_video)

        resolved_megabytes = [
            intent.size_goal.resolve(minutes * 60).to_payload()["target_size_mb"]
            for minutes in (22.5, 45, 88, 90)
        ]

        self.assertEqual(resolved_megabytes, [150.0, 300.0, 586.667, 600.0])

    def test_absolute_goal_does_not_scale_with_runtime(self) -> None:
        size_goal = SizeGoalIntent(
            mode="absolute",
            value_bytes=megabytes_to_bytes(225),
            reference_runtime_seconds=None,
            sample_projection_tolerance_percent=10,
            final_output_tolerance_percent=5,
            source="operator",
        )

        self.assertEqual(size_goal.resolve(22.5 * 60).target_size_bytes, 225_000_000)
        self.assertEqual(size_goal.resolve(90 * 60).target_size_bytes, 225_000_000)

    def test_normalized_goal_with_missing_runtime_cannot_start(self) -> None:
        resolved = operator_intent_from_policy(
            self.default_video,
            default_video_policy=self.default_video,
        ).size_goal.resolve(None)

        self.assertEqual(resolved.status, "missing_item_runtime")
        self.assertTrue(resolved.requires_confirmation)
        self.assertIsNone(resolved.target_size_bytes)
        self.assertEqual(
            guided_size_goal_options(
                self.default_video,
                item_runtime_seconds=None,
                default_video_policy=self.default_video,
            ),
            [],
        )

    def test_legacy_unknown_goal_is_ambiguous(self) -> None:
        intent = operator_intent_from_policy(
            {"target_size_mb": 225, "target_runtime_minutes": 45, "max_height": 1080},
            default_video_policy=self.default_video,
        )

        self.assertEqual(intent.size_goal.mode, "ambiguous")
        self.assertTrue(intent.requires_confirmation)
        self.assertIsNone(intent.to_payload(item_runtime_seconds=88 * 60)["request"])

    def test_legacy_folder_override_with_item_runtime_is_inferred_absolute(self) -> None:
        config = self._config_with_override({"target_size_mb": 225, "target_runtime_minutes": 88})

        self.assertEqual(config.resolve_policy("tv/show/episode.mkv")["video"]["size_goal_mode"], "absolute")

    def test_legacy_folder_override_with_default_runtime_is_ambiguous(self) -> None:
        config = self._config_with_override({"target_size_mb": 225, "target_runtime_minutes": 45})

        self.assertEqual(config.resolve_policy("tv/show/episode.mkv")["video"]["size_goal_mode"], "ambiguous")

    def test_legacy_default_folder_override_remains_normalized(self) -> None:
        config = self._config_with_override({"target_size_mb": 300, "target_runtime_minutes": 45})

        self.assertEqual(config.resolve_policy("tv/show/episode.mkv")["video"]["size_goal_mode"], "normalized")

    def test_legacy_runtime_defaults_replace_stale_default_bytes(self) -> None:
        merged = _merge_runtime_settings(
            {"video": dict(self.default_video)},
            {"video": {"target_size_mb": 225}},
        )

        self.assertEqual(merged["video"]["target_size_bytes"], 225_000_000)
        self.assertEqual(merged["video"]["size_goal_mode"], "normalized")
        intent = operator_intent_from_policy(
            merged["video"],
            default_video_policy=merged["video"],
        )
        self.assertEqual(intent.size_goal.resolve(45 * 60).target_size_bytes, 225_000_000)

    def test_typed_request_preserves_absolute_size_and_source_resolution(self) -> None:
        intent = operator_intent_from_request(
            {
                "schema_version": 1,
                "size_goal": {
                    "mode": "absolute",
                    "value_mb": 225,
                    "sample_projection_tolerance_percent": 10,
                    "final_output_tolerance_percent": 5,
                },
                "resolution": {"mode": "source"},
            }
        )

        self.assertEqual(intent.size_goal.resolve(88 * 60).target_size_bytes, 225_000_000)
        self.assertEqual(
            intent.policy_fragment(item_runtime_seconds=88 * 60),
            {
                "video": {
                    "size_goal_schema_version": 1,
                    "size_goal_mode": "absolute",
                    "size_goal_source": "operator",
                    "sample_projection_tolerance_percent": 10.0,
                    "final_output_tolerance_percent": 5.0,
                    "target_size_bytes": 225_000_000,
                    "target_size_mb": 225.0,
                    "target_runtime_minutes": 88.0,
                    "resolution_intent_mode": "source",
                    "resolution_intent_source": "operator",
                    "max_height": 0,
                }
            },
        )

    def test_guided_options_keep_normalized_reference_semantics(self) -> None:
        options = guided_size_goal_options(
            self.default_video,
            item_runtime_seconds=88 * 60,
            default_video_policy=self.default_video,
        )

        self.assertEqual([option["key"] for option in options], ["recommended", "smaller", "roomier"])
        self.assertEqual(
            [option["resolved_size_goal"]["target_size_mb"] for option in options],
            [586.667, 440.0, 880.0],
        )
        self.assertEqual(
            [option["operator_intent"]["size_goal"]["value_mb"] for option in options],
            [300.0, 225.0, 450.0],
        )
        self.assertTrue(
            all(option["operator_intent"]["size_goal"]["mode"] == "normalized" for option in options)
        )

    def test_ambiguous_legacy_goal_requires_an_explicit_mode_choice(self) -> None:
        options = guided_size_goal_options(
            {"target_size_mb": 225, "target_runtime_minutes": 45, "max_height": 1080},
            item_runtime_seconds=88 * 60,
            default_video_policy=self.default_video,
        )

        self.assertEqual([option["key"] for option in options], ["confirm_normalized", "confirm_absolute"])
        self.assertTrue(all(option["requires_explicit_selection"] for option in options))
        self.assertEqual(
            [option["operator_intent"]["size_goal"]["mode"] for option in options],
            ["normalized", "absolute"],
        )
        self.assertEqual(
            [option["resolved_size_goal"]["target_size_mb"] for option in options],
            [440.0, 225.0],
        )

    def test_default_tolerances_are_distinct(self) -> None:
        intent = OperatorIntent(
            size_goal=SizeGoalIntent(
                mode="absolute",
                value_bytes=megabytes_to_bytes(300),
                reference_runtime_seconds=None,
                sample_projection_tolerance_percent=DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
                final_output_tolerance_percent=DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
                source="operator",
            ),
            resolution=ResolutionIntent("source", None, "operator"),
        )

        payload = intent.to_payload(item_runtime_seconds=45 * 60)["size_goal"]
        self.assertEqual(payload["sample_lower_bound_bytes"], 270_000_000)
        self.assertEqual(payload["sample_upper_bound_bytes"], 330_000_000)
        self.assertEqual(payload["final_lower_bound_bytes"], 285_000_000)
        self.assertEqual(payload["final_upper_bound_bytes"], 315_000_000)

    def test_typed_request_round_trips_quality_and_stream_constraints(self) -> None:
        policy_intent = operator_intent_from_policy(
            {**self.default_video, "quality_metric": "vmaf", "target_vmaf": 85, "min_target_vmaf": 80},
            default_video_policy=self.default_video,
            audio_policy={"keep_languages": ["eng"], "stereo_opus_bitrate": "128k"},
            subtitle_policy={"keep_languages": ["eng"], "keep_forced": True},
        )

        request_intent = operator_intent_from_request(policy_intent.request_payload())
        fragment = request_intent.policy_fragment(item_runtime_seconds=45 * 60)

        self.assertEqual(fragment["video"]["quality_metric"], "vmaf")
        self.assertEqual(fragment["video"]["min_target_vmaf"], 80.0)
        self.assertEqual(fragment["audio"]["keep_languages"], ["eng"])
        self.assertEqual(fragment["subtitle"]["keep_forced"], True)

    def test_manifest_persists_the_per_item_resolved_contract(self) -> None:
        config = self._config_with_override({})
        item = build_manifest_item(
            {
                "id": 1,
                "source_path": "/tmp/source/tv/show/episode.mkv",
                "rel_path": "tv/show/episode.mkv",
                "media_root": "tv",
                "fingerprint": "fingerprint-1",
                "size_bytes": 4_000_000_000,
                "video_codec": "h264",
                "video_bitrate": 5_000_000,
                "width": 1920,
                "height": 1080,
                "duration_seconds": 88 * 60,
                "container": "mkv",
                "status": "discovered",
                "audio_track_count": 1,
                "english_audio_count": 1,
                "english_subtitle_count": 1,
                "audio_summary_json": json.dumps([]),
                "subtitle_summary_json": json.dumps([]),
            },
            config,
        )

        resolved = item["resolved_operator_intent"]["size_goal"]
        self.assertEqual(resolved["mode"], "normalized")
        self.assertEqual(resolved["target_size_bytes"], 586_666_667)
        self.assertEqual(resolved["sample_projection_tolerance_percent"], 10.0)
        self.assertEqual(resolved["final_output_tolerance_percent"], 5.0)

    def _config_with_override(self, video_override: dict[str, object]) -> MediaforceConfig:
        paths = ConfigPaths(
            project_root=Path("/tmp/mediaforce-size-goal-tests"),
            config_path=Path("/tmp/mediaforce-size-goal-tests/config.toml"),
            db_path=Path("/tmp/mediaforce-size-goal-tests/library.sqlite3"),
            run_manifest_dir=Path("/tmp/mediaforce-size-goal-tests/runs"),
            web_state_dir=Path("/tmp/mediaforce-size-goal-tests/web"),
            review_dir=Path("/tmp/mediaforce-size-goal-tests/review"),
            runtime_settings_path=Path("/tmp/mediaforce-size-goal-tests/runtime.json"),
        )
        return MediaforceConfig(
            raw={
                "video": self.default_video,
                "audio": {},
                "subtitle": {},
                "planning": {},
                "overrides": [{"path_prefix": "tv/show", "video": video_override}],
                "media": {
                    "source_roots": {},
                    "staging_root": "/tmp",
                    "archive_root": "/tmp",
                    "output_container": "mkv",
                },
            },
            paths=paths,
        )


if __name__ == "__main__":
    unittest.main()
