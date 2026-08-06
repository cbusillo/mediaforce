from __future__ import annotations

import inspect
from pathlib import Path
import unittest
from typing import Any
from unittest.mock import MagicMock, call

from mediaforce.encoding.quality import QualitySearchResult, QualitySearchWarmStart
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    AV1_VALIDATION_V4_CONTRACT_VERSION,
    AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
    AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA,
    V4QualificationContractError,
    V4QualificationOperationResult,
    av1_validation_v4_qualification_search_invocation_sha256,
    run_v4_qualification_search,
)
from mediaforce.tuning.stream_budget import StreamBudgetLedger


_BALANCED_POLICY: dict[str, Any] = {
    "compression_intent_schema_version": 1,
    "compression_intent": "balanced",
    "compression_intent_source": "operator",
    "compression_intent_confirmed": True,
    "min_crf": 10,
    "max_crf": 45,
}
_REFERENCE_POLICY: dict[str, Any] = {
    "compression_intent_schema_version": 1,
    "compression_intent": "reference",
    "compression_intent_source": "operator",
    "compression_intent_confirmed": True,
    "min_crf": 10,
    "max_crf": 45,
}
_UNCONFIRMED_BALANCED_POLICY: dict[str, Any] = {
    "compression_intent_schema_version": 1,
    "compression_intent": "balanced",
    "compression_intent_source": "operator",
    "compression_intent_confirmed": False,
    "min_crf": 10,
    "max_crf": 45,
}
_SIG_ID = "acss1_abc123"
_COHORT_ID = "acsh1_xyz789"
_SOURCE_PATH = Path("/does/not/exist.mp4")


def _mock_ledger() -> MagicMock:
    return MagicMock(spec=StreamBudgetLedger)


def _v4_warm_start(**overrides: Any) -> QualitySearchWarmStart:
    fields: dict[str, Any] = dict(
        requested_crf=28.0,
        candidate_crf=28,
        search_signature_id=_SIG_ID,
        cohort_id=_COHORT_ID,
        source=AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
    )
    fields.update(overrides)
    return QualitySearchWarmStart(**fields)


def _baseline_quality_result() -> QualitySearchResult:
    return QualitySearchResult(
        crf=28.0,
        metric="vmaf",
        target=93.0,
        score=93.1,
        stdout="",
        target_size_trace={
            "schema_version": 1,
            "status": "selected",
            "selection_reason": "candidate_inside_sample_projection_band",
        },
    )


def _guided_quality_result(
    warm_status: str,
    **trace_overrides: Any,
) -> QualitySearchResult:
    warm_start_trace: dict[str, Any] = {
        "schema_version": 1,
        "status": warm_status,
        "attempted": True,
        "requested_crf": 28.0,
        "candidate_crf": 28,
        "search_signature_id": _SIG_ID,
        "cohort_id": _COHORT_ID,
        "source": AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
        "confidence": None,
        "provenance_id": None,
        "review_risks": [],
        "candidate_count": 0,
        "duration_seconds": 0.001,
        "fallback_used": warm_status != "accepted",
        "fallback_reason": None,
        "error_type": None,
        "candidate": None,
    }
    warm_start_trace.update(trace_overrides)
    return QualitySearchResult(
        crf=28.0,
        metric="vmaf",
        target=93.0,
        score=93.1,
        stdout="",
        target_size_trace={
            "schema_version": 1,
            "status": "selected",
            "warm_start": warm_start_trace,
        },
    )


