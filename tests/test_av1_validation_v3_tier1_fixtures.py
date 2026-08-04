import json
from pathlib import Path
import unittest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash


FIXTURE_MATRIX_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v1.json")


class AV1ValidationV3Tier1FixtureMatrixTests(unittest.TestCase):
    def test_fixture_matrix_is_canonical_and_synthetic_only(self) -> None:
        raw = FIXTURE_MATRIX_PATH.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(raw, canonical_json_bytes(payload) + b"\n")
        self.assertEqual(payload["schema"], "mediaforce.av1_cold_start_v3_tier1_fixture_matrix")
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["fixture_scope"], "deterministic_synthetic_only")
        self.assertEqual(payload["generator_contract"], "mediaforce.synthetic_fixture.v1")
        fixtures = payload["fixtures"]
        self.assertEqual(
            [
                (fixture["fixture_id"], fixture["generator"]["pattern"], fixture["generator"]["seed"])
                for fixture in fixtures
            ],
            [
                ("tier1_flat_field", "flat_field", 101),
                ("tier1_high_motion", "high_motion", 202),
                ("tier1_high_detail_noise", "high_detail_noise", 303),
                ("tier1_scene_change", "scene_change", 404),
            ],
        )
        self.assertTrue(all(set(fixture) == {"fixture_id", "generator"} for fixture in fixtures))
        generator_keys = {
            "color_matrix", "color_primaries", "color_transfer", "fps", "frame_count",
            "height", "pattern", "pixel_format", "seconds", "seed", "width",
        }
        self.assertTrue(all(set(fixture["generator"]) == generator_keys for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["seconds"] == 12 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["frame_count"] == 288 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["width"] == 1280 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["height"] == 720 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["fps"] == 24 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["pixel_format"] == "yuv420p10le" for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["color_primaries"] == "bt709" for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["color_transfer"] == "bt709" for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["color_matrix"] == "bt709" for fixture in fixtures))
        self.assertEqual(
            stable_json_hash(payload),
            "0d37ab0dbf258158faa99f06cd34267b245fc3676e3d41c0b63b5c666b53dc07",
        )
