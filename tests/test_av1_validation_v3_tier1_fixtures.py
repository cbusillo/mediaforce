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
        fixtures = payload["fixtures"]
        self.assertEqual([fixture["fixture_id"] for fixture in fixtures], [
            "tier1_flat_field",
            "tier1_high_motion",
            "tier1_high_detail_noise",
            "tier1_scene_change",
        ])
        self.assertEqual({fixture["generator"]["seed"] for fixture in fixtures}, {101, 202, 303, 404})
        self.assertTrue(all(fixture["generator"]["seconds"] == 12 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["width"] == 1280 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["height"] == 720 for fixture in fixtures))
        self.assertTrue(all(fixture["generator"]["fps"] == 24 for fixture in fixtures))
        self.assertEqual(
            stable_json_hash(payload),
            "aa0264d5db79c253826899e3ee7b7d9e6e1a75696dc0a3c85e743a565ac8f21f",
        )
