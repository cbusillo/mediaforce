from __future__ import annotations

import unittest

from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_SOURCE_IDS
from mediaforce.tuning.av1_validation_v4_path_privacy import (
    AV1ValidationV4PathPrivacyError,
    av1_validation_v4_instance_path_hmac_id,
    av1_validation_v4_path_privacy_key_id,
    av1_validation_v4_source_path_hmac_id,
)


class AV1ValidationV4PathPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = bytes(range(32))

    def test_derivations_are_deterministic_and_domain_separated(self) -> None:
        key_id = av1_validation_v4_path_privacy_key_id(self.key)
        instance_id = av1_validation_v4_instance_path_hmac_id(
            self.key,
            role="state_root",
            normalized_path="/private/workspace/state",
        )
        source_id = av1_validation_v4_source_path_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/media/source.mp4",
        )
        self.assertEqual(
            key_id,
            "av1vpathkey4_0be7d09b0500aae0abb7f81a4877a8c4",
        )
        self.assertEqual(
            instance_id,
            "av1vpath4_068eeaaf818d69c53150c77a7a14db0d",
        )
        self.assertEqual(
            source_id,
            "av1vsource4_e7026ca0f247f2b44f1d33174e03fda7",
        )
        self.assertEqual(key_id, av1_validation_v4_path_privacy_key_id(self.key))
        self.assertEqual(len({key_id, instance_id, source_id}), 3)

    def test_instance_role_and_source_identity_change_outputs(self) -> None:
        first_instance = av1_validation_v4_instance_path_hmac_id(
            self.key,
            role="state_root",
            normalized_path="/private/workspace/shared",
        )
        second_instance = av1_validation_v4_instance_path_hmac_id(
            self.key,
            role="temp_root",
            normalized_path="/private/workspace/shared",
        )
        first_source = av1_validation_v4_source_path_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/media/shared.mp4",
        )
        second_source = av1_validation_v4_source_path_hmac_id(
            self.key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[1],
            normalized_path="/private/media/shared.mp4",
        )
        self.assertNotEqual(first_instance, second_instance)
        self.assertNotEqual(first_source, second_source)

    def test_invalid_key_roles_sources_and_paths_fail_closed(self) -> None:
        cases = (
            lambda: av1_validation_v4_path_privacy_key_id(b"short"),
            lambda: av1_validation_v4_path_privacy_key_id(bytearray(range(32))),
            lambda: av1_validation_v4_instance_path_hmac_id(
                self.key,
                role="unknown",
                normalized_path="/private/state",
            ),
            lambda: av1_validation_v4_source_path_hmac_id(
                self.key,
                asset_id="unknown",
                normalized_path="/private/source.mp4",
            ),
            lambda: av1_validation_v4_instance_path_hmac_id(
                self.key,
                role="state_root",
                normalized_path="relative/path",
            ),
            lambda: av1_validation_v4_instance_path_hmac_id(
                self.key,
                role="state_root",
                normalized_path="/private/../state",
            ),
            lambda: av1_validation_v4_instance_path_hmac_id(
                self.key,
                role="state_root",
                normalized_path="/private//state",
            ),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(AV1ValidationV4PathPrivacyError):
                    operation()


if __name__ == "__main__":
    unittest.main()
