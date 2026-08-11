import copy
from dataclasses import replace
from datetime import UTC, datetime
import json
from typing import Sequence
import unittest
from unittest.mock import Mock, patch

from mediaforce.core.evidence import stable_json_hash
from mediaforce.tuning.av1_cold_start import (
    AV1_COLD_START_INTENT_OBJECTIVES,
    AV1ColdStartCellCompatibilityV1,
    AV1ColdStartBundleLoadResult,
    AV1ColdStartConfidenceV1,
    AV1ColdStartContractError,
    AV1ColdStartEvaluationSummaryV1,
    AV1ColdStartPriorBundleV1,
    AV1ColdStartPriorCellV1,
    AV1ColdStartRequestV1,
    av1_cold_start_bundle_from_payload,
    av1_cold_start_traits,
    build_av1_cold_start_cell,
    build_av1_cold_start_request,
    build_av1_cold_start_runtime_compatibility,
    combine_av1_cold_start_predictions,
    load_packaged_av1_cold_start_bundle,
    plan_av1_cold_start,
    predict_local_av1_cold_start,
    predict_public_av1_cold_start,
    serialize_av1_cold_start_bundle,
    _row_at_or_after,
    _row_timestamp_status,
)
from mediaforce.tuning.compression_intent import CompressionIntentLevel
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
        load_packaged_av1_cold_start_bundle.cache_clear()
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
        self.runtime_compatibility = build_av1_cold_start_runtime_compatibility(
            self.compatibility,
            target_video_bitrate_bps=2_000_000,
        )
        self.request = self._request()

    def test_packaged_bundle_is_canonical_versioned_and_intentionally_empty(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()

        self.assertIsNone(loaded.fallback_reason)
        self.assertIsNotNone(loaded.bundle)
        bundle = loaded.bundle
        assert bundle is not None
        self.assertEqual(bundle.state, "validation_pending")
        self.assertEqual(bundle.cells, ())
        artifact = __import__("importlib.resources", fromlist=["files"]).files(
            "mediaforce.tuning.data"
        ).joinpath("av1_cold_start_priors_v1.json")
        self.assertEqual(artifact.read_bytes(), serialize_av1_cold_start_bundle(bundle))

    def test_packaged_bundle_contains_no_private_runtime_tokens(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()
        assert loaded.bundle is not None
        flattened = json.dumps(loaded.bundle.to_payload(), sort_keys=True)

        for forbidden in (
            "library_item_id",
            "source_id",
            "content_id",
            "rel_path",
            "source_path",
            "job_id",
            "review-media",
            ".sqlite3",
            "/Users/",
        ):
            self.assertNotIn(forbidden, flattened)

    def test_planner_uses_empty_public_bundle_without_querying_private_exports(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()
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
                bundle_result=loaded,
            )

        self.assertEqual(prediction.fallback_reason, "no_public_evidence")
        self.assertTrue(prediction.to_payload()["fallback"]["full_measured_calibration"])
        load_local.assert_called_once_with(
            connection,
            intent_semantic_id=self.request.intent_semantic_id,
            compatibility_key=self.compatibility.compatibility_key,
        )

    def test_invalid_public_bundle_fails_closed_before_local_personalization(self) -> None:
        connection = Mock()
        with patch(
                "mediaforce.tuning.av1_cold_start.load_current_content_intent_boundary_observations"
        ) as load_local:
            prediction = plan_av1_cold_start(
                connection,
                sample_item=self.sample_item,
                prefix="TV/Example",
                compatibility_payload=self.compatibility.to_payload(),
                configured_min_crf=20,
                configured_max_crf=40,
                as_of="2026-07-27T12:00:00Z",
                bundle_result=AV1ColdStartBundleLoadResult(
                    bundle=None,
                    fallback_reason="public_bundle_invalid",
                ),
            )

        self.assertEqual(prediction.fallback_reason, "public_bundle_invalid")
        load_local.assert_not_called()

    def test_planner_bounds_invalid_request_reasons(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()
        item = copy.deepcopy(self.sample_item)
        item.pop("content_version_fingerprint")

        prediction = plan_av1_cold_start(
            Mock(),
            sample_item=item,
            prefix="TV/Example",
            compatibility_payload=self.compatibility.to_payload(),
            configured_min_crf=20,
            configured_max_crf=40,
            as_of="2026-07-27T12:00:00Z",
            bundle_result=loaded,
        )
        self.assertEqual(prediction.fallback_reason, "cold_start_request_invalid")

        prediction = plan_av1_cold_start(
            Mock(),
            sample_item=self.sample_item,
            prefix="TV/Example",
            compatibility_payload=self.compatibility.to_payload(),
            configured_min_crf=20,
            configured_max_crf=40,
            as_of="2026-07-27T12:00:00Z",
            bundle_result=AV1ColdStartBundleLoadResult(
                bundle=None,
                fallback_reason="unbounded prose",
            ),
        )
        self.assertEqual(prediction.fallback_reason, "public_bundle_invalid")

        with patch(
                "mediaforce.tuning.av1_cold_start.load_current_content_intent_boundary_observations",
                side_effect=ValueError("invalid replay rows"),
        ):
            prediction = plan_av1_cold_start(
                Mock(),
                sample_item=self.sample_item,
                prefix="TV/Example",
                compatibility_payload=self.compatibility.to_payload(),
                configured_min_crf=20,
                configured_max_crf=40,
                as_of="2026-07-27T12:00:00Z",
                bundle_result=AV1ColdStartBundleLoadResult(
                    bundle=self._bundle([self._cell()]),
                    fallback_reason=None,
                ),
            )
        self.assertEqual(prediction.fallback_reason, "cold_start_request_invalid")

        compatibility_payload = self.compatibility.to_payload()
        compatibility_payload["encoder_parameters"] = ["film-grain=not-a-number"]
        prediction = plan_av1_cold_start(
            Mock(),
            sample_item=self.sample_item,
            prefix="TV/Example",
            compatibility_payload=compatibility_payload,
            configured_min_crf=20,
            configured_max_crf=40,
            as_of="2026-07-27T12:00:00Z",
            bundle_result=loaded,
        )
        self.assertEqual(prediction.fallback_reason, "cold_start_request_invalid")

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
                bundle_result=AV1ColdStartBundleLoadResult(
                    bundle=self._bundle([self._cell()]),
                    fallback_reason=None,
                ),
            )

        self.assertTrue(prediction.recommended)
        scope_rows.assert_called_once()
        self.assertEqual(scope_rows.call_args.kwargs["scope"], "item")

    def test_bundle_digest_and_strict_fields_fail_closed(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()
        assert loaded.bundle is not None
        payload = loaded.bundle.to_payload()
        payload["bundle_version"] = "tampered"
        with self.assertRaisesRegex(AV1ColdStartContractError, "digest"):
            av1_cold_start_bundle_from_payload(payload)

        payload = loaded.bundle.to_payload()
        payload["unexpected"] = True
        with self.assertRaisesRegex(AV1ColdStartContractError, "unexpected"):
            av1_cold_start_bundle_from_payload(payload)

    def test_bundle_privacy_scan_rejects_local_paths_even_with_valid_digest(self) -> None:
        loaded = load_packaged_av1_cold_start_bundle()
        assert loaded.bundle is not None
        for private_path in (
            "/Users/alice/private/runtime.sqlite3",
            "path=/Users/alice/private/runtime.sqlite3",
            "file:///Users/alice/private/runtime.sqlite3",
            "/private/var/folders/runtime.db",
            "/tmp/private-training-export.json",
            "/tmp",
            "smb://server/private/review-media",
            "~/Library/Application Support/mediaforce/library.sqlite3",
        ):
            with self.subTest(private_path=private_path):
                payload = copy.deepcopy(loaded.bundle.to_payload())
                payload["provenance"]["source"] = private_path
                semantic = dict(payload)
                semantic.pop("payload_sha256")
                payload["payload_sha256"] = f"sha256:{stable_json_hash(semantic)}"

                with self.assertRaisesRegex(AV1ColdStartContractError, "local path"):
                    av1_cold_start_bundle_from_payload(payload)

    def test_local_staleness_requires_a_hash_bound_matching_timestamp(self) -> None:
        cutoff = datetime(2026, 7, 1, tzinfo=UTC)
        as_of = datetime(2026, 7, 27, tzinfo=UTC)
        row = {
            "recorded_at": "2026-07-27T00:00:00Z",
            "provenance_json": json.dumps({"recorded_at": "2026-07-27T00:00:00Z"}),
        }
        self.assertTrue(_row_at_or_after(row, cutoff))

        row["recorded_at"] = "2027-07-27T00:00:00Z"
        self.assertFalse(_row_at_or_after(row, cutoff))
        self.assertEqual(_row_timestamp_status(row, cutoff), "unversioned")

        row = {
            "recorded_at": "2026-06-01T00:00:00Z",
            "provenance_json": json.dumps({"recorded_at": "2026-06-01T00:00:00Z"}),
        }
        self.assertFalse(_row_at_or_after(row, cutoff))
        self.assertEqual(_row_timestamp_status(row, cutoff), "stale")

        row = {
            "recorded_at": "2027-07-27T00:00:00Z",
            "provenance_json": json.dumps({"recorded_at": "2027-07-27T00:00:00Z"}),
        }
        self.assertFalse(_row_at_or_after(row, cutoff, as_of))
        self.assertEqual(_row_timestamp_status(row, cutoff, as_of), "future")

    def test_content_traits_are_multi_label_with_mixed_and_unknown_support(self) -> None:
        traits = av1_cold_start_traits(
            [
                "animation_cues",
                "dark_luma",
                "high_motion",
                "high_texture",
                "likely_film_grain",
                "low_motion_dialogue",
            ]
        )

        self.assertEqual(
            traits,
            (
                "animation",
                "darkness",
                "grain_noise",
                "low_motion_dialogue",
                "mixed",
                "motion",
                "texture_detail",
            ),
        )
        self.assertEqual(av1_cold_start_traits([]), ("unknown",))
        self.assertEqual(av1_cold_start_traits(["typical"]), ("typical",))

    def test_every_intent_has_an_explicit_objective(self) -> None:
        for intent_level, expected_objective in AV1_COLD_START_INTENT_OBJECTIVES.items():
            with self.subTest(intent_level=intent_level):
                item = copy.deepcopy(self.sample_item)
                item["compression_intent"]["level"] = intent_level
                request = self._request(item=item)
                self.assertEqual(request.intent_level, intent_level)
                self.assertEqual(AV1_COLD_START_INTENT_OBJECTIVES[request.intent_level], expected_objective)
                prediction = predict_public_av1_cold_start(
                    self._bundle([self._cell(intent_level=intent_level)]),
                    request,
                )
                if intent_level == "balanced":
                    self.assertTrue(prediction.recommended)
                else:
                    self.assertEqual(
                        prediction.fallback_reason,
                        "compression_intent_requires_directional_search",
                    )

    def test_unconfirmed_intent_and_unmeasured_content_return_no_request(self) -> None:
        item = copy.deepcopy(self.sample_item)
        item["compression_intent"]["confirmed"] = False
        with self.assertRaisesRegex(AV1ColdStartContractError, "compression_intent_unconfirmed"):
            self._request(item=item)

        item = copy.deepcopy(self.sample_item)
        item["media_fingerprint_decision"]["status"] = "unknown"
        with self.assertRaisesRegex(AV1ColdStartContractError, "content_evidence_missing"):
            self._request(item=item)

    def test_public_prior_recommends_a_bounded_first_probe(self) -> None:
        cell = self._cell()
        prediction = predict_public_av1_cold_start(self._bundle([cell]), self.request)

        self.assertTrue(prediction.recommended)
        self.assertEqual(prediction.source, "public")
        self.assertEqual(prediction.requested_crf, 29.0)
        self.assertEqual(prediction.candidate_crf, 29)
        self.assertEqual((prediction.crf_lower, prediction.crf_upper), (28.0, 30.0))
        self.assertEqual(prediction.confidence, "moderate")
        self.assertIn("banding_dark_scene_damage", prediction.review_risks)
        hint = prediction.search_hint()
        assert hint is not None
        self.assertEqual(hint.source, "av1_cold_start_public")
        self.assertEqual(hint.search_signature_id, self.request.search_signature_id)
        quality_kwargs = _av1_cold_start_quality_kwargs(prediction)
        self.assertEqual(quality_kwargs["warm_start"], hint)
        self.assertEqual(
            quality_kwargs["expected_search_signature_id"],
            self.request.search_signature_id,
        )

        narrow_cell = self._cell(required_traits=["animation"])
        uncovered = predict_public_av1_cold_start(
            self._bundle([narrow_cell]),
            self.request,
        )
        self.assertEqual(uncovered.fallback_reason, "content_profile_uncovered")

    def test_public_prior_low_confidence_and_unvalidated_cells_return_no_recommendation(self) -> None:
        low_confidence = self._cell(
            confidence=AV1ColdStartConfidenceV1(
                level="limited",
                score=0.6,
                evidence_count=2,
                independent_source_count=2,
            )
        )
        prediction = predict_public_av1_cold_start(self._bundle([low_confidence]), self.request)
        self.assertFalse(prediction.recommended)
        self.assertEqual(prediction.fallback_reason, "public_evidence_low_confidence")

        unvalidated = self._cell(
            evaluation=AV1ColdStartEvaluationSummaryV1(
                held_out_count=2,
                range_hit_count=1,
                baseline_candidate_count=3,
                guided_candidate_count=3,
                quality_floor_regression_count=0,
                final_size_regression_count=0,
                visual_rejection_regression_count=0,
                operator_attention_regression_count=0,
            )
        )
        prediction = predict_public_av1_cold_start(self._bundle([unvalidated]), self.request)
        self.assertFalse(prediction.recommended)
        self.assertEqual(prediction.fallback_reason, "public_evidence_not_validated")

        low_accuracy = self._cell(
            evaluation=AV1ColdStartEvaluationSummaryV1(
                held_out_count=4,
                range_hit_count=1,
                baseline_candidate_count=8,
                guided_candidate_count=4,
                quality_floor_regression_count=0,
                final_size_regression_count=0,
                visual_rejection_regression_count=0,
                operator_attention_regression_count=0,
            )
        )
        prediction = predict_public_av1_cold_start(self._bundle([low_accuracy]), self.request)
        self.assertEqual(prediction.fallback_reason, "public_evidence_not_validated")

    def test_stale_conflicting_and_out_of_bounds_public_priors_fall_back(self) -> None:
        future_bundle = AV1ColdStartPriorBundleV1(
            bundle_version="future-test",
            predictor_contract_version="acsp1",
            state="active",
            published_at="2026-07-28T00:00:00Z",
            valid_until="2027-01-23T00:00:00Z",
            provenance_source="controlled_public_aggregate",
            provenance_method="bounded_tables_with_controlled_holdouts",
            privacy_contract="aggregate_only_no_runtime_or_media_identifiers",
            governance_issue="#277",
            cells=(self._cell(),),
        )
        future = predict_public_av1_cold_start(future_bundle, self.request)
        self.assertEqual(future.fallback_reason, "public_bundle_not_yet_effective")

        stale_bundle = self._bundle([self._cell()], valid_until="2026-07-27T00:00:00Z")
        stale = predict_public_av1_cold_start(stale_bundle, self.request)
        self.assertEqual(stale.fallback_reason, "public_bundle_stale")

        first = self._cell(
            crf_lower=27.0,
            crf_center=28.0,
            crf_upper=29.0,
        )
        second = self._cell(
            cell_compatibility=replace(
                first.compatibility,
                target_video_bitrate_min_bps=1_600_000,
            ),
            crf_lower=31.0,
            crf_center=32.0,
            crf_upper=33.0,
        )
        conflicting = predict_public_av1_cold_start(self._bundle([first, second]), self.request)
        self.assertEqual(conflicting.fallback_reason, "public_evidence_conflicting")

        with self.assertRaisesRegex(AV1ColdStartContractError, "coverage"):
            self._bundle([
                self._cell(crf_lower=27.0, crf_center=28.0, crf_upper=29.0),
                self._cell(crf_lower=31.0, crf_center=32.0, crf_upper=33.0),
            ])

        outside = self._cell(crf_lower=19.0, crf_center=20.0, crf_upper=21.0)
        bounded_request = self._request(configured_min_crf=20, configured_max_crf=40)
        prediction = predict_public_av1_cold_start(self._bundle([outside]), bounded_request)
        self.assertEqual(prediction.fallback_reason, "recommended_range_outside_configured_bounds")

        no_integer = self._cell(crf_lower=28.2, crf_center=28.3, crf_upper=28.4)
        prediction = predict_public_av1_cold_start(self._bundle([no_integer]), self.request)
        self.assertEqual(prediction.fallback_reason, "recommended_range_has_no_integer_probe")

        shifted = self._cell(crf_lower=28.2, crf_center=28.3, crf_upper=29.4)
        prediction = predict_public_av1_cold_start(self._bundle([shifted]), self.request)
        self.assertEqual(prediction.candidate_crf, 29)

    def test_every_compatibility_dimension_is_fail_closed(self) -> None:
        cell = self._cell()
        fields = {
            "encoder_version": (4, 0, 0),
            "public_version_contract_supported": False,
            "encoder_runtime_major": 9,
            "quality_tool_major": 1,
            "preset": 7,
            "pixel_format": "yuv420p",
            "bit_depth": 8,
            "output_width": 1280,
            "output_height": 720,
            "frame_rate": "30000/1001",
            "cadence_transform": "deinterlace",
            "video_filter": "scale=1280:720",
            "encoder_parameters": ("film-grain-denoise=0", "film-grain=0", "tune=0"),
            "grain_strength": 0,
            "quality_metric": "VMAF",
            "quality_target": 95.0,
            "minimum_quality_score": 94.0,
            "output_container": "mp4",
            "target_video_bitrate_bps": 3_000_000,
        }
        for field_name, value in fields.items():
            with self.subTest(field_name=field_name):
                request = replace(self.request)
                runtime_values = self.runtime_compatibility.to_payload()
                runtime_values[field_name] = value
                incompatible = type(self.runtime_compatibility)(
                    encoder=runtime_values["encoder"],
                    encoder_version=(
                        value if field_name == "encoder_version" else self.runtime_compatibility.encoder_version
                    ),
                    public_version_contract_supported=runtime_values[
                        "public_version_contract_supported"
                    ],
                    encoder_runtime_major=runtime_values["encoder_runtime_major"],
                    quality_tool=runtime_values["quality_tool"],
                    quality_tool_major=runtime_values["quality_tool_major"],
                    measurement_basis=runtime_values["measurement_basis"],
                    assessment_contract=runtime_values["assessment_contract"],
                    preset=runtime_values["preset"],
                    pixel_format=runtime_values["pixel_format"],
                    bit_depth=runtime_values["bit_depth"],
                    output_width=runtime_values["output_width"],
                    output_height=runtime_values["output_height"],
                    frame_rate=runtime_values["frame_rate"],
                    cadence_transform=runtime_values["cadence_transform"],
                    video_filter=runtime_values["video_filter"],
                    encoder_parameters=tuple(runtime_values["encoder_parameters"]),
                    grain_strength=runtime_values["grain_strength"],
                    grain_denoise=runtime_values["grain_denoise"],
                    quality_metric=runtime_values["quality_metric"],
                    quality_target=runtime_values["quality_target"],
                    minimum_quality_score=runtime_values["minimum_quality_score"],
                    output_container=runtime_values["output_container"],
                    target_video_bitrate_bps=runtime_values["target_video_bitrate_bps"],
                )
                incompatible_request = type(self.request)(
                    intent_level=request.intent_level,
                    intent_semantic_id=request.intent_semantic_id,
                    content_traits=request.content_traits,
                    runtime_compatibility=incompatible,
                    configured_min_crf=request.configured_min_crf,
                    configured_max_crf=request.configured_max_crf,
                    as_of=request.as_of,
                )
                prediction = predict_public_av1_cold_start(
                    self._bundle([cell]),
                    incompatible_request,
                )
                expected_reason = (
                    "public_runtime_version_unsupported"
                    if field_name == "public_version_contract_supported"
                    else "public_evidence_incompatible"
                )
                self.assertEqual(prediction.fallback_reason, expected_reason)

    def test_non_semver_runtime_preserves_local_personalization(self) -> None:
        runtime = build_av1_cold_start_runtime_compatibility(
            replace(
                self.compatibility,
                encoder_runtime_version="ffmpeg version N-121164-g1a2b3c4",
            ),
            target_video_bitrate_bps=2_000_000,
        )
        request = replace(self.request, runtime_compatibility=runtime)

        self.assertFalse(runtime.public_version_contract_supported)
        public = predict_public_av1_cold_start(self._bundle([self._cell()]), request)
        self.assertEqual(public.fallback_reason, "public_runtime_version_unsupported")

        local_state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
        )
        local = predict_local_av1_cold_start(local_state, local_state, request)
        self.assertTrue(local.recommended)
        combined = combine_av1_cold_start_predictions(public, local, request)
        self.assertTrue(combined.recommended)
        self.assertEqual(combined.source, "local")

    def test_unsupported_measurement_contract_returns_no_request(self) -> None:
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

    def test_local_personalization_recommends_only_fresh_moderate_evidence(self) -> None:
        moderate_state = self._state(
            confidence="moderate",
            boundary_status="acceptable_only",
            crfs=(28.0, 29.0, 30.0),
        )
        recommendation = predict_local_av1_cold_start(moderate_state, moderate_state, self.request)
        self.assertTrue(recommendation.recommended)
        self.assertEqual(recommendation.source, "local")
        self.assertEqual(recommendation.local_scope, "folder")
        self.assertEqual(recommendation.requested_crf, 29.0)

        stale = predict_local_av1_cold_start(moderate_state, None, self.request)
        self.assertEqual(stale.fallback_reason, "local_evidence_stale")

        limited_state = self._state(
            confidence="limited",
            boundary_status="acceptable_only",
            crfs=(29.0,),
        )
        limited = predict_local_av1_cold_start(limited_state, limited_state, self.request)
        self.assertEqual(limited.fallback_reason, "local_evidence_low_confidence")

        conflict_state = self._state(
            confidence="high",
            boundary_status="conflicting",
            crfs=(28.0, 29.0, 30.0),
        )
        conflict = predict_local_av1_cold_start(conflict_state, conflict_state, self.request)
        self.assertEqual(conflict.fallback_reason, "local_evidence_conflicting")

        item_state = self._state(
            confidence="limited",
            boundary_status="acceptable_only",
            crfs=(29.0,),
            scope="item",
        )
        item = predict_local_av1_cold_start(item_state, item_state, self.request)
        self.assertTrue(item.recommended)
        self.assertEqual(item.confidence, "moderate")

        dispersed_state = self._state(
            confidence="high",
            boundary_status="acceptable_only",
            crfs=(20.0, 30.0, 40.0),
        )
        dispersed = predict_local_av1_cold_start(dispersed_state, dispersed_state, self.request)
        self.assertEqual(dispersed.fallback_reason, "local_evidence_crf_dispersion")

        unversioned = predict_local_av1_cold_start(
            moderate_state,
            moderate_state,
            self.request,
            freshness_fallback_reason="local_evidence_unversioned",
        )
        self.assertEqual(unversioned.fallback_reason, "local_evidence_unversioned")

    def test_local_evidence_layers_over_public_without_writing_back(self) -> None:
        public = predict_public_av1_cold_start(self._bundle([self._cell()]), self.request)
        local_state = self._state(
            confidence="high",
            boundary_status="acceptable_only",
            crfs=(28.5, 29.0, 29.5),
        )
        local = predict_local_av1_cold_start(local_state, local_state, self.request)
        combined = combine_av1_cold_start_predictions(public, local, self.request)

        self.assertTrue(combined.recommended)
        self.assertEqual(combined.source, "combined")
        self.assertEqual((combined.crf_lower, combined.crf_upper), (28.5, 29.5))
        self.assertEqual(self._bundle([self._cell()]).cells[0].crf_center, 29.0)
        self.assertNotIn("TV/Example", json.dumps(combined.to_payload()))

        conflicting_state = self._state(
            confidence="high",
            boundary_status="acceptable_only",
            crfs=(34.0, 35.0, 36.0),
        )
        conflicting_local = predict_local_av1_cold_start(
            conflicting_state,
            conflicting_state,
            self.request,
        )
        conflict = combine_av1_cold_start_predictions(public, conflicting_local, self.request)
        self.assertEqual(conflict.fallback_reason, "public_local_evidence_conflicting")

        changed_snapshot = self._state(
            confidence="high",
            boundary_status="acceptable_only",
            crfs=(28.5, 29.0, 29.5),
            evidence_snapshot_id="cics1_changed-test-snapshot",
        )
        changed_local = predict_local_av1_cold_start(
            changed_snapshot,
            changed_snapshot,
            self.request,
        )
        self.assertNotEqual(local.provenance_ids, changed_local.provenance_ids)

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

    def _cell(
            self,
            *,
            intent_level: CompressionIntentLevel = "balanced",
            cell_compatibility: AV1ColdStartCellCompatibilityV1 | None = None,
            required_traits: Sequence[str] | None = None,
            crf_lower: float = 28.0,
            crf_center: float = 29.0,
            crf_upper: float = 30.0,
            confidence: AV1ColdStartConfidenceV1 | None = None,
            evaluation: AV1ColdStartEvaluationSummaryV1 | None = None,
    ) -> AV1ColdStartPriorCellV1:
        runtime = self.runtime_compatibility
        compatibility = cell_compatibility or AV1ColdStartCellCompatibilityV1(
            encoder=runtime.encoder,
            encoder_version_min=(3, 0, 0),
            encoder_version_max_exclusive=(4, 0, 0),
            encoder_runtime_major=runtime.encoder_runtime_major,
            quality_tool=runtime.quality_tool,
            quality_tool_major=runtime.quality_tool_major,
            measurement_basis=runtime.measurement_basis,
            assessment_contract=runtime.assessment_contract,
            preset=runtime.preset,
            pixel_format=runtime.pixel_format,
            bit_depth=runtime.bit_depth,
            output_width=runtime.output_width,
            output_height=runtime.output_height,
            frame_rate=runtime.frame_rate,
            cadence_transform=runtime.cadence_transform,
            video_filter=runtime.video_filter,
            encoder_parameters=runtime.encoder_parameters,
            grain_strength=runtime.grain_strength,
            grain_denoise=runtime.grain_denoise,
            quality_metric=runtime.quality_metric,
            quality_target=runtime.quality_target,
            minimum_quality_score=runtime.minimum_quality_score,
            output_container=runtime.output_container,
            target_video_bitrate_min_bps=1_500_000,
            target_video_bitrate_max_bps=2_500_000,
        )
        return build_av1_cold_start_cell(
            intent_level=intent_level,
            required_traits=required_traits or ["animation", "darkness", "mixed", "texture_detail"],
            compatibility=compatibility,
            crf_lower=crf_lower,
            crf_center=crf_center,
            crf_upper=crf_upper,
            confidence=confidence or AV1ColdStartConfidenceV1(
                level="moderate",
                score=0.78,
                evidence_count=8,
                independent_source_count=6,
            ),
            evaluation=evaluation or AV1ColdStartEvaluationSummaryV1(
                held_out_count=4,
                range_hit_count=3,
                baseline_candidate_count=18,
                guided_candidate_count=9,
                quality_floor_regression_count=0,
                final_size_regression_count=0,
                visual_rejection_regression_count=0,
                operator_attention_regression_count=0,
            ),
            provenance_version="controlled-cohort-v1",
            provenance_method="held_out_bounded_table",
            reviewed_at="2026-07-27T00:00:00Z",
            review_risks=["banding_dark_scene_damage", "flat_regions_and_edges", "softness_detail_loss"],
        )

    @staticmethod
    def _bundle(
            cells: Sequence[AV1ColdStartPriorCellV1],
            *,
            valid_until: str = "2027-01-23T00:00:00Z",
    ) -> AV1ColdStartPriorBundleV1:
        return AV1ColdStartPriorBundleV1(
            bundle_version="test-1",
            predictor_contract_version="acsp1",
            state="active",
            published_at="2026-07-26T00:00:00Z",
            valid_until=valid_until,
            provenance_source="controlled_public_aggregate",
            provenance_method="bounded_tables_with_controlled_holdouts",
            privacy_contract="aggregate_only_no_runtime_or_media_identifiers",
            governance_issue="#277",
            cells=tuple(sorted(cells, key=lambda cell: cell.cell_id)),
        )

    @staticmethod
    def _state(
            *,
            confidence: BoundaryCohortConfidence,
            boundary_status: BoundarySummaryStatus,
            crfs: tuple[float, ...],
            scope: BoundaryCohortScope = "folder",
            evidence_snapshot_id: str | None = None,
    ) -> ContentIntentPersonalizationState:
        empty_boundary = ContentIntentBoundarySummary(
            status="empty",
            acceptable_upper_bound_bytes=None,
            unacceptable_lower_bound_bytes=None,
            acceptable_observation_ids=(),
            unacceptable_observation_ids=(),
        )

        def cohort(
                scope: BoundaryCohortScope,
                *,
                populated: bool = False,
        ) -> ContentIntentBoundaryCohort:
            return ContentIntentBoundaryCohort(
                scope=scope,
                cohort_id=f"{scope}:safe-test",
                boundary_status=boundary_status if populated else "empty",
                observation_count=len(crfs) if populated else 0,
                source_count=len(crfs) if populated else 0,
                confidence=confidence if populated else "none",
                acceptable_count=len(crfs) if populated else 0,
                unacceptable_count=1 if populated and boundary_status == "conflicting" else 0,
                median_acceptable_bitrate_bps=2_000_000 if populated else None,
                acceptable_bitrate_mad=100_000 if populated else None,
                minimum_acceptable_bitrate_bps=1_800_000 if populated else None,
                maximum_acceptable_bitrate_bps=2_200_000 if populated else None,
                median_acceptable_crf=sorted(crfs)[len(crfs) // 2] if populated and crfs else None,
                acceptable_crf_mad=0.5 if populated and crfs else None,
                minimum_acceptable_crf=min(crfs) if populated and crfs else None,
                maximum_acceptable_crf=max(crfs) if populated and crfs else None,
                evidence_snapshot_id=(
                    evidence_snapshot_id or f"cics1_{scope}-test-snapshot"
                    if populated
                    else None
                ),
                actionable=populated and (scope == "item" or confidence in {"moderate", "high"}),
                reason="test",
            )

        return ContentIntentPersonalizationState(
            model_compatibility_id="model-test",
            intent_semantic_id="intent-test",
            compatibility_key="compatibility-test",
            item_boundary=empty_boundary,
            cohorts=(
                cohort("item", populated=scope == "item"),
                cohort("folder", populated=scope == "folder"),
                cohort("content_class", populated=scope == "content_class"),
                cohort("operator", populated=scope == "operator"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
