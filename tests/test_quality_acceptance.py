import json
import unittest
from datetime import UTC, datetime

from mediaforce.tuning.quality_acceptance import quality_acceptance_report
from mediaforce.tuning.quality_shadow import QualityShadowRecommendation, build_quality_shadow_payload


class QualityAcceptanceReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)
        self.signature_id = "qms1_acceptance"
        self.policy_hash = "policy-acceptance"

    def test_report_uses_stable_scope_signature_policy_key(self) -> None:
        rows = [
            self._row(index=0, arm="baseline_holdout", cohort_id="qsc1_old"),
            self._row(index=1, arm="baseline_holdout", cohort_id="qsc1_new"),
        ]

        report = quality_acceptance_report(rows, library_types={"tv": "tv"})

        self.assertEqual(len(report.groups), 1)
        group = report.groups[0]
        self.assertEqual(group.key.scope_kind, "season")
        self.assertEqual(group.key.scope_prefix, "tv/Show/Season 01")
        self.assertEqual(group.key.search_signature_id, self.signature_id)
        self.assertEqual(group.key.policy_hash, self.policy_hash)
        self.assertEqual(group.distinct_items, 2)

    def test_nested_item_evidence_does_not_enter_season_group(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]
        rows.append(
            self._row(
                index=999,
                arm="baseline_holdout",
                scope="item",
                scope_prefix="tv/Show/Season 01/Episode 999.mkv",
            )
        )

        report = quality_acceptance_report(rows, library_types={"tv": "tv"})
        groups = {group.key.scope_kind: group for group in report.groups}

        self.assertEqual(groups["season"].active_observed.holdout_runs, 10)
        self.assertEqual(groups["item"].active_observed.holdout_runs, 1)

    def test_concurrent_holdout_evidence_can_satisfy_closure_contract(self) -> None:
        rows = [
            self._row(
                index=index,
                arm="baseline_holdout",
                candidate_count=5,
                search_seconds=100.0,
                workflow_seconds=120.0,
            )
            for index in range(10)
        ]
        rows.extend(
            self._row(
                index=100 + index,
                arm="warm_start",
                candidate_count=2,
                search_seconds=40.0,
                workflow_seconds=80.0,
            )
            for index in range(20)
        )

        report = quality_acceptance_report(rows, library_types={"tv": "tv"})

        group = report.groups[0]
        active = group.active_observed
        self.assertTrue(group.passive_readiness.active_eligible)
        self.assertEqual(active.attempted_warm_runs, 20)
        self.assertEqual(active.accepted_warm_runs, 20)
        self.assertEqual(active.holdout_runs, 10)
        self.assertEqual(active.complete_warm_distinct_items, 20)
        self.assertEqual(active.complete_holdout_distinct_items, 10)
        self.assertEqual(active.observed_candidate_savings_rate, 0.6)
        self.assertEqual(active.observed_search_time_savings_rate, 0.6)
        self.assertEqual(active.observed_workflow_time_savings_rate, 0.333333)
        self.assertTrue(group.closure_eligible)
        self.assertEqual(group.closure_blocking_reasons, ())

    def test_active_miss_blocks_closure_while_legacy_failure_is_unattributed(self) -> None:
        rows = [
            self._row(index=index, arm="baseline_holdout")
            for index in range(10)
        ]
        rows.extend(
            self._row(index=100 + index, arm="warm_start", final_size_miss=index == 0)
            for index in range(20)
        )
        rows.append(
            {
                "source_rel_path": "tv/Show/Season 01/Legacy.mkv",
                "search_signature_id": None,
                "policy_hash": self.policy_hash,
                "outcome_kind": "final_size_failure",
            }
        )

        report = quality_acceptance_report(rows, library_types={"tv": "tv"})

        group = report.groups[0]
        self.assertEqual(group.safety_and_data_quality.active_final_size_misses, 1)
        self.assertFalse(group.closure_eligible)
        self.assertIn("active_final_size_miss", group.closure_blocking_reasons)
        self.assertEqual(report.unattributed_final_size_failures, 1)

    def test_terminal_warm_failure_is_reported_without_entering_medians(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]
        rows.extend(
            self._row(index=100 + index, arm="warm_start", candidate_count=2, search_seconds=40.0)
            for index in range(20)
        )
        rows.append(
            self._row(
                index=999,
                arm="warm_start",
                outcome_kind="deterministic_search_failure",
                candidate_count=1,
                search_seconds=1.0,
                workflow_seconds=1.0,
            )
        )

        group = quality_acceptance_report(rows, library_types={"tv": "tv"}).groups[0]

        self.assertEqual(group.active_observed.failed_warm_runs, 1)
        self.assertEqual(group.active_observed.median_warm_candidate_count, 2.0)
        self.assertIn("warm_terminal_failure", group.closure_blocking_reasons)

    def test_legacy_active_and_signature_mismatch_rows_block_closure(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]
        rows.extend(self._row(index=100 + index, arm="warm_start") for index in range(20))
        legacy = self._row(index=500, arm="warm_start")
        legacy_shadow = json.loads(str(legacy["shadow_json"]))
        legacy_shadow["warm_start"].pop("experiment_version", None)
        legacy["shadow_json"] = json.dumps(legacy_shadow)
        legacy_outcome = json.loads(str(legacy["outcome_json"]))
        legacy_outcome.pop("quality_memory_experiment_version", None)
        legacy["outcome_json"] = json.dumps(legacy_outcome)
        rows.append(legacy)
        rows.append(self._row(index=501, arm="warm_start", signature_match=False))

        group = quality_acceptance_report(rows, library_types={"tv": "tv"}).groups[0]

        self.assertEqual(group.active_observed.legacy_active_runs, 1)
        self.assertEqual(group.active_observed.warm_signature_mismatch_runs, 1)
        self.assertEqual(group.active_observed.holdout_signature_mismatch_runs, 0)
        self.assertIn("legacy_active_evidence", group.closure_blocking_reasons)
        self.assertIn("warm_signature_mismatch", group.closure_blocking_reasons)

    def test_holdout_signature_mismatch_only_reduces_control_sample(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]
        rows.append(self._row(index=500, arm="baseline_holdout", signature_match=False))
        rows.extend(self._row(index=100 + index, arm="warm_start") for index in range(20))

        group = quality_acceptance_report(rows, library_types={"tv": "tv"}).groups[0]

        self.assertEqual(group.active_observed.holdout_signature_mismatch_runs, 1)
        self.assertEqual(group.active_observed.warm_signature_mismatch_runs, 0)
        self.assertNotIn("warm_signature_mismatch", group.closure_blocking_reasons)

    def test_prefix_filter_keeps_complete_group_evidence(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]

        report = quality_acceptance_report(
            rows,
            library_types={"tv": "tv"},
            prefix="tv/Show/Season 01/Episode 000.mkv",
        )

        self.assertEqual(report.requested_prefix, "tv/Show/Season 01/Episode 000.mkv")
        self.assertEqual(len(report.groups), 1)
        self.assertEqual(report.groups[0].distinct_items, 10)

    def test_incomplete_metrics_do_not_satisfy_holdout_minimum(self) -> None:
        rows = [
            self._row(
                index=index,
                arm="baseline_holdout",
                workflow_seconds=None if index == 0 else 120.0,
            )
            for index in range(10)
        ]
        rows.extend(self._row(index=100 + index, arm="warm_start") for index in range(20))

        group = quality_acceptance_report(rows, library_types={"tv": "tv"}).groups[0]

        self.assertEqual(group.active_observed.complete_holdout_metric_runs, 9)
        self.assertIn("insufficient_holdout_runs", group.closure_blocking_reasons)

    def test_correction_revision_replaces_original_run(self) -> None:
        original = self._row(index=0, arm="baseline_holdout", candidate_count=9)
        correction = self._row(index=1, arm="baseline_holdout", candidate_count=5)
        correction["search_run_id"] = original["search_run_id"]
        correction["authority"] = "correction"
        correction["revision"] = 1

        group = quality_acceptance_report(
            [original, correction],
            library_types={"tv": "tv"},
        ).groups[0]

        self.assertEqual(group.active_observed.holdout_runs, 1)
        self.assertEqual(group.active_observed.median_holdout_candidate_count, 5.0)

    def test_terminal_warm_encode_event_blocks_closure(self) -> None:
        rows = [self._row(index=index, arm="baseline_holdout") for index in range(10)]
        rows.extend(self._row(index=100 + index, arm="warm_start") for index in range(20))
        event = {
            "event_type": "encoding_failed",
            "details_json": json.dumps(
                {
                    "quality_memory_plan": {
                        "experiment_version": "qwa1",
                        "experiment_arm": "warm_start",
                        "scope": "season",
                        "scope_prefix": "tv/Show/Season 01",
                        "search_signature_id": self.signature_id,
                    },
                    "quality_memory_policy_hash": self.policy_hash,
                }
            ),
        }

        group = quality_acceptance_report(
            rows,
            terminal_events=[event],
            library_types={"tv": "tv"},
        ).groups[0]

        self.assertEqual(group.active_observed.terminal_event_failures, 1)
        self.assertIn("warm_terminal_event_failure", group.closure_blocking_reasons)

    def _row(
            self,
            *,
            index: int,
            arm: str,
            cohort_id: str = "qsc1_acceptance",
            candidate_count: int = 5,
            search_seconds: float = 100.0,
            workflow_seconds: float | None = 120.0,
            final_size_miss: bool = False,
            outcome_kind: str = "selected",
            signature_match: bool = True,
            final_signature_id: str | None = None,
            scope: str = "season",
            scope_prefix: str = "tv/Show/Season 01",
    ) -> dict[str, object]:
        recommendation = QualityShadowRecommendation(
            search_signature_id=self.signature_id,
            evidence_cutoff_at=self.as_of.isoformat(),
            first_crf=42.0,
            scope=scope,
            confidence="high",
            sample_count=10,
            cohort_id=cohort_id,
            minimum_crf=42.0,
            maximum_crf=42.0,
            iqr=0.0,
            median_absolute_deviation=0.0,
            evidence_observation_ids=tuple(f"qso1_evidence_{evidence_index}" for evidence_index in range(10)),
            fallback_reason=None,
            reason="test",
            exclusions=(),
        )
        execution = None
        if arm == "warm_start":
            execution = {
                "status": "fallback" if final_size_miss else "accepted",
                "attempted": True,
                "fallback_used": final_size_miss,
                "fallback_reason": "final_size_miss" if final_size_miss else None,
                "candidate_count": candidate_count,
                "total_candidate_count": candidate_count,
                "duration_seconds": search_seconds,
                "signature_match": signature_match,
            }
        shadow = build_quality_shadow_payload(
            recommendation,
            selected_crf=42.0,
            selected_score=85.0,
            minimum_quality_score=80.0,
            candidate_count=candidate_count,
            search_duration_seconds=search_seconds,
            actual_output_bytes=300_000_000,
            size_target_bytes=300_000_000,
            warm_start_plan={
                "eligible": True,
                "experiment_version": "qwa1",
                "experiment_arm": arm,
                "holdout_percent": 20,
                "scope": scope,
                "scope_prefix": scope_prefix,
                "search_signature_id": self.signature_id,
                "requested_crf": 42.0,
                "candidate_crf": 42,
                "baseline_median_candidate_count": 5.0,
                "baseline_median_search_seconds": 100.0,
            },
            warm_start_trace=execution,
            final_size_miss=final_size_miss,
        )
        return {
            "observation_id": f"qso1_{index}",
            "search_run_id": f"qsr1_{index}",
            "authority": "runtime_native",
            "revision": 0,
            "source_rel_path": f"tv/Show/Season 01/Episode {index:03d}.mkv",
            "search_signature_id": (
                final_signature_id
                or (self.signature_id if signature_match else "qms1_final_mismatch")
            ),
            "policy_hash": self.policy_hash,
            "outcome_kind": outcome_kind,
            "candidate_count": candidate_count,
            "search_duration_seconds": search_seconds,
            "outcome_json": json.dumps(
                {
                    "quality_memory_arm": arm,
                    "quality_memory_experiment_version": "qwa1",
                    "quality_memory_scope": scope,
                    "quality_memory_scope_prefix": scope_prefix,
                    "quality_memory_search_signature_id": self.signature_id,
                    "quality_memory_attempted": arm == "warm_start",
                }
            ),
            "timing_json": json.dumps({"workflow_duration_seconds": workflow_seconds}),
            "candidate_trace_json": json.dumps(
                {
                    "warm_start": {
                        **(execution or {}),
                        "signature_match": signature_match,
                    }
                }
            ),
            "shadow_json": json.dumps(shadow),
        }


if __name__ == "__main__":
    unittest.main()
