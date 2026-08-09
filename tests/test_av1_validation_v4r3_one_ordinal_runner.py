from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.tuning import av1_validation_v4r3_execution_custody as custody_module
from mediaforce.tuning import (
    av1_validation_v4r3_execution_preflight_operation as preflight_module,
)
from mediaforce.tuning.av1_validation_v4r3_execution_custody import (
    AV1V4R3ExecutionCustodyError,
    claim_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    av1_v4_r3_transform_plan_payload,
)
from mediaforce.tuning.av1_validation_v4r3_one_ordinal_runner import (
    AV1V4R3OneOrdinalRunnerError,
    run_av1_v4r3_one_ordinal,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    _locked_registry,
    publish_av1_v4r3_ordinal_window_grant,
)
from mediaforce.tuning.av1_validation_v4r3_runner_admission import (
    serialize_av1_v4r3_runner_admission,
)
from scripts import run_av1_v4r3_one_ordinal as script_module
from tests.test_av1_validation_v4r3_execution_custody import _claim, _prepared
from tests.test_av1_validation_v4r3_preparation_custody import _rights
from tests.test_av1_validation_v4r3_runner_admission import _rebind


class AV1V4R3OneOrdinalRunnerTests(unittest.TestCase):
    def test_admission_and_started_precede_exactly_one_search(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            _claim(preparation, ordinal, repository)
            calls: list[Path] = []

            def search(
                source_path: Path, _policy: object, **kwargs: object
            ) -> QualitySearchResult:
                calls.append(source_path)
                self.assertTrue((ordinal.registry / "ordinal_01.claim.json").exists())
                self.assertTrue(
                    (ordinal.registry / "ordinal_01.runner-admission.json").exists()
                )
                self.assertTrue((ordinal.registry / "ordinal_01.started.json").exists())
                ledger = kwargs["stream_budget_ledger"]
                self.assertEqual(ledger.container_bytes, 4_000_000)
                return _quality_result(
                    ledger_id=ledger.ledger_id,
                    source_id=ledger.source_id,
                    stream_plan_id=ledger.stream_plan.plan_id,
                )

            with patch.object(
                preflight_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                result = run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=_sequence_clock(
                        datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                        datetime(2026, 8, 8, 4, 21, tzinfo=UTC),
                    ),
                )

            self.assertTrue(result.success)
            self.assertEqual(result.ordinal, 1)
            self.assertEqual(len(calls), 1)
            self.assertTrue((ordinal.registry / "ordinal_01.outcome.json").exists())
            self.assertFalse((ordinal.registry / "terminal.json").exists())

            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3OneOrdinalRunnerError),
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=lambda: datetime(2026, 8, 8, 4, 22, tzinfo=UTC),
                )
            self.assertEqual(len(calls), 1)

    def test_search_failure_is_path_free_and_absorbing(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            _claim(preparation, ordinal, repository)

            def search(*_args: object, **_kwargs: object) -> QualitySearchResult:
                raise RuntimeError("failed at /Volumes/private/source.mkv")

            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3OneOrdinalRunnerError) as captured,
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=_sequence_clock(
                        datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                        datetime(2026, 8, 8, 4, 21, tzinfo=UTC),
                    ),
                )

            self.assertNotIn("/Volumes", str(captured.exception))
            self.assertTrue((ordinal.registry / "ordinal_01.outcome.json").exists())
            self.assertTrue((ordinal.registry / "terminal.json").exists())

    def test_json_cli_never_reports_private_paths(self) -> None:
        fake_result = SimpleNamespace(
            ordinal=1,
            asset_id="av1v4_animation_primary_sintel",
            success=True,
            admission={
                "admission_id": "av1v4r3admit_" + "a" * 32,
                "payload_sha256": "sha256:" + "b" * 64,
                "dogfood_authorized": False,
            },
            started={"started_id": "av1vordstarted4r3_" + "c" * 32},
            outcome={"outcome_id": "av1vordoutcome4r3_" + "d" * 32},
            public_summary={"status": "selected"},
        )
        with (
            patch.object(script_module, "_load_mapping", return_value=_rights()),
            patch.object(
                script_module,
                "run_av1_v4r3_one_ordinal",
                return_value=fake_result,
            ),
            patch("builtins.print") as output,
        ):
            status = script_module.main(
                [
                    "--repository-root",
                    "/private/repository",
                    "--preparation-registry",
                    "/private/preparation",
                    "--ordinal-registry",
                    "/private/ordinal",
                    "--run-registry-id",
                    "av1v4r3runreg_" + "e" * 64,
                    "--rights-attestation",
                    "/private/rights.json",
                    "--ordinal",
                    "1",
                    "--owner-principal",
                    "owner:test",
                    "--confirm-owner-principal",
                    "owner:test",
                ]
            )
        self.assertEqual(status, 0)
        rendered = output.call_args.args[0]
        self.assertNotIn("/private", rendered)
        self.assertIn('"dogfood_authorized":false', rendered)

        with (
            patch.object(
                script_module,
                "run_av1_v4r3_one_ordinal",
                side_effect=KeyboardInterrupt("/private/interrupted"),
            ),
            patch("builtins.print") as interrupted_output,
        ):
            interrupted_status = script_module.main(
                [
                    "--repository-root",
                    "/private/repository",
                    "--preparation-registry",
                    "/private/preparation",
                    "--ordinal-registry",
                    "/private/ordinal",
                    "--run-registry-id",
                    "av1v4r3runreg_" + "e" * 64,
                    "--rights-attestation",
                    "/private/rights.json",
                    "--ordinal",
                    "1",
                    "--owner-principal",
                    "owner:test",
                    "--confirm-owner-principal",
                    "owner:test",
                ]
            )
        self.assertEqual(interrupted_status, 1)
        self.assertNotIn("/private", interrupted_output.call_args.args[0])

    def test_malformed_burned_claim_seals_without_media(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            claim_result = _claim(preparation, ordinal, repository)
            claim_path = (
                ordinal.registry / f"{claim_result.grant['grant_id']}.claim.json"
            )
            claim_path.write_bytes(b"not canonical\n")
            calls = 0

            def search(*_args: object, **_kwargs: object) -> QualitySearchResult:
                nonlocal calls
                calls += 1
                raise AssertionError("search must not run")

            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3OneOrdinalRunnerError),
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=lambda: datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                )
            self.assertEqual(calls, 0)
            self.assertTrue((ordinal.registry / "terminal.json").exists())

    def test_successful_execution_admission_unlocks_next_ordinal_authority(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            _claim(preparation, ordinal, repository)

            def search(
                _source: Path, _policy: object, **kwargs: object
            ) -> QualitySearchResult:
                ledger = kwargs["stream_budget_ledger"]
                return _quality_result(
                    ledger_id=ledger.ledger_id,
                    source_id=ledger.source_id,
                    stream_plan_id=ledger.stream_plan.plan_id,
                )

            with patch.object(
                preflight_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=_sequence_clock(
                        datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                        datetime(2026, 8, 8, 4, 21, tzinfo=UTC),
                    ),
                )
            with _locked_registry(ordinal.registry) as context:
                plan = context.load_plan()
            publish_av1_v4r3_ordinal_window_grant(
                binding=ordinal,
                plan=plan,
                ordinal=2,
                clock=lambda: datetime(2026, 8, 8, 4, 25, tzinfo=UTC),
                valid_until="2026-08-08T07:50:00Z",
            )
            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    custody_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
            ):
                claimed = claim_av1_v4r3_execution_grant(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=2,
                    valid_until="2026-08-08T07:40:00Z",
                    clock=lambda: datetime(2026, 8, 8, 4, 30, tzinfo=UTC),
                )
            self.assertEqual(claimed.grant["ordinal"], 2)

    def test_mutated_prior_admission_cannot_unlock_next_authority(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            _claim(preparation, ordinal, repository)

            def search(
                _source: Path, _policy: object, **kwargs: object
            ) -> QualitySearchResult:
                ledger = kwargs["stream_budget_ledger"]
                return _quality_result(
                    ledger_id=ledger.ledger_id,
                    source_id=ledger.source_id,
                    stream_plan_id=ledger.stream_plan.plan_id,
                )

            with patch.object(
                preflight_module,
                "_measure_clean_repository",
                return_value=repository,
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=_sequence_clock(
                        datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                        datetime(2026, 8, 8, 4, 21, tzinfo=UTC),
                    ),
                )
            admission_path = ordinal.registry / "ordinal_01.runner-admission.json"
            admission = json.loads(admission_path.read_bytes())
            admission["path_privacy_key_id"] = "av1vpathkey4r3_" + "f" * 32
            admission_path.write_bytes(
                serialize_av1_v4r3_runner_admission(
                    _rebind(admission, "admission_id", "av1v4r3admit")
                )
            )
            with _locked_registry(ordinal.registry) as context:
                plan = context.load_plan()
            publish_av1_v4r3_ordinal_window_grant(
                binding=ordinal,
                plan=plan,
                ordinal=2,
                clock=lambda: datetime(2026, 8, 8, 4, 25, tzinfo=UTC),
                valid_until="2026-08-08T07:50:00Z",
            )
            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                patch.object(
                    custody_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3ExecutionCustodyError),
            ):
                claim_av1_v4r3_execution_grant(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=2,
                    valid_until="2026-08-08T07:40:00Z",
                    clock=lambda: datetime(2026, 8, 8, 4, 30, tzinfo=UTC),
                )
            self.assertTrue((ordinal.registry / "terminal.json").exists())

    def test_runtime_identity_mismatch_publishes_failure(self) -> None:
        with TemporaryDirectory() as raw:
            preparation, ordinal, repository = _prepared(Path(raw))
            _claim(preparation, ordinal, repository)

            def search(
                _source: Path, _policy: object, **kwargs: object
            ) -> QualitySearchResult:
                ledger = kwargs["stream_budget_ledger"]
                return _quality_result(
                    ledger_id="sb1_" + "f" * 32,
                    source_id=ledger.source_id,
                    stream_plan_id=ledger.stream_plan.plan_id,
                )

            with (
                patch.object(
                    preflight_module,
                    "_measure_clean_repository",
                    return_value=repository,
                ),
                self.assertRaises(AV1V4R3OneOrdinalRunnerError),
            ):
                run_av1_v4r3_one_ordinal(
                    preparation_binding=preparation,
                    ordinal_binding=ordinal,
                    rights_attestation=_rights(),
                    owner_principal="owner:test",
                    confirmed_owner_principal="owner:test",
                    ordinal=1,
                    search_quality_for_source=search,
                    clock=_sequence_clock(
                        datetime(2026, 8, 8, 4, 20, tzinfo=UTC),
                        datetime(2026, 8, 8, 4, 21, tzinfo=UTC),
                    ),
                )
            self.assertTrue((ordinal.registry / "terminal.json").exists())


def _quality_result(
    *,
    ledger_id: str,
    source_id: str,
    stream_plan_id: str,
) -> QualitySearchResult:
    return QualitySearchResult(
        crf=28.0,
        metric="vmaf",
        target=85.0,
        score=90.0,
        stdout="",
        target_size_trace={
            "schema_version": 1,
            "status": "selected",
            "selection_reason": "candidate_inside_sample_projection_band",
            "ledger": {
                "ledger_id": ledger_id,
                "source_id": source_id,
                "stream_plan_id": stream_plan_id,
                "feasibility_status": "feasible",
            },
            "transform_plan": {
                "schema_version": 1,
                "cadence_evidence_id": None,
                "cadence_class": None,
                "cadence_transform": None,
                "video_filter": None,
                "transform_plan_id": av1_v4_r3_transform_plan_payload()[
                    "transform_plan_id"
                ],
            },
        },
    )


def _sequence_clock(*values: datetime) -> Callable[[], datetime]:
    iterator = iter(values)
    return lambda: next(iterator)


if __name__ == "__main__":
    unittest.main()
