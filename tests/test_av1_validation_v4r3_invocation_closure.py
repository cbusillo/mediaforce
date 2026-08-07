from __future__ import annotations

import ast
import json
import sys
import unittest
from pathlib import Path

import tomllib

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_SOURCE_LAYOUT,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    av1_validation_v4_guided_warm_start_identities,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_BINDING_STATE,
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_MANIFEST_REVISION,
    AV1_V4_R3_OUTPUT_CONTAINER,
    AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER,
    AV1_V4_R3_PROTOCOL_VERSION,
    AV1_V4_R3_REQUIRED_PRIVATE_BINDINGS,
    AV1_V4_R3_REVISION_2_BASE_VIDEO_POLICY,
    AV1_V4_R3_SOURCE_MANIFEST_FACTS,
    AV1_V4_R3_SOURCE_VIDEO_BITRATE_BPS,
    AV1_V4_R3_WARM_START_CRF,
    AV1V4R3InvocationClosureError,
    assert_av1_v4_r3_protocol_v4_invariants,
    av1_v4_r3_all_resolved_size_goal_payloads,
    av1_v4_r3_bounds,
    av1_v4_r3_false_authority_payload,
    av1_v4_r3_guided_warm_start_identities,
    av1_v4_r3_public_contract_payload,
    av1_v4_r3_quality_search_adapter_contract,
    av1_v4_r3_quality_temp_hmac_id,
    av1_v4_r3_quality_temp_key_id,
    av1_v4_r3_resolved_size_goal_payload,
    av1_v4_r3_resolved_video_policy,
    av1_v4_r3_source_closure_payload,
    av1_v4_r3_stream_ledger_closure_payload,
    av1_v4_r3_transform_plan_payload,
    build_av1_v4_r3_all_closure_payloads,
    validate_av1_v4_r3_source_facts_against_revision_2_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_manifest() -> dict[str, object]:
    return json.loads(
        (ROOT / "docs/validation/av1-cold-start-preregistration-v4.json").read_text()
    )


class AV1V4R3PolicyTests(unittest.TestCase):
    def test_full_policy_matches_defaults_with_revision_2_crf_override(self) -> None:
        defaults = tomllib.loads((ROOT / "config/defaults.toml").read_text())
        expected = dict(defaults["video"])
        expected["min_crf"] = 10
        expected["max_crf"] = 45
        expected["compression_intent_source"] = "operator"
        self.assertEqual(av1_v4_r3_resolved_video_policy(), expected)

    def test_required_production_policy_keys_are_present(self) -> None:
        required = {
            "encoder",
            "crf_search",
            "preset",
            "pixel_format",
            "quality_metric",
            "target_vmaf",
            "target_xpsnr",
            "min_target_vmaf",
            "min_target_xpsnr",
            "target_relax_step_vmaf",
            "target_relax_step_xpsnr",
            "target_size_mb",
            "target_size_bytes",
            "target_runtime_minutes",
            "size_goal_schema_version",
            "size_goal_mode",
            "size_goal_source",
            "sample_projection_tolerance_percent",
            "final_output_tolerance_percent",
            "sample_every",
            "sample_duration",
            "max_encoded_percent",
            "default_grain",
            "grain_denoise",
            "thorough",
            "max_height",
            "resolution_intent_mode",
            "resolution_intent_source",
            "decision_model",
            "quality_engine",
            "target_search_max_crf",
        }
        self.assertTrue(required <= set(AV1_V4_R3_FULL_VIDEO_POLICY))

    def test_revision_2_bounds_and_warm_start_are_preserved(self) -> None:
        for key, value in AV1_V4_R3_REVISION_2_BASE_VIDEO_POLICY.items():
            self.assertEqual(AV1_V4_R3_FULL_VIDEO_POLICY[key], value)
        self.assertEqual(AV1_V4_R3_FULL_VIDEO_POLICY["min_crf"], 10)
        self.assertEqual(AV1_V4_R3_FULL_VIDEO_POLICY["max_crf"], 45)
        self.assertEqual(AV1_V4_R3_WARM_START_CRF, 28)

    def test_revision_3_warm_start_ids_are_domain_separated(self) -> None:
        revision_2 = av1_validation_v4_guided_warm_start_identities()
        revision_3 = av1_v4_r3_guided_warm_start_identities()
        self.assertEqual(set(revision_3), set(AV1_VALIDATION_V4_SOURCE_IDS))
        for asset_id in AV1_VALIDATION_V4_SOURCE_IDS:
            self.assertNotEqual(
                revision_2[asset_id]["search_signature_id"],
                revision_3[asset_id]["search_signature_id"],
            )
            self.assertNotEqual(
                revision_2[asset_id]["cohort_id"],
                revision_3[asset_id]["cohort_id"],
            )