class TestBaselinePath(unittest.TestCase):
    def test_baseline_accepted_succeeds(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        ledger = _mock_ledger()

        result = run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="baseline",
            stream_budget_ledger=ledger,
            warm_start=None,
        )

        self.assertIsInstance(result, V4QualificationOperationResult)
        self.assertEqual(result.mode, "baseline")
        self.assertEqual(result.cold_start_prior_mirror["status"], "no_recommendation")
        self.assertIsNone(result.cold_start_prior_mirror["execution"])

    def test_baseline_call_kwargs(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        ledger = _mock_ledger()

        run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="baseline",
            stream_budget_ledger=ledger,
            warm_start=None,
            extra_search_kwargs={"source_codec": "h264"},
        )

        self.assertEqual(mock_search.call_count, 1)
        call_args = mock_search.call_args
        self.assertEqual(call_args[0][0], _SOURCE_PATH)
        self.assertEqual(call_args[0][1], _BALANCED_POLICY)
        self.assertIs(call_args[1]["stream_budget_ledger"], ledger)
        self.assertEqual(call_args[1]["source_codec"], "h264")
        self.assertIsNone(call_args[1]["warm_start"])
        self.assertIsNone(call_args[1]["expected_search_signature_id"])
        self.assertNotIn("_allow_validation_warm_start", call_args[1])

    def test_baseline_execution_mirror_is_null(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        result = run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="baseline",
            stream_budget_ledger=_mock_ledger(),
            warm_start=None,
        )

        self.assertIsNone(result.cold_start_prior_mirror.get("execution"))

    def test_baseline_rejects_any_warm_start_trace_key(self) -> None:
        for warm_start_value in ({}, "accepted", None):
            with self.subTest(warm_start_value=warm_start_value):
                quality_result = _baseline_quality_result()
                quality_result.target_size_trace["warm_start"] = warm_start_value
                mock_search = MagicMock(return_value=quality_result)
                with self.assertRaisesRegex(
                    V4QualificationContractError,
                    "must not contain",
                ):
                    run_v4_qualification_search(
                        mock_search,
                        _SOURCE_PATH,
                        _BALANCED_POLICY,
                        mode="baseline",
                        stream_budget_ledger=_mock_ledger(),
                        warm_start=None,
                    )


class TestGuidedPath(unittest.TestCase):
    def test_guided_accepted_succeeds(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        warm_start = _v4_warm_start()
        ledger = _mock_ledger()

        result = run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=ledger,
            warm_start=warm_start,
        )

        self.assertEqual(result.mode, "guided")
        self.assertEqual(result.cold_start_prior_mirror["status"], "no_recommendation")
        execution = result.cold_start_prior_mirror["execution"]
        self.assertIsNotNone(execution)
        self.assertEqual(execution["status"], "accepted")
        self.assertTrue(execution["attempted"])

    def test_guided_rejected_fallback_succeeds(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("rejected_fallback"))
        result = run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=_mock_ledger(),
            warm_start=_v4_warm_start(),
        )

        execution = result.cold_start_prior_mirror["execution"]
        self.assertEqual(execution["status"], "rejected_fallback")

    def test_guided_call_kwargs(self) -> None:
        warm_start = _v4_warm_start()
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        ledger = _mock_ledger()

        run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=ledger,
            warm_start=warm_start,
        )

        self.assertEqual(mock_search.call_count, 1)
        call_args = mock_search.call_args
        self.assertEqual(call_args[0][0], _SOURCE_PATH)
        self.assertEqual(call_args[0][1], _BALANCED_POLICY)
        self.assertIs(call_args[1]["stream_budget_ledger"], ledger)
        self.assertIs(call_args[1]["warm_start"], warm_start)
        self.assertEqual(call_args[1]["expected_search_signature_id"], _SIG_ID)
        self.assertNotIn("_allow_validation_warm_start", call_args[1])

    def test_guided_identity_mirror_matches_frozen_input(self) -> None:
        warm_start = _v4_warm_start(requested_crf=27.5, candidate_crf=27)
        mock_search = MagicMock(return_value=QualitySearchResult(
            crf=27.0,
            metric="vmaf",
            target=93.0,
            score=93.1,
            stdout="",
            target_size_trace={
                "schema_version": 1,
                "status": "selected",
                "warm_start": {
                    "schema_version": 1,
                    "status": "accepted",
                    "attempted": True,
                    "requested_crf": 27.5,
                    "candidate_crf": 27,
                    "search_signature_id": _SIG_ID,
                    "cohort_id": _COHORT_ID,
                    "source": AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
                    "confidence": None,
                    "provenance_id": None,
                    "review_risks": [],
                    "candidate_count": 0,
                    "duration_seconds": 0.001,
                    "fallback_used": False,
                    "fallback_reason": None,
                    "error_type": None,
                    "candidate": None,
                },
            },
        ))
        result = run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=_mock_ledger(),
            warm_start=warm_start,
        )

        execution = result.cold_start_prior_mirror["execution"]
        self.assertEqual(execution["search_signature_id"], warm_start.search_signature_id)
        self.assertEqual(execution["cohort_id"], warm_start.cohort_id)
        self.assertEqual(execution["candidate_crf"], warm_start.candidate_crf)
        self.assertEqual(execution["requested_crf"], warm_start.requested_crf)

    def test_guided_mirror_identity_mismatches_raise(self) -> None:
        warm_start = _v4_warm_start()
        mismatches: dict[str, Any] = {
            "search_signature_id": "acss1_DIFFERENT",
            "cohort_id": "acsh1_DIFFERENT",
            "candidate_crf": 27,
            "requested_crf": 27.5,
            "source": "av1_cold_start_other",
            "confidence": "limited",
            "provenance_id": "acprov1_DIFFERENT",
            "review_risks": ["different_risk"],
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                mock_search = MagicMock(
                    return_value=_guided_quality_result("accepted", **{field: value})
                )
                with self.assertRaisesRegex(V4QualificationContractError, "identity"):
                    run_v4_qualification_search(
                        mock_search,
                        _SOURCE_PATH,
                        _BALANCED_POLICY,
                        mode="guided",
                        stream_budget_ledger=_mock_ledger(),
                        warm_start=warm_start,
                    )

    def test_guided_attempted_false_raises(self) -> None:
        mock_search = MagicMock(
            return_value=_guided_quality_result("accepted", attempted=False)
        )
        with self.assertRaisesRegex(V4QualificationContractError, "attempted=True"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(),
            )

    def test_guided_mirror_does_not_alias_search_trace(self) -> None:
        quality_result = _guided_quality_result("accepted")
        result = run_v4_qualification_search(
            MagicMock(return_value=quality_result),
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=_mock_ledger(),
            warm_start=_v4_warm_start(),
        )

        quality_result.target_size_trace["warm_start"]["status"] = "mutated"
        self.assertEqual(result.cold_start_prior_mirror["execution"]["status"], "accepted")

    def test_guided_unexpected_status_raises(self) -> None:
        warm_start = _v4_warm_start()
        mock_search = MagicMock(return_value=QualitySearchResult(
            crf=28.0,
            metric="vmaf",
            target=93.0,
            score=93.1,
            stdout="",
            target_size_trace={
                "schema_version": 1,
                "status": "selected",
                "warm_start": {
                    "schema_version": 1,
                    "status": "guard_rejected",  # unexpected
                    "attempted": True,
                    "requested_crf": 28.0,
                    "candidate_crf": 28,
                    "search_signature_id": _SIG_ID,
                    "cohort_id": _COHORT_ID,
                    "source": AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
                    "confidence": None,
                    "provenance_id": None,
                    "review_risks": [],
                    "candidate_count": 0,
                    "duration_seconds": 0.001,
                    "fallback_used": False,
                    "fallback_reason": None,
                    "error_type": None,
                    "candidate": None,
                },
            },
        ))
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=warm_start,
            )
        self.assertIn("guard_rejected", str(ctx.exception))

    def test_guided_absent_trace_raises(self) -> None:
        mock_search = MagicMock(return_value=QualitySearchResult(
            crf=28.0,
            metric="vmaf",
            target=93.0,
            score=93.1,
            stdout="",
            target_size_trace={
                "schema_version": 1,
                "status": "selected",
                # no warm_start key
            },
        ))
        with self.assertRaises(V4QualificationContractError):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(),
            )


