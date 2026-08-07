from __future__ import annotations

import ast
from dataclasses import replace
import json
from pathlib import Path
import unittest

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_guided_warm_start_identities,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    AV1ValidationV4InvocationIdentity,
    AV1ValidationV4PreparationError,
    AV1ValidationV4PreparationInputs,
    AV1ValidationV4ToolIdentity,
    assert_av1_validation_v4_preparation_bundle,
    av1_validation_v4_preparation_covered_requirements,
    build_av1_validation_v4_preparation_record,
    serialize_av1_validation_v4_preparation_record,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    build_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_runtime_compatibility import (
    av1_validation_v4_runtime_compatibility_payload,
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
        rights = self._rights_attestation()
        self.assertEqual(first, second)
        self.assertEqual(
            serialize_av1_validation_v4_preparation_record(
                first,
                rights_attestation=rights,
                preparation_grant=self._grant(),
            ),
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
        self.assertFalse(first["builder_subprocess_executed"])
        self.assertFalse(first["media_processing_subprocess_executed"])
        self.assertTrue(first["tool_version_probe_subprocess_executed"])
        self.assertEqual(len(first["guided_warm_start_identities"]), 4)
        self.assertEqual(len(first["invocations"]), 8)
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            with self.subTest(field=field):
                self.assertIs(first[field], False)

    def test_preparation_module_is_structurally_pure(self) -> None:
        tree = ast.parse(MODULE_PATH.read_text())
        allowed_import_modules = {
            "__future__",
            "collections.abc",
            "dataclasses",
            "datetime",
            "json",
            "mediaforce.core.evidence",
            "mediaforce.core.type_defs",
            "mediaforce.tuning.av1_validation_v4",
            "mediaforce.tuning.av1_validation_v4_preparation_grant",
            "mediaforce.tuning.av1_validation_v4_rights",
            "mediaforce.tuning.av1_validation_v4_runtime_compatibility",
            "re",
            "typing",
        }
        forbidden_named_calls = {
            "__import__",
            "compile",
            "eval",
            "exec",
        }
        forbidden_attribute_calls = {
            "open",
            "popen",
            "read_bytes",
            "read_text",
            "run",
            "write_bytes",
            "write_text",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name, allowed_import_modules)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertIn(node.module, allowed_import_modules)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    self.assertNotIn(
                        node.func.id.lower(),
                        forbidden_named_calls | forbidden_attribute_calls,
                    )
                elif isinstance(node.func, ast.Attribute):
                    self.assertNotIn(
                        node.func.attr.lower(),
                        forbidden_attribute_calls,
                    )

    def test_template_rights_record_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationError,
            "completed owner rights attestation",
        ):
            build_av1_validation_v4_preparation_record(
                inputs=self._inputs(),
                rights_attestation=build_av1_validation_v4_rights_template(),
                preparation_grant=self._grant(),
            )

    def test_bundle_rejects_a_different_rights_attestation(self) -> None:
        rights = self._rights_attestation()
        record = build_av1_validation_v4_preparation_record(
            inputs=self._inputs(),
            rights_attestation=rights,
            preparation_grant=self._grant(),
        )
        other_rights = self._rights_attestation(
            attested_at="2026-08-07T05:31:00Z"
        )
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationError,
            "binding does not match",
        ):
            assert_av1_validation_v4_preparation_bundle(
                record,
                other_rights,
                self._grant(),
            )

    def test_bundle_rejects_a_different_preparation_grant(self) -> None:
        record = self._record()
        other_grant = self._grant(valid_until="2026-08-07T07:30:00Z")
        with self.assertRaisesRegex(
            AV1ValidationV4PreparationError,
            "grant binding does not match",
        ):
            assert_av1_validation_v4_preparation_bundle(
                record,
                self._rights_attestation(),
                other_grant,
            )

    def test_input_mutations_fail_closed(self) -> None:
        cases = [
            (
                replace(
                    self._inputs(),
                    repository_commit="not-a-commit",
                ),
                "grant repository binding",
            ),
            (
                replace(
                    self._inputs(),
                    path_privacy_key_id="wrong-key-id",
                ),
                "path privacy key ID",
            ),
            (
                replace(
                    self._inputs(),
                    invocations=tuple(
                        replace(invocation, invocation_sha256="sha256:" + "b" * 64)
                        for invocation in self._inputs().invocations
                    ),
                ),
                "traversal invocation digests must differ",
            ),
            (
                replace(
                    self._inputs(),
                    invocations=tuple(
                        replace(
                            invocation,
                            base_config_sha256=(
                                "sha256:" + "c" * 64
                                if invocation.ordinal == 2
                                else invocation.base_config_sha256
                            ),
                        )
                        for invocation in self._inputs().invocations
                    ),
                ),
                "base config digests must match within source",
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
            (
                replace(
                    self._inputs(),
                    guided_warm_start_identities={
                        key: value
                        for key, value in self._inputs().guided_warm_start_identities.items()
                        if key != AV1_VALIDATION_V4_SOURCE_IDS[0]
                    },
                ),
                "guided warm-start identities",
            ),
            (
                replace(
                    self._inputs(),
                    runtime_compatibility_payload={"scope": "forged"},
                ),
                "runtime compatibility payload",
            ),
            (
                replace(
                    self._inputs(),
                    runtime_compatibility_payload=(
                        self._runtime_compatibility_payload(
                            ffmpeg_binary_sha256="sha256:" + "f" * 64
                        )
                    ),
                ),
                "runtime compatibility measurements do not match",
            ),
            (
                replace(
                    self._inputs(),
                    invocations=tuple(reversed(self._inputs().invocations)),
                ),
                "invocation order is invalid",
            ),
            (
                replace(
                    self._inputs(),
                    invocations=(
                        replace(
                            self._inputs().invocations[0],
                            source_path_hmac_id="av1vsource4_" + "f" * 32,
                        ),
                        *self._inputs().invocations[1:],
                    ),
                ),
                "source path binding is invalid",
            ),
            (
                replace(
                    self._inputs(),
                    invocations=(
                        replace(
                            self._inputs().invocations[0],
                            configuration="unknown",
                        ),
                        *self._inputs().invocations[1:],
                    ),
                ),
                "configuration is invalid",
            ),
        ]
        for inputs, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1ValidationV4PreparationError, message):
                    build_av1_validation_v4_preparation_record(
                        inputs=inputs,
                        rights_attestation=self._rights_attestation(),
                        preparation_grant=self._grant(),
                    )

    def test_probe_accounting_can_report_no_subprocess(self) -> None:
        record = build_av1_validation_v4_preparation_record(
            inputs=replace(
                self._inputs(),
                tool_version_probe_subprocess_executed=False,
            ),
            rights_attestation=self._rights_attestation(),
            preparation_grant=self._grant(),
        )
        self.assertFalse(record["tool_version_probe_subprocess_executed"])

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
        repository = self._record()
        repository["repository"]["commit"] = "not-a-commit"
        cases.append((repository, "repository commit is invalid"))
        embedded_windows_path = self._record()
        embedded_windows_path["rights_attested_at"] = (
            "stamp --prefix=C:\\Users\\private"
        )
        cases.append((embedded_windows_path, "machine-local path"))
        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(AV1ValidationV4PreparationError, message):
                    assert_av1_validation_v4_preparation_bundle(
                        payload,
                        self._rights_attestation(),
                        self._grant(),
                    )

    def _record(self) -> dict[str, object]:
        return build_av1_validation_v4_preparation_record(
            inputs=self._inputs(),
            rights_attestation=self._rights_attestation(),
            preparation_grant=self._grant(),
        )

    def _inputs(self) -> AV1ValidationV4PreparationInputs:
        return AV1ValidationV4PreparationInputs(
            prepared_at="2026-08-07T06:00:00Z",
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
            runtime_compatibility_payload=self._runtime_compatibility_payload(),
            guided_warm_start_identities=(
                av1_validation_v4_guided_warm_start_identities()
            ),
            invocations=self._invocations(),
            path_privacy_key_id="av1vpathkey4_" + "1" * 32,
            tool_version_probe_subprocess_executed=True,
        )

    def _invocations(self) -> tuple[AV1ValidationV4InvocationIdentity, ...]:
        source_path_ids = {
            asset_id: f"av1vsource4_{index:032x}"
            for index, asset_id in enumerate(AV1_VALIDATION_V4_SOURCE_IDS, start=1)
        }
        asset_order = (
            AV1_VALIDATION_V4_SOURCE_IDS[0],
            AV1_VALIDATION_V4_SOURCE_IDS[2],
            AV1_VALIDATION_V4_SOURCE_IDS[1],
            AV1_VALIDATION_V4_SOURCE_IDS[3],
        )
        configurations = (
            "balanced_full_search_baseline",
            "balanced_frozen_search_hint",
        )
        invocations: list[AV1ValidationV4InvocationIdentity] = []
        ordinal = 1
        for source_index, asset_id in enumerate(asset_order, start=1):
            base_config_sha256 = f"sha256:{source_index + 100:064x}"
            for configuration in configurations:
                invocations.append(
                    AV1ValidationV4InvocationIdentity(
                        ordinal=ordinal,
                        asset_id=asset_id,
                        configuration=configuration,
                        source_path_hmac_id=source_path_ids[asset_id],
                        invocation_sha256=f"sha256:{ordinal:064x}",
                        base_config_sha256=base_config_sha256,
                    )
                )
                ordinal += 1
        return tuple(invocations)

    def _grant(
        self,
        *,
        valid_until: str = "2026-08-07T07:00:00Z",
    ) -> dict[str, object]:
        rights = self._rights_attestation()
        return build_av1_validation_v4_preparation_grant(
            rights_attestation=rights,
            owner_principal=str(rights["owner_principal"]),
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            authorized_at="2026-08-07T05:45:00Z",
            valid_until=valid_until,
        )

    def _runtime_compatibility_payload(
        self,
        *,
        ffmpeg_binary_sha256: str = "sha256:" + "4" * 64,
    ) -> dict[str, object]:
        return av1_validation_v4_runtime_compatibility_payload(
            effective_config_sha256="sha256:" + "3" * 64,
            toolchain={
                "ffmpeg": {
                    "version": "ffmpeg version test",
                    "binary_sha256": ffmpeg_binary_sha256,
                },
                "ffprobe": {
                    "version": "ffprobe version test",
                    "binary_sha256": "sha256:" + "5" * 64,
                },
                "ab_av1": {
                    "version": "ab-av1 test",
                    "binary_sha256": "sha256:" + "6" * 64,
                },
            },
            operating_system="macOS",
            operating_system_version="27.0 build 26A5388g",
            architecture="arm64",
            python_version="3.13.7",
        )

    def _rights_attestation(
        self,
        *,
        attested_at: str = "2026-08-07T05:30:00Z",
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
