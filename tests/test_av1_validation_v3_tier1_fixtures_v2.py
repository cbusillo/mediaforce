import json
from pathlib import Path
import unittest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash


V1_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v1.json")
V2_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json")


class AV1ValidationV3Tier1FixtureMatrixV2Tests(unittest.TestCase):
    def test_successor_matrix_is_canonical_and_executable(self) -> None:
        payload = json.loads(V2_PATH.read_text(encoding="utf-8"))
        self.assertEqual(V2_PATH.read_bytes(), canonical_json_bytes(payload) + b"\n")
        self.assertEqual(
            set(payload),
            {"content_hash", "determinism_scope", "fixture_scope", "fixtures", "frame_limit", "frame_spec", "generator_contract", "intermediate", "probe", "scene_change_contract", "schema", "schema_version", "supersedes"},
        )
        self.assertEqual(payload["schema"], "mediaforce.av1_cold_start_v3_tier1_fixture_matrix")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["generator_contract"], "mediaforce.synthetic_fixture.v2")
        self.assertEqual(payload["frame_limit"], {"method": "frames_v", "value": 288})
        self.assertEqual(payload["frame_spec"], {"color_matrix": "bt709", "color_primaries": "bt709", "color_range": "limited", "color_transfer": "bt709", "fps": 24, "frame_count": 288, "height": 720, "pixel_format": "yuv420p10le", "seconds": 12, "width": 1280})
        self.assertEqual(payload["intermediate"], {"codec": "ffv1", "container": "nut", "file_hash_authoritative": False})
        self.assertEqual(payload["scene_change_contract"], {"frames_per_pattern": 36, "selected_patterns": 8, "source_patterns": 10, "source_total_frames": 360})
        self.assertEqual(payload["probe"]["frame_count_field"], "nb_read_frames")
        self.assertEqual(payload["probe"]["frame_count_json_type"], "string")
        self.assertIn("-count_frames", payload["probe"]["command"])
        self.assertEqual(payload["content_hash"]["algorithm"], "sha256")
        self.assertEqual(payload["content_hash"]["artifact"], "decoded_video_stream")
        self.assertEqual(payload["determinism_scope"], "exact_bound_ffmpeg_toolchain")
        expected = ["tier1_flat_field", "tier1_high_motion", "tier1_high_detail_noise", "tier1_scene_change"]
        self.assertEqual([fixture["fixture_id"] for fixture in payload["fixtures"]], expected)
        self.assertTrue(all(set(fixture) == {"fixture_id", "lavfi_graph"} for fixture in payload["fixtures"]))
        self.assertTrue(all("setparams=range=limited" in fixture["lavfi_graph"] for fixture in payload["fixtures"]))
        v1_payload = json.loads(V1_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["supersedes"], {"canonical_payload_sha256": f"sha256:{stable_json_hash(v1_payload)}", "schema_version": 1})
        self.assertEqual(
            stable_json_hash(payload),
            "faa40e0ee1dfd71715440d413b4d8e138266b27202a1e3c995ddffcf1a370572",
        )