class TestGuardFailures(unittest.TestCase):
    def test_non_path_source_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaisesRegex(V4QualificationContractError, "pathlib.Path"):
            run_v4_qualification_search(
                mock_search,
                str(_SOURCE_PATH),  # type: ignore[arg-type]
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        mock_search.assert_not_called()

    def test_null_stream_budget_ledger_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaisesRegex(V4QualificationContractError, "StreamBudgetLedger"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=None,  # type: ignore[arg-type]
                warm_start=None,
            )
        mock_search.assert_not_called()

    def test_non_balanced_intent_raises(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _REFERENCE_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        self.assertIn("balanced", str(ctx.exception))
        mock_search.assert_not_called()

    def test_unconfirmed_intent_raises(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _UNCONFIRMED_BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        mock_search.assert_not_called()

    def test_legacy_unconfirmed_raises(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                {},
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        mock_search.assert_not_called()

    def test_v3_harness_source_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        v3_warm_start = QualitySearchWarmStart(
            requested_crf=28.0,
            candidate_crf=28,
            search_signature_id=_SIG_ID,
            cohort_id=_COHORT_ID,
            source="av1_validation_harness",
        )
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=v3_warm_start,
            )
        self.assertIn("V3", str(ctx.exception))
        mock_search.assert_not_called()

    def test_wrong_source_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        wrong_warm_start = QualitySearchWarmStart(
            requested_crf=28.0,
            candidate_crf=28,
            search_signature_id=_SIG_ID,
            cohort_id=_COHORT_ID,
            source="quality_memory",
        )
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=wrong_warm_start,
            )
        self.assertIn(AV1_VALIDATION_V4_QUALIFICATION_SOURCE, str(ctx.exception))
        mock_search.assert_not_called()

    def test_empty_signature_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(search_signature_id=""),
            )
        self.assertIn("search_signature_id", str(ctx.exception))
        mock_search.assert_not_called()

    def test_empty_cohort_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(cohort_id=""),
            )
        self.assertIn("cohort_id", str(ctx.exception))
        mock_search.assert_not_called()

    def test_invalid_signature_prefix_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "search_signature_id"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(search_signature_id="wrong_abc123"),
            )
        mock_search.assert_not_called()

    def test_invalid_cohort_prefix_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "cohort_id"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(cohort_id="wrong_xyz789"),
            )
        mock_search.assert_not_called()

    def test_authoritative_confidence_levels_are_accepted(self) -> None:
        for confidence in ("none", "limited", "moderate", "high"):
            with self.subTest(confidence=confidence):
                warm_start = _v4_warm_start(confidence=confidence)
                mock_search = MagicMock(
                    return_value=_guided_quality_result(
                        "accepted",
                        confidence=confidence,
                    )
                )
                run_v4_qualification_search(
                    mock_search,
                    _SOURCE_PATH,
                    _BALANCED_POLICY,
                    mode="guided",
                    stream_budget_ledger=_mock_ledger(),
                    warm_start=warm_start,
                )

    def test_non_authoritative_confidence_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "confidence"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(confidence="low"),
            )
        mock_search.assert_not_called()

    def test_invalid_provenance_id_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "provenance_id"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(provenance_id="bad id"),
            )
        mock_search.assert_not_called()

    def test_invalid_review_risk_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "review_risks"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(review_risks=("bad risk",)),
            )
        mock_search.assert_not_called()

    def test_invalid_requested_crf_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "requested_crf"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(requested_crf=float("nan")),
            )
        mock_search.assert_not_called()

    def test_invalid_candidate_crf_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "candidate_crf"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(candidate_crf=True),
            )
        mock_search.assert_not_called()

    def test_candidate_crf_outside_policy_bounds_rejected(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "policy bounds"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(candidate_crf=50),
            )
        mock_search.assert_not_called()

    def test_guided_missing_policy_bounds_rejected(self) -> None:
        policy = dict(_BALANCED_POLICY)
        policy.pop("max_crf")
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaisesRegex(V4QualificationContractError, "CRF bounds"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                policy,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(),
            )
        mock_search.assert_not_called()

    def test_invalid_search_result_rejected(self) -> None:
        mock_search = MagicMock(return_value=object())
        with self.assertRaisesRegex(V4QualificationContractError, "invalid result"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )

    def test_null_target_size_trace_raises(self) -> None:
        mock_search = MagicMock(return_value=QualitySearchResult(
            crf=28.0, metric="vmaf", target=93.0, score=93.1, stdout="",
            target_size_trace=None,
        ))
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        self.assertIn("trace", str(ctx.exception).lower())

    def test_baseline_with_warm_start_raises(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(),
            )
        mock_search.assert_not_called()

    def test_guided_without_warm_start_raises(self) -> None:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        with self.assertRaises(V4QualificationContractError):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
            )
        mock_search.assert_not_called()


