import json
from pathlib import Path
import unittest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash


MATRIX_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json")


class AV1ValidationV3Tier1FixtureMatrixV2Tests(unittest.TestCase):
    def test_successor_matrix_is_canonical_and_executable(self) -> None:
        raw = MATRIX_PATH.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(raw, canonical_json_bytes(payload) + b"\n")
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["generator_contract"], "mediaforce.synthetic_fixture.v2")
        self.assertEqual(payload["fixture_scope"], "deterministic_synthetic_only")
        self.assertEqual(payload["intermediate"], {"codec": "ffv1", "container": "nut"})
        self.assertEqual(
            payload["content_hash_scope"],
            "decoded_rawvideo_yuv420p10le_little_endian_frame_order",
        )
        self.assertEqual(
            payload["verification"],
            {"frame_count_field": "nb_read_frames", "frame_count_method": "ffprobe_count_frames"},
        )
        self.assertEqual(
            [fixture["fixture_id"] for fixture in payload["fixtures"]],
            ["tier1_flat_field", "tier1_high_motion", "tier1_high_detail_noise", "tier1_scene_change"],
        )
        self.assertTrue(all(set(fixture) == {"fixture_id", "lavfi_graph"} for fixture in payload["fixtures"]))
        self.assertTrue(all(fixture["lavfi_graph"].endswith("format=yuv420p10le") for fixture in payload["fixtures"]))
        self.assertEqual(payload["frame_spec"]["frame_count"], 288)
        self.assertEqual(payload["supersedes_sha256"], "sha256:0d37ab0dbf258158faa99f06cd34267b245fc3676e3d41c0b63b5c666b53dc07")
        self.assertEqual(
            stable_json_hash(payload),
            "08700ebf56283fe155394fa2696bd2fd5ad0856bcb165434b33035df4b98d66a",
        )