class AV1V4R3SourceFactTests(unittest.TestCase):
    def test_source_facts_validate_against_checked_in_manifest_mapping(self) -> None:
        validate_av1_v4_r3_source_facts_against_revision_2_manifest(_load_manifest())

    def test_source_fact_validator_accepts_direct_mapping(self) -> None:
        manifest = _load_manifest()
        mapping = {source["asset_id"]: source for source in manifest["sources"]}  # type: ignore[index]
        validate_av1_v4_r3_source_facts_against_revision_2_manifest(mapping)

    def test_source_fact_validator_rejects_mutated_public_fact(self) -> None:
        manifest = _load_manifest()
        manifest["sources"][0]["duration_seconds"] = 1.0  # type: ignore[index]
        with self.assertRaises(AV1V4R3InvocationClosureError):
            validate_av1_v4_r3_source_facts_against_revision_2_manifest(manifest)

    def test_hardcoded_source_facts_have_no_private_paths(self) -> None:
        serialized = json.dumps(
            {
                asset_id: dict(facts)
                for asset_id, facts in AV1_V4_R3_SOURCE_MANIFEST_FACTS.items()
            }
        )
        for fragment in ("/Users/", "/Volumes/", "/home/", "/tmp/", "/private/"):
            self.assertNotIn(fragment, serialized)


class AV1V4R3SizeGoalTests(unittest.TestCase):
    def test_size_goal_payloads_are_exact_for_all_sources(self) -> None:
        expected_targets = {
            "av1v4_animation_primary_sintel": 98_670_222,
            "av1v4_animation_confirmation_cosmos_laundromat": 81_118_815,
            "av1v4_live_action_primary_tears_of_steel": 81_574_074,
            "av1v4_live_action_confirmation_nasa_earth_views": 22_305_618,
        }
        payloads = av1_v4_r3_all_resolved_size_goal_payloads()
        self.assertEqual(set(payloads), set(AV1_VALIDATION_V4_SOURCE_IDS))
        for asset_id, target in expected_targets.items():
            payload = payloads[asset_id]
            self.assertEqual(payload["target_size_bytes"], target)
            self.assertEqual(payload["sample_bounds"], av1_v4_r3_bounds(target, 10.0))
            self.assertEqual(payload["final_bounds"], av1_v4_r3_bounds(target, 5.0))
            self.assertTrue(str(payload["size_goal_id"]).startswith("av1v4r3size_"))

    def test_unknown_size_goal_source_raises(self) -> None:
        with self.assertRaises(AV1V4R3InvocationClosureError):
            av1_v4_r3_resolved_size_goal_payload("missing")


