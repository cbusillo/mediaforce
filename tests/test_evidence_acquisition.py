import unittest
from typing import Any

from mediaforce.core.evidence import stable_source_id
from mediaforce.encoding.fingerprint import DEFAULT_FINGERPRINT_MAX_FRAMES
from mediaforce.library.evidence_acquisition import (
    DEFAULT_UNCERTAINTY_FRONTIER_LIMIT,
    plan_fingerprint_acquisition,
    replay_representative_dimensions,
)
from mediaforce.library.representatives import select_representatives


class EvidenceAcquisitionTests(unittest.TestCase):
    def test_plan_starts_with_unmeasured_technical_representatives_deterministically(self) -> None:
        items = [
            self._item(1, measured=False),
            self._item(
                2,
                measured=False,
                video_codec="hevc",
                width=1280,
                height=720,
                cadence_class="interlaced_tff",
                channels=2,
            ),
            self._item(3, measured=False),
        ]
        technical_selection = select_representatives(
            items,
            prefix="tv/Example/Season 1",
            fingerprint_dimensions=(),
        )

        plan = plan_fingerprint_acquisition(items, prefix="tv/Example/Season 1")
        reversed_plan = plan_fingerprint_acquisition(list(reversed(items)), prefix="tv/Example/Season 1")

        self.assertEqual(plan, reversed_plan)
        self.assertEqual(plan.phase, "technical_representatives")
        self.assertEqual(
            plan.candidate_ids,
            tuple(item["source_id"] for item in technical_selection.payload["selected_items"]),
        )
        self.assertEqual(plan.stop_reason, "technical_representatives_pending")
        self.assertEqual(plan.coverage.technical_representative_coverage, 0.0)
        self.assertEqual(plan.intentionally_unqueued_count, len(items) - len(plan.candidate_ids))
        self.assertEqual(
            plan.estimated_analysis_cost.shared_visual_pass.estimated_frame_count,
            len(plan.candidate_ids) * DEFAULT_FINGERPRINT_MAX_FRAMES,
        )

    def test_plan_caps_the_uncertainty_frontier_and_stops_at_representative_coverage(self) -> None:
        items = [
            self._item(1, media_traits=["dark_luma"]),
            self._item(2, media_traits=["dark_luma"]),
            self._item(3, media_traits=["high_motion"]),
            self._item(4, media_traits=["typical"]),
            self._item(5, media_traits=["typical"]),
            self._item(6, media_traits=["dark_gradient_banding_risk"]),
        ]
        technical_selection = select_representatives(
            items,
            prefix="tv/Example/Season 1",
            fingerprint_dimensions=(),
        )
        technical_ids = tuple(item["source_id"] for item in technical_selection.payload["selected_items"])

        plan = plan_fingerprint_acquisition(
            items,
            prefix="tv/Example/Season 1",
            measured_source_ids=technical_ids,
            max_uncertainty_frontier=1,
        )

        self.assertEqual(plan.phase, "uncertainty_frontier")
        self.assertEqual(plan.candidate_ids, (stable_source_id(items[1]),))
        self.assertEqual(plan.stop_reason, "uncertainty_frontier_capped")
        self.assertEqual(
            plan.intentionally_unqueued_count,
            len(items) - len(technical_ids) - len(plan.candidate_ids),
        )

        complete_plan = plan_fingerprint_acquisition(
            items,
            prefix="tv/Example/Season 1",
            measured_source_ids=(*technical_ids, stable_source_id(items[1])),
        )

        self.assertEqual(complete_plan.phase, "complete")
        self.assertEqual(complete_plan.candidate_ids, ())
        self.assertEqual(complete_plan.stop_reason, "representative_coverage_satisfied")
        self.assertEqual(complete_plan.intentionally_unqueued_count, len(items) - 2)
        self.assertLess(complete_plan.coverage.global_fingerprint_coverage, 1.0)

    def test_replay_reports_dimension_benefit_and_separate_audio_cost(self) -> None:
        items = [
            self._item(1, media_traits=["typical"]),
            self._item(2, media_traits=["dark_luma", "dark_gradient_banding_risk"]),
            self._item(3, media_traits=["audio_complexity"]),
        ]

        report = replay_representative_dimensions(
            items,
            prefix="tv/Example/Season 1",
            measured_source_ids=(),
        )
        audio_omission = next(
            replay
            for replay in report.leave_one_dimension_out
            if replay.omitted_fingerprint_dimension == "audio_complexity"
        )

        self.assertGreater(
            report.all_dimensions.sample_set_growth.selected_count,
            report.technical_only.sample_set_growth.selected_count,
        )
        self.assertTrue(report.all_dimensions.representative_changes.changed)
        self.assertLess(
            audio_omission.hard_case_recall.recalled_hard_case_count,
            report.all_dimensions.hard_case_recall.recalled_hard_case_count,
        )
        self.assertEqual(
            report.shared_visual_pass_cost,
            report.all_dimensions.estimated_analysis_cost.shared_visual_pass,
        )
        self.assertEqual(
            report.shared_visual_pass_cost.estimated_frame_count,
            report.shared_visual_pass_cost.candidate_count * DEFAULT_FINGERPRINT_MAX_FRAMES,
        )
        self.assertEqual(report.audio_complexity_cost.recommendation, "retain")
        self.assertGreater(report.audio_complexity_cost.estimated_sample_seconds, 0.0)

    def test_plan_rejects_non_positive_frontier_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_uncertainty_frontier must be greater than zero"):
            plan_fingerprint_acquisition([self._item(1)], max_uncertainty_frontier=0)
        self.assertGreater(DEFAULT_UNCERTAINTY_FRONTIER_LIMIT, 0)

    @staticmethod
    def _item(
            index: int,
            *,
            measured: bool = True,
            video_codec: str = "h264",
            width: int = 1920,
            height: int = 1080,
            cadence_class: str = "progressive",
            channels: int = 6,
            media_traits: list[str] | None = None,
    ) -> dict[str, Any]:
        rel_path = f"tv/Example/Season 1/Episode {index:02d}.mkv"
        traits = media_traits or ["typical"]
        return {
            "library_item_id": index,
            "rel_path": rel_path,
            "source_fingerprint": f"fingerprint-{index}",
            "source_size_bytes": 1_000_000_000,
            "video_codec": video_codec,
            "width": width,
            "height": height,
            "cadence_class": cadence_class,
            "duration_seconds": 2700,
            "audio_summary": [{"codec_name": "eac3", "channels": channels}],
            "media_fingerprint_decision": {
                "status": "measured" if measured else "unknown",
                "traits": traits,
                "findings": [
                    {"id": trait, "confidence": 0.9}
                    for trait in traits
                    if trait != "typical"
                ],
            },
        }


if __name__ == "__main__":
    unittest.main()
