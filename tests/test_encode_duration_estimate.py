from datetime import UTC, datetime, timedelta
from unittest import TestCase

from mediaforce.encoding.duration_estimate import EncodeDurationEstimate, EncodeDurationSample, \
    estimate_encode_duration, estimate_encode_job_duration, estimate_fits_before_schedule_close


class EncodeDurationEstimateTests(TestCase):
    def test_sparse_host_history_is_more_conservative_than_well_sampled_history(self) -> None:
        item = _item(duration_seconds=2_700.0)
        known_host = {"key": "known"}
        new_host = {"key": "new"}
        samples = tuple(_sample("known", ratio=1.0, overhead=300.0) for _ in range(12))

        known = estimate_encode_duration(item, known_host, samples)
        sparse = estimate_encode_duration(item, new_host, samples)

        self.assertEqual(known.confidence, "high")
        self.assertEqual(sparse.confidence, "limited")
        self.assertGreater(sparse.total_seconds, known.total_seconds)

    def test_percentile_estimate_resists_one_extreme_outlier(self) -> None:
        item = _item(duration_seconds=2_700.0)
        host = {"key": "worker"}
        baseline_samples = tuple(_sample("worker", ratio=1.0, overhead=300.0) for _ in range(20))
        outlier_samples = (*baseline_samples, _sample("worker", ratio=20.0, overhead=21_000.0))

        baseline = estimate_encode_duration(item, host, baseline_samples)
        with_outlier = estimate_encode_duration(item, host, outlier_samples)

        self.assertLess(with_outlier.total_seconds, baseline.total_seconds * 1.10)

    def test_thorough_quality_search_increases_fallback_overhead(self) -> None:
        host = {"key": "new"}

        regular = estimate_encode_duration(_item(duration_seconds=2_700.0, thorough=False), host, ())
        thorough = estimate_encode_duration(_item(duration_seconds=2_700.0, thorough=True), host, ())

        self.assertGreater(thorough.quality_search_seconds, regular.quality_search_seconds)
        self.assertGreater(thorough.total_seconds, regular.total_seconds)

    def test_host_specific_history_keeps_fast_and_slow_hosts_distinct(self) -> None:
        item = _item(duration_seconds=2_700.0)
        samples = tuple(
            [*(_sample("fast", ratio=0.7, overhead=180.0) for _ in range(10)),
             *(_sample("slow", ratio=2.5, overhead=900.0) for _ in range(10))]
        )

        fast = estimate_encode_duration(item, {"key": "fast"}, samples)
        slow = estimate_encode_duration(item, {"key": "slow"}, samples)

        self.assertLess(fast.total_seconds, slow.total_seconds)

    def test_job_estimate_sums_atomic_episode_estimates(self) -> None:
        host = {"key": "worker"}
        samples = tuple(_sample("worker", ratio=1.0, overhead=300.0) for _ in range(10))
        first_item = _item(duration_seconds=1_200.0)
        second_item = _item(duration_seconds=2_400.0)

        first = estimate_encode_duration(first_item, host, samples)
        second = estimate_encode_duration(second_item, host, samples)
        combined = estimate_encode_job_duration([first_item, second_item], host, samples)

        self.assertEqual(combined.total_seconds, first.total_seconds + second.total_seconds)
        self.assertEqual(combined.encode_seconds, first.encode_seconds + second.encode_seconds)
        self.assertEqual(combined.quality_search_seconds, first.quality_search_seconds + second.quality_search_seconds)

    def test_schedule_close_admission_uses_exact_utc_deadline(self) -> None:
        now = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
        estimate = EncodeDurationEstimate(
            total_seconds=900,
            encode_seconds=600,
            quality_search_seconds=300,
            source_duration_seconds=1_200.0,
            host_sample_size=10,
            confidence="high",
        )

        self.assertTrue(
            estimate_fits_before_schedule_close(
                estimate,
                {"schedule_closes_at": (now + timedelta(seconds=900)).isoformat()},
                now=now,
            )
        )
        self.assertFalse(
            estimate_fits_before_schedule_close(
                estimate,
                {"schedule_closes_at": (now + timedelta(seconds=899)).isoformat()},
                now=now,
            )
        )
        self.assertTrue(estimate_fits_before_schedule_close(estimate, {}, now=now))


def _item(*, duration_seconds: float, thorough: bool = True) -> dict[str, object]:
    return {
        "duration_seconds": duration_seconds,
        "resolved_policy": {
            "video": {
                "sample_every": "8m",
                "sample_duration": "20s",
                "thorough": thorough,
            }
        },
    }


def _sample(host_key: str, *, ratio: float, overhead: float) -> EncodeDurationSample:
    return EncodeDurationSample(
        host_key=host_key,
        host_label=host_key,
        encode_realtime_factor=ratio,
        quality_overhead_seconds=overhead,
    )