class AV1V4R3TransformLedgerTests(unittest.TestCase):
    def test_transform_plan_matches_production_null_cadence_identity(self) -> None:
        payload = {
            "schema_version": 1,
            "cadence_evidence_id": None,
            "cadence_class": None,
            "cadence_transform": None,
            "video_filter": None,
        }
        self.assertEqual(
            av1_v4_r3_transform_plan_payload(),
            {**payload, "transform_plan_id": f"tp1_{stable_json_hash(payload)[:32]}"},
        )

    def test_stream_ledger_closure_is_honest_and_unresolved(self) -> None:
        closure = av1_v4_r3_stream_ledger_closure_payload(
            AV1_VALIDATION_V4_SOURCE_IDS[0]
        )
        self.assertEqual(closure["binding_state"], AV1_V4_R3_BINDING_STATE)
        self.assertFalse(closure["execution_ready"])
        self.assertEqual(closure["output_container"], AV1_V4_R3_OUTPUT_CONTAINER)
        self.assertTrue(closure["non_video_streams_excluded"])
        self.assertEqual(closure["non_video_target_bytes"], 0)
        self.assertIsNone(closure["source_video_bitrate_bps"])
        self.assertIsNone(closure["production_stream_plan_id"])
        self.assertIsNone(closure["stream_budget_ledger_id"])
        self.assertIsNone(closure["quality_temp_hmac_id"])
        self.assertEqual(
            closure["required_private_bindings"],
            list(AV1_V4_R3_REQUIRED_PRIVATE_BINDINGS),
        )

    def test_closure_public_payload_has_no_raw_paths(self) -> None:
        payload = av1_v4_r3_source_closure_payload(
            AV1_VALIDATION_V4_SOURCE_IDS[0],
            AV1_VALIDATION_V4_CONFIGURATIONS[0],
            1,
        )
        serialized = json.dumps(payload)
        for fragment in ("/Users/", "/Volumes/", "/home/", "/tmp/", "/private/"):
            self.assertNotIn(fragment, serialized)


class AV1V4R3AdapterAuthorityTests(unittest.TestCase):
    def test_adapter_contract_binds_the_existing_production_search_seam(self) -> None:
        contract = av1_v4_r3_quality_search_adapter_contract()
        self.assertEqual(contract["module"], "mediaforce.execution")
        self.assertEqual(contract["callable"], "search_quality_for_source")
        self.assertTrue(contract["must_build_stream_budget_ledger"])
        self.assertTrue(contract["must_pass_stream_budget_ledger"])
        self.assertTrue(contract["must_pass_quality_temp_dir"])
        self.assertEqual(
            contract["transform_plan_derivation"],
            "mediaforce.encoding.quality_search._transform_plan_payload",
        )
        self.assertEqual(
            contract["must_verify_returned_transform_plan_id"],
            av1_v4_r3_transform_plan_payload()["transform_plan_id"],
        )
        self.assertTrue(contract["sample_encode_support_provided_by_seam"])
        self.assertTrue(contract["requires_real_target_size_trace"])
        self.assertTrue(contract["must_verify_returned_ledger_id"])

    def test_public_contract_is_not_execution_ready_and_all_authority_false(
        self,
    ) -> None:
        payload = av1_v4_r3_public_contract_payload()
        self.assertFalse(payload["execution_ready"])
        self.assertEqual(payload["binding_state"], "requires_private_preparation")
        self.assertEqual(
            payload["owner_decision"],
            {
                "target_size_path_intent_frozen_in_revision_2": True,
                "target_size_value_frozen_in_revision_2": False,
                "production_profile_approved_for_revision_3": True,
            },
        )
        self.assertEqual(
            av1_v4_r3_false_authority_payload(),
            {
                field: False
                for field in sorted(AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS)
            },
        )
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
            self.assertIs(payload[field], False)


