import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.library.candidate_selection import project_candidates, workflow_eligibility
from mediaforce.library.other_library import load_other_library_payload, load_other_scope_payload, \
    other_group_scope_for_rel_path, other_scope_action_blocker
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.folder_actions import (
    promote_folder_outputs_action,
    queue_folder_encode_action,
    validate_folder_outputs_action,
)


class OtherWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_folder_grouping_uses_exact_root_files_and_bounded_top_level_folders(self) -> None:
        config = self._config(grouping="folder")

        root_file = other_group_scope_for_rel_path("other/Loose.mkv", config)
        nested = other_group_scope_for_rel_path("other/Collection/Nested/Clip.mkv", config)

        self.assertEqual(
            (root_file.prefix, root_file.match, root_file.scope_label),
            ("other/Loose.mkv", "exact_item", "File"),
        )
        self.assertEqual(
            (nested.prefix, nested.match, nested.scope_label),
            ("other/Collection", "descendants", "Folder"),
        )

    def test_file_grouping_keeps_every_item_exact(self) -> None:
        config = self._config(grouping="file")

        nested = other_group_scope_for_rel_path("other/Collection/Nested/Clip.mkv", config)

        self.assertEqual(
            (nested.prefix, nested.match, nested.scope_label),
            ("other/Collection/Nested/Clip.mkv", "exact_item", "File"),
        )

    def test_profile_probe_requirements_block_the_whole_bounded_scope(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            ready_id = self._insert_item(connection, "other/Collection/Ready.mkv")
            blocked_id = self._insert_item(
                connection,
                "other/Collection/Unprobed.mkv",
                width=None,
                height=None,
            )
            decisions = project_candidates(connection, config, prefixes=["other/Collection"])
            workflow = build_folder_workflow_state(
                connection,
                "other/Collection",
                candidate_eligibility=workflow_eligibility(decisions),
                library_types=config.library_type_map,
            )

        by_id = {decision.item_id: decision for decision in decisions}
        self.assertFalse(by_id[ready_id].eligible)
        self.assertIn("frame dimensions", by_id[ready_id].profile_blocker)
        self.assertFalse(by_id[blocked_id].eligible)
        self.assertIn("frame dimensions", by_id[blocked_id].profile_blocker)
        self.assertEqual(workflow.primary_lane, "blocked")
        self.assertIn("frame dimensions", workflow.blockers[0])

    def test_library_and_scope_payloads_keep_membership_and_confirmation_explicit(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "other/Collection/One.mkv", size_bytes=1_000)
            self._insert_item(connection, "other/Collection/Two.mkv", size_bytes=2_000)
            self._insert_item(connection, "other/Loose.mkv", size_bytes=3_000)
            payload = load_other_library_payload(connection, config, include_details=True)
            context = load_other_scope_payload(connection, config, "other/Collection")
            blocked = other_scope_action_blocker(
                connection,
                config,
                "other/Collection",
                membership_token="",
            )
            allowed = other_scope_action_blocker(
                connection,
                config,
                "other/Collection",
                membership_token=str(context["membership_token"]),
            )

        units = {unit["prefix"]: unit for unit in payload["work_units"]}
        folder = units["other/Collection"]
        self.assertEqual(folder["item_count"], 2)
        self.assertEqual(folder["total_size_bytes"], 3_000)
        self.assertEqual(folder["profile_readiness"]["state"], "ready")
        self.assertTrue(folder["membership_requires_confirmation"])
        self.assertEqual(
            [member["prefix"] for member in context["members"]],
            ["other/Collection/One.mkv", "other/Collection/Two.mkv"],
        )
        self.assertTrue(context["membership_complete"])
        self.assertEqual(blocked["code"], "other_scope_confirmation_required")
        self.assertIsNone(allowed)

    def test_single_member_folder_still_requires_membership_confirmation(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "other/Collection/Only.mkv")
            context = load_other_scope_payload(connection, config, "other/Collection")
            blocker = other_scope_action_blocker(
                connection,
                config,
                "other/Collection",
                membership_token="",
            )

        self.assertTrue(context["membership_requires_confirmation"])
        self.assertEqual(blocker["code"], "other_scope_confirmation_required")

    def test_server_rejects_noncanonical_other_scopes(self) -> None:
        folder_config = self._config(grouping="folder")
        with open_db(folder_config.paths.db_path) as connection:
            self._insert_item(connection, "other/Collection/Nested/Clip.mkv")
            nested_file = other_scope_action_blocker(
                connection,
                folder_config,
                "other/Collection/Nested/Clip.mkv",
                membership_token="",
            )
            root_scope = other_scope_action_blocker(
                connection,
                folder_config,
                "other",
                membership_token="",
            )

        file_config = self._config(grouping="file")
        with open_db(file_config.paths.db_path) as connection:
            folder_scope = other_scope_action_blocker(
                connection,
                file_config,
                "other/Collection",
                membership_token="",
            )

        self.assertEqual(nested_file["code"], "other_scope_not_bounded")
        self.assertEqual(root_scope["code"], "other_scope_not_bounded")
        self.assertEqual(folder_scope["code"], "other_scope_not_bounded")

    def test_unscoped_other_candidates_are_not_production_eligible(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "other/Collection/Clip.mkv")
            unscoped = project_candidates(connection, config)
            scoped = project_candidates(connection, config, prefixes=["other/Collection"])

        self.assertFalse({decision.item_id: decision for decision in unscoped}[item_id].eligible)
        self.assertTrue({decision.item_id: decision for decision in scoped}[item_id].eligible)

    def test_membership_confirmation_expires_when_the_scope_changes(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "other/Collection/One.mkv")
            self._insert_item(connection, "other/Collection/Two.mkv")
            context = load_other_scope_payload(connection, config, "other/Collection")
            reviewed_token = str(context["membership_token"])
            self._insert_item(connection, "other/Collection/Three.mkv")
            blocker = other_scope_action_blocker(
                connection,
                config,
                "other/Collection",
                membership_token=reviewed_token,
            )

        self.assertEqual(blocker["code"], "other_scope_confirmation_required")
        self.assertIn("all 3 files", blocker["message"])

    def test_oversized_folder_scope_is_blocked_before_processing(self) -> None:
        config = self._config(grouping="folder")
        with open_db(config.paths.db_path) as connection:
            for index in range(251):
                self._insert_item(connection, f"other/Large/Clip-{index:03d}.mkv")
            with patch(
                "mediaforce.library.other_library.project_candidates",
                side_effect=AssertionError("oversized scopes must short-circuit candidate projection"),
            ):
                context = load_other_scope_payload(connection, config, "other/Large")
                blocker = other_scope_action_blocker(
                    connection,
                    config,
                    "other/Large",
                    membership_token="ignored-for-oversized-scope",
                )

        self.assertEqual(context["item_count"], 251)
        self.assertFalse(context["membership_complete"])
        self.assertEqual(context["profile_readiness"]["label"], "Scope too large")
        self.assertEqual(blocker["code"], "other_profile_not_ready")

    def test_other_library_catalog_is_bounded_and_reports_truncation(self) -> None:
        config = self._config(grouping="file")
        with open_db(config.paths.db_path) as connection:
            for index in range(3):
                self._insert_item(connection, f"other/Clip-{index}.mkv")
            with patch("mediaforce.library.other_library.OTHER_LIBRARY_CATALOG_MAX_ITEMS", 2):
                payload = load_other_library_payload(connection, config, include_details=False)

        self.assertTrue(payload["catalog_truncated"])
        self.assertEqual(payload["catalog_item_limit"], 2)
        self.assertEqual(len(payload["work_units"]), 2)

    def test_other_scope_can_validate_and_promote_without_media_type_assumptions(self) -> None:
        config = self._config(grouping="folder")
        source_path = self.root / "other" / "Delivery" / "Clip.mp4"
        staging_path = self.root / "staging" / "other" / "Delivery" / "Clip.mkv"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"source")
        staging_path.write_bytes(b"staged")
        item = {
            "source_path": str(source_path),
            "rel_path": "other/Delivery/Clip.mp4",
            "staging_path": str(staging_path),
        }

        validated = validate_folder_outputs_action(
            config,
            "other/Delivery",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [item],
            validate_manifest_items_fn=lambda *_args, **_kwargs: [{"passed": True}],
        )
        promoted = promote_folder_outputs_action(
            config,
            "other/Delivery",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [item],
            promote_manifest_items_fn=lambda *_args, **_kwargs: [source_path.with_suffix(".mkv")],
        )

        self.assertEqual(validated["validated_count"], 1)
        self.assertEqual(promoted["promoted_count"], 1)

    def test_queue_validates_membership_inside_an_immediate_transaction(self) -> None:
        config = self._config(grouping="folder")
        transaction_states: list[bool] = []

        def validate_scope(connection: DBClient, _prefix: str) -> dict[str, object]:
            transaction_states.append(bool(connection.connection.driver_connection.in_transaction))
            return {"ok": False, "code": "blocked", "message": "Membership changed."}

        result = queue_folder_encode_action(
            config,
            "other/Collection",
            "",
            False,
            now_iso=lambda: "2026-07-14T00:00:00+00:00",
            load_job_state=lambda *_args: None,
            load_calibration_state=lambda *_args: None,
            review_gate=lambda *_args: {},
            upsert_override=lambda *_args: None,
            load_active_encode_job_for_prefix_fn=lambda *_args: None,
            clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
            prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None,
            save_encode_job=lambda *_args: None,
            validate_scope_action=validate_scope,
        )

        self.assertEqual(result["code"], "blocked")
        self.assertEqual(transaction_states, [True])

    def test_delivery_actions_validate_membership_inside_their_database_transaction(self) -> None:
        config = self._config(grouping="folder")
        transaction_states: list[bool] = []

        def validate_scope(connection: DBClient, _prefix: str) -> dict[str, object]:
            transaction_states.append(bool(connection.connection.driver_connection.in_transaction))
            return {"ok": False, "code": "blocked", "message": "Membership changed."}

        validated = validate_folder_outputs_action(
            config,
            "other/Collection",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [],
            validate_manifest_items_fn=lambda *_args, **_kwargs: [],
            validate_scope_action=validate_scope,
        )
        promoted = promote_folder_outputs_action(
            config,
            "other/Collection",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: [],
            promote_manifest_items_fn=lambda *_args, **_kwargs: [],
            validate_scope_action=validate_scope,
        )

        self.assertEqual(validated["code"], "blocked")
        self.assertEqual(promoted["code"], "blocked")
        self.assertEqual(transaction_states, [True, True])

    def _config(self, *, grouping: str, availability: str = "production") -> MediaforceConfig:
        return MediaforceConfig(
            raw={
                "media": {
                    "libraries": [
                        {
                            "key": "other",
                            "label": "Other",
                            "path": str(self.root / "other"),
                            "type": "other",
                            "availability": availability,
                            "default_profile": "other_conservative",
                            "policy": {"grouping": grouping},
                        }
                    ],
                    "output_container": "mkv",
                    "staging_root": str(self.root / "staging"),
                    "archive_root": str(self.root / "archive"),
                },
                "video": {},
                "audio": {},
                "subtitle": {},
                "planning": {},
                "validation": {},
                "overrides": [],
                "remote_hosts": [],
            },
            paths=ConfigPaths(
                project_root=self.root,
                config_path=self.root / "config.toml",
                db_path=self.root / "library.sqlite3",
                run_manifest_dir=self.root / "runs",
                web_state_dir=self.root / "web",
                review_dir=self.root / "review",
                runtime_settings_path=self.root / "runtime-settings.json",
            ),
        )

    @staticmethod
    def _insert_item(
            connection: DBClient,
            rel_path: str,
            *,
            size_bytes: int = 1_024,
            width: int | None = 1_920,
            height: int | None = 1_080,
    ) -> int:
        timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
        path = Path(rel_path)
        result = connection.execute(
            library_items.insert().values(
                source_path=f"/media/{rel_path}",
                rel_path=rel_path,
                media_root=path.parts[0],
                parent_dir=str(path.parent),
                file_name=path.name,
                container=path.suffix.lstrip(".") or "mkv",
                size_bytes=size_bytes,
                mtime_ns=1_700_000_000_000_000_000,
                fingerprint=f"other-{rel_path}",
                duration_seconds=3_600.0,
                video_codec="h264",
                width=width,
                height=height,
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                status="discovered",
                priority_score=50,
                recommendation="priority_encode",
                recommendation_reason="Other workflow fixture.",
                last_scan_id="other-scan",
                discovered_at=timestamp,
                last_seen_at=timestamp,
                updated_at=timestamp,
            )
        )
        return int(result.inserted_primary_key[0])
