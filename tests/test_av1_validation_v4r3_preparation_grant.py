from __future__ import annotations

import ast
import copy
import unittest
from pathlib import Path

from mediaforce.tuning import (
    av1_validation_v4r3_preparation_grant as preparation_grant_module,
)
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    build_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    AV1V4R3PreparationGrantError,
    AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN,
    assert_av1_v4r3_preparation_grant,
    assert_av1_v4r3_preparation_grant_active,
    build_av1_v4r3_preparation_grant,
    deserialize_av1_v4r3_preparation_grant,
    serialize_av1_v4r3_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    build_av1_v4r3_rights_attestation,
)


class AV1V4R3PreparationGrantTests(unittest.TestCase):
    def test_grant_is_canonical_single_use_and_non_executing(self) -> None:
        grant = _grant()
        self.assertEqual(grant, _grant())
        self.assertTrue(grant["grant_id"].startswith("av1vprepgrant4r3_"))
        self.assertEqual(
            grant["consumption_registry_token"],
            AV1_V4R3_PREPARATION_CONSUMPTION_REGISTRY_TOKEN,
        )
        scope = grant["operation_scope"]
        self.assertTrue(scope["single_use_authorized"])
        self.assertTrue(scope["path_privacy_key_creation_authorized"])
        self.assertEqual(scope["key_bytes"], 32)
        self.assertTrue(scope["key_file_create_exclusive_required"])
        self.assertEqual(scope["key_file_mode"], "0600")
        self.assertEqual(scope["key_persistence"], "machine_local_only")
        for field in (
            "grant_reuse_authorized",
            "key_material_disclosure_authorized",
            "media_bytes_read_authorized",
            "media_path_probe_authorized",
            "media_processing_subprocess_authorized",
            "network_access_authorized",
            "path_privacy_key_selection_or_partition_use_authorized",
        ):
            self.assertIs(scope[field], False)
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(grant[field], False)

    def test_round_trip_requires_canonical_bytes(self) -> None:
        grant = _grant()
        serialized = serialize_av1_v4r3_preparation_grant(grant)
        self.assertEqual(deserialize_av1_v4r3_preparation_grant(serialized), grant)
        with self.assertRaises(AV1V4R3PreparationGrantError):
            deserialize_av1_v4r3_preparation_grant(serialized.rstrip(b"\n"))

    def test_grant_window_is_active_only_inside_bounds(self) -> None:
        grant = _grant()
        assert_av1_v4r3_preparation_grant_active(grant, as_of="2026-08-08T03:30:00Z")
        with self.assertRaisesRegex(AV1V4R3PreparationGrantError, "not active"):
            assert_av1_v4r3_preparation_grant_active(
                grant, as_of="2026-08-09T03:00:00Z"
            )

    def test_builder_requires_r3_rights_owner_and_bounded_window(self) -> None:
        r3_rights = _r3_rights()
        prior = _prior_rights()
        cases = (
            (
                prior,
                "owner:test",
                "2026-08-08T03:00:00Z",
                "2026-08-08T04:00:00Z",
                "r3 rights",
            ),
            (
                r3_rights,
                "owner:other",
                "2026-08-08T03:00:00Z",
                "2026-08-08T04:00:00Z",
                "owner must match",
            ),
            (
                r3_rights,
                "owner:test",
                "2026-08-08T03:00:00.1Z",
                "2026-08-08T04:00:00Z",
                "canonical UTC seconds",
            ),
            (
                r3_rights,
                "owner:test",
                "2026-08-08T03:00:00Z",
                "2026-08-09T03:00:01Z",
                "maximum duration",
            ),
        )
        for rights, owner, authorized_at, valid_until, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1V4R3PreparationGrantError, message):
                    build_av1_v4r3_preparation_grant(
                        rights_attestation=rights,
                        owner_principal=owner,
                        repository_commit="1" * 40,
                        repository_tree="2" * 40,
                        authorized_at=authorized_at,
                        valid_until=valid_until,
                    )

    def test_revision_2_grant_and_registry_cannot_be_reused(self) -> None:
        r2_grant = build_av1_validation_v4_preparation_grant(
            rights_attestation=_prior_rights(),
            owner_principal="owner:test",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            consumption_registry="/registry/av1-v4-preparation",
            authorized_at="2026-08-07T05:45:00Z",
            valid_until="2026-08-07T07:00:00Z",
        )
        self.assertEqual(_grant()["operation_scope"], r2_grant["operation_scope"])
        with self.assertRaises(AV1V4R3PreparationGrantError):
            assert_av1_v4r3_preparation_grant(r2_grant)

        grant = _grant()
        grant["consumption_registry_token"] = "/registry/av1-v4-preparation"
        with self.assertRaisesRegex(
            AV1V4R3PreparationGrantError, "consumption registry token"
        ):
            assert_av1_v4r3_preparation_grant(grant)

    def test_mutations_fail_closed(self) -> None:
        cases: list[tuple[dict[str, object], str]] = []
        authority = copy.deepcopy(_grant())
        authority["key_creation_authorized"] = True
        cases.append((authority, "cannot authorize key_creation_authorized"))
        scope = copy.deepcopy(_grant())
        scope["operation_scope"]["network_access_authorized"] = True
        cases.append((scope, "binding is invalid"))
        repository = copy.deepcopy(_grant())
        repository["repository"]["tree"] = "not-a-tree"
        cases.append((repository, "repository identity"))
        manifest = copy.deepcopy(_grant())
        manifest["manifest_revision"] = 2
        cases.append((manifest, "binding is invalid"))
        rights = copy.deepcopy(_grant())
        rights["rights_attestation_id"] = "av1vrights4_" + "0" * 32
        cases.append((rights, "rights binding"))
        private = copy.deepcopy(_grant())
        private["owner_principal"] = "/Volumes/private"
        cases.append((private, "private path-like text"))
        unknown = copy.deepcopy(_grant())
        unknown["custom_authority"] = True
        cases.append((unknown, "unknown fields"))
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1V4R3PreparationGrantError, message):
                    assert_av1_v4r3_preparation_grant(payload)

    def test_deep_recursive_and_nonfinite_payloads_fail_closed(self) -> None:
        payload = _grant()
        nested: list[object] = []
        cursor = nested
        for _ in range(40):
            child: list[object] = []
            cursor.append(child)
            cursor = child
        payload["operation_scope"] = nested
        with self.assertRaisesRegex(AV1V4R3PreparationGrantError, "too deep"):
            assert_av1_v4r3_preparation_grant(payload)

        deeply_nested_json = ("[" * 2_000 + "0" + "]" * 2_000).encode()
        with self.assertRaises(AV1V4R3PreparationGrantError):
            deserialize_av1_v4r3_preparation_grant(deeply_nested_json)

        with self.assertRaisesRegex(AV1V4R3PreparationGrantError, "non-finite"):
            deserialize_av1_v4r3_preparation_grant(b'{"schema":NaN}\n')

    def test_module_has_no_filesystem_or_execution_imports(self) -> None:
        source = Path(preparation_grant_module.__file__).read_text()
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


def _grant() -> dict[str, object]:
    return build_av1_v4r3_preparation_grant(
        rights_attestation=_r3_rights(),
        owner_principal="owner:test",
        repository_commit="1" * 40,
        repository_tree="2" * 40,
        authorized_at="2026-08-08T03:00:00Z",
        valid_until="2026-08-08T04:00:00Z",
    )


def _r3_rights() -> dict[str, object]:
    return build_av1_v4r3_rights_attestation(prior_revision_attestation=_prior_rights())


def _prior_rights() -> dict[str, object]:
    source_claims = {
        AV1_VALIDATION_V4_SOURCE_IDS[0]: {
            "license_basis": "CC-BY-3.0",
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": ["sintel-sharing.html", "cc-by-3.0-legalcode.html"],
            "terms_summary": "Official Sintel sharing terms reviewed.",
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
            "terms_reviewed": ["tears-sharing.html", "cc-by-3.0-legalcode.html"],
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
            "terms_summary": "NASA guidance reviewed; only video stream 0 is eligible.",
        },
    }
    return build_av1_validation_v4_rights_attestation(
        owner_principal="owner:test",
        attested_at="2026-08-07T05:30:00Z",
        source_claims=source_claims,
    )


if __name__ == "__main__":
    unittest.main()
