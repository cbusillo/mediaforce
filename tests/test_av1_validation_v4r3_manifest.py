import ast
import copy
import hashlib
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_INSTANCE_PATH_HMAC_DOMAIN,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_PATH_PRIVACY_KEY_ID_DOMAIN,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_SOURCE_PATH_HMAC_DOMAIN,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning import av1_validation_v4r3_manifest as manifest_module
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    AV1V4R3ManifestError,
    AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
    AV1_V4R3_MANIFEST_ID,
    AV1_V4R3_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R3_MANIFEST_SCHEMA,
    AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
    AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
    assert_av1_v4r3_manifest,
    build_av1_v4r3_manifest_payload,
    deserialize_av1_v4r3_manifest,
    serialize_av1_v4r3_manifest,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX,
    AV1_V4R3_OW_WINDOW_SECONDS_MAX,
)


ROOT = Path(__file__).resolve().parents[1]
REVISION_2_MANIFEST = ROOT / "docs/validation/av1-cold-start-preregistration-v4.json"
REVISION_3_MANIFEST = ROOT / "docs/validation/av1-cold-start-preregistration-v4r3.json"


class AV1V4R3ManifestTests(unittest.TestCase):
    def test_checked_in_manifest_is_canonical_and_matches_builder(self) -> None:
        raw = REVISION_3_MANIFEST.read_bytes()
        payload = deserialize_av1_v4r3_manifest(raw)
        self.assertEqual(payload, build_av1_v4r3_manifest_payload())
        self.assertEqual(serialize_av1_v4r3_manifest(payload), raw)
        self.assertEqual(payload["manifest_id"], AV1_V4R3_MANIFEST_ID)
        self.assertEqual(payload["payload_sha256"], AV1_V4R3_MANIFEST_PAYLOAD_SHA256)

    def test_manifest_binds_exact_closures_and_ordinal_budget(self) -> None:
        payload = build_av1_v4r3_manifest_payload()
        closures = build_av1_v4_r3_all_closure_payloads()
        self.assertEqual(
            payload["closure_ids"],
            [closure["closure_id"] for closure in closures],
        )
        self.assertEqual(
            payload["revision_3_invocation_contract"]["closures"], list(closures)
        )
        policy = payload["ordinal_window_policy"]
        self.assertEqual(policy["ordinal_count"], AV1_V4R3_OW_ORDINAL_COUNT)
        self.assertEqual(policy["window_seconds_max"], AV1_V4R3_OW_WINDOW_SECONDS_MAX)
        self.assertEqual(
            policy["aggregate_seconds_max"], AV1_V4R3_OW_AGGREGATE_SECONDS_MAX
        )
        self.assertEqual(
            policy["public_run_seconds_max"], AV1_V4R3_OW_PUBLIC_RUN_SECONDS_MAX
        )
        self.assertLessEqual(
            policy["aggregate_seconds_max"], policy["public_run_seconds_max"]
        )

    def test_manifest_is_non_authorizing_and_requires_a_fresh_chain(self) -> None:
        payload = build_av1_v4r3_manifest_payload()
        self.assertFalse(payload["execution_ready"])
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False)
        self.assertFalse(
            payload["revision_2_base_binding"]["reuse_as_revision_3_authority_allowed"]
        )
        self.assertFalse(
            payload["fresh_chain_requirements"]["revision_2_artifact_reuse_allowed"]
        )

    def test_revision_3_domains_do_not_reuse_revision_2(self) -> None:
        self.assertNotEqual(
            AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
            AV1_VALIDATION_V4_PATH_PRIVACY_KEY_ID_DOMAIN,
        )
        self.assertNotEqual(
            AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
            AV1_VALIDATION_V4_INSTANCE_PATH_HMAC_DOMAIN,
        )
        self.assertNotEqual(
            AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
            AV1_VALIDATION_V4_SOURCE_PATH_HMAC_DOMAIN,
        )
        for domain in (
            AV1_V4R3_PATH_PRIVACY_KEY_ID_DOMAIN,
            AV1_V4R3_INSTANCE_PATH_HMAC_DOMAIN,
            AV1_V4R3_SOURCE_PATH_HMAC_DOMAIN,
        ):
            self.assertIn("protocol:4:revision:3", domain)

    def test_revision_2_manifest_identity_and_bytes_are_unchanged(self) -> None:
        self.assertEqual(
            hashlib.sha256(REVISION_2_MANIFEST.read_bytes()).hexdigest(),
            "6e9f4f0a7490b90567528b39d9c92588984eb4f523db348494f9f4526c2d6535",
        )
        superseded = build_av1_v4r3_manifest_payload()["supersedes_manifest_revision"]
        self.assertEqual(superseded["manifest_id"], AV1_VALIDATION_V4_MANIFEST_ID)
        self.assertEqual(superseded["payload_sha256"], AV1_VALIDATION_V4_PAYLOAD_SHA256)

    def test_tampering_private_text_and_noncanonical_bytes_fail_closed(self) -> None:
        payload = build_av1_v4r3_manifest_payload()
        tampered = copy.deepcopy(payload)
        tampered["implementation_issue"] = "/Users/operator/private"
        with self.assertRaises(AV1V4R3ManifestError):
            assert_av1_v4r3_manifest(tampered)

        changed = copy.deepcopy(payload)
        changed["closure_ids"][0] = "av1vclosure4r3_" + "f" * 32
        with self.assertRaises(AV1V4R3ManifestError):
            assert_av1_v4r3_manifest(changed)

        with self.assertRaises(AV1V4R3ManifestError):
            deserialize_av1_v4r3_manifest(
                serialize_av1_v4r3_manifest(payload).rstrip(b"\n")
            )

    def test_preregistration_verifier_recognizes_revision_3(self) -> None:
        from scripts import verify_av1_cold_start_preregistration

        loaded = verify_av1_cold_start_preregistration._load_manifest(
            REVISION_3_MANIFEST
        )
        self.assertEqual(loaded["schema"], AV1_V4R3_MANIFEST_SCHEMA)
        payload = verify_av1_cold_start_preregistration._validation_payload(loaded)
        self.assertEqual(payload["protocol_version"], 4)
        self.assertEqual(payload["manifest_revision"], 3)
        self.assertEqual(payload["closure_count"], 8)
        self.assertFalse(payload["execution_ready"])
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False)

    def test_preregistration_verifier_validate_cli_accepts_revision_3(self) -> None:
        from scripts import verify_av1_cold_start_preregistration

        output = io.StringIO()
        with (
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_preregistration_bootstrap_authority",
            ),
            patch.object(
                verify_av1_cold_start_preregistration,
                "_assert_canonical_preregistration_runner",
            ),
            redirect_stdout(output),
        ):
            exit_code = verify_av1_cold_start_preregistration.main(
                ["validate", str(REVISION_3_MANIFEST), "--json"]
            )
        self.assertEqual(exit_code, 0)
        rendered = output.getvalue()
        self.assertIn(f'"manifest_id": "{AV1_V4R3_MANIFEST_ID}"', rendered)
        self.assertIn('"manifest_revision": 3', rendered)
        self.assertIn('"execution_ready": false', rendered)
        self.assertIn('"runtime_execution_authorized": false', rendered)

    def test_manifest_module_has_no_io_or_execution_imports(self) -> None:
        source = Path(manifest_module.__file__).read_text()
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


if __name__ == "__main__":
    unittest.main()