class TestSearchKwargAllowlist(unittest.TestCase):
    def test_stream_budget_ledger_in_extra_kwargs_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaisesRegex(V4QualificationContractError, "stream_budget_ledger"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"stream_budget_ledger": _mock_ledger()},
            )
        mock_search.assert_not_called()

    def test_allow_validation_warm_start_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"_allow_validation_warm_start": True},
            )
        self.assertIn("_allow_validation_warm_start", str(ctx.exception))
        mock_search.assert_not_called()

    def test_warm_start_in_extra_kwargs_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"warm_start": _v4_warm_start()},
            )
        self.assertIn("warm_start", str(ctx.exception))
        mock_search.assert_not_called()

    def test_expected_sig_in_extra_kwargs_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaises(V4QualificationContractError) as ctx:
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"expected_search_signature_id": _SIG_ID},
            )
        self.assertIn("expected_search_signature_id", str(ctx.exception))
        mock_search.assert_not_called()

    def test_resolved_plan_cannot_override_validated_policy(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaisesRegex(V4QualificationContractError, "resolved_plan"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"resolved_plan": object()},
            )
        mock_search.assert_not_called()

    def test_unknown_kwarg_rejected(self) -> None:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        with self.assertRaisesRegex(V4QualificationContractError, "extra_flag"):
            run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="baseline",
                stream_budget_ledger=_mock_ledger(),
                warm_start=None,
                extra_search_kwargs={"extra_flag": True},
            )
        mock_search.assert_not_called()


