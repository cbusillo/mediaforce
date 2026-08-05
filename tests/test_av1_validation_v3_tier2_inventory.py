from dataclasses import fields
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db_tables import (
    library_item_evidence_state,
    library_items,
    metadata,
)
from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.encoding.fingerprint import (
    MEDIA_FINGERPRINT_EVIDENCE_KIND,
    MEDIA_FINGERPRINT_SCHEMA_VERSION,
    MEDIA_FINGERPRINT_TOOL_NAME,
    MEDIA_FINGERPRINT_TOOL_VERSION,
)
from mediaforce.tuning.av1_validation_v3 import (
    AV1_VALIDATION_V3_EXPERIMENT_ID,
    AV1_VALIDATION_V3_PROTOCOL_VERSION,
    av1_validation_v3_qualification_key_id,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
    AV1ValidationV3Tier2Inventory,
    AV1ValidationV3Tier2InventoryError,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_selection import (
    assert_av1_validation_v3_tier2_selection_record,
    build_av1_validation_v3_tier2_selection_record,
    validate_av1_validation_v3_tier2_selection_record_sources,
)


V3_PROTOCOL_PATH = Path("docs/validation/av1-cold-start-preregistration-v3.json")
SHA256 = f"sha256:{'a' * 64}"
COMMIT = "1" * 40
TREE = "2" * 40
FROZEN_AT = "2026-08-03T12:00:00Z"
VALID_UNTIL = "2026-08-04T12:00:00Z"
SELECTED_AT = "2026-08-03T13:00:00Z"


class AV1ValidationV3Tier2InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.config = _config(self.root)
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        metadata.create_all(self.engine)
        self.connection = self.engine.connect()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.connection.close)

    def test_happy_path_projects_animation_and_typical_candidates(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        inventory = self._inventory()

        self.assertEqual(inventory.measured_row_count, 2)
        self.assertEqual(len(inventory.entries), 2)
        self.assertEqual(
            {source.exact_traits for source in inventory.candidate_sources},
            {("animation",), ("typical",)},
        )
        self.assertEqual(
            [count.private_candidate_count for count in inventory.frozen_stratum_private_counts],
            [1, 1],
        )
        self.assertTrue(
            all(source.pipeline_ready for source in inventory.candidate_sources)
        )

    def test_deterministic_and_read_only(self) -> None:
        _insert_item(self.connection, 1, "typical", "0" * 39 + "1")
        before = self.connection.exec_driver_sql(
            "select count(*) from library_items"
        ).scalar_one()

        first = self._inventory()
        second = self._inventory()
        after = self.connection.exec_driver_sql(
            "select count(*) from library_items"
        ).scalar_one()

        self.assertEqual(first, second)
        self.assertEqual(before, after)

    def test_source_token_is_independent_of_path_and_uses_only_identity(self) -> None:
        identity = "0" * 39 + "1"
        _insert_item(
            self.connection,
            1,
            "typical",
            identity,
            rel_path="tv/First/episode.mkv",
        )
        original = self._inventory().candidate_sources[0].source_fingerprint
        self.connection.execute(library_items.delete())
        self.connection.execute(library_item_evidence_state.delete())
        _insert_item(
            self.connection,
            2,
            "typical",
            identity,
            rel_path="movies/Private Title (2026).mkv",
        )
        changed_path = self._inventory().candidate_sources[0].source_fingerprint

        self.assertEqual(original, changed_path)
        self.assertEqual(original, _expected_source_fingerprint(identity))

    def test_inventory_entries_store_no_path_or_title_fields(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        entry = self._inventory().entries[0]
        self.assertEqual(
            {field.name for field in fields(entry)},
            {
                "local_item_id",
                "source_identity",
                "evidence_summary_sha256",
                "qualification_source",
            },
        )
        self.assertNotIn("path", repr(entry).lower())
        self.assertNotIn("title", repr(entry).lower())
        self.assertNotIn("series", repr(entry).lower())
        self.assertNotIn("group", repr(entry).lower())

    def test_powered_mixed_and_unknown_rows_are_excluded(self) -> None:
        _insert_item(self.connection, 1, "darkness", "0" * 39 + "1")
        _insert_item(self.connection, 2, "motion", "0" * 39 + "2")
        _insert_item(self.connection, 3, "mixed", "0" * 39 + "3")
        _insert_item(self.connection, 4, "unknown", "0" * 39 + "4")

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 0)
        self.assertEqual(inventory.powered_candidate_cell_overlap_count, 2)
        self.assertEqual(inventory.ambiguous_trait_count, 2)

    def test_non_balanced_and_unconfirmed_policies_are_excluded(self) -> None:
        _insert_item(
            self.connection,
            1,
            "typical",
            "0" * 39 + "1",
            rel_path="tv/Transparent/S01E01.mkv",
        )
        _override_video(
            self.config,
            "tv/Transparent/S01E01.mkv",
            {"compression_intent": "transparent"},
        )
        _insert_item(
            self.connection,
            2,
            "typical",
            "0" * 39 + "2",
            rel_path="tv/Unconfirmed/S01E01.mkv",
        )
        _override_video(
            self.config,
            "tv/Unconfirmed/S01E01.mkv",
            {"compression_intent_confirmed": False},
        )
        _insert_item(
            self.connection,
            3,
            "typical",
            "0" * 39 + "3",
            rel_path="tv/Legacy/S01E01.mkv",
        )
        _override_video(
            self.config,
            "tv/Legacy/S01E01.mkv",
            {"compression_intent_schema_version": 0},
        )

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 0)
        self.assertEqual(inventory.non_balanced_intent_count, 1)
        self.assertEqual(inventory.unconfirmed_intent_count, 2)

    def test_infeasible_stream_budget_is_excluded(self) -> None:
        _insert_item(
            self.connection,
            1,
            "typical",
            "0" * 39 + "1",
            rel_path="tv/Infeasible/S01E01.mkv",
        )
        _override_video(
            self.config,
            "tv/Infeasible/S01E01.mkv",
            {"target_size_bytes": 1_000_000},
        )

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 0)
        self.assertEqual(inventory.infeasible_stream_budget_count, 1)

    def test_duplicate_identity_drops_all_rows_in_group(self) -> None:
        identity = "0" * 39 + "1"
        _insert_item(self.connection, 1, "animation", identity)
        _insert_item(self.connection, 2, "typical", identity)

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 0)
        self.assertEqual(inventory.duplicate_source_identity_row_count, 2)

    def test_malformed_identity_is_rejected_and_counted(self) -> None:
        _insert_item(self.connection, 1, "animation", "not-a-git-sha")

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 0)
        self.assertEqual(inventory.malformed_identity_count, 1)

    def test_derived_fingerprint_collision_raises(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")

        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier2_inventory._source_fingerprint",
                return_value=f"sha256:{'f' * 64}",
            ),
            self.assertRaisesRegex(
                AV1ValidationV3Tier2InventoryError,
                "collision",
            ),
        ):
            self._inventory()

    def test_empty_stratum_returns_count_zero(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")

        inventory = self._inventory()

        counts = {
            count.stratum_name: count.private_candidate_count
            for count in inventory.frozen_stratum_private_counts
        }
        self.assertEqual(counts["animation_balanced_qualification"], 1)
        self.assertEqual(counts["typical_balanced_qualification"], 0)

    def test_evidence_cohort_incompatibility_is_excluded(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(
            self.connection,
            2,
            "typical",
            "0" * 39 + "2",
            analyzer_policy_digest="sha256:minority",
        )

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 1)
        self.assertEqual(inventory.incompatible_evidence_count, 1)
        self.assertEqual(
            inventory.candidate_sources[0].exact_traits,
            ("animation",),
        )

    def test_candidate_sources_feed_selection_record_and_validation(self) -> None:
        _insert_item(self.connection, 1, "animation", "0" * 39 + "1")
        _insert_item(self.connection, 2, "typical", "0" * 39 + "2")
        inventory = self._inventory()
        key = b"q" * 32
        plan = build_av1_validation_v3_qualification_plan(
            protocol=self.protocol,
            qualification_key_id=av1_validation_v3_qualification_key_id(key),
            eligibility_predicate_sha256=SHA256,
            repository_commit=COMMIT,
            repository_tree=TREE,
            config_sha256=SHA256,
            toolchain_sha256=SHA256,
            fixture_matrix_sha256=SHA256,
            frozen_at=FROZEN_AT,
            valid_until=VALID_UNTIL,
        )

        record = build_av1_validation_v3_tier2_selection_record(
            protocol=self.protocol,
            plan=plan,
            sources=inventory.candidate_sources,
            qualification_key=key,
            selected_at=SELECTED_AT,
        )

        self.assertEqual(record.candidate_sources, inventory.candidate_sources)
        assert_av1_validation_v3_tier2_selection_record(
            self.protocol,
            plan,
            record,
        )
        validate_av1_validation_v3_tier2_selection_record_sources(
            protocol=self.protocol,
            plan=plan,
            record=record,
            sources=inventory.candidate_sources,
            qualification_key=key,
        )

    def _inventory(self) -> AV1ValidationV3Tier2Inventory:
        return load_av1_validation_v3_tier2_inventory(
            self.connection,
            config=self.config,
            protocol=self.protocol,
        )


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "media": {
                "libraries": [
                    {
                        "key": "tv",
                        "label": "TV",
                        "path": str(root / "tv"),
                        "type": "tv",
                        "availability": "production",
                    },
                    {
                        "key": "movies",
                        "label": "Movies",
                        "path": str(root / "movies"),
                        "type": "movie",
                        "availability": "production",
                    },
                ],
                "output_container": "mkv",
                "staging_root": str(root / "staging"),
                "archive_root": str(root / "archive"),
            },
            "video": _video_policy(),
            "audio": {},
            "subtitle": {},
            "planning": {},
            "validation": {},
            "overrides": [],
            "remote_hosts": [],
        },
        paths=ConfigPaths(
            project_root=root,
            config_path=root / "config.toml",
            db_path=root / "mediaforce.sqlite3",
            run_manifest_dir=root / "runs",
            web_state_dir=root / "web",
            review_dir=root / "review",
            runtime_settings_path=root / "runtime-settings.json",
            runtime_reservation_dir=root / "runtime-reservations",
        ),
    )


