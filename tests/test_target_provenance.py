import copy
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, DEFAULT_CONFIG_PATH, MediaforceConfig
from mediaforce.library.run_manifests import build_run_manifest
from mediaforce.tuning.target_provenance import (
    EXACT_ITEM_TARGET_BELOW_QUALITY_SAFE_MINIMUM,
    exact_item_target_provenance_blocker,
    target_size_provenance,
)


class TargetProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.rel_path = "tv/Example/Season 01/Episode 08.mkv"
        self.fingerprint = "episode-08-current"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_inherited_target_below_exact_measured_minimum_blocks_without_mutation(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        policy = config.resolve_policy(self.rel_path)
        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=policy,
            duration_seconds=2_700.0,
        )
        item = self._item(provenance)
        original_item = copy.deepcopy(item)
        original_overrides = copy.deepcopy(config.overrides)

        with patch(
            "mediaforce.tuning.target_provenance.load_current_quality_search_observations",
            return_value=[self._observation(
                rel_path=self.rel_path,
                fingerprint=self.fingerprint,
                policy_hash=str(provenance["policy_hash"]),
                minimum=523_000_000,
            )],
        ) as load_observations:
            blocker = exact_item_target_provenance_blocker(object(), item)  # type: ignore[arg-type]

        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.code, EXACT_ITEM_TARGET_BELOW_QUALITY_SAFE_MINIMUM)
        self.assertEqual(blocker.requested_target_bytes, 225_000_000)
        self.assertEqual(blocker.quality_safe_minimum_bytes, 523_000_000)
        self.assertEqual(provenance["source"], "ancestor_override")
        self.assertEqual(provenance["override_prefix"], "tv/Example")
        self.assertEqual(item, original_item)
        self.assertEqual(config.overrides, original_overrides)
        self.assertEqual(load_observations.call_args.kwargs["scope"].match, "exact_item")
        self.assertEqual(load_observations.call_args.kwargs["scope"].prefix, self.rel_path)

    def test_exact_override_preserves_explicit_target_without_blocking(self) -> None:
        config = self._config(overrides=[self._override(self.rel_path, 225_000_000)])
        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )

        with patch("mediaforce.tuning.target_provenance.load_current_quality_search_observations") as load_observations:
            blocker = exact_item_target_provenance_blocker(object(), self._item(provenance))  # type: ignore[arg-type]

        self.assertEqual(provenance["source"], "exact_override")
        self.assertIsNone(blocker)
        load_observations.assert_not_called()

    def test_transient_exact_target_uses_effective_policy_provenance(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        effective_policy = config.resolve_policy(self.rel_path)
        effective_policy["video"].update({
            "target_size_bytes": 523_000_000,
            "target_size_mb": 523,
            "size_goal_mode": "absolute",
        })

        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=effective_policy,
            duration_seconds=2_700.0,
        )

        self.assertEqual(provenance["source"], "exact_override")
        self.assertEqual(provenance["override_prefix"], self.rel_path)
        self.assertEqual(provenance["requested_target_bytes"], 523_000_000)

    def test_metadata_only_override_does_not_claim_target_provenance(self) -> None:
        config = self._config(overrides=[{
            "path_prefix": "tv/Example",
            "video": {
                "size_goal_schema_version": 1,
                "size_goal_mode": "absolute",
                "size_goal_source": "metadata-only",
                "sample_projection_tolerance_percent": 5,
                "final_output_tolerance_percent": 5,
            },
        }])

        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )

        self.assertEqual(provenance["source"], "config_default")
        self.assertIsNone(provenance["override_prefix"])

    def test_sibling_and_stale_observations_do_not_authorize_an_exact_item(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )
        observations = [
            self._observation(
                rel_path="tv/Example/Season 01/Episode 07.mkv",
                fingerprint=self.fingerprint,
                policy_hash=str(provenance["policy_hash"]),
                minimum=523_000_000,
            ),
            self._observation(
                rel_path=self.rel_path,
                fingerprint="stale-fingerprint",
                policy_hash=str(provenance["policy_hash"]),
                minimum=523_000_000,
            ),
        ]

        with patch(
            "mediaforce.tuning.target_provenance.load_current_quality_search_observations",
            return_value=observations,
        ):
            blocker = exact_item_target_provenance_blocker(object(), self._item(provenance))  # type: ignore[arg-type]

        self.assertIsNone(blocker)

    def test_truncated_candidate_trace_does_not_authorize_blocker(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )
        observation = self._observation(
            rel_path=self.rel_path,
            fingerprint=self.fingerprint,
            policy_hash=str(provenance["policy_hash"]),
            minimum=523_000_000,
        )
        trace = json.loads(str(observation["candidate_trace_json"]))
        trace["truncated"] = True
        observation["candidate_trace_json"] = json.dumps(trace)

        with patch(
            "mediaforce.tuning.target_provenance.load_current_quality_search_observations",
            return_value=[observation],
        ):
            blocker = exact_item_target_provenance_blocker(object(), self._item(provenance))  # type: ignore[arg-type]

        self.assertIsNone(blocker)

    def test_candidate_trace_without_completeness_marker_does_not_authorize_blocker(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )
        observation = self._observation(
            rel_path=self.rel_path,
            fingerprint=self.fingerprint,
            policy_hash=str(provenance["policy_hash"]),
            minimum=523_000_000,
        )
        trace = json.loads(str(observation["candidate_trace_json"]))
        del trace["truncated"]
        observation["candidate_trace_json"] = json.dumps(trace)

        with patch(
            "mediaforce.tuning.target_provenance.load_current_quality_search_observations",
            return_value=[observation],
        ):
            blocker = exact_item_target_provenance_blocker(object(), self._item(provenance))  # type: ignore[arg-type]

        self.assertIsNone(blocker)

    def test_config_default_provenance_is_displayable_without_measured_minimum(self) -> None:
        config = self._config()

        provenance = target_size_provenance(
            source_config=config,
            rel_path=self.rel_path,
            effective_policy=config.resolve_policy(self.rel_path),
            duration_seconds=2_700.0,
        )

        self.assertEqual(provenance["schema_version"], 1)
        self.assertEqual(provenance["source"], "config_default")
        self.assertIsNone(provenance["override_prefix"])
        self.assertGreater(provenance["requested_target_bytes"], 0)

    def test_manifest_persists_rederived_target_provenance(self) -> None:
        config = self._config(overrides=[self._override("tv/Example", 225_000_000)])
        row = {
            "id": 8,
            "source_path": str(self.root / "source" / self.rel_path),
            "rel_path": self.rel_path,
            "media_root": "tv",
            "fingerprint": self.fingerprint,
            "content_version_fingerprint": None,
            "size_bytes": 1_000_000_000,
            "duration_seconds": 2_700.0,
            "video_codec": "h264",
            "video_bitrate": 4_000_000,
            "width": 1920,
            "height": 1080,
            "container": "mkv",
            "status": "planned",
            "audio_summary_json": "[]",
            "subtitle_summary_json": "[]",
            "attachment_summary_json": "[]",
            "cadence_summary_json": None,
            "media_fingerprint_json": None,
            "audio_track_count": 0,
            "subtitle_track_count": 0,
            "english_audio_count": 0,
            "english_subtitle_count": 0,
        }

        manifest = build_run_manifest([row], config)

        provenance = manifest["items"][0]["target_size_provenance"]
        self.assertEqual(provenance["schema_version"], 1)
        self.assertEqual(provenance["source"], "ancestor_override")
        self.assertEqual(provenance["override_prefix"], "tv/Example")
        self.assertEqual(provenance["requested_target_bytes"], 225_000_000)
        self.assertEqual(provenance["status"], "resolved")
        self.assertEqual(provenance["size_goal_mode"], "absolute")
        self.assertTrue(provenance["policy_hash"])

    def _config(self, *, overrides: list[dict[str, object]] | None = None) -> MediaforceConfig:
        with DEFAULT_CONFIG_PATH.open("rb") as handle:
            raw = tomllib.load(handle)
        raw["overrides"] = overrides or []
        raw["video"].update({
            "target_size_bytes": 300_000_000,
            "target_size_mb": 300,
            "size_goal_schema_version": 1,
            "size_goal_mode": "absolute",
            "size_goal_source": "test",
        })
        return MediaforceConfig(
            raw=raw,
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

    @staticmethod
    def _override(prefix: str, target_bytes: int) -> dict[str, object]:
        return {
            "path_prefix": prefix,
            "video": {
                "target_size_bytes": target_bytes,
                "target_size_mb": target_bytes / 1_000_000,
                "size_goal_schema_version": 1,
                "size_goal_mode": "absolute",
                "size_goal_source": "test",
            },
        }

    def _item(self, provenance: dict[str, object]) -> dict[str, object]:
        return {
            "rel_path": self.rel_path,
            "source_fingerprint": self.fingerprint,
            "target_size_provenance": provenance,
        }

    @staticmethod
    def _observation(
            *,
            rel_path: str,
            fingerprint: str,
            policy_hash: str,
            minimum: int,
    ) -> dict[str, object]:
        return {
            "observation_id": "qso1_exact-minimum",
            "source_rel_path": rel_path,
            "source_fingerprint": fingerprint,
            "policy_hash": policy_hash,
            "search_objective": "target_size",
            "candidate_trace_json": json.dumps({
                "objective": "target_size",
                "truncated": False,
                "candidates": [
                    {
                        "quality_floor_met": True,
                        "violates_source_cap": False,
                        "predicted_whole_episode_bytes": minimum,
                    }
                ],
            }),
        }