class TestPublicSummaryPrivacy(unittest.TestCase):
    def _run_baseline(self) -> V4QualificationOperationResult:
        mock_search = MagicMock(return_value=_baseline_quality_result())
        return run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="baseline",
            stream_budget_ledger=_mock_ledger(),
            warm_start=None,
        )

    def _run_guided(self) -> V4QualificationOperationResult:
        mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
        return run_v4_qualification_search(
            mock_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="guided",
            stream_budget_ledger=_mock_ledger(),
            warm_start=_v4_warm_start(),
        )

    def test_public_summary_no_crf(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            summary_str = str(result.public_summary)
            self.assertNotIn("crf", summary_str.lower())

    def test_public_summary_no_signature(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            summary = result.public_summary
            self.assertNotIn("search_signature_id", summary)
            self.assertNotIn("signature", str(summary))

    def test_public_summary_no_cohort(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            summary = result.public_summary
            self.assertNotIn("cohort_id", summary)
            self.assertNotIn("cohort", str(summary))

    def test_public_summary_no_source_path(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            self.assertNotIn(str(_SOURCE_PATH), str(result.public_summary))

    def test_public_summary_no_provenance(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            self.assertNotIn("provenance", str(result.public_summary))

    def test_public_summary_no_metrics(self) -> None:
        for result in (self._run_baseline(), self._run_guided()):
            summary_str = str(result.public_summary)
            self.assertNotIn("score", summary_str.lower())
            self.assertNotIn("vmaf", summary_str.lower())
            self.assertNotIn("target", summary_str.lower())

    def test_public_summary_has_required_fields(self) -> None:
        result = self._run_baseline()
        self.assertEqual(result.public_summary["schema_version"], 1)
        self.assertEqual(result.public_summary["contract_version"], AV1_VALIDATION_V4_CONTRACT_VERSION)
        self.assertEqual(result.public_summary["mode"], "baseline")
        self.assertTrue(result.public_summary["planner_bypassed"])
        self.assertFalse(result.public_summary["evidence_eligible"])
        self.assertFalse(result.public_summary["media_read_authorized"])

    def test_public_summary_baseline_execution_not_attempted(self) -> None:
        result = self._run_baseline()
        self.assertFalse(result.public_summary["execution_attempted"])
        self.assertIsNone(result.public_summary["execution_status"])

    def test_public_summary_guided_accepted_execution_attempted(self) -> None:
        result = self._run_guided()
        self.assertTrue(result.public_summary["execution_attempted"])
        self.assertEqual(result.public_summary["execution_status"], "accepted")


class TestNoMediaOrDbAccess(unittest.TestCase):
    def test_no_real_filesystem_access(self) -> None:
        import mediaforce.tuning.av1_validation_v4_qualification_search as seam_module
        source = inspect.getsource(seam_module)
        for forbidden in ("open(", "os.path", "subprocess", "requests", "httpx"):
            self.assertNotIn(forbidden, source, f"seam must not reference {forbidden!r}")

    def test_callable_is_fully_injected(self) -> None:
        calls: list[Any] = []

        def no_op_search(source_path: Any, video_policy: Any, **kwargs: Any) -> QualitySearchResult:
            calls.append((source_path, video_policy, kwargs))
            return _baseline_quality_result()

        run_v4_qualification_search(
            no_op_search,
            _SOURCE_PATH,
            _BALANCED_POLICY,
            mode="baseline",
            stream_budget_ledger=_mock_ledger(),
            warm_start=None,
        )
        self.assertEqual(len(calls), 1)

    def test_module_does_not_import_web_runtime(self) -> None:
        import mediaforce.tuning.av1_validation_v4_qualification_search as seam_module
        source = inspect.getsource(seam_module)
        self.assertNotIn("calibration_runtime", source)
        self.assertNotIn("mediaforce.web", source)

    def test_module_not_imported_by_calibration_runtime(self) -> None:
        import mediaforce.web.runtime.calibration_runtime as runtime
        runtime_source = inspect.getsource(runtime)
        self.assertNotIn("av1_validation_v4_qualification_search", runtime_source)


class TestDeterministicInvocation(unittest.TestCase):
    def test_same_inputs_produce_same_mirror_structure(self) -> None:
        results = []
        for _ in range(2):
            mock_search = MagicMock(return_value=_guided_quality_result("accepted"))
            result = run_v4_qualification_search(
                mock_search,
                _SOURCE_PATH,
                _BALANCED_POLICY,
                mode="guided",
                stream_budget_ledger=_mock_ledger(),
                warm_start=_v4_warm_start(),
            )
            results.append(result)

        self.assertEqual(results[0].cold_start_prior_mirror["status"], results[1].cold_start_prior_mirror["status"])
        self.assertEqual(
            results[0].cold_start_prior_mirror["execution"]["status"],
            results[1].cold_start_prior_mirror["execution"]["status"],
        )
        self.assertEqual(results[0].public_summary, results[1].public_summary)
        self.assertEqual(results[0].invocation_sha256, results[1].invocation_sha256)

    def test_baseline_and_guided_invocation_digests_differ(self) -> None:
        baseline = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="baseline",
            warm_start=None,
        )
        guided = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="guided",
            warm_start=_v4_warm_start(),
        )
        self.assertNotEqual(baseline, guided)
        self.assertRegex(baseline, r"sha256:[0-9a-f]{64}")
        self.assertRegex(guided, r"sha256:[0-9a-f]{64}")

    def test_material_search_inputs_change_invocation_digest(self) -> None:
        baseline = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="baseline",
            warm_start=None,
            extra_search_kwargs={"source_codec": "h264"},
        )
        changed_codec = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="baseline",
            warm_start=None,
            extra_search_kwargs={"source_codec": "hevc"},
        )
        changed_policy = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy={**_BALANCED_POLICY, "min_crf": 11},
            mode="baseline",
            warm_start=None,
            extra_search_kwargs={"source_codec": "h264"},
        )
        changed_source = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=Path("/does/not/exist-other.mp4"),
            video_policy=_BALANCED_POLICY,
            mode="baseline",
            warm_start=None,
            extra_search_kwargs={"source_codec": "h264"},
        )

        self.assertEqual(len({baseline, changed_codec, changed_policy, changed_source}), 4)

    def test_invocation_digests_are_pinned(self) -> None:
        baseline = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="baseline",
            warm_start=None,
        )
        guided = av1_validation_v4_qualification_search_invocation_sha256(
            source_path=_SOURCE_PATH,
            video_policy=_BALANCED_POLICY,
            mode="guided",
            warm_start=_v4_warm_start(),
        )

        self.assertEqual(
            baseline,
            "sha256:f3ec9f1946b895eece28aa13b982e7e2d239f75b803b8a3cd4cd8120f0c17c6c",
        )
        self.assertEqual(
            guided,
            "sha256:f8dc2a285d4c48cefc9ed1bbaeacf60332f6fbc11a0afa8336e2cc8eb9cd2028",
        )

    def test_v4_ids_distinct_from_v3(self) -> None:
        from mediaforce.tuning.av1_validation_v4_qualification_search import (
            AV1_VALIDATION_V4_CONTRACT_VERSION,
            AV1_VALIDATION_V4_QUALIFICATION_SOURCE,
        )
        self.assertNotIn("v3", AV1_VALIDATION_V4_QUALIFICATION_SOURCE)
        self.assertNotIn("harness", AV1_VALIDATION_V4_QUALIFICATION_SOURCE)
        self.assertNotIn("v3", AV1_VALIDATION_V4_CONTRACT_VERSION)
        self.assertNotEqual(AV1_VALIDATION_V4_QUALIFICATION_SOURCE, "av1_validation_harness")
        self.assertNotEqual(AV1_VALIDATION_V4_CONTRACT_VERSION, "av1vh1")

    def test_v4_source_constant_is_v4(self) -> None:
        self.assertEqual(AV1_VALIDATION_V4_QUALIFICATION_SOURCE, "av1_cold_start_v4_qualification")
        self.assertEqual(AV1_VALIDATION_V4_CONTRACT_VERSION, "av1vq4s1")
        self.assertEqual(
            AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA,
            "mediaforce.av1_cold_start_v4_qualification_search_invocation",
        )


if __name__ == "__main__":
    unittest.main()
