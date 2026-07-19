import tempfile
import unittest
from pathlib import Path

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.library.planner import _evidence_summary, build_manifest_item


class PlannerEvidenceTests(unittest.TestCase):
    def test_malformed_evidence_is_projected_as_missing(self) -> None:
        self.assertIsNone(_evidence_summary(None))
        self.assertIsNone(_evidence_summary("not-json"))
        self.assertIsNone(_evidence_summary("[]"))
        self.assertEqual(_evidence_summary('{"retry_required":true}'), {"retry_required": True})

    def test_manifest_compatibility_remains_canonical_json_driven(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project_root = Path(temp_dir)
            config = MediaforceConfig(
                raw={
                    "media": {
                        "staging_root": str(project_root / "staging"),
                        "output_container": "mkv",
                        "libraries": [
                            {
                                "key": "tv",
                                "label": "TV",
                                "path": str(project_root / "tv"),
                                "type": "tv",
                                "availability": "browse_only",
                            }
                        ],
                    },
                    "video": {},
                    "audio": {},
                    "subtitle": {},
                    "planning": {},
                    "validation": {},
                    "overrides": [],
                    "remote_hosts": [],
                },
                paths=ConfigPaths(
                    project_root=project_root,
                    config_path=project_root / "config.toml",
                    db_path=project_root / "library.sqlite3",
                    run_manifest_dir=project_root / "runs",
                    web_state_dir=project_root / "web",
                    review_dir=project_root / "review",
                    runtime_settings_path=project_root / "runtime-settings.json",
                ),
            )
            item = build_manifest_item(
                {
                    "id": 1,
                    "source_path": str(project_root / "tv" / "Show" / "Episode.mkv"),
                    "rel_path": "tv/Show/Episode.mkv",
                    "media_root": "tv",
                    "fingerprint": "file-1",
                    "size_bytes": 1_000_000,
                    "duration_seconds": 60.0,
                    "video_codec": "h264",
                    "video_bitrate": 1_000_000,
                    "width": 1920,
                    "height": 1080,
                    "container": ".mkv",
                    "status": "discovered",
                    "audio_track_count": 1,
                    "subtitle_track_count": 0,
                    "english_audio_count": 1,
                    "english_subtitle_count": 0,
                    "audio_summary_json": "[]",
                    "subtitle_summary_json": "[]",
                    "attachment_summary_json": None,
                    "cadence_summary_json": "not-json",
                    "media_fingerprint_json": "not-json",
                },
                config,
            )

        self.assertIsNone(item["cadence_summary"])
        self.assertEqual(item["cadence_decision"]["classification"], "unknown")
        self.assertIsNone(item["media_fingerprint"])
        self.assertEqual(item["media_fingerprint_decision"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
