from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ContextManager
import unittest
from unittest.mock import patch

from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_SOURCE_IDS
from mediaforce.tuning.av1_validation_v4_rights import (
    build_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    av1_validation_v4_qualification_search_invocation_sha256,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    build_av1_v4r3_rights_attestation,
)
from mediaforce.tuning import av1_validation_v4r4_preparation_flow as flow_module
from mediaforce.tuning.av1_validation_v4r4_invocation import (
    av1_v4r4_mode_for_ordinal,
    av1_v4r4_runner_invocation_sha256,
    av1_v4r4_search_kwargs_for_inputs,
    av1_v4r4_video_policy_for_ordinal,
    av1_v4r4_warm_start_for_ordinal,
)
from mediaforce.tuning.av1_validation_v4r4_preparation import (
    AV1_V4R4_PREPARATION_SOURCE_SPECS,
    AV1_V4R4_PREPARATION_METHOD,
    AV1V4R4PreparationError,
    assert_av1_v4r4_preparation_measurement,
    build_av1_v4r4_rights_attestation,
    deserialize_av1_v4r4_execution_preflight,
    deserialize_av1_v4r4_owner_freeze,
    deserialize_av1_v4r4_preparation_bundle,
    deserialize_av1_v4r4_preparation_measurement,
    deserialize_av1_v4r4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_manifest import build_av1_v4r3_manifest_payload
from mediaforce.tuning.av1_validation_v4r4_preparation_flow import (
    AV1V4R4PreparationFlowError,
    prepare_av1_v4r4_preparation_custody_readiness,
)
from mediaforce.tuning.av1_validation_v4r4_preparation_registry import (
    AV1V4R4PreparationRegistryBinding,
    AV1V4R4PreparationRegistryError,
    assert_av1_v4r4_preparation_registry,
    initialize_av1_v4r4_preparation_registry,
    publish_av1_v4r4_preparation_grant,
)
from scripts import prepare_av1_v4r4_preparation_custody_readiness as script_module


def _clock(hour: int, minute: int = 0) -> object:
    from datetime import UTC, datetime

    return lambda: datetime(2026, 8, 10, hour, minute, tzinfo=UTC)


def _prior_rights() -> dict[str, object]:
    claims = {
        AV1_VALIDATION_V4_SOURCE_IDS[0]: {
            "license_basis": "CC-BY-3.0",
            "attribution_required": True,
            "redistribution_disposition": "none",
            "terms_reviewed": ["sintel-sharing.html", "cc-by-3.0-legalcode.html"],
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
            "terms_summary": "NASA guidance and metadata reviewed; only video stream 0 is eligible.",
        },
    }
    return build_av1_v4r3_rights_attestation(
        prior_revision_attestation=build_av1_validation_v4_rights_attestation(
            owner_principal="owner.mediaforce",
            attested_at="2026-08-07T05:30:00Z",
            source_claims=claims,
        )
    )


def _rights() -> dict[str, object]:
    return build_av1_v4r4_rights_attestation(
        prior_revision_attestation=_prior_rights()
    )


class AV1V4R4PreparationFlowTests(unittest.TestCase):
    def test_flow_publishes_stop_line_artifacts_and_no_later_authority(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            ordinal = root / "ordinal"
            tools = _stub_tools(root)
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=ordinal,
                tools=tools,
            )
            with _patched_runtime(repo):
                result = prepare_av1_v4r4_preparation_custody_readiness(**request)
                resumed = prepare_av1_v4r4_preparation_custody_readiness(**request)

            self.assertFalse(resumed.created["ordinal_1_grant"])
            self.assertEqual(result.ordinal_grant["ordinal"], 1)
            self.assertEqual(
                {path.name for path in preparation.iterdir()},
                {
                    "v4r4-registry.lock",
                    "v4r4-registry-binding.json",
                    "v4r4-preparation-grant.json",
                    "v4r4-preparation-claim.json",
                    "v4r4-path-privacy.key",
                    "v4r4-path-privacy-key-custody.json",
                    "v4r4-effective-config.json",
                    "v4r4-preparation-bundle.json",
                    "v4r4-preparation-terminal-measurement.json",
                    "v4r4-owner-freeze.json",
                    "v4r4-qualification-request.json",
                    "v4r4-execution-preflight.json",
                },
            )
            self.assertEqual(
                {path.name for path in ordinal.iterdir()},
                {
                    "v4r4-ordinal-registry-plan.json",
                    "v4r4-qualification-request.json",
                    "v4r4-execution-preflight.json",
                    "v4r4-ordinal-01-sequencing-grant.json",
                },
            )
            self.assertTrue((preparation / "v4r4-preparation-grant.json").exists())
            self.assertTrue((preparation / "v4r4-preparation-claim.json").exists())
            self.assertTrue((preparation / "v4r4-path-privacy.key").exists())
            self.assertTrue((preparation / "v4r4-path-privacy-key-custody.json").exists())
            self.assertEqual((preparation / "v4r4-path-privacy.key").stat().st_mode & 0o777, 0o600)
            self.assertEqual((preparation / "v4r4-path-privacy.key").stat().st_size, 32)
            self.assertTrue((preparation / "v4r4-effective-config.json").exists())
            self.assertTrue((preparation / "v4r4-preparation-bundle.json").exists())
            self.assertTrue((preparation / "v4r4-preparation-terminal-measurement.json").exists())
            self.assertTrue((preparation / "v4r4-owner-freeze.json").exists())
            self.assertTrue((preparation / "v4r4-qualification-request.json").exists())
            self.assertTrue((preparation / "v4r4-execution-preflight.json").exists())
            self.assertFalse((preparation / "v4r4-preparation-attempt-started.json").exists())
            self.assertFalse(any(path.name.startswith("preparation-") for path in preparation.iterdir()))
            self.assertTrue((ordinal / "v4r4-ordinal-registry-plan.json").exists())
            self.assertTrue((ordinal / "v4r4-ordinal-01-sequencing-grant.json").exists())
            self.assertFalse((ordinal / "v4r4-ordinal-01-sequencing-claim.json").exists())
            self.assertFalse(any(ordinal.glob("*execution-grant.json")))
            self.assertFalse(any(ordinal.glob("*execution-claim.json")))
            self.assertFalse(any(ordinal.glob("*runner-admission.json")))
            self.assertFalse(any(ordinal.glob("*started.json")))
            self.assertFalse(any(ordinal.glob("*outcome.json")))
            self.assertFalse((ordinal / "v4r4-ordinal-registry-terminal.json").exists())

            measurement = deserialize_av1_v4r4_preparation_measurement(
                (preparation / "v4r4-preparation-terminal-measurement.json").read_bytes()
            )
            self.assertEqual(measurement["state"], "terminal_success")
            bundle = deserialize_av1_v4r4_preparation_bundle(
                (preparation / "v4r4-preparation-bundle.json").read_bytes()
            )
            self.assertEqual(bundle["unresolved_execution_bindings"]["assigned_stage"], "execution_authority")
            freeze = deserialize_av1_v4r4_owner_freeze(
                (preparation / "v4r4-owner-freeze.json").read_bytes()
            )
            self.assertTrue(freeze["manifest_revision_4_owner_freeze_approved"])
            self.assertTrue(freeze["freeze_scope"]["sequencing_grant_creation_authorized"])
            self.assertFalse(freeze["freeze_scope"]["sequencing_claim_creation_authorized"])
            self.assertFalse(freeze["freeze_scope"]["execution_grant_creation_authorized"])
            req = deserialize_av1_v4r4_qualification_request(
                (preparation / "v4r4-qualification-request.json").read_bytes()
            )
            self.assertTrue(req["manifest_revision_4_qualification_request_approved"])
            self.assertTrue(req["request_scope"]["sequencing_grant_creation_authorized"])
            self.assertFalse(req["request_scope"]["sequencing_claim_creation_authorized"])
            self.assertFalse(req["request_scope"]["execution_grant_creation_authorized"])
            self.assertFalse(req["request_scope"]["execution_claim_creation_authorized"])
            preflight = deserialize_av1_v4r4_execution_preflight(
                (preparation / "v4r4-execution-preflight.json").read_bytes()
            )
            self.assertEqual(
                preflight["execution_readiness_state"],
                "ready_pending_owner_execution_authority_decision",
            )
            self.assertTrue(preflight["preflight_scope"]["sequencing_grant_creation_authorized"])
            self.assertFalse(preflight["preflight_scope"]["sequencing_claim_creation_authorized"])
            self.assertFalse(preflight["preflight_scope"]["execution_grant_creation_authorized"])
            self.assertFalse(preflight["preflight_scope"]["execution_claim_creation_authorized"])
            self.assertFalse(preflight["preflight_scope"]["media_read_authorized"])
            self.assertFalse(preflight["preflight_scope"]["runtime_execution_authorized"])
            self.assertEqual(preflight["next_sequencing_ordinal"], 1)
            self.assertEqual(
                preflight["sequencing_grant_binding_stage"],
                "post_preflight_publication",
            )
            self.assertNotIn("ordinal_1_next_grant_id", preflight)

    def test_preflight_failure_leaves_no_sequencing_grant(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            ordinal = root / "ordinal"
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=ordinal,
                tools=_stub_tools(root),
            )
            with (
                _patched_runtime(repo),
                patch.object(
                    flow_module,
                    "_ensure_execution_preflight",
                    side_effect=AV1V4R4PreparationFlowError("preflight failed"),
                ),
                self.assertRaisesRegex(AV1V4R4PreparationFlowError, "preflight failed"),
            ):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertFalse(any(ordinal.glob("*sequencing-grant.json")))
            with _patched_runtime(repo, hour=5):
                resumed = prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertFalse(resumed.created["plan"])
            self.assertTrue(resumed.created["preflight"])
            self.assertTrue(resumed.created["ordinal_1_grant"])

    def test_rights_rollover_rejects_minimal_or_tampered_v4r3_rights(self) -> None:
        with self.assertRaisesRegex(AV1V4R4PreparationError, "canonical v4r3"):
            build_av1_v4r4_rights_attestation(
                prior_revision_attestation={
                    "owner_principal": "owner.mediaforce",
                    "terms": {"license": "test-only"},
                    "source_claims": [{"source": "synthetic"}],
                }
            )
        tampered = _prior_rights()
        tampered["payload_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(AV1V4R4PreparationError, "canonical v4r3"):
            build_av1_v4r4_rights_attestation(prior_revision_attestation=tampered)

    def test_invocation_digests_equal_actual_runner_derivation_for_all_ordinals(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                result = prepare_av1_v4r4_preparation_custody_readiness(**request)
            for invocation in result.bundle["invocation_digests"]:
                ordinal = int(invocation["ordinal"])
                asset_id = str(invocation["asset_id"])
                spec = AV1_V4R4_PREPARATION_SOURCE_SPECS[asset_id]
                expected = av1_validation_v4_qualification_search_invocation_sha256(
                    source_path=Path(str(request["source_paths"][asset_id])),
                    video_policy=av1_v4r4_video_policy_for_ordinal(ordinal),
                    mode=av1_v4r4_mode_for_ordinal(ordinal),
                    warm_start=av1_v4r4_warm_start_for_ordinal(ordinal),
                    extra_search_kwargs=av1_v4r4_search_kwargs_for_inputs(
                        source_codec=str(spec["source_codec"]),
                        width=int(spec["width"]),
                        height=int(spec["height"]),
                        quality_temp_path=Path(str(request["quality_temp_paths"][asset_id])),
                    ),
                )
                self.assertEqual(
                    invocation["invocation_sha256"],
                    expected,
                )
                self.assertEqual(
                    invocation["invocation_sha256"],
                    av1_v4r4_runner_invocation_sha256(
                        ordinal=ordinal,
                        source_path=Path(str(request["source_paths"][asset_id])),
                        quality_temp_path=Path(str(request["quality_temp_paths"][asset_id])),
                        source_codec=str(spec["source_codec"]),
                        width=int(spec["width"]),
                        height=int(spec["height"]),
                    ),
                )

    def test_retained_bundle_rejects_tampered_invocation_digest(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            bundle_path = preparation / "v4r4-preparation-bundle.json"
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            bundle["invocation_digests"][0]["invocation_sha256"] = "sha256:" + "0" * 64
            bundle_path.write_text(json.dumps(bundle, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            bundle_path.chmod(0o600)
            with _patched_runtime(repo):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "preparation is invalid|invocation"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_measurement_rejects_success_and_failure_stage_tampering(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                result = prepare_av1_v4r4_preparation_custody_readiness(**request)
            success = dict(result.measurement)
            success["stages_completed"] = success["stages_completed"][:-1]
            with self.assertRaisesRegex(AV1V4R4PreparationError, "success stages"):
                assert_av1_v4r4_preparation_measurement(success)
            failure = dict(result.measurement)
            failure.update(
                {
                    "state": "terminal_failure",
                    "bundle_id": None,
                    "bundle_payload_sha256": None,
                    "path_privacy_key_id": None,
                    "key_custody": None,
                    "source_digest_sha256": None,
                    "invocation_count": None,
                    "closure_count": None,
                    "rollback": {"removed_artifacts": [], "retained_artifacts": []},
                    "failure": {
                        "stage": "publish",
                        "reason_code": "stage_failed",
                        "error_class": "ValueError",
                        "message_sha256": "sha256:" + "1" * 64,
                    },
                }
            )
            failure["stages_completed"] = ["validate_custody_chain", "hash_toolchain"]
            with self.assertRaisesRegex(AV1V4R4PreparationError, "stages"):
                assert_av1_v4r4_preparation_measurement(failure)

    def test_preparation_registry_rejects_r3_or_unknown_artifacts_before_lock_creation(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            registry = root / "registry"
            registry.mkdir(mode=0o700)
            (registry / "path-privacy.key").write_bytes(b"0" * 32)
            (registry / "path-privacy.key").chmod(0o600)
            binding = AV1V4R4PreparationRegistryBinding(registry=registry, repository_root=repo)
            with self.assertRaisesRegex(AV1V4R4PreparationRegistryError, "revision-3"):
                initialize_av1_v4r4_preparation_registry(binding)
            self.assertFalse((registry / "v4r4-registry.lock").exists())
            (registry / "path-privacy.key").unlink()
            (registry / "mystery.json").write_text("{}\n", encoding="utf-8")
            (registry / "mystery.json").chmod(0o600)
            with self.assertRaisesRegex(AV1V4R4PreparationRegistryError, "unsupported"):
                assert_av1_v4r4_preparation_registry(binding)
            (registry / "mystery.json").unlink()
            (registry / "preparation-grant.json.12.0123456789abcdef.tmp").write_bytes(b"x")
            with self.assertRaisesRegex(AV1V4R4PreparationRegistryError, "unsupported"):
                assert_av1_v4r4_preparation_registry(binding)

    def test_fresh_preparation_registry_initializes_cleanly(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            binding = AV1V4R4PreparationRegistryBinding(
                registry=root / "registry",
                repository_root=repo,
            )
            initialize_av1_v4r4_preparation_registry(binding)
            assert_av1_v4r4_preparation_registry(binding)
            self.assertEqual(list((root / "registry").iterdir()), [])

    def test_owner_mismatch_precedes_mutation(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            request["confirmed_owner_principal"] = "owner.other"
            with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "owner confirmation"):
                prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_retained_grant_rejects_repository_drift_before_consumption(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            binding = AV1V4R4PreparationRegistryBinding(
                registry=preparation,
                repository_root=repo,
            )
            initialize_av1_v4r4_preparation_registry(binding)
            publish_av1_v4r4_preparation_grant(
                binding=binding,
                rights_attestation=_rights(),
                owner_principal="owner.mediaforce",
                repository_commit="a" * 40,
                repository_tree="b" * 40,
                authorized_at="2026-08-10T04:00:00Z",
                valid_until="2026-08-10T23:00:00Z",
                clock=_clock(4),
            )
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                with self.assertRaisesRegex(
                    AV1V4R4PreparationFlowError,
                    "retained preparation grant conflicts",
                ):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertFalse((preparation / "v4r4-preparation-claim.json").exists())
            self.assertFalse((preparation / "v4r4-preparation-bundle.json").exists())
            self.assertFalse((root / "ordinal").exists())

    def test_ordinal_registry_inside_repository_fails_before_mutation(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=repo / "ordinal",
                tools=_stub_tools(root),
            )
            with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "outside"):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertFalse(preparation.exists())
            self.assertFalse((repo / "ordinal").exists())

    def test_invalid_window_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            request["ordinal_1_valid_until"] = "2026-08-10T03:59:59Z"
            with _patched_runtime(repo):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "sequencing window"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_retained_ordinal_grant_rejects_deadline_drift(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            request["ordinal_1_valid_until"] = "2026-08-10T05:30:00Z"
            with _patched_runtime(repo):
                with self.assertRaisesRegex(
                    AV1V4R4PreparationFlowError,
                    "ordinal sequencing grant publication failed",
                ):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_source_digest_drift_fails_and_does_not_publish_bundle(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo, source_digest_failure=True):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "source digest"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertTrue((preparation / "v4r4-preparation-claim.json").exists())
            self.assertTrue((preparation / "v4r4-path-privacy.key").exists())
            self.assertTrue((preparation / "v4r4-path-privacy-key-custody.json").exists())
            self.assertFalse((preparation / "v4r4-effective-config.json").exists())
            self.assertFalse((preparation / "v4r4-preparation-bundle.json").exists())
            self.assertTrue((preparation / "v4r4-preparation-terminal-measurement.json").exists())

    def test_source_digest_reverification_rejects_fifo_and_path_swap(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            fifo = root / "source.fifo"
            os.mkfifo(fifo, mode=0o600)
            with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "source digest"):
                flow_module._sha256_file(fifo)

            source = root / "source.mkv"
            replacement = root / "replacement.mkv"
            source.write_bytes(b"source")
            replacement.write_bytes(b"replacement")
            original_hash = flow_module._hash_open_file

            def swap_after_hash(
                descriptor: int,
                expected: os.stat_result,
                label: str,
            ) -> str:
                digest = original_hash(descriptor, expected, label)
                os.replace(replacement, source)
                return digest

            with (
                patch.object(flow_module, "_hash_open_file", side_effect=swap_after_hash),
                self.assertRaisesRegex(AV1V4R4PreparationFlowError, "source digest"),
            ):
                flow_module._sha256_file(source)

    def test_custody_failure_rolls_back_and_retry_succeeds(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with (
                _patched_runtime(repo),
                patch(
                    "mediaforce.tuning.av1_validation_v4r4_preparation_registry._RegistryContext.write_key",
                    side_effect=OSError("injected custody failure"),
                ),
                self.assertRaises(AV1V4R4PreparationRegistryError),
            ):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertFalse((preparation / "v4r4-preparation-claim.json").exists())
            self.assertFalse((preparation / "v4r4-path-privacy.key").exists())
            self.assertFalse(
                (preparation / "v4r4-preparation-custody-attempt.json").exists()
            )
            with _patched_runtime(repo):
                result = prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertEqual(result.measurement["state"], "terminal_success")

    def test_visible_claim_after_publication_error_recovers_as_committed(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            from mediaforce.tuning import av1_validation_v4r4_preparation_registry as registry_module

            original_write = registry_module._RegistryContext.write_exclusive

            def fail_after_claim_write(context: object, filename: str, data: bytes) -> None:
                original_write(context, filename, data)
                if filename == "v4r4-preparation-claim.json":
                    raise OSError("injected post-publication failure")

            with (
                _patched_runtime(repo),
                patch.object(
                    registry_module._RegistryContext,
                    "write_exclusive",
                    new=fail_after_claim_write,
                ),
                self.assertRaises(AV1V4R4PreparationRegistryError),
            ):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertTrue((preparation / "v4r4-preparation-claim.json").exists())
            self.assertTrue((preparation / "v4r4-path-privacy.key").exists())
            self.assertTrue(
                (preparation / "v4r4-preparation-custody-attempt.json").exists()
            )
            with _patched_runtime(repo):
                result = prepare_av1_v4r4_preparation_custody_readiness(**request)
            self.assertEqual(result.measurement["state"], "terminal_success")
            self.assertFalse(
                (preparation / "v4r4-preparation-custody-attempt.json").exists()
            )

    def test_registry_recovers_two_link_exclusive_publication_temp(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            registry = root / "registry"
            binding = AV1V4R4PreparationRegistryBinding(
                registry=registry,
                repository_root=repo,
            )
            initialize_av1_v4r4_preparation_registry(binding)
            temp = registry / ".v4r4-effective-config.json.123.0123456789abcdef.tmp"
            target = registry / "v4r4-effective-config.json"
            temp.write_bytes(b"{}\n")
            temp.chmod(0o600)
            os.link(temp, target)
            self.assertEqual(temp.stat().st_nlink, 2)
            from mediaforce.tuning import av1_validation_v4r4_preparation_registry as registry_module

            with registry_module._locked_registry(binding):
                pass
            self.assertFalse(temp.exists())
            self.assertTrue(target.exists())
            self.assertEqual(target.stat().st_nlink, 1)

    def test_rights_input_rejects_revision_three_artifact(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            request["rights_attestation"] = build_av1_v4r3_manifest_payload()
            with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "rights attestation"):
                prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_measurement_records_exact_tool_probe_argv(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                prepare_av1_v4r4_preparation_custody_readiness(**request)
            measurement = deserialize_av1_v4r4_preparation_measurement(
                (root / "preparation" / "v4r4-preparation-terminal-measurement.json").read_bytes()
            )
            self.assertEqual(
                measurement["method"]["tool_version_probe_argv"],
                AV1_V4R4_PREPARATION_METHOD["tool_version_probe_argv"],
            )
            self.assertEqual(
                [probe["argv"] for probe in measurement["probes"]],
                [
                    AV1_V4R4_PREPARATION_METHOD["tool_version_probe_argv"][name]
                    for name in ("ab_av1", "ffmpeg", "ffprobe")
                ],
            )

    def test_tool_path_swap_after_descriptor_open_is_detected(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            tools = _stub_tools(root)
            original_probe = flow_module.subprocess.run

            def swapping_probe(*args: object, **kwargs: object) -> object:
                argv = args[0]
                if (
                    isinstance(argv, list)
                    and argv
                    and Path(str(argv[0])).name == "tool"
                    and Path(str(argv[0])).parent.name.startswith(
                        "mediaforce-v4r4-tool-probe-"
                    )
                ):
                    tools["ab_av1"].write_text("#!/bin/sh\necho swapped\n", encoding="utf-8")
                    tools["ab_av1"].chmod(0o700)
                return original_probe(*args, **kwargs)

            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=tools,
            )
            with (
                _patched_runtime(repo),
                patch(
                    "mediaforce.tuning.av1_validation_v4r4_preparation_flow.subprocess.run",
                    side_effect=swapping_probe,
                ),
            ):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "tool binary changed"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_dirty_repository_binding_fails_before_plan(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            request = _request_payload(
                repo=repo,
                preparation=root / "preparation",
                ordinal=root / "ordinal",
                tools=_stub_tools(root),
            )
            with patch(
                "mediaforce.tuning.av1_validation_v4r4_preparation_flow._repository_identity",
                side_effect=RuntimeError("dirty"),
            ):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "clean and canonical"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)

    def test_partial_restart_seals_measurement(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            repo = root / "repo"
            repo.mkdir()
            preparation = root / "preparation"
            ordinal = root / "ordinal"
            request = _request_payload(
                repo=repo,
                preparation=preparation,
                ordinal=ordinal,
                tools=_stub_tools(root),
            )
            with _patched_runtime(repo):
                try:
                    prepare_av1_v4r4_preparation_custody_readiness(**request)
                except AV1V4R4PreparationFlowError:
                    self.fail("initial preparation should succeed")
            (preparation / "v4r4-preparation-terminal-measurement.json").unlink()
            (preparation / "v4r4-preparation-attempt-started.json").write_bytes(
                b'{"schema":"mediaforce.av1_cold_start_v4r4_preparation_attempt_started","schema_version":1,"state":"attempt_started","grant_id":"x","grant_payload_sha256":"sha256:'
                + b'0' * 64
                + b'","claim_id":"y","claim_payload_sha256":"sha256:'
                + b'1' * 64
                + b'","custody_id":"z","custody_payload_sha256":"sha256:'
                + b'2' * 64
                + b'","started_at":"2026-08-10T04:00:00Z"}\n'
            )
            (preparation / "v4r4-preparation-attempt-started.json").chmod(0o600)
            with _patched_runtime(repo):
                with self.assertRaisesRegex(AV1V4R4PreparationFlowError, "already started"):
                    prepare_av1_v4r4_preparation_custody_readiness(**request)
            measurement = deserialize_av1_v4r4_preparation_measurement(
                (preparation / "v4r4-preparation-terminal-measurement.json").read_bytes()
            )
            self.assertEqual(measurement["state"], "terminal_failure")
            self.assertEqual(measurement["failure"]["reason_code"], "partial_restart")

    def test_json_cli_suppresses_private_paths(self) -> None:
        fake = SimpleNamespace(
            created={},
            grant={"grant_id": "av1vprepgrant4r4_" + "a" * 32},
            claim={"claim_id": "av1v4r4prepclaim_" + "b" * 32},
            custody={"custody_id": "av1v4r4keycustody_" + "c" * 32},
            bundle={
                "bundle_id": "av1v4r4prepbundle_" + "d" * 32,
                "effective_config_hmac_id": "av1v4r4confighmac_" + "e" * 32,
            },
            measurement={"measurement_id": "av1v4r4prepmeas_" + "f" * 32},
            freeze={"freeze_id": "av1vfreeze4r4_" + "1" * 32},
            request={"request_id": "av1v4r4req_" + "2" * 32},
            ordinal_registry_id="av1v4r4ordreg_" + "3" * 64,
            plan={"plan_id": "av1v4r4ordplan_" + "4" * 32, "plan_closes_at": "x"},
            preflight={"preflight_id": "av1v4r4preflight_" + "5" * 32},
            ordinal_grant={
                "grant_id": "av1v4r4ordgrant_" + "6" * 32,
                "valid_until": "y",
            },
        )
        payload = {
            "repository_root": "/private/repository",
            "preparation_registry": "/private/preparation",
            "ordinal_registry": "/private/ordinal",
            "rights_attestation": {},
            "owner_principal": "owner.mediaforce",
            "confirm_owner_principal": "owner.mediaforce",
            "preparation_grant_valid_until": "2026-08-10T23:00:00Z",
            "ordinal_1_valid_until": "2026-08-10T06:00:00Z",
            "source_paths": {},
            "dedicated_instance_paths": {},
            "quality_temp_paths": {},
            "tool_paths": {},
        }
        with (
            patch.object(
                script_module,
                "prepare_av1_v4r4_preparation_custody_readiness",
                return_value=fake,
            ),
            patch.object(
                script_module.sys,
                "stdin",
                SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())),
            ),
            patch("builtins.print") as output,
        ):
            status = script_module.main()
        self.assertEqual(status, 0)
        line = output.call_args.args[0]
        self.assertNotIn("/private", line)
        self.assertIn("ready_for_execution_authority_decision", line)


def _request_payload(
    *,
    repo: Path,
    preparation: Path,
    ordinal: Path,
    tools: dict[str, Path],
) -> dict[str, object]:
    return {
        "repository_root": repo,
        "preparation_registry": preparation,
        "ordinal_registry": ordinal,
        "rights_attestation": _rights(),
        "owner_principal": "owner.mediaforce",
        "confirmed_owner_principal": "owner.mediaforce",
        "preparation_grant_valid_until": "2026-08-10T23:00:00Z",
        "ordinal_1_valid_until": "2026-08-10T06:00:00Z",
        "source_paths": {
            asset_id: f"/private/media/{index}.mkv"
            for index, asset_id in enumerate(AV1_V4R4_PREPARATION_SOURCE_SPECS, start=1)
        },
        "dedicated_instance_paths": {
            "runtime_lock": "/private/runtime/lock",
            "source_root": "/private/media",
            "state_root": "/private/state",
            "temp_root": "/private/temp",
        },
        "quality_temp_paths": {
            asset_id: f"/private/temp/{index}"
            for index, asset_id in enumerate(AV1_V4R4_PREPARATION_SOURCE_SPECS, start=1)
        },
        "tool_paths": tools,
    }


def _stub_tools(root: Path) -> dict[str, Path]:
    tools = {}
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        path = root / name
        path.write_text("#!/bin/sh\necho stub-version\n", encoding="utf-8")
        path.chmod(0o700)
        tools[name] = path
    return tools


def _patched_runtime(
    repo: Path, *, source_digest_failure: bool = False, hour: int = 4
) -> ContextManager[object]:
    source_specs = {
        asset_id: spec["media_sha256"]
        for asset_id, spec in AV1_V4R4_PREPARATION_SOURCE_SPECS.items()
    }
    digests = dict(source_specs)
    if source_digest_failure:
        first_key = next(iter(digests))
        digests[first_key] = "sha256:" + "0" * 64

    def fake_sha256_file(path: Path) -> str:
        if path.name in {"ab_av1", "ffmpeg", "ffprobe"}:
            import hashlib

            return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return digests[_asset_id_from_path(path)]

    return patch.multiple(
        "mediaforce.tuning.av1_validation_v4r4_preparation_flow",
        _repository_identity=lambda _path: ("1" * 40, "2" * 40),
        _sha256_file=fake_sha256_file,
        _utc_now=_clock(hour),
    )


def _asset_id_from_path(path: Path) -> str:
    name = path.name
    index = int(name.split(".")[0])
    return list(AV1_V4R4_PREPARATION_SOURCE_SPECS)[index - 1]


if __name__ == "__main__":
    unittest.main()
