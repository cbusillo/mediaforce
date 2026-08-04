from pathlib import Path
import sys
import unittest

from mediaforce.tuning.av1_validation_v3 import load_av1_validation_protocol_v3
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
)
from mediaforce.tuning.av1_validation_v3_tier1_preparation import (
    AV1ValidationV3Tier1PreparationError,
    av1_validation_v3_tier1_config_sha256,
    av1_validation_v3_tier1_eligibility_predicate,
    av1_validation_v3_tier1_eligibility_predicate_sha256,
    build_av1_validation_v3_tier1_prepared_plan,
    build_av1_validation_v3_tier1_prepared_request,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    build_av1_validation_v3_tier1_toolchain_binding,
)


class AV1ValidationV3Tier1PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = load_av1_validation_protocol_v3(
            Path("docs/validation/av1-cold-start-preregistration-v3.json")
        )
        self.toolchain = build_av1_validation_v3_tier1_toolchain_binding(
            ffmpeg_path=Path(sys.executable),
            ffprobe_path=Path(sys.executable),
        )

    def test_eligibility_predicate_is_isolated_and_stable(self) -> None:
        payload = av1_validation_v3_tier1_eligibility_predicate()
        self.assertEqual(payload["tier"], "tier1")
        self.assertEqual(
            payload["fixture_matrix_sha256"],
            AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
        )
        for field in (
            "private_inventory_read_authorized",
            "media_library_read_authorized",
            "tier2_execution_authorized",
            "path_matrix_coverage_claimed",
            "evidence_eligible",
            "qualification_complete",
            "empirical_authority_conferred",
            "derivation_authorized",
            "holdout_authorized",
            "publication_authorized",
            "activation_authorized",
        ):
            self.assertFalse(payload[field])
        self.assertTrue(
            av1_validation_v3_tier1_eligibility_predicate_sha256().startswith(
                "sha256:"
            )
        )

    def test_config_digest_binds_exact_nonempty_bytes(self) -> None:
        self.assertNotEqual(
            av1_validation_v3_tier1_config_sha256(b"a\n"),
            av1_validation_v3_tier1_config_sha256(b"a"),
        )
        for value in (b"", bytearray(b"config")):
            with self.subTest(value=value):
                with self.assertRaises(AV1ValidationV3Tier1PreparationError):
                    av1_validation_v3_tier1_config_sha256(value)

    def test_prepared_plan_and_request_bind_frozen_inputs_without_authority(self) -> None:
        plan = build_av1_validation_v3_tier1_prepared_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_bytes=b"[mediaforce]\n",
            toolchain=self.toolchain,
            frozen_at="2026-08-04T12:00:00Z",
            valid_until="2026-08-05T12:00:00Z",
        )
        self.assertEqual(
            plan.eligibility_predicate_sha256,
            av1_validation_v3_tier1_eligibility_predicate_sha256(),
        )
        self.assertEqual(plan.toolchain_sha256, self.toolchain.payload_sha256)
        self.assertEqual(plan.fixture_matrix_sha256, AV1_VALIDATION_V3_TIER1_MATRIX_SHA256)
        request = build_av1_validation_v3_tier1_prepared_request(
            protocol=self.protocol,
            plan=plan,
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_bytes=b"[mediaforce]\n",
            toolchain=self.toolchain,
            requested_at="2026-08-04T13:00:00Z",
            valid_until="2026-08-04T20:00:00Z",
        )
        self.assertFalse(request.to_payload()["qualification_execution_authorized"])
        self.assertFalse(request.to_payload()["media_read_authorized"])

    def test_prepared_request_rejects_drifted_preparation_inputs(self) -> None:
        plan = build_av1_validation_v3_tier1_prepared_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_bytes=b"[mediaforce]\n",
            toolchain=self.toolchain,
            frozen_at="2026-08-04T12:00:00Z",
            valid_until="2026-08-05T12:00:00Z",
        )
        with self.assertRaises(AV1ValidationV3Tier1PreparationError):
            build_av1_validation_v3_tier1_prepared_request(
                protocol=self.protocol,
                plan=plan,
                repository_commit="1" * 40,
                repository_tree="2" * 40,
                config_bytes=b"[changed]\n",
                toolchain=self.toolchain,
                requested_at="2026-08-04T13:00:00Z",
                valid_until="2026-08-04T20:00:00Z",
            )
        for repository_commit, repository_tree in (
            ("3" * 40, "2" * 40),
            ("1" * 40, "4" * 40),
        ):
            with self.subTest(
                repository_commit=repository_commit,
                repository_tree=repository_tree,
            ):
                with self.assertRaises(AV1ValidationV3Tier1PreparationError):
                    build_av1_validation_v3_tier1_prepared_request(
                        protocol=self.protocol,
                        plan=plan,
                        repository_commit=repository_commit,
                        repository_tree=repository_tree,
                        config_bytes=b"[mediaforce]\n",
                        toolchain=self.toolchain,
                        requested_at="2026-08-04T13:00:00Z",
                        valid_until="2026-08-04T20:00:00Z",
                    )
        drifted_plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=f"av1vqkey3_{'a' * 32}",
            eligibility_predicate_sha256=(
                av1_validation_v3_tier1_eligibility_predicate_sha256()
            ),
            repository_commit="1" * 40,
            repository_tree="2" * 40,
            config_sha256=plan.config_sha256,
            toolchain_sha256=f"sha256:{'f' * 64}",
            fixture_matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            frozen_at="2026-08-04T12:00:00Z",
            valid_until="2026-08-05T12:00:00Z",
        )
        with self.assertRaises(AV1ValidationV3Tier1PreparationError):
            build_av1_validation_v3_tier1_prepared_request(
                protocol=self.protocol,
                plan=drifted_plan,
                repository_commit="1" * 40,
                repository_tree="2" * 40,
                config_bytes=b"[mediaforce]\n",
                toolchain=self.toolchain,
                requested_at="2026-08-04T13:00:00Z",
                valid_until="2026-08-04T20:00:00Z",
            )
