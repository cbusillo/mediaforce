from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    AV1ValidationV4PreparationGrantError,
    assert_av1_validation_v4_preparation_grant,
    assert_av1_validation_v4_preparation_grant_active,
    build_av1_validation_v4_preparation_grant,
    load_av1_validation_v4_preparation_grant,
    serialize_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
    build_av1_validation_v4_rights_template,
)


class AV1ValidationV4PreparationGrantTests(unittest.TestCase):
    def test_valid_grant_is_canonical_bounded_and_non_executing(self) -> None:
        grant = self._grant()
        self.assertEqual(grant, self._grant())
        self.assertEqual(grant["state"], "owner_authorized")
        self.assertEqual(grant["schema_version"], 2)
        self.assertEqual(grant["contract_version"], "av1v4pgg2")
        self.assertEqual(
            grant["consumption_registry"],
            "/registry/av1-v4-preparation",
        )
        self.assertTrue(
            grant["operation_scope"]["path_privacy_key_creation_authorized"]
        )
        self.assertFalse(
            grant["operation_scope"][
                "path_privacy_key_selection_or_partition_use_authorized"
            ]
        )
        self.assertFalse(grant["operation_scope"]["media_bytes_read_authorized"])
        self.assertFalse(grant["operation_scope"]["network_access_authorized"])
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(field=field):
                self.assertIs(grant[field], False)

    def test_round_trip_loader_requires_canonical_bytes(self) -> None:
        grant = self._grant()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "grant.json"
            path.write_bytes(serialize_av1_validation_v4_preparation_grant(grant))
            self.assertEqual(load_av1_validation_v4_preparation_grant(path), grant)
            path.write_text("{}\n")
            with self.assertRaises(AV1ValidationV4PreparationGrantError):
                load_av1_validation_v4_preparation_grant(path)

    def test_grant_window_is_active_only_inside_its_bounds(self) -> None:
        grant = self._grant()
        assert_av1_validation_v4_preparation_grant_active(
            grant,
            as_of="2026-08-07T05:45:00Z",
        )
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationGrantError,
            "not active",
        ):
            assert_av1_validation_v4_preparation_grant_active(
                grant,
                as_of="2026-08-07T07:00:00Z",
            )

    def test_builder_rejects_wrong_owner_template_and_invalid_windows(self) -> None:
        rights = self._rights_attestation()
        cases = (
            {
                "rights_attestation": rights,
                "owner_principal": "owner:other",
                "authorized_at": "2026-08-07T05:45:00Z",
                "valid_until": "2026-08-07T07:00:00Z",
                "message": "owner must match",
            },
            {
                "rights_attestation": build_av1_validation_v4_rights_template(),
                "owner_principal": "owner:test",
                "authorized_at": "2026-08-07T05:45:00Z",
                "valid_until": "2026-08-07T07:00:00Z",
                "message": "owner-attested rights record",
            },
            {
                "rights_attestation": rights,
                "owner_principal": "owner:test",
                "authorized_at": "2026-08-07T05:45:00.100000Z",
                "valid_until": "2026-08-07T07:00:00Z",
                "message": "canonical UTC seconds",
            },
            {
                "rights_attestation": rights,
                "owner_principal": "owner:test",
                "authorized_at": "2026-08-07T05:45:00Z",
                "valid_until": "2026-08-08T05:45:01Z",
                "message": "maximum duration",
            },
        )
        for case in cases:
            with self.subTest(message=case["message"]):
                with self.assertRaisesRegex(
                    AV1ValidationV4PreparationGrantError,
                    str(case["message"]),
                ):
                    build_av1_validation_v4_preparation_grant(
                        rights_attestation=case["rights_attestation"],
                        owner_principal=str(case["owner_principal"]),
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        consumption_registry="/registry/av1-v4-preparation",
                        authorized_at=str(case["authorized_at"]),
                        valid_until=str(case["valid_until"]),
                    )

    def test_mutations_fail_closed(self) -> None:
        cases: list[tuple[dict[str, object], str]] = []
        authority = copy.deepcopy(self._grant())
        authority["key_creation_authorized"] = True
        cases.append((authority, "cannot authorize key_creation_authorized"))
        scope = copy.deepcopy(self._grant())
        scope["operation_scope"]["network_access_authorized"] = True
        cases.append((scope, "binding is invalid"))
        unknown = copy.deepcopy(self._grant())
        unknown["custom_authority"] = True
        cases.append((unknown, "contains unknown fields"))
        private = copy.deepcopy(self._grant())
        private["owner_principal"] = "/Volumes/private"
        cases.append((private, "machine-local path"))
        windows_private = copy.deepcopy(self._grant())
        windows_private["owner_principal"] = "C:\\Users\\private"
        cases.append((windows_private, "machine-local path"))
        embedded_windows_private = copy.deepcopy(self._grant())
        embedded_windows_private["owner_principal"] = (
            "owner --prefix=C:\\Users\\private"
        )
        cases.append((embedded_windows_private, "machine-local path"))
        invalid_registry = copy.deepcopy(self._grant())
        invalid_registry["consumption_registry"] = "relative/registry"
        cases.append((invalid_registry, "consumption registry is invalid"))
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    AV1ValidationV4PreparationGrantError,
                    message,
                ):
                    assert_av1_validation_v4_preparation_grant(payload)

    def _grant(self) -> dict[str, object]:
        rights = self._rights_attestation()
        return build_av1_validation_v4_preparation_grant(
            rights_attestation=rights,
            owner_principal="owner:test",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            consumption_registry="/registry/av1-v4-preparation",
            authorized_at="2026-08-07T05:45:00Z",
            valid_until="2026-08-07T07:00:00Z",
        )

    def _rights_attestation(self) -> dict[str, object]:
        claims = {
            AV1_VALIDATION_V4_SOURCE_IDS[0]: {
                "license_basis": "CC-BY-3.0",
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "sintel-sharing.html",
                    "cc-by-3.0-legalcode.html",
                ],
                "terms_summary": "Sintel sharing terms reviewed.",
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
                "terms_summary": "Gooseberry title grant controls.",
            },
            AV1_VALIDATION_V4_SOURCE_IDS[2]: {
                "license_basis": "CC-BY-3.0",
                "attribution_required": True,
                "redistribution_disposition": "none",
                "terms_reviewed": [
                    "tears-sharing.html",
                    "cc-by-3.0-legalcode.html",
                ],
                "terms_summary": "Tears of Steel sharing terms reviewed.",
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
                "terms_summary": "NASA guidance reviewed; only video stream 0 is eligible.",
            },
        }
        return build_av1_validation_v4_rights_attestation(
            owner_principal="owner:test",
            attested_at="2026-08-07T05:30:00Z",
            source_claims=claims,
        )


if __name__ == "__main__":
    unittest.main()
