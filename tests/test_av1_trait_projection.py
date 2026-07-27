import json
import unittest

from mediaforce.tuning.av1_trait_projection import (
    av1_trait_selector_producer_blockers,
    project_av1_cold_start_fingerprint_summary,
    project_av1_cold_start_traits,
    serialize_av1_cold_start_trait_projection,
)


class AV1TraitProjectionTests(unittest.TestCase):
    def test_typical_identity_ignores_audio_only_review_advisory(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("audio_complexity", confidence=0.85, advisory=True)],
        })

        self.assertEqual(projection.cell_traits, ("typical",))
        self.assertEqual(projection.review_advisories, ("audio_complexity",))

    def test_low_confidence_animation_remains_review_only(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("animation_cues", confidence=0.69, advisory=True)],
        })

        self.assertEqual(projection.cell_traits, ("typical",))
        self.assertEqual(projection.review_advisories, ("animation_cues",))
        self.assertEqual(projection.promoted_findings, ())

    def test_high_confidence_animation_is_explicitly_promoted(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("animation_cues", confidence=0.70, advisory=True)],
        })

        self.assertEqual(projection.cell_traits, ("animation",))
        self.assertEqual(projection.promoted_findings, ("animation_cues",))

    def test_film_grain_promotion_respects_confidence_boundary(self) -> None:
        low = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("likely_film_grain", confidence=0.69, advisory=True)],
        })
        high = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("likely_film_grain", confidence=0.70, advisory=True)],
        })

        self.assertEqual(low.cell_traits, ("typical",))
        self.assertEqual(low.review_advisories, ("likely_film_grain",))
        self.assertEqual(high.cell_traits, ("grain_noise",))
        self.assertEqual(high.promoted_findings, ("likely_film_grain",))

    def test_low_confidence_direct_finding_keeps_advisories_from_becoming_typical(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [
                self._finding("dark_luma", confidence=0.64),
                self._finding("animation_cues", confidence=0.69, advisory=True),
            ],
        })

        self.assertEqual(projection.cell_traits, ("unknown",))
        self.assertEqual(projection.review_advisories, ("animation_cues", "dark_luma"))

    def test_multiple_stable_findings_add_mixed_identity(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [
                self._finding("dark_luma", confidence=0.75),
                self._finding("high_motion", confidence=0.80),
            ],
        })

        self.assertEqual(projection.cell_traits, ("darkness", "mixed", "motion"))

    def test_unknown_evidence_has_unknown_identity(self) -> None:
        projection = project_av1_cold_start_traits({"status": "unknown", "findings": []})

        self.assertEqual(projection.evidence_status, "unknown")
        self.assertEqual(projection.cell_traits, ("unknown",))

    def test_unrecognized_finding_does_not_become_typical(self) -> None:
        projection = project_av1_cold_start_traits({
            "status": "measured",
            "findings": [self._finding("future_video_trait", confidence=0.95)],
        })

        self.assertEqual(projection.cell_traits, ("unknown",))
        self.assertEqual(projection.unrecognized_finding_count, 1)

    def test_low_motion_dialogue_and_unknown_are_not_currently_reachable(self) -> None:
        low_motion = av1_trait_selector_producer_blockers(
            intent_level="balanced",
            required_traits=("low_motion_dialogue",),
            match="contains_all",
            expected_prediction="no_recommendation",
        )
        unknown = av1_trait_selector_producer_blockers(
            intent_level="balanced",
            required_traits=("unknown",),
            match="contains_all",
            expected_prediction="no_recommendation",
        )

        self.assertIn("low_motion_dialogue_analyzer_unavailable", low_motion)
        self.assertIn("unknown_evidence_rejected_by_request_contract", unknown)

    def test_projection_serialization_and_hash_are_deterministic(self) -> None:
        decision = {
            "status": "measured",
            "findings": [
                self._finding("high_texture", confidence=0.80),
                self._finding("audio_complexity", confidence=0.75, advisory=True),
            ],
        }

        first = project_av1_cold_start_traits(decision)
        second = project_av1_cold_start_traits(decision)

        self.assertEqual(first.projection_id, second.projection_id)
        self.assertEqual(serialize_av1_cold_start_trait_projection(first), serialize_av1_cold_start_trait_projection(second))
        self.assertEqual(json.loads(serialize_av1_cold_start_trait_projection(first)), first.to_payload())

    def test_retained_analysis_reclassifies_without_media_access(self) -> None:
        projection = project_av1_cold_start_fingerprint_summary({
            "schema_version": 1,
            "analysis": self._analysis(duplicate_like_frame_fraction=0.80, edge_density_p90=0.10),
        })

        self.assertEqual(projection.cell_traits, ("animation", "mixed", "texture_detail"))
        self.assertIn("animation_cues", projection.promoted_findings)

    @staticmethod
    def _finding(finding_id: str, *, confidence: float, advisory: bool = False) -> dict[str, object]:
        return {
            "id": finding_id,
            "confidence": confidence,
            "coverage": 1.0,
            "advisory": advisory,
            "rationale": "test",
        }

    @staticmethod
    def _analysis(**metrics: float) -> dict[str, object]:
        aggregate = {
            "dark_frame_fraction": 0.05,
            "gradient_frame_fraction": 0.02,
            "banding_risk_score": 0.02,
            "high_motion_frame_fraction": 0.04,
            "ydif_p90": 3.0,
            "high_texture_frame_fraction": 0.04,
            "edge_density_p90": 0.035,
            "duplicate_like_frame_fraction": 0.05,
            "temporal_noise_proxy": 0.5,
            "chroma_instability": 0.2,
            "smooth_temporal_noise_fraction": 0.02,
            "audio_loudness_range_lu_max": 0.0,
        }
        aggregate.update(metrics)
        return {
            "sampled_frames": 120,
            "coverage": 1.0,
            "aggregate": aggregate,
            "audio_probe": {"track_count": 0, "max_channels": 0},
            "ranges": [],
            "tool": {
                "name": "mediaforce.media_fingerprint",
                "version": "1",
                "ffmpeg_version": "ffmpeg test",
            },
        }


if __name__ == "__main__":
    unittest.main()
