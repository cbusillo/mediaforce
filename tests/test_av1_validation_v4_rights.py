from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    AV1ValidationV4RightsError,
    assert_av1_validation_v4_rights_attestation,
    build_av1_validation_v4_rights_attestation,
    build_av1_validation_v4_rights_template,
    load_av1_validation_v4_rights_attestation,
    serialize_av1_validation_v4_rights_attestation,
)


TEMPLATE_PATH = Path("docs/validation/av1-v4-rights-attestation-template-v1.json")
DISCOVERY_PATH = Path("docs/validation/av1-v4-discovery-public-v1.json")


class AV1ValidationV4RightsTests(unittest.TestCase):
    def test_checked_in_template_is_canonical_and_unattested(self) -> None:
        expected = build_av1_validation_v4_rights_template()
        loaded = load_av1_validation_v4_rights_attestation(TEMPLATE_PATH)
        self.assertEqual(loaded, expected)
        self.assertEqual(
            TEMPLATE_PATH.read_bytes(),
            serialize_av1_validation_v4_rights_attestation(expected),
        )
        self.assertEqual(loaded["state"], "template_unattested")
        self.assertFalse(loaded["owner_attested"])
        self.assertIsNone(loaded["owner_principal"])
        self.assertIsNone(loaded["attested_at"])
        self.assertEqual(
            loaded["source_claims"],
            {asset_id: None for asset_id in AV1_VALIDATION_V4_SOURCE_IDS},
        )

    def test_template_and_completed_record_confer_no_authority(self) -> None:
        for name, payload in (
            ("template", build_av1_validation_v4_rights_template()),
            ("completed", self._completed_attestation()),
        ):
            with self.subTest(name=name):
                for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                    self.assertIs(payload[field], False)

    def test_template_terms_match_frozen_discovery_projection(self) -> None:
        discovery = json.loads(DISCOVERY_PATH.read_text())
        template = build_av1_validation_v4_rights_template()
        self.assertEqual(
            template["terms"],
            {
                name: value
                for name, value in discovery["terms"].items()
                if name != "Sintel.2010.1080p.mkv.md5"
            },
        )

    def test_completed_record_binds_cosmos_and_nasa_constraints(self) -> None:
        payload = self._completed_attestation()
        cosmos = payload["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[1]]
        nasa = payload["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[3]]
        self.assertEqual(
            cosmos["license_precedence"],
            "official_blender_gooseberry_title_grant",
        )
        self.assertTrue(cosmos["netflix_mirror_is_technical_provenance_only"])
        self.assertTrue(nasa["video_stream_only"])
        self.assertEqual(nasa["allowed_stream_indexes"], [0])
        self.assertEqual(nasa["excluded_stream_indexes"], [1])

    def test_mutations_fail_closed(self) -> None:
        cases = []
        binding = self._completed_attestation()
        binding["manifest_payload_sha256"] = "sha256:" + "0" * 64
        cases.append((binding, "binding is invalid"))
        authority = self._completed_attestation()
        authority["manifest_freeze_authorized"] = True
        cases.append((authority, "cannot authorize manifest_freeze_authorized"))
        unknown_authority = self._completed_attestation()
        unknown_authority["custom_authority"] = True
        cases.append((unknown_authority, "contains unknown fields"))
        cosmos = self._completed_attestation()
        cosmos["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[1]][
            "netflix_mirror_is_technical_provenance_only"
        ] = False
        cases.append((cosmos, "Cosmos rights precedence"))
        nasa = self._completed_attestation()
        nasa["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[3]][
            "allowed_stream_indexes"
        ] = [0, 1]
        cases.append((nasa, "NASA rights constraints"))
        summary = self._completed_attestation()
        summary["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[0]][
            "terms_summary"
        ] = "x" * 401
        cases.append((summary, "summary is invalid"))
        private_path = self._completed_attestation()
        private_path["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[0]][
            "terms_summary"
        ] = "/Volumes/private"
        cases.append((private_path, "machine-local path"))
        captured = self._completed_attestation()
        captured["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[0]][
            "raw_terms"
        ] = "copied page"
        cases.append((captured, "captured terms content"))
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1ValidationV4RightsError, message):
                    assert_av1_validation_v4_rights_attestation(payload)

    def test_template_cannot_claim_owner_attestation(self) -> None:
        payload = build_av1_validation_v4_rights_template()
        payload["owner_attested"] = True
        with self.assertRaisesRegex(AV1ValidationV4RightsError, "remain unattested"):
            assert_av1_validation_v4_rights_attestation(payload)

    def _completed_attestation(self) -> dict[str, object]:
        claims = {
            AV1_VALIDATION_V4_SOURCE_IDS[0]: {
                "license_basis": "CC-BY-3.0",
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "sintel-sharing.html",
                    "cc-by-3.0-legalcode.html",
                ],
                "terms_summary": "Official Sintel sharing terms and CC BY 3.0 reviewed.",
            },
            AV1_VALIDATION_V4_SOURCE_IDS[1]: {
                "license_basis": "CC-BY-3.0",
                "license_precedence": "official_blender_gooseberry_title_grant",
                "netflix_mirror_is_technical_provenance_only": True,
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "cosmos-license.html",
                    "cosmos-object-license-response.xml",
                    "netflix-techblog-readme.txt",
                    "cc-by-3.0-legalcode.html",
                ],
                "terms_summary": "Gooseberry title grant controls; Netflix is provenance only.",
            },
            AV1_VALIDATION_V4_SOURCE_IDS[2]: {
                "license_basis": "CC-BY-3.0",
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "tears-sharing.html",
                    "cc-by-3.0-legalcode.html",
                ],
                "terms_summary": "Official Tears of Steel sharing terms reviewed.",
            },
            AV1_VALIDATION_V4_SOURCE_IDS[3]: {
                "license_basis": "NASA-media-guidelines",
                "video_stream_only": True,
                "allowed_stream_indexes": [0],
                "excluded_stream_indexes": [1],
                "third_party_audio_ingredients_present": True,
                "nasa_acknowledgement_required": True,
                "endorsement_prohibited": True,
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "nasa-media-guidelines.html",
                    "nasa-earth-views-metadata.json",
                    "nasa-earth-views-asset.json",
                ],
                "terms_summary": "NASA guidance and metadata reviewed; only video stream 0 is eligible.",
            },
        }
        return build_av1_validation_v4_rights_attestation(
            owner_principal="owner:test",
            attested_at="2026-08-07T05:30:00Z",
            source_claims=claims,
        )


if __name__ == "__main__":
    unittest.main()
