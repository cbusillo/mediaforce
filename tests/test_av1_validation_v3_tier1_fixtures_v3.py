import json
from pathlib import Path
import unittest

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash


V2_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v2.json")
V3_PATH = Path("docs/validation/av1-tier1-synthetic-fixture-matrix-v3.json")


class AV1ValidationV3Tier1FixtureMatrixV3Tests(unittest.TestCase):
    def test_successor_matrix_is_canonical_and_provenance_bound(self) -> None:
        payload = json.loads(V3_PATH.read_text(encoding="utf-8"))
        self.assertEqual(V3_PATH.read_bytes(), canonical_json_bytes(payload) + b"\n")
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["generator_contract"], "mediaforce.synthetic_fixture.v3")
        self.assertEqual(
            payload["intermediate"],
            {
                "codec": "ffv1",
                "container": "matroska",
                "file_hash_authoritative": False,
                "output_color_args": [
                    "-color_primaries", "bt709", "-color_trc", "bt709",
                    "-colorspace", "bt709", "-color_range", "tv",
                ],
                "output_suffix": ".mkv",
            },
        )
        v2_payload = json.loads(V2_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["supersedes"],
            {
                "canonical_payload_sha256": f"sha256:{stable_json_hash(v2_payload)}",
                "schema_version": 2,
            },
        )
        self.assertEqual(
            payload["informed_by"],
            {
                "compat_probe_matrix_sha256": "sha256:2e6f515fa79224942805851ec0ed62b4a3d9db33931b37200fa9f27e514a36f1",
                "compat_result_id": "av1vtier1compatresult3_3da976eb70749755072b14c0925e3f4e",
                "compat_result_payload_sha256": "sha256:c9981d66b07aae06b565f28db29e077042b048dd58350b1645ba60510c6e25ee",
                "diagnostic_only": True,
                "evidence_eligible": False,
                "selected_representation_id": "compat_ffv1_matroska_explicit",
            },
        )
        self.assertEqual(
            stable_json_hash(payload),
            "b624ed891797bfad7e217cadca4272e7b5f1ea1453598ac71b126e7d5a510dcf",
        )
