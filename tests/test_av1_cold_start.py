import copy
from dataclasses import replace
from datetime import UTC, datetime
import unittest
from unittest.mock import Mock, patch

from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    AV1ColdStartContractError,
    AV1ColdStartRequestV1,
    av1_cold_start_traits,
    build_av1_cold_start_request,
    build_av1_cold_start_runtime_compatibility,
    plan_av1_cold_start,
    predict_local_av1_cold_start,
    _row_at_or_after,
    _row_timestamp_status,
)
from mediaforce.tuning.content_intent_observations import (
    BoundaryCohortConfidence,
    BoundaryCohortScope,
    BoundarySummaryStatus,
    ContentIntentBoundaryCohort,
    ContentIntentBoundarySummary,
    ContentIntentPersonalizationState,
    build_content_intent_boundary_compatibility,
)
from mediaforce.web.runtime.calibration_runtime import _av1_cold_start_quality_kwargs


class AV1ColdStartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compatibility = build_content_intent_boundary_compatibility(
            encoder="libsvtav1",
            encoder_version="SVT-AV1 Encoder Lib v3.0.2",
            encoder_runtime_version="ffmpeg version 8.0",
            encoder_runtime_signature_id="ffmpeg-test-signature",
            quality_tool="ab-av1",
            quality_tool_version="ab-av1 0.9.1",
            metric_runtime_signature_id="metric-test-signature",
            preset=6,
            pixel_format="yuv420p10le",
            encoder_parameters=["film-grain-denoise=0", "film-grain=8", "tune=0"],
            output_width=1920,
            output_height=1080,
            frame_rate="24000/1001",
            cadence_transform="none",
            video_filter=None,
            output_container="mkv",
            stream_plan_id="cisp1_test",
            measurement_basis="sample_projection",
            quality_metric="XPSNR",
            quality_target=41.0,
            minimum_quality_score=40.0,
        )
        self.sample_item = {
            "library_item_id": 11,
            "rel_path": "TV/Example/episode.mkv",
            "source_fingerprint": "source-fingerprint",
            "content_version_fingerprint": "content-version-fingerprint",
            "compression_intent": {
                "schema_version": 1,
                "level": "balanced",
                "source": "operator",
                "confirmed": True,
            },
            "media_fingerprint_decision": {
                "status": "measured",
                "evidence_id": "ev1_test",
                "traits": ["animation_cues", "dark_luma", "high_texture"],
            },
            "stream_budget_ledger": {
                "totals": {"remaining_video_bitrate_bps": 2_000_000},
            },
        }
        self.request = self._request()

    def test_request_builds_runtime_contract_and_rejects_missing_inputs(self) -> None:
        self.assertEqual(self.request.intent_level, "balanced")
        self.assertEqual(self.request.content_traits, ("animation", "darkness", "mixed", "texture_detail"))
        self.assertEqual(self.request.runtime_compatibility.target_video_bitrate_bps, 2_000_000)
        self.assertNotIn("public_version_contract_supported", self.request.runtime_compatibility.to_payload())
        self.assertNotIn("version_contract_supported", self.request.runtime_compatibility.to_payload())

        unconfirmed = copy.deepcopy(self.sample_item)
        unconfirmed["compression_intent"]["confirmed"] = False
        with self.assertRaisesRegex(AV1ColdStartContractError, "compression_intent_unconfirmed"):
            self._request(item=unconfirmed)

        missing_evidence = copy.deepcopy(self.sample_item)
        missing_evidence["media_fingerprint_decision"]["status"] = "pending"
        with self.assertRaisesRegex(AV1ColdStartContractError, "content_evidence_missing"):
            self._request(item=missing_evidence)

        missing_budget = copy.deepcopy(self.sample_item)
        missing_budget["stream_budget_ledger"]["totals"]["remaining_video_bitrate_bps"] = 0
        with self.assertRaisesRegex(AV1ColdStartContractError, "target_video_bitrate_missing"):
            self._request(item=missing_budget)

    def test_traits_normalize_known_signals_and_unknown_content(self) -> None:
        self.assertEqual(
            av1_cold_start_traits(["high_motion", "likely_film_grain", "typical"]),
            ("grain_noise", "mixed", "motion", "typical"),
        )
        self.assertEqual(av1_cold_start_traits([]), ("unknown",))

    def test_local_prediction_recommends_and_provides_search_hint(self) -> None:
        state = self._state(confidence="moderate", boundary_status="acceptable_only", crfs=(28.0, 29.0, 30.0))

        prediction = predict_local_av1_cold_start(state, state, self.request)

        self.assertTrue(prediction.recommended)
        self.assertEqual(prediction.source, "local")
        self.assertEqual(prediction.candidate_crf, 29)
        self.assertEqual(prediction.local_scope, "folder")
        self.assertEqual(prediction.to_payload()["source"], "local")
        self.assertNotIn("bundle_id", prediction.to_payload())
        self.assertNotIn("public_evidence_present", prediction.to_payload())
        hint = prediction.search_hint()
        self.assertIsNotNone(hint)
        assert hint is not None
        self.assertEqual(hint.source, "av1_cold_start_local")
        self.assertEqual(hint.candidate_crf, 29)

        quality_kwargs = _av1_cold_start_quality_kwargs(prediction)
        self.assertEqual(quality_kwargs["warm_start"], hint)
        self.assertEqual(quality_kwargs["expected_search_signature_id"], hint.search_signature_id)

    def test_local_prediction_fallbacks_fail_closed(self) -> None:
        cases = (
            (None, None, "no_local_evidence"),
            (self._state(confidence="moderate", boundary_status="acceptable_only", crfs=(28.0, 29.0)), None, "local_evidence_stale"),
            (self._state(confidence="limited", boundary_status="acceptable_only", crfs=(28.0, 29.0)), self._state(confidence="limited", boundary_status="acceptable_only", crfs=(28.0, 29.0)), "local_evidence_low_confidence"),
            (self._state(confidence="moderate", boundary_status="conflicting", crfs=(28.0, 29.0)), self._state(confidence="moderate", boundary_status="conflicting", crfs=(28.0, 29.0)), "local_evidence_conflicting"),
        )
        for all_state, fresh_state, reason in cases:
            with self.subTest(reason=reason):
                prediction = predict_local_av1_cold_start(all_state, fresh_state, self.request)
                self.assertFalse(prediction.recommended)
                self.assertEqual(prediction.source, "none")
                self.assertEqual(prediction.fallback_reason, reason)
                self.assertIsNone(prediction.search_hint())

        state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
        )
        unversioned = predict_local_av1_cold_start(
            state,
            state,
            self.request,
            freshness_fallback_reason="local_evidence_unversioned",
        )
        self.assertEqual(unversioned.fallback_reason, "local_evidence_unversioned")
        self.assertTrue(unversioned.local_evidence_present)
        self.assertTrue(unversioned.provenance_ids)

    def test_local_prediction_rejects_incompatible_target_and_dispersion(self) -> None:
        target_state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
            minimum_bitrate_bps=2_100_000,
            maximum_bitrate_bps=2_500_000,
        )
        target = predict_local_av1_cold_start(target_state, target_state, self.request)
        self.assertEqual(target.fallback_reason, "local_evidence_target_incompatible")

        dispersed_state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(20.0, 29.0, 40.0),
            acceptable_crf_mad=3.0,
        )
        dispersed = predict_local_av1_cold_start(dispersed_state, dispersed_state, self.request)
        self.assertEqual(dispersed.fallback_reason, "local_evidence_crf_dispersion")

    def test_local_prediction_respects_intent_and_configured_bounds(self) -> None:
        state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
        )
        for intent_level, expected_objective in AV1_COLD_START_INTENT_OBJECTIVES.items():
            with self.subTest(intent_level=intent_level):
                item = copy.deepcopy(self.sample_item)
                item["compression_intent"]["level"] = intent_level
                request = self._request(item=item)
                prediction = predict_local_av1_cold_start(state, state, request)
                self.assertEqual(
                    prediction.optimization_objective,
                    expected_objective,
                )
                if intent_level == "balanced":
                    self.assertTrue(prediction.recommended)
                else:
                    self.assertEqual(
                        prediction.fallback_reason,
                        "compression_intent_requires_directional_search",
                    )

        outside_request = self._request(configured_min_crf=30, configured_max_crf=40)
        outside = predict_local_av1_cold_start(state, state, outside_request)
        self.assertEqual(
            outside.fallback_reason,
            "recommended_range_outside_configured_bounds",
        )

        fractional_state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.2, 28.3, 28.4),
        )
        fractional = predict_local_av1_cold_start(
            fractional_state,
            fractional_state,
            self.request,
        )
        self.assertEqual(
            fractional.fallback_reason,
            "recommended_range_has_no_integer_probe",
        )

    def test_item_evidence_and_non_semver_runtime_remain_usable(self) -> None:
        item_state = self._state(
            confidence="limited",
            boundary_status="acceptable_only",
            crfs=(29.0,),
            scope="item",
        )
        item_prediction = predict_local_av1_cold_start(
            item_state,
            item_state,
            self.request,
        )
        self.assertTrue(item_prediction.recommended)
        self.assertEqual(item_prediction.confidence, "moderate")

        runtime = build_av1_cold_start_runtime_compatibility(
            replace(
                self.compatibility,
                encoder_runtime_version="ffmpeg version N-121164-g1a2b3c4",
            ),
            target_video_bitrate_bps=2_000_000,
        )
        self.assertEqual(runtime.encoder_runtime_major, 0)
        request = replace(self.request, runtime_compatibility=runtime)
        prediction = predict_local_av1_cold_start(item_state, item_state, request)
        self.assertTrue(prediction.recommended)

    def test_runtime_contract_rejects_unsupported_measurement_inputs(self) -> None:
        for changes in (
            {"quality_tool": "other-tool"},
            {"measurement_basis": "full_output"},
            {"assessment_contract": "operator_visual_v2"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(AV1ColdStartContractError, "unsupported"):
                    build_av1_cold_start_runtime_compatibility(
                        replace(self.compatibility, **changes),
                        target_video_bitrate_bps=2_000_000,
                    )

    def test_local_provenance_changes_with_evidence_snapshot(self) -> None:
        first = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
            evidence_snapshot_id="cics1_first",
        )
        second = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
            evidence_snapshot_id="cics1_second",
        )

        first_prediction = predict_local_av1_cold_start(first, first, self.request)
        second_prediction = predict_local_av1_cold_start(second, second, self.request)

        self.assertNotEqual(
            first_prediction.provenance_ids,
            second_prediction.provenance_ids,
        )

    def test_plan_uses_only_local_compatible_observations(self) -> None:
        connection = Mock()
        with patch(
                "mediaforce.tuning.av1_cold_start.load_current_content_intent_boundary_observations",
                return_value=[],
        ) as load_local:
            prediction = plan_av1_cold_start(
                connection,
                sample_item=self.sample_item,
                prefix="TV/Example",
                compatibility_payload=self.compatibility.to_payload(),
                configured_min_crf=20,
                configured_max_crf=40,
                as_of="2026-07-27T12:00:00Z",
            )

        self.assertFalse(prediction.recommended)
        self.assertEqual(prediction.source, "none")
        self.assertEqual(prediction.fallback_reason, "no_local_evidence")
        load_local.assert_called_once_with(
            connection,
            intent_semantic_id=self.request.intent_semantic_id,
            compatibility_key=self.compatibility.compatibility_key,
        )

    def test_plan_request_fallbacks_do_not_query_observations(self) -> None:
        item = copy.deepcopy(self.sample_item)
        item.pop("content_version_fingerprint")

        with patch(
                "mediaforce.tuning.av1_cold_start.load_current_content_intent_boundary_observations"
        ) as load_local:
            prediction = plan_av1_cold_start(
                Mock(),
                sample_item=item,
                prefix="TV/Example",
                compatibility_payload=self.compatibility.to_payload(),
                configured_min_crf=20,
                configured_max_crf=40,
                as_of="2026-07-27T12:00:00Z",
            )

        self.assertEqual(prediction.fallback_reason, "cold_start_request_invalid")
        load_local.assert_not_called()

    def test_unversioned_evidence_outside_selected_scope_does_not_veto(self) -> None:
        selected_row = {
            "recorded_at": "2026-07-27T10:00:00Z",
            "provenance_json": '{"recorded_at":"2026-07-27T10:00:00Z"}',
        }
        unrelated_unversioned_row = {
            "recorded_at": "2026-07-27T10:00:00Z",
            "provenance_json": "{}",
        }
        item_state = self._state(
            confidence="limited",
            boundary_status="acceptable_only",
            crfs=(29.0,),
            scope="item",
        )
        with (
            patch(
                "mediaforce.tuning.av1_cold_start.load_current_content_intent_boundary_observations",
                return_value=[selected_row, unrelated_unversioned_row],
            ),
            patch(
                "mediaforce.tuning.av1_cold_start._rows_for_replay_context",
                return_value=[selected_row, unrelated_unversioned_row],
            ),
            patch(
                "mediaforce.tuning.av1_cold_start._replay_local_state",
                side_effect=[item_state, item_state],
            ),
            patch(
                "mediaforce.tuning.av1_cold_start.content_intent_replay_scope_rows",
                return_value=(selected_row,),
            ) as scope_rows,
        ):
            prediction = plan_av1_cold_start(
                Mock(),
                sample_item=self.sample_item,
                prefix="TV/Example",
                compatibility_payload=self.compatibility.to_payload(),
                configured_min_crf=20,
                configured_max_crf=40,
                as_of="2026-07-27T12:00:00Z",
            )

        self.assertTrue(prediction.recommended)
        scope_rows.assert_called_once()
        self.assertEqual(scope_rows.call_args.kwargs["scope"], "item")

    def test_timestamp_helpers_require_versioned_observations(self) -> None:
        cutoff = datetime(2026, 7, 1, tzinfo=UTC)
        as_of = datetime(2026, 7, 28, tzinfo=UTC)
        fresh = {
            "recorded_at": "2026-07-27T10:00:00Z",
            "provenance_json": '{"recorded_at":"2026-07-27T10:00:00Z"}',
        }
        self.assertEqual(_row_timestamp_status(fresh, cutoff, as_of), "fresh")
        self.assertTrue(_row_at_or_after(fresh, cutoff, as_of))
        self.assertEqual(
            _row_timestamp_status(
                {
                    "recorded_at": "2026-07-27T10:00:00Z",
                    "provenance_json": "{}",
                },
                cutoff,
                as_of,
            ),
            "unversioned",
        )
        stale = {
            "recorded_at": "2026-06-01T10:00:00Z",
            "provenance_json": '{"recorded_at":"2026-06-01T10:00:00Z"}',
        }
        future = {
            "recorded_at": "2026-07-29T10:00:00Z",
            "provenance_json": '{"recorded_at":"2026-07-29T10:00:00Z"}',
        }
        self.assertEqual(_row_timestamp_status(stale, cutoff, as_of), "stale")
        self.assertEqual(_row_timestamp_status(future, cutoff, as_of), "future")

    def _request(
            self,
            *,
            item: dict[str, object] | None = None,
            configured_min_crf: int = 20,
            configured_max_crf: int = 40,
    ) -> AV1ColdStartRequestV1:
        return build_av1_cold_start_request(
            sample_item=item or self.sample_item,
            compatibility_payload=self.compatibility.to_payload(),
            configured_min_crf=configured_min_crf,
            configured_max_crf=configured_max_crf,
            as_of="2026-07-27T12:00:00Z",
        )

    @staticmethod
    def _state(
            *,
            confidence: BoundaryCohortConfidence,
            boundary_status: BoundarySummaryStatus,
            crfs: tuple[float, ...],
            scope: BoundaryCohortScope = "folder",
            minimum_bitrate_bps: int = 1_800_000,
            maximum_bitrate_bps: int = 2_200_000,
            acceptable_crf_mad: float = 0.5,
            evidence_snapshot_id: str | None = None,
    ) -> ContentIntentPersonalizationState:
        empty_boundary = ContentIntentBoundarySummary(
            status="empty",
            acceptable_upper_bound_bytes=None,
            unacceptable_lower_bound_bytes=None,
            acceptable_observation_ids=(),
            unacceptable_observation_ids=(),
        )

        def cohort(cohort_scope: BoundaryCohortScope, *, populated: bool = False) -> ContentIntentBoundaryCohort:
            return ContentIntentBoundaryCohort(
                scope=cohort_scope,
                cohort_id=f"{cohort_scope}:safe-test",
                boundary_status=boundary_status if populated else "empty",
                observation_count=len(crfs) if populated else 0,
                source_count=len(crfs) if populated else 0,
                confidence=confidence if populated else "none",
                acceptable_count=len(crfs) if populated else 0,
                unacceptable_count=1 if populated and boundary_status == "conflicting" else 0,
                median_acceptable_bitrate_bps=2_000_000 if populated else None,
                acceptable_bitrate_mad=100_000 if populated else None,
                minimum_acceptable_bitrate_bps=minimum_bitrate_bps if populated else None,
                maximum_acceptable_bitrate_bps=maximum_bitrate_bps if populated else None,
                median_acceptable_crf=sorted(crfs)[len(crfs) // 2] if populated and crfs else None,
                acceptable_crf_mad=acceptable_crf_mad if populated and crfs else None,
                minimum_acceptable_crf=min(crfs) if populated and crfs else None,
                maximum_acceptable_crf=max(crfs) if populated and crfs else None,
                evidence_snapshot_id=(
                    evidence_snapshot_id or f"cics1_{cohort_scope}-test-snapshot"
                    if populated
                    else None
                ),
                actionable=populated and (cohort_scope == "item" or confidence in {"moderate", "high"}),
                reason="test",
            )

        return ContentIntentPersonalizationState(
            model_compatibility_id="model-test",
            intent_semantic_id="intent-test",
            compatibility_key="compatibility-test",
            item_boundary=empty_boundary,
            cohorts=tuple(
                cohort(cohort_scope, populated=scope == cohort_scope)
                for cohort_scope in ("item", "folder", "content_class", "operator")
            ),
        )


if __name__ == "__main__":
    unittest.main()