def _video_policy(**overrides: object) -> dict[str, object]:
    policy: dict[str, object] = {
        "target_size_mb": 1_000,
        "target_size_bytes": 1_000_000_000,
        "target_runtime_minutes": 60,
        "size_goal_schema_version": 1,
        "size_goal_mode": "normalized",
        "size_goal_source": "test",
        "compression_intent_schema_version": 1,
        "compression_intent": "balanced",
        "compression_intent_source": "test",
        "compression_intent_confirmed": True,
        "quality_metric": "vmaf",
        "target_vmaf": 85.0,
        "min_target_vmaf": 80.0,
        "sample_projection_tolerance_percent": 10,
        "final_output_tolerance_percent": 5,
        "max_encoded_percent": 95,
    }
    policy.update(overrides)
    return policy


def _insert_item(
    connection: Connection,
    item_id: int,
    trait: str,
    source_identity: str,
    *,
    rel_path: str | None = None,
    analyzer_policy_digest: str = "sha256:majority",
) -> None:
    rel_path = rel_path or f"tv/Series {item_id}/S01E01.mkv"
    result = connection.execute(
        library_items.insert().values(
            id=item_id,
            source_path=f"/private/{item_id}.mkv",
            rel_path=rel_path,
            media_root=rel_path.split("/", 1)[0],
            parent_dir=str(Path(rel_path).parent),
            file_name=Path(rel_path).name,
            container="mkv",
            size_bytes=2_000_000_000,
            mtime_ns=1,
            fingerprint=f"source-{item_id}",
            duration_seconds=3600.0,
            video_codec="h264",
            video_bitrate=4_000_000,
            audio_summary_json="[]",
            subtitle_summary_json="[]",
            attachment_summary_json="[]",
            media_fingerprint_json=json.dumps(_fingerprint_summary(trait)),
            content_version_fingerprint=source_identity,
            status="discovered",
            priority_score=0,
            last_scan_id="synthetic",
            discovered_at="2026-08-03T00:00:00Z",
            last_seen_at="2026-08-03T00:00:00Z",
            updated_at="2026-08-03T00:00:00Z",
        )
    )
    local_id = int(result.inserted_primary_key[0])
    connection.execute(
        library_item_evidence_state.insert().values(
            library_item_id=local_id,
            evidence_kind=MEDIA_FINGERPRINT_EVIDENCE_KIND,
            state="current",
            summary_sha256=_summary_sha256(_fingerprint_summary(trait)),
            summary_schema_version=MEDIA_FINGERPRINT_SCHEMA_VERSION,
            analyzer_name=MEDIA_FINGERPRINT_TOOL_NAME,
            analyzer_version=MEDIA_FINGERPRINT_TOOL_VERSION,
            analyzer_runtime_version="ffmpeg-test",
            policy_hash=analyzer_policy_digest,
            decision_status="measured",
            attempt_count=1,
            updated_at="2026-08-03T00:00:00Z",
        )
    )