class AV1V4R3IdentityTests(unittest.TestCase):
    def test_all_closures_follow_manifest_primary_first_traversal_order(self) -> None:
        closures = build_av1_v4_r3_all_closure_payloads()
        self.assertEqual(len(closures), AV1_VALIDATION_V4_TRAVERSAL_COUNT)
        self.assertEqual(
            [closure["ordinal"] for closure in closures],
            list(range(1, AV1_VALIDATION_V4_TRAVERSAL_COUNT + 1)),
        )
        self.assertEqual(
            [closure["asset_id"] for closure in closures],
            [
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[0],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[0],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[1],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[1],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[2],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[2],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[3],
                AV1_V4_R3_PRIMARY_FIRST_ASSET_ORDER[3],
            ],
        )

    def test_baseline_and_guided_payloads_differ_only_by_mode_sensitive_fields(
        self,
    ) -> None:
        baseline = av1_v4_r3_source_closure_payload(
            AV1_VALIDATION_V4_SOURCE_IDS[0],
            "balanced_full_search_baseline",
            1,
        )
        guided = av1_v4_r3_source_closure_payload(
            AV1_VALIDATION_V4_SOURCE_IDS[0],
            "balanced_frozen_search_hint",
            2,
        )
        self.assertIsNone(baseline["warm_start"])
        self.assertEqual(
            guided["warm_start"]["candidate_crf"], AV1_V4_R3_WARM_START_CRF
        )  # type: ignore[index]
        self.assertNotEqual(baseline["closure_id"], guided["closure_id"])

    def test_protocol_v4_reasoning_invariants_pass(self) -> None:
        assert_av1_v4_r3_protocol_v4_invariants()
        self.assertEqual(AV1_V4_R3_PROTOCOL_VERSION, 4)
        self.assertEqual(AV1_V4_R3_MANIFEST_REVISION, 3)
        self.assertEqual(len(AV1_VALIDATION_V4_SOURCE_LAYOUT), 4)
        self.assertIsNone(AV1_V4_R3_SOURCE_VIDEO_BITRATE_BPS)

    def test_quality_temp_hmac_ids_hide_paths(self) -> None:
        key = bytes(range(32))
        key_id = av1_v4_r3_quality_temp_key_id(key)
        hmac_id = av1_v4_r3_quality_temp_hmac_id(
            key,
            asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
            normalized_path="/private/tmp/av1_v4_quality/sintel",
        )
        self.assertTrue(key_id.startswith("av1vqtkey4r3_"))
        self.assertTrue(hmac_id.startswith("av1vqtemp4r3_"))
        self.assertNotIn("/private", hmac_id)
        self.assertNotIn("/tmp", hmac_id)

    def test_invalid_quality_temp_inputs_raise(self) -> None:
        with self.assertRaises(AV1V4R3InvocationClosureError):
            av1_v4_r3_quality_temp_key_id(b"short")
        with self.assertRaises(AV1V4R3InvocationClosureError):
            av1_v4_r3_quality_temp_hmac_id(
                bytes(range(32)),
                asset_id="missing",
                normalized_path="/private/tmp/av1",
            )
        with self.assertRaises(AV1V4R3InvocationClosureError):
            av1_v4_r3_quality_temp_hmac_id(
                bytes(range(32)),
                asset_id=AV1_VALIDATION_V4_SOURCE_IDS[0],
                normalized_path="relative/path",
            )


class AV1V4R3ImportBoundaryTests(unittest.TestCase):
    def test_module_has_no_io_or_runtime_import_boundaries(self) -> None:
        module = sys.modules["mediaforce.tuning.av1_validation_v4r3_invocation_closure"]
        assert module.__file__ is not None
        source = Path(module.__file__).read_text()
        tree = ast.parse(source)
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
        forbidden = (
            "pathlib",
            "mediaforce.encoding",
            "mediaforce.execution",
            "mediaforce.web",
            "mediaforce.core.db",
            "mediaforce.library",
            "mediaforce.hosts",
            "subprocess",
            "sqlite3",
            "urllib",
            "requests",
        )
        for name in imported_names:
            self.assertFalse(
                any(
                    name == value or name.startswith(value + ".") for value in forbidden
                ),
                name,
            )

    def test_module_does_not_call_open_or_path_read_methods(self) -> None:
        module = sys.modules["mediaforce.tuning.av1_validation_v4r3_invocation_closure"]
        assert module.__file__ is not None
        tree = ast.parse(Path(module.__file__).read_text())
        forbidden_calls = {
            "open",
            "read_text",
            "read_bytes",
            "stat",
            "exists",
            "is_file",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                )
                self.assertNotIn(name, forbidden_calls)


if __name__ == "__main__":
    unittest.main()
