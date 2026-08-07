from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import unittest

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    AV1ValidationV4PreparationError,
    AV1ValidationV4PreparationInputs,
    AV1ValidationV4ToolIdentity,
    assert_av1_validation_v4_preparation_bundle,
    assert_av1_validation_v4_preparation_record,
    av1_validation_v4_preparation_covered_requirements,
    build_av1_validation_v4_preparation_record,
    serialize_av1_validation_v4_preparation_record,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
    build_av1_validation_v4_rights_template,
)


MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v4.json")
MODULE_PATH = Path("mediaforce/tuning/av1_validation_v4_preparation.py")


class AV1ValidationV4PreparationTests(unittest.TestCase):
    def test_record_covers_frozen_manifest_requirements(self) -> None:
        manifest = json.loads(MANIFEST_PATH.read_text())
        expected = {
            key
            for key, value in manifest["preparation_requirements"].items()
            if value is True
        }
        self.assertEqual(
            av1_validation_v4_preparation_covered_requirements(),
            expected,
        )

    def test_valid_record_is_deterministic_and_non_authorizing(self) -> None:
        first = self._record()
        second = self._record()
        self.assertEqual(first, second)
        self.assertEqual(
            serialize_av1_validation_v4_preparation_record(first),
            json.dumps(
                first,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
            + b"\n",
        )
        self.assertEqual(first["state"], "prepared_unfrozen")
        self.assertFalse(first["media_bytes_read"])
        self.assertFalse(first["subprocess_executed"])
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(field=field):
                self.assertIs(first[field], False)

    def test_preparation_module_is_structurally_pure(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden_imports = {
            "os",
            "pathlib",
            "requests",
            "shutil",
            "socket",
            "subprocess",
            "urllib",
        }
        forbidden_calls = {
            "open",
            "popen",
            "read_bytes",
            "read_text",
            "run",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.assertFalse(
                    {alias.name.split(".")[0] for alias in node.names}
                    & forbidden_imports
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn(node.module.split(".")[0], forbidden_imports)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id.lower(), forbidden_calls)
                elif isinstance(node.func, ast.Attribute):
                    self.assertNotIn(node.func.attr.lower(), forbidden_calls)

    def test_template_rights_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationError,
            "completed owner rights attestation",
        ):
            build_av1_validation_v4_preparation_record(
                inputs=self._inputs(),
                rights_attestation=build_av1_validation_v4_rights_template(),
            )

    def test_bundle_rejects_a_different_rights_attestation(self) -> None:
        rights = self._rights_attestation()
        record = build_av1_validation_v4_preparation_record(
            inputs=self._inputs(),
            rights_attestation=rights,
        )
        other_rights = self._rights_attestation(
            attested_at="2026-08-07T02:31:00Z"
        )
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationError,
            "binding does not match",
        ):
            assert_av1_validation_v4_preparation_bundle(record, other_rights)

    def test_input_mutations_fail_closed(self) -> None:
        cases = [
            (
                replace(
                    self._inputs(),
                    repository_commit="not-a-commit",
                ),
                "repository commit",
            ),
            (
                replace(
                    self._inputs(),
                    qualification_key_id="wrong-key-id",
                ),
                "qualification key ID",
            ),
            (
                replace(
                    self._inputs(),
                    baseline_invocation_sha256="sha256:" + "b" * 64,
                    guided_invocation_sha256="sha256:" + "b" * 64,
                ),
                "invocation digests must differ",
            ),
            (
                replace(
                    self._inputs(),
                    guided_base_config_sha256="sha256:" + "c" * 64,
                ),
                "base config digests must match",
            ),
            (
                replace(
                    self._inputs(),
                    dedicated_instance_path_hmac_ids={
                        **self._inputs().dedicated_instance_path_hmac_ids,
                        "state_root": "/Volumes/private",
                    },
                ),
                "machine-local path",
            ),
            (
                replace(
                    self._inputs(),
                    source_path_hmac_ids={
                        key: value
                        for key, value in self._inputs().source_path_hmac_ids.items()
                        if key != AV1_VALIDATION_V4_SOURCE_IDS[0]
                    },
                ),
                "source path identity set",
            ),
        ]
        for inputs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1ValidationV4PreparationError, message):
                    build_av1_validation_v4_preparation_record(
                        inputs=inputs,
                        rights_attestation=self._rights_attestation(),
                    )

    def test_record_mutations_fail_closed(self) -> None:
        cases = []
        authority = self._record()
        authority["public_traversal_authorized"] = True
        cases.append((authority, "cannot authorize public_traversal_authorized"))
        unknown = self._record()
        unknown["custom_authority"] = True
        cases.append((unknown, "contains unknown fields"))
        media_read = self._record()
        media_read["media_bytes_read"] = True
        cases.append((media_read, "binding is invalid"))
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1ValidationV4PreparationError, message):
                    assert_av1_validation_v4_preparation_record(payload)

    def _record(self) -> dict[str, object]:
        return build_av1_validation_v4_preparation_record(
            inputs=self._inputs(),
            rights_attestation=self._rights_attestation(),
        )

    def _inputs(self) -> AV1ValidationV4PreparationInputs:
        return AV1ValidationV4PreparationInputs(
            prepared_at="2026-08-07T03:00:00Z",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            effective_config_sha256="sha256:" + "3" * 64,
            ffmpeg=AV1ValidationV4ToolIdentity(
                version="ffmpeg version test",
                binary_sha256="sha256:" + "4" * 64,
            ),
            ffprobe=AV1ValidationV4ToolIdentity(
                version="ffprobe version test",
                binary_sha256="sha256:" + "5" * 64,
            ),
            ab_av1=AV1ValidationV4ToolIdentity(
                version="ab-av1 test",
                binary_sha256="sha256:" + "6" * 64,
            ),
            dedicated_instance_path_hmac_ids={
                "runtime_lock": "av1vpath4_" + "7" * 32,
                "source_root": "av1vpath4_" + "8" * 32,
                "state_root": "av1vpath4_" + "9" * 32,
                "temp_root": "av1vpath4_" + "a" * 32,
            },
            source_path_hmac_ids={
                asset_id: f"av1vsource4_{index:032x}"
                for index, asset_id in enumerate(AV1_VALIDATION_V4_SOURCE_IDS, start=1)
            },
            runtime_compatibility_id="av1vruntime4_" + "b" * 32,
            guided_search_signature_id="acss1_test_signature",
            guided_cohort_id="acsh1_test_cohort",
            guided_warm_start_payload_sha256="sha256:" + "c" * 64,
            baseline_invocation_sha256="sha256:" + "d" * 64,
            baseline_base_config_sha256="sha256:" + "e" * 64,
            guided_invocation_sha256="sha256:" + "f" * 64,
            guided_base_config_sha256="sha256:" + "e" * 64,
            qualification_key_id="av1vqkey4_" + "1" * 32,
        )

    def _rights_attestation(
        self,
        *,
        attested_at: str = "2026-08-07T02:30:00Z",
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
            attested_at=attested_at,
            source_claims=claims,
        )


if __name__ == "__main__":
    unittest.main()
