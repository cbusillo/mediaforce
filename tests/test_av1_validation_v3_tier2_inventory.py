from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any
import unittest
from unittest.mock import Mock, patch

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
    AV1ValidationProtocolV3,
    av1_validation_v3_qualification_key_id,
    load_av1_validation_protocol_v3,
)
from mediaforce.tuning.av1_validation_v3_qualification import (
    AV1ValidationV3QualificationPlan,
    build_av1_validation_v3_qualification_plan,
)
from mediaforce.tuning.av1_validation_v3_tier1_config_snapshot import (
    build_av1_validation_v3_tier1_config_snapshot,
)
from mediaforce.tuning.av1_validation_v3_tier1_preparation import (
    av1_validation_v3_tier1_config_sha256,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory import (
    AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
    AV1ValidationV3Tier2Inventory,
    AV1ValidationV3Tier2InventoryError,
    load_av1_validation_v3_tier2_inventory,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_authorization import (
    AV1ValidationV3Tier2InventoryReadContext,
    build_av1_validation_v3_tier2_inventory_read_claim,
    build_av1_validation_v3_tier2_inventory_read_grant,
    build_av1_validation_v3_tier2_inventory_read_request,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_operation import (
    run_av1_validation_v3_tier2_inventory_read,
)
from mediaforce.tuning.av1_validation_v3_tier2_inventory_publication import (
    publish_av1_validation_v3_tier2_inventory_read_claim,
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
VALID_UNTIL = "2026-08-06T12:00:00Z"
REQUESTED_AT = "2026-08-03T13:00:00Z"
REQUEST_VALID_UNTIL = "2026-08-06T10:00:00Z"
AUTHORIZED_AT = "2026-08-03T14:00:00Z"
GRANT_VALID_UNTIL = "2026-08-06T09:00:00Z"
CLAIMED_AT = "2026-08-03T15:00:00Z"
READ_AT = "2026-08-03T15:00:01Z"
SELECTED_AT = "2026-08-03T13:00:00Z"
OWNER = "owner-1234abcd"


class AV1ValidationV3Tier2InventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.config = _config(self.root)
        self.protocol = load_av1_validation_protocol_v3(V3_PROTOCOL_PATH)
        self.config_snapshot_bytes, self.read_context = self._read_context_for_config(
            self.config
        )
        self.plan = self.read_context.plan
        self.request = self.read_context.request
        self.grant = self.read_context.grant
        self.claim = self.read_context.claim
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
        item_count_before = self.connection.exec_driver_sql(
            "select count(*) from library_items"
        ).scalar_one()
        evidence_count_before = self.connection.exec_driver_sql(
            "select count(*) from library_item_evidence_state"
        ).scalar_one()

        first = self._inventory()
        second = self._inventory()
        item_count_after = self.connection.exec_driver_sql(
            "select count(*) from library_items"
        ).scalar_one()
        evidence_count_after = self.connection.exec_driver_sql(
            "select count(*) from library_item_evidence_state"
        ).scalar_one()

        self.assertEqual(first, second)
        self.assertEqual(item_count_before, item_count_after)
        self.assertEqual(evidence_count_before, evidence_count_after)

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
        self.assertEqual(
            AV1_VALIDATION_V3_TIER2_INVENTORY_FINGERPRINT_DOMAIN,
            "mediaforce:av1:v3:tier2-qualification-source:v1",
        )
        self.assertEqual(
            original,
            "sha256:b4bd4d23318a662b3be0135cfdc556c571a72e5741b73152948280206bde62f5",
        )

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
        self.assertEqual(inventory.incompatible_evidence_count, 0)

    def test_inventory_has_no_candidate_cap(self) -> None:
        for item_id in range(1, 65):
            _insert_item(
                self.connection,
                item_id,
                "animation" if item_id % 2 else "typical",
                f"{item_id:040x}",
            )

        inventory = self._inventory()

        self.assertEqual(len(inventory.entries), 64)
        self.assertEqual(
            sum(
                count.private_candidate_count
                for count in inventory.frozen_stratum_private_counts
            ),
            64,
        )

    def test_duplicate_identity_drops_rows_across_evidence_cohorts(self) -> None:
        identity = "0" * 39 + "1"
        _insert_item(self.connection, 1, "animation", identity)
        _insert_item(
            self.connection,
            2,
            "typical",
            identity,
            analyzer_policy_digest="sha256:minority",
        )
        _insert_item(self.connection, 3, "typical", "0" * 39 + "2")

        inventory = self._inventory()

        self.assertEqual(
            [entry.source_identity for entry in inventory.entries],
            ["0" * 39 + "2"],
        )
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

    def test_auth_failure_makes_zero_db_calls(self) -> None:
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier2_inventory."
                "av1_validation_measured_fingerprint_rows",
            ) as measured_rows,
            self.assertRaisesRegex(ValueError, "not active"),
        ):
            self._inventory(clock=lambda: "2026-08-03T14:59:59Z")

        measured_rows.assert_not_called()

    def test_config_snapshot_failure_makes_zero_db_calls(self) -> None:
        changed = _config(self.root)
        changed.raw["video"]["target_vmaf"] = 90.0
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier2_inventory."
                "av1_validation_measured_fingerprint_rows",
            ) as measured_rows,
            self.assertRaisesRegex(ValueError, "config"),
        ):
            load_av1_validation_v3_tier2_inventory(
                self.connection,
                config=changed,
                protocol=self.protocol,
                read_context=self.read_context,
                config_snapshot_bytes=self.config_snapshot_bytes,
                clock=lambda: READ_AT,
            )

        measured_rows.assert_not_called()

    def test_stale_context_rejected_by_real_clock_before_db_read(self) -> None:
        stale_context = self._read_context_for_config(
            self.config,
            frozen_at="2026-08-03T12:00:00Z",
            plan_valid_until="2026-08-04T12:00:00Z",
            request_valid_until="2026-08-04T10:00:00Z",
            grant_valid_until="2026-08-04T09:00:00Z",
        )
        with (
            patch(
                "mediaforce.tuning.av1_validation_v3_tier2_inventory."
                "av1_validation_measured_fingerprint_rows",
            ) as measured_rows,
            self.assertRaisesRegex(ValueError, "not active"),
        ):
            load_av1_validation_v3_tier2_inventory(
                self.connection,
                config=self.config,
                protocol=self.protocol,
                read_context=stale_context[1],
                config_snapshot_bytes=stale_context[0],
                clock=lambda: datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            )

        measured_rows.assert_not_called()

    def test_operation_publishes_claim_before_inventory_read(self) -> None:
        events: list[str] = []

        def adapter(
            *_args: object,
            **_kwargs: object,
        ) -> AV1ValidationV3Tier2Inventory:
            events.append("adapter")
            return self._inventory()

        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"
            with patch(
                "mediaforce.tuning.av1_validation_v3_tier2_inventory_operation."
                "publish_av1_validation_v3_tier2_inventory_read_claim",
                side_effect=lambda **kwargs: events.append("claim")
                or publish_av1_validation_v3_tier2_inventory_read_claim(**kwargs),
            ):
                result = run_av1_validation_v3_tier2_inventory_read(
                    self.connection,
                    config=self.config,
                    protocol=self.protocol,
                    read_context=self.read_context,
                    config_snapshot_bytes=self.config_snapshot_bytes,
                    output_root=output,
                    repository_root=repository,
                    clock=lambda: READ_AT,
                    adapter=adapter,
                )

        self.assertEqual(events, ["claim", "adapter"])
        summary = result.to_public_summary()
        self.assertTrue(summary["read_claim_published"])
        self.assertIsInstance(result.inventory, AV1ValidationV3Tier2Inventory)
        self.assertNotIn("candidate_sources", summary)
        self.assertNotIn("measured_row_count", summary)
        self.assertNotIn("frozen_stratum_count", summary)
        self.assertFalse(summary["private_inventory_serialization_authorized"])

    def test_wrong_claim_id_makes_zero_reads(self) -> None:
        other_claim = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claimed_at="2026-08-03T15:01:00Z",
        )
        wrong_context = AV1ValidationV3Tier2InventoryReadContext(
            plan=self.plan,
            request=self.request,
            grant=self.grant,
            claim=other_claim,
        )
        with tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        ) as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir(mode=0o700)
            output = root / "artifacts"
            publish_av1_validation_v3_tier2_inventory_read_claim(
                claim=self.claim,
                output_root=output,
                repository_root=repository,
            )
            adapter = Mock()
            with self.assertRaisesRegex(ValueError, "already consumed"):
                run_av1_validation_v3_tier2_inventory_read(
                    self.connection,
                    config=self.config,
                    protocol=self.protocol,
                    read_context=wrong_context,
                    config_snapshot_bytes=self.config_snapshot_bytes,
                    output_root=output,
                    repository_root=repository,
                    clock=lambda: "2026-08-03T15:01:01Z",
                    adapter=adapter,
                )

        adapter.assert_not_called()

    def _inventory(
        self,
        *,
        clock: Callable[[], str] | None = None,
    ) -> AV1ValidationV3Tier2Inventory:
        snapshot_bytes, read_context = self._read_context_for_config(self.config)
        return load_av1_validation_v3_tier2_inventory(
            self.connection,
            config=self.config,
            protocol=self.protocol,
            read_context=read_context,
            config_snapshot_bytes=snapshot_bytes,
            clock=clock or (lambda: READ_AT),
        )

    def _read_context_for_config(
        self,
        config: MediaforceConfig,
        *,
        frozen_at: str = FROZEN_AT,
        plan_valid_until: str = VALID_UNTIL,
        request_valid_until: str = REQUEST_VALID_UNTIL,
        grant_valid_until: str = GRANT_VALID_UNTIL,
    ) -> tuple[bytes, AV1ValidationV3Tier2InventoryReadContext]:
        snapshot_bytes = build_av1_validation_v3_tier1_config_snapshot(config)
        plan = _plan(
            self.protocol,
            config_sha256=av1_validation_v3_tier1_config_sha256(snapshot_bytes),
            frozen_at=frozen_at,
            valid_until=plan_valid_until,
        )
        request = build_av1_validation_v3_tier2_inventory_read_request(
            protocol=self.protocol,
            plan=plan,
            requested_at=REQUESTED_AT,
            valid_until=request_valid_until,
        )
        grant = build_av1_validation_v3_tier2_inventory_read_grant(
            protocol=self.protocol,
            plan=plan,
            request=request,
            owner_principal=OWNER,
            authorized_at=AUTHORIZED_AT,
            valid_until=grant_valid_until,
        )
        claim = build_av1_validation_v3_tier2_inventory_read_claim(
            protocol=self.protocol,
            plan=plan,
            request=request,
            grant=grant,
            claimed_at=CLAIMED_AT,
        )
        return snapshot_bytes, AV1ValidationV3Tier2InventoryReadContext(
            plan=plan,
            request=request,
            grant=grant,
            claim=claim,
        )


def _plan(
    protocol: AV1ValidationProtocolV3,
    *,
    config_sha256: str,
    frozen_at: str = FROZEN_AT,
    valid_until: str = VALID_UNTIL,
) -> AV1ValidationV3QualificationPlan:
    return build_av1_validation_v3_qualification_plan(
        protocol=protocol,
        qualification_key_id=f"av1vqkey3_{'b' * 32}",
        eligibility_predicate_sha256=SHA256,
        repository_commit=COMMIT,
        repository_tree=TREE,
        config_sha256=config_sha256,
        toolchain_sha256=SHA256,
        fixture_matrix_sha256=SHA256,
        frozen_at=frozen_at,
        valid_until=valid_until,
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
