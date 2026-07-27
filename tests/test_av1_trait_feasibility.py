from datetime import UTC, datetime
import json
from pathlib import Path
import unittest

from sqlalchemy import create_engine, insert, select

from mediaforce.core.db_tables import library_item_evidence_state, library_items, metadata
from mediaforce.tuning.av1_cold_start_evaluation import load_av1_cold_start_validation_manifest
from mediaforce.tuning.av1_trait_feasibility import (
    _cell_feasibility,
    load_av1_trait_feasibility_report,
    serialize_av1_trait_feasibility_report,
)


MANIFEST_PATH = Path("docs/validation/av1-cold-start-preregistration-v1.json")


class AV1TraitFeasibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.manifest = load_av1_cold_start_validation_manifest(MANIFEST_PATH)
        self.as_of = "2026-07-27T18:00:00Z"

    def tearDown(self) -> None:
        self.connection.close()
        self.engine.dispose()

    def test_report_is_deterministic_read_only_and_privacy_safe(self) -> None:
        self._insert_item(1, "/private/media/Secret Show/Episode 01.mkv")
        before = self.connection.execute(select(library_items.c.id)).all()

        first = load_av1_trait_feasibility_report(
            self.connection,
            manifest=self.manifest,
            as_of=self.as_of,
        )
        second = load_av1_trait_feasibility_report(
            self.connection,
            manifest=self.manifest,
            as_of=self.as_of,
        )
        after = self.connection.execute(select(library_items.c.id)).all()

        self.assertEqual(first.report_id, second.report_id)
        self.assertEqual(serialize_av1_trait_feasibility_report(first), serialize_av1_trait_feasibility_report(second))
        self.assertEqual(before, after)
        self.assertTrue(first.observation_store_available)
        payload_text = json.dumps(first.to_payload(), sort_keys=True)
        for private_value in ("Secret Show", "Episode 01", "/private/media", "source-1", "fingerprint-1"):
            self.assertNotIn(private_value, payload_text)

    def test_v1_unreachable_cells_are_reported_instead_of_accepted(self) -> None:
        report = load_av1_trait_feasibility_report(
            self.connection,
            manifest=self.manifest,
            as_of=self.as_of,
        )
        cells = {cell.name: cell for cell in report.cells}

        self.assertEqual(cells["low_motion_dialogue_risk_fallback"].status, "unreachable")
        self.assertIn(
            "low_motion_dialogue_analyzer_unavailable",
            cells["low_motion_dialogue_risk_fallback"].producer_blockers,
        )
        self.assertEqual(cells["unknown_risk_fallback"].status, "unreachable")
        self.assertIn(
            "unknown_evidence_rejected_by_request_contract",
            cells["unknown_risk_fallback"].producer_blockers,
        )
        self.assertFalse(report.ready)
        self.assertFalse(report.to_payload()["execution_authorized"])
        self.assertTrue(report.to_payload()["runtime_integration_required"])

    def test_missing_observation_store_is_reported_without_mutation(self) -> None:
        self.connection.exec_driver_sql("DROP TABLE content_intent_boundary_observations")

        report = load_av1_trait_feasibility_report(
            self.connection,
            manifest=self.manifest,
            as_of=self.as_of,
        )

        self.assertFalse(report.observation_store_available)
        self.assertFalse(report.ready)
        publication_cells = [cell for cell in report.cells if cell.mode == "publication_candidate"]
        self.assertTrue(publication_cells)
        self.assertTrue(all(cell.status == "insufficient_inventory" for cell in publication_cells))

    def test_publication_cell_requires_coherent_fresh_derivation_evidence(self) -> None:
        plan = next(cell for cell in self.manifest.cell_plans if cell.name == "typical_live_action_balanced_candidate")
        projected_items = {
            item_id: {
                "traits": ("typical",),
                "parent_scope": f"parent-{item_id}",
                "content_version": f"content-{item_id}",
            }
            for item_id in range(1, 17)
        }
        observations = [
            {
                "library_item_id": item_id,
                "content_fingerprint": f"content-{item_id}",
                "intent_level": "balanced",
                "intent_semantic_id": "cis1_balanced",
                "compatibility_key": "compatibility-a",
                "policy_hash": "policy-a",
                "disposition": "active",
                "personalization_eligible": 1,
                "recorded_at": "2026-07-01T12:00:00+00:00",
                "source_id": f"source-{(item_id - 1) % 6}",
                "series_id": f"series-{item_id}",
                "boundary_group_id": f"group-{(item_id - 1) % 6}",
                "boundary_bitrate_bps": 1_500_000 + item_id,
            }
            for item_id in range(1, 13)
        ]

        cell = _cell_feasibility(
            plan,
            projected_items=projected_items,
            observations=observations,
            as_of=datetime(2026, 7, 27, 18, 0, tzinfo=UTC),
            minimum_derivation_count=self.manifest.criteria.minimum_derivation_evidence_count,
            minimum_derivation_sources=self.manifest.criteria.minimum_derivation_source_count,
        )

        self.assertEqual(cell.status, "ready_for_private_mapping")
        self.assertEqual(cell.eligible_observation_count, 12)
        self.assertEqual(cell.independent_source_count, 6)
        self.assertEqual(cell.independent_series_count, 12)
        self.assertEqual(cell.independent_source_group_count, 6)

    def _insert_item(self, item_id: int, source_path: str) -> None:
        timestamp = "2026-07-27T17:00:00Z"
        summary = {
            "schema_version": 1,
            "analysis": {
                "sampled_frames": 120,
                "coverage": 1.0,
                "aggregate": {
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
                },
                "audio_probe": {"track_count": 0, "max_channels": 0},
                "ranges": [],
                "tool": {
                    "name": "mediaforce.media_fingerprint",
                    "version": "1",
                    "ffmpeg_version": "ffmpeg test",
                },
            },
        }
        self.connection.execute(insert(library_items).values(
            id=item_id,
            source_path=source_path,
            rel_path="tv/private/episode.mkv",
            media_root="tv",
            parent_dir="private",
            file_name="episode.mkv",
            container="matroska",
            size_bytes=1_000_000,
            mtime_ns=1,
            fingerprint=f"fingerprint-{item_id}",
            audio_track_count=1,
            subtitle_track_count=0,
            english_audio_count=1,
            english_subtitle_count=0,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            media_fingerprint_json=json.dumps(summary),
            content_version_fingerprint=f"content-{item_id}",
            status="discovered",
            priority_score=0,
            last_scan_id="scan-1",
            discovered_at=timestamp,
            last_seen_at=timestamp,
            updated_at=timestamp,
        ))
        self.connection.execute(insert(library_item_evidence_state).values(
            library_item_id=item_id,
            evidence_kind="media_fingerprint",
            state="current",
            policy_hash="policy-test",
            decision_status="measured",
            updated_at=timestamp,
        ))


if __name__ == "__main__":
    unittest.main()