def _override_video(
    config: MediaforceConfig,
    rel_path: str,
    overrides: dict[str, object],
) -> None:
    config.raw["overrides"].append({
        "path_prefix": rel_path,
        "video": _video_policy(**overrides),
    })


def _fingerprint_summary(trait: str) -> dict[str, object]:
    return {
        "schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
        "analysis": {
            "sampled_frames": 120,
            "coverage": 1.0,
            "aggregate": _aggregate(trait),
            "audio_probe": {},
            "tool": {
                "name": MEDIA_FINGERPRINT_TOOL_NAME,
                "version": MEDIA_FINGERPRINT_TOOL_VERSION,
            },
        },
    }


def _aggregate(trait: str) -> dict[str, Any]:
    if trait == "animation":
        return {
            "duplicate_like_frame_fraction": 0.8,
            "edge_density_p90": 0.04,
        }
    if trait == "darkness":
        return {"dark_frame_fraction": 0.5}
    if trait == "motion":
        return {"high_motion_frame_fraction": 0.4}
    if trait == "mixed":
        return {"dark_frame_fraction": 0.5, "high_motion_frame_fraction": 0.4}
    if trait == "unknown":
        return {}
    return {
        "dark_frame_fraction": 0.0,
        "gradient_frame_fraction": 0.0,
        "banding_risk_score": 0.0,
        "high_motion_frame_fraction": 0.0,
        "ydif_p90": 0.0,
        "high_texture_frame_fraction": 0.0,
        "edge_density_p90": 0.0,
        "duplicate_like_frame_fraction": 0.0,
        "temporal_noise_proxy": 0.0,
        "chroma_instability": 0.0,
        "smooth_temporal_noise_fraction": 0.0,
    }


def _summary_sha256(summary: object) -> str:
    payload = json.dumps(summary, separators=(",", ":"), sort_keys=True)
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def _expected_source_fingerprint(identity: str) -> str:
    digest = hashlib.sha256(
        canonical_json_bytes({
            "domain": AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
            "protocol_version": AV1_VALIDATION_V3_PROTOCOL_VERSION,
            "experiment_id": AV1_VALIDATION_V3_EXPERIMENT_ID,
            "content_version_fingerprint": identity,
        })
    ).hexdigest()
    return f"sha256:{digest}"


if __name__ == "__main__":
    unittest.main()
