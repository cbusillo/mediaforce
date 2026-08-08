from __future__ import annotations

import ast
import unittest
from pathlib import Path

from mediaforce.tuning import av1_validation_v4r3_rights as rights_module
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
    build_av1_validation_v4_rights_template,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    AV1_V4R3_RIGHTS_REAFFIRMED_AT,
    assert_av1_v4r3_rights_attestation,
    build_av1_v4r3_rights_attestation,
    deserialize_av1_v4r3_rights_attestation,
    serialize_av1_v4r3_rights_attestation,
)


class AV1V4R3RightsTests(unittest.TestCase):
    def test_rollover_round_trips_and_binds_revision_3(self) -> None:
        prior = _prior_attestation()
        payload = build_av1_v4r3_rights_attestation(prior_revision_attestation=prior)
        serialized = serialize_av1_v4r3_rights_attestation(payload)
        self.assertEqual(deserialize_av1_v4r3_rights_attestation(serialized), payload)
        self.assertTrue(payload["attestation_id"].startswith("av1vrights4r3_"))
        self.assertFalse(payload["attestation_id"].startswith("av1vrights4_"))
        self.assertEqual(payload["manifest_id"], AV1_V4R3_MANIFEST_ID)
        self.assertEqual(
            payload["manifest_payload_sha256"],
            AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
        )
        self.assertEqual(payload["attested_at"], AV1_V4R3_RIGHTS_REAFFIRMED_AT)

    def test_prior_record_is_provenance_only_and_claims_are_exact(self) -> None:
        prior = _prior_attestation()
        payload = build_av1_v4r3_rights_attestation(prior_revision_attestation=prior)
        wrapper = payload["prior_revision_attestation"]
        self.assertIs(wrapper["confers_revision_3_authority"], False)
        self.assertEqual(wrapper["artifact"], prior)
        self.assertEqual(payload["terms"], prior["terms"])
        self.assertEqual(payload["source_claims"], prior["source_claims"])
        self.assertEqual(payload["owner_principal"], prior["owner_principal"])

    def test_derivative_profile_carries_nasa_obligations(self) -> None:
        payload = build_av1_v4r3_rights_attestation(
            prior_revision_attestation=_prior_attestation()
        )
        profile = payload["derivative_profile_acknowledgement"]
        self.assertEqual(profile["output_video_codec"], "av1")
        self.assertEqual(profile["output_container"], "mkv")
        self.assertTrue(profile["video_only"])
        self.assertEqual(profile["max_height"], 1080)
        self.assertEqual(profile["target_size_mode"], "normalized")
        self.assertEqual(profile["reference_target_size_bytes"], 300_000_000)
        self.assertTrue(
            profile["nasa_acknowledgement_required_on_future_output_or_publication"]
        )
        self.assertTrue(profile["endorsement_prohibited"])
        self.assertEqual(profile["redistribution_disposition"], "none")
        self.assertFalse(profile["terms_documents_re_reviewed"])

    def test_rollover_conveys_no_authority(self) -> None:
        payload = build_av1_v4r3_rights_attestation(
            prior_revision_attestation=_prior_attestation()
        )
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False)
        self.assertFalse(
            payload["prior_revision_attestation"]["confers_revision_3_authority"]
        )

    def test_unattested_tampered_or_later_prior_records_fail_closed(self) -> None:
        with self.assertRaises(AV1V4R3RightsError):
            build_av1_v4r3_rights_attestation(
                prior_revision_attestation=build_av1_validation_v4_rights_template()
            )

        tampered = _prior_attestation()
        tampered["payload_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(AV1V4R3RightsError):
            build_av1_v4r3_rights_attestation(prior_revision_attestation=tampered)

        later = _prior_attestation(attested_at="2026-08-08T03:00:00Z")
        with self.assertRaisesRegex(AV1V4R3RightsError, "timestamps are out of order"):
            build_av1_v4r3_rights_attestation(prior_revision_attestation=later)

        deeply_nested = _prior_attestation()
        nested: list[object] = []
        cursor = nested
        for _ in range(40):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        deeply_nested["terms"] = nested
        with self.assertRaisesRegex(AV1V4R3RightsError, "too deep or large"):
            build_av1_v4r3_rights_attestation(prior_revision_attestation=deeply_nested)

    def test_rollover_mutations_fail_closed(self) -> None:
        cases: list[dict[str, object]] = []
        authority = _rollover()
        authority["media_read_authorized"] = True
        cases.append(authority)
        prior_authority = _rollover()
        prior_authority["prior_revision_attestation"][
            "confers_revision_3_authority"
        ] = True
        cases.append(prior_authority)
        claims = _rollover()
        claims["source_claims"][AV1_VALIDATION_V4_SOURCE_IDS[3]][
            "allowed_stream_indexes"
        ] = [0, 1]
        cases.append(claims)
        owner = _rollover()
        owner["owner_principal"] = "owner:other"
        cases.append(owner)
        timestamp = _rollover()
        timestamp["attested_at"] = "2026-08-08T02:22:21Z"
        cases.append(timestamp)
        private = _rollover()
        private["owner_principal"] = "/Users/private"
        cases.append(private)
        unknown = _rollover()
        unknown["custom_authority"] = True
        cases.append(unknown)
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(AV1V4R3RightsError):
                    assert_av1_v4r3_rights_attestation(payload)

    def test_deserializer_rejects_noncanonical_bytes(self) -> None:
        serialized = serialize_av1_v4r3_rights_attestation(_rollover())
        with self.assertRaises(AV1V4R3RightsError):
            deserialize_av1_v4r3_rights_attestation(serialized.rstrip(b"\n"))

    def test_deep_or_decoder_recursive_payloads_fail_with_contract_error(self) -> None:
        payload = _rollover()
        nested: list[object] = []
        cursor = nested
        for _ in range(40):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        payload["terms"] = nested
        with self.assertRaisesRegex(AV1V4R3RightsError, "too deep or large"):
            assert_av1_v4r3_rights_attestation(payload)

        deeply_nested_json = ("[" * 2_000 + "0" + "]" * 2_000).encode()
        with self.assertRaises(AV1V4R3RightsError):
            deserialize_av1_v4r3_rights_attestation(deeply_nested_json)

        with self.assertRaisesRegex(AV1V4R3RightsError, "non-finite number"):
            deserialize_av1_v4r3_rights_attestation(b'{"schema":NaN}\n')

    def test_module_has_no_filesystem_or_execution_imports(self) -> None:
        source = Path(rights_module.__file__).read_text()
        tree = ast.parse(source)
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        for forbidden in {
            "os",
            "pathlib",
            "subprocess",
            "mediaforce.execution",
            "mediaforce.core.db",
        }:
            self.assertNotIn(forbidden, imported_modules)
            self.assertNotIn(forbidden, imported_names)


def _rollover() -> dict[str, object]:
    return build_av1_v4r3_rights_attestation(
        prior_revision_attestation=_prior_attestation()
    )


def _prior_attestation(
    *, attested_at: str = "2026-08-07T05:30:00Z"
) -> dict[str, object]:
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
        attested_at=attested_at,
        source_claims=claims,
    )


if __name__ == "__main__":
    unittest.main()
