from __future__ import annotations

import ast
import unittest
from pathlib import Path

from mediaforce.tuning import av1_validation_v4r3_path_privacy as path_privacy_module
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_SOURCE_IDS
from mediaforce.tuning.av1_validation_v4_path_privacy import (
    av1_validation_v4_instance_path_hmac_id,
    av1_validation_v4_path_privacy_key_id,
    av1_validation_v4_source_path_hmac_id,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    av1_v4_r3_quality_temp_hmac_id,
    av1_v4_r3_quality_temp_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import (
    build_av1_v4r3_manifest_payload,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    AV1V4R3PathPrivacyError,
    assert_av1_v4r3_path_privacy_contract,
    av1_v4r3_instance_path_hmac_id,
    av1_v4r3_path_privacy_contract_payload,
    av1_v4r3_path_privacy_key_id,
    av1_v4r3_source_path_hmac_id,
)


class AV1V4R3PathPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = bytes(range(32))

    def test_contract_matches_the_approved_manifest_exactly(self) -> None:
        assert_av1_v4r3_path_privacy_contract()
        self.assertEqual(
            av1_v4r3_path_privacy_contract_payload(),
            build_av1_v4r3_manifest_payload()["revision_3_path_privacy"],
        )
        self.assertFalse(
            av1_v4r3_path_privacy_contract_payload()[
                "selection_or_partition_use_allowed"
            ]
        )

    def test_derivations_are_deterministic_and_revision_scoped(self) -> None:
        key_id = av1_v4r3_path_privacy_key_id(self.key)
        instance_id = av1_v4r3_instance_path_hmac_id(
            self.key,
            role="state_root",
            normalized_path="/private/workspace/state",
        )
        source_id = av1_v4r3_source_path_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/media/source.mp4",
        )
        self.assertEqual(
            key_id,
            "av1vpathkey4r3_d9c9b65c53d72a589dd8da4122a093d8",
        )
        self.assertEqual(
            instance_id,
            "av1vpath4r3_d5cb6f9f020d49ffd83ea80ac80ff6e6",
        )
        self.assertEqual(
            source_id,
            "av1vsource4r3_10de8e4d3fcfa8ff680b5b18bd19fe4c",
        )
        self.assertEqual(key_id, av1_v4r3_path_privacy_key_id(self.key))
        self.assertEqual(len({key_id, instance_id, source_id}), 3)

        self.assertNotEqual(key_id, av1_validation_v4_path_privacy_key_id(self.key))
        self.assertNotEqual(
            instance_id,
            av1_validation_v4_instance_path_hmac_id(
                self.key,
                role="state_root",
                normalized_path="/private/workspace/state",
            ),
        )
        self.assertNotEqual(
            source_id,
            av1_validation_v4_source_path_hmac_id(
                self.key,
                asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
                normalized_path="/private/media/source.mp4",
            ),
        )

    def test_quality_temp_and_path_privacy_domains_are_distinct(self) -> None:
        path_key_id = av1_v4r3_path_privacy_key_id(self.key)
        quality_key_id = av1_v4_r3_quality_temp_key_id(self.key)
        source_id = av1_v4r3_source_path_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/shared",
        )
        quality_temp_id = av1_v4_r3_quality_temp_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/shared",
        )
        self.assertNotEqual(path_key_id, quality_key_id)
        self.assertNotEqual(source_id, quality_temp_id)

    def test_roles_sources_and_paths_fail_closed(self) -> None:
        invalid_paths = (
            "",
            "/",
            "relative/path",
            "//private/state",
            "/private//state",
            "/private/state/",
            "/private/./state",
            "/private/state/.",
            "/private/../state",
            "/private/state/..",
            "/private/\x00state",
            "/private/\nstate",
        )
        for value in invalid_paths:
            with self.subTest(value=value):
                with self.assertRaises(AV1V4R3PathPrivacyError):
                    av1_v4r3_instance_path_hmac_id(
                        self.key,
                        role="state_root",
                        normalized_path=value,
                    )
        for operation in (
            lambda: av1_v4r3_path_privacy_key_id(b"short"),
            lambda: av1_v4r3_path_privacy_key_id(bytearray(range(32))),
            lambda: av1_v4r3_instance_path_hmac_id(
                self.key,
                role="unknown",
                normalized_path="/private/state",
            ),
            lambda: av1_v4r3_source_path_hmac_id(
                self.key,
                asset_id="unknown",
                normalized_path="/private/source.mp4",
            ),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(AV1V4R3PathPrivacyError):
                    operation()

    def test_module_has_no_filesystem_or_execution_imports(self) -> None:
        source = Path(path_privacy_module.__file__).read_text()
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
