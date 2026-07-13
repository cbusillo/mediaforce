import copy
import tempfile
import tomllib
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mediaforce.core.config import ConfigPaths, DEFAULT_CONFIG_PATH, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, plex_item_metadata, series_metadata
from mediaforce.library.candidate_selection import project_candidates, season_identity
from mediaforce.library.run_manifests import build_run_manifest, create_folder_manifest, select_encode_candidates
from mediaforce.library.workflow_state import build_folder_workflow_state, derive_item_workflow_state
from mediaforce.web.routes.folders import _request_flag
from mediaforce.web.runtime.folder_cards import list_folder_cards


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)


class LibraryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_season_identity_uses_numbered_folders_and_excludes_specials(self) -> None:
        numbered = season_identity({"rel_path": "tv/Big Brother/Season 027/Episode 01.mkv"})
        specials = season_identity({"rel_path": "tv/Big Brother/Specials/Aftershow.mkv"})
        plex_special = season_identity({
            "rel_path": "tv/Big Brother/Season 27/Misfiled Special.mkv",
            "plex_season_index": 0,
        })

        self.assertIsNotNone(numbered)
        self.assertEqual(numbered.season_number, 27)
        self.assertFalse(numbered.is_special)
        self.assertIsNotNone(specials)
        self.assertIsNone(specials.season_number)
        self.assertTrue(specials.is_special)
        self.assertIsNotNone(plex_special)
        self.assertIsNone(plex_special.season_number)
        self.assertTrue(plex_special.is_special)

    def test_auto_protects_only_highest_numbered_active_season(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Big Brother/Season 26/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Big Brother/Season 27/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Big Brother", status="Returning Series", in_production=True)

            decisions = project_candidates(connection, config, prefixes=["tv/Big Brother"], now=NOW)

        by_season = {decision.season.season_number: decision for decision in decisions if decision.season}
        self.assertTrue(by_season[26].eligible)
        self.assertFalse(by_season[27].eligible)
        self.assertEqual([reason.code for reason in by_season[27].hold_reasons], ["current_season"])

    def test_complete_legacy_config_inherits_lifecycle_defaults(self) -> None:
        config = self._config()
        for key in (
            "series_lifecycle_mode",
            "season_acquisition_hold_days",
            "current_season_inactive_days",
            "series_metadata_stale_days",
        ):
            config.raw["planning"].pop(key, None)
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)

            decision = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)[0]

        self.assertFalse(decision.eligible)
        self.assertEqual([reason.code for reason in decision.hold_reasons], ["current_season"])

    def test_successor_releases_previous_current_season(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 2/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)
            initial = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)
            self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=100)
            with_successor = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)

        self.assertFalse(initial[0].eligible)
        by_season = {decision.season.season_number: decision for decision in with_successor if decision.season}
        self.assertTrue(by_season[2].eligible)
        self.assertFalse(by_season[3].eligible)

    def test_current_season_releases_after_inactivity_or_when_series_ended(self) -> None:
        active_config = self._config()
        ended_config = self._config()
        with open_db(active_config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Active/Season 1/Episode 01.mkv", age_days=366)
            self._insert_item(connection, "tv/Ended/Season 4/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Active", status="Returning Series", in_production=True)
            self._insert_series_metadata(connection, "tv/Ended", status="Ended", in_production=False)

            active = project_candidates(connection, active_config, prefixes=["tv/Active"], now=NOW)
            ended = project_candidates(connection, ended_config, prefixes=["tv/Ended"], now=NOW)

        self.assertTrue(active[0].eligible)
        self.assertTrue(ended[0].eligible)

    def test_acquisition_guard_holds_whole_season_from_newest_episode(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Show/Season 1/Episode 02.mkv", age_days=5)

            decisions = project_candidates(connection, config, prefixes=["tv/Show/Season 1"], now=NOW)

        self.assertEqual(len(decisions), 2)
        self.assertTrue(all(not decision.eligible for decision in decisions))
        self.assertTrue(all([reason.code for reason in decision.hold_reasons] == ["recent_acquisition"] for decision in decisions))

    def test_specials_do_not_become_current_season(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Show/Specials/Aftershow.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)

            decisions = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)

        numbered = next(decision for decision in decisions if decision.season and not decision.season.is_special)
        special = next(decision for decision in decisions if decision.season and decision.season.is_special)
        self.assertFalse(numbered.eligible)
        self.assertTrue(numbered.is_current_season)
        self.assertTrue(special.eligible)
        self.assertFalse(special.is_current_season)

    def test_conflicting_folder_and_plex_season_numbers_fail_closed(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 2/Episode 01.mkv", age_days=100)
            self._insert_plex_metadata(connection, item_id, added_at=NOW - timedelta(days=100), season_index=3)

            decision = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)[0]

        self.assertTrue(decision.season and decision.season.ambiguous)
        self.assertFalse(decision.eligible)
        self.assertEqual([reason.code for reason in decision.hold_reasons], ["season_identity_conflict"])
        self.assertFalse(decision.override_applied)

    def test_explicit_modes_and_manual_override_are_scoped(self) -> None:
        on_config = self._config(mode="on")
        off_config = self._config(mode="off")
        with open_db(on_config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=5)
            self._insert_item(connection, "tv/Show/Season 2/Episode 01.mkv", age_days=5)
            self._insert_series_metadata(connection, "tv/Show", status="Ended", in_production=False)

            protected = project_candidates(connection, on_config, prefixes=["tv/Show"], now=NOW)
            unprotected = project_candidates(connection, off_config, prefixes=["tv/Show"], now=NOW)
            overridden = project_candidates(
                connection,
                on_config,
                prefixes=["tv/Show"],
                now=NOW,
                manual_override_prefix="tv/Show/Season 2",
            )

        protected_by_season = {decision.season.season_number: decision for decision in protected if decision.season}
        off_by_season = {decision.season.season_number: decision for decision in unprotected if decision.season}
        override_by_season = {decision.season.season_number: decision for decision in overridden if decision.season}
        self.assertFalse(protected_by_season[2].eligible)
        self.assertFalse(off_by_season[1].eligible)
        self.assertFalse(off_by_season[2].eligible)
        self.assertFalse(override_by_season[1].eligible)
        self.assertTrue(override_by_season[2].eligible)
        self.assertTrue(override_by_season[2].manual_override)

    def test_selection_ranks_plex_age_then_mediaforce_discovery(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            plex_item = self._insert_item(
                connection,
                "tv/A Plex Show/Season 1/Episode 01.mkv",
                age_days=200,
            )
            discovered_item = self._insert_item(
                connection,
                "tv/B Discovered Show/Season 1/Episode 01.mkv",
                age_days=1_000,
            )
            self._insert_plex_metadata(connection, plex_item, added_at=NOW - timedelta(days=2_000), season_index=1)

            rows = select_encode_candidates(connection, config, prefixes=["tv"], limit=None)

        self.assertEqual(
            [row["rel_path"] for row in rows],
            [
                "tv/A Plex Show/Season 1/Episode 01.mkv",
                "tv/B Discovered Show/Season 1/Episode 01.mkv",
            ],
        )
        self.assertEqual(rows[0]["selection_provenance"]["season_rank_age"]["source"], "plex")
        self.assertEqual(
            rows[1]["selection_provenance"]["season_rank_age"]["source"],
            "mediaforce_discovered",
        )

    def test_selection_recomputes_recommendation_before_bucket_filtering(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(
                connection,
                "tv/Show/Season 1/Episode 01.mkv",
                age_days=100,
            )
            connection.execute(
                library_items.update()
                .where(library_items.c.id == item_id)
                .values(
                    recommendation="low_value_review",
                    priority_score=0.0,
                    recommendation_reason="stale scanner recommendation",
                )
            )

            rows = select_encode_candidates(
                connection,
                config,
                prefixes=["tv/Show"],
                limit=None,
                buckets=["priority_encode"],
            )

        self.assertEqual([row["id"] for row in rows], [item_id])
        self.assertEqual(rows[0]["recommendation"], "priority_encode")
        self.assertGreater(rows[0]["priority_score"], 0.0)

    def test_manifest_freezes_policy_and_override_provenance(self) -> None:
        config = self._config(mode="on")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=5)
            rows = select_encode_candidates(
                connection,
                config,
                prefixes=["tv/Show/Season 1"],
                limit=None,
                manual_override_prefix="tv/Show/Season 1",
            )

        manifest = build_run_manifest(rows, config)

        self.assertEqual(manifest["selection"]["selector"], "library_lifecycle_v1")
        provenance = manifest["items"][0]["selection_provenance"]
        self.assertTrue(provenance["manual_override"])
        self.assertEqual(provenance["policy_mode"], "on")
        self.assertEqual(
            {reason["code"] for reason in provenance["hold_reasons"]},
            {"recent_acquisition", "current_season"},
        )
        self.assertTrue(provenance["override_applied"])

    def test_manifest_exact_root_movie_excludes_similarly_named_sibling(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            exact_id = self._insert_item(connection, "movies/Foo.mkv", age_days=100)
            self._insert_item(connection, "movies/Foo.mkv.backup.mkv", age_days=100)

            manifest, _ = create_folder_manifest(
                connection,
                config,
                prefix="movies/Foo.mkv",
            )

        self.assertEqual([item["library_item_id"] for item in manifest["items"]], [exact_id])
        self.assertEqual(manifest["selection"]["media_scope"]["kind"], "movie_file")
        self.assertEqual(manifest["selection"]["media_scope"]["match"], "exact_item")

    def test_manual_override_does_not_bypass_unknown_content_age(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            item_id = self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=100)
            connection.execute(
                library_items.update()
                .where(library_items.c.id == item_id)
                .values(discovered_at="unknown", content_version_changed_at=None)
            )

            decision = project_candidates(
                connection,
                config,
                prefixes=["tv/Show/Season 1"],
                now=NOW,
                manual_override_prefix="tv/Show/Season 1",
            )[0]

        self.assertEqual([reason.code for reason in decision.hold_reasons], ["content_age_unknown"])
        self.assertTrue(decision.manual_override)
        self.assertFalse(decision.override_applied)
        self.assertFalse(decision.eligible)

    def test_request_flags_require_literal_json_true(self) -> None:
        self.assertTrue(_request_flag({"override_policy_holds": True}, "override_policy_holds"))
        self.assertFalse(_request_flag({"override_policy_holds": "true"}, "override_policy_holds"))
        self.assertFalse(_request_flag({"override_policy_holds": "false"}, "override_policy_holds"))
        self.assertFalse(_request_flag({"override_policy_holds": 1}, "override_policy_holds"))

    def test_held_workflow_state_preserves_policy_blocker(self) -> None:
        state = derive_item_workflow_state(
            {
                "item_id": 1,
                "rel_path": "tv/Show/Season 1/Episode 01.mkv",
                "status": "discovered",
                "staged_library_item_id": None,
                "staging_path": None,
                "promoted_at": None,
            },
            encode_eligible=False,
            policy_blocker="This season is still receiving episodes.",
        )

        self.assertEqual(state.state, "held")
        self.assertEqual(state.blocker, "This season is still receiving episodes.")

    def test_folder_cards_keep_held_candidates_without_estimating_savings(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=5)
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)

            cards = list_folder_cards(
                connection,
                config=config,
                minimum_recommended_savings_bytes=None,
                folder_group=lambda _path: ("tv/Show/Season 1", "Season 1", "Show", "Season"),
                age_days=lambda _path: 0.0,
                estimate_savings_bytes=lambda **_kwargs: 123,
                review_badge_for_prefix=lambda _prefix: {"label": None, "tone": None, "detail": None},
                rel_path_root="tv/",
            )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].estimated_savings_bytes, 0)
        self.assertEqual(cards[0].projected_reclaim_bytes, 0)
        self.assertEqual(cards[0].lifecycle["held_candidate_count"], 1)
        self.assertEqual(cards[0].workflow_state["state"], "held")
        self.assertEqual(cards[0].workflow_state["blockers"], [])

    def test_folder_workflow_keeps_eligible_lane_when_another_season_is_held(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            eligible_item_id = self._insert_item(
                connection,
                "tv/Show/Season 1/Episode 01.mkv",
                age_days=100,
            )
            held_item_id = self._insert_item(
                connection,
                "tv/Show/Season 2/Episode 01.mkv",
                age_days=5,
            )

            state = build_folder_workflow_state(
                connection,
                "tv/Show",
                candidate_eligibility={
                    eligible_item_id: (True, None),
                    held_item_id: (False, "This season is still receiving episodes."),
                },
            )

        self.assertEqual(state.state, "encode_candidates")
        self.assertEqual(state.counts["encode_candidates"], 1)
        self.assertEqual(state.counts["held"], 1)
        self.assertEqual(state.blockers, [])

    def _config(self, *, mode: str | None = None) -> MediaforceConfig:
        with DEFAULT_CONFIG_PATH.open("rb") as handle:
            raw = copy.deepcopy(tomllib.load(handle))
        raw["media"]["source_roots"] = {
            "movies": str(self.root / "source" / "movies"),
            "other": str(self.root / "source" / "other"),
            "tv": str(self.root / "source" / "tv"),
        }
        raw["media"]["staging_root"] = str(self.root / "staging")
        raw["media"]["archive_root"] = str(self.root / "archive")
        if mode is not None:
            raw["overrides"] = [
                {
                    "path_prefix": "tv",
                    "planning": {"series_lifecycle_mode": mode},
                }
            ]
        paths = ConfigPaths(
            project_root=self.root,
            config_path=self.root / "config.toml",
            db_path=self.root / "library.sqlite3",
            run_manifest_dir=self.root / "runs",
            web_state_dir=self.root / "web",
            review_dir=self.root / "review",
            runtime_settings_path=self.root / "runtime.json",
        )
        return MediaforceConfig(raw=raw, paths=paths)

    def _insert_item(self, connection: DBClient, rel_path: str, *, age_days: int) -> int:
        timestamp = NOW - timedelta(days=age_days)
        timestamp_text = timestamp.isoformat(timespec="seconds")
        source_path = self.root / "source" / rel_path
        result = connection.execute(
            library_items.insert().values(
                source_path=str(source_path),
                rel_path=rel_path,
                media_root=Path(rel_path).parts[0],
                parent_dir=str(Path(rel_path).parent),
                file_name=Path(rel_path).name,
                container="mkv",
                size_bytes=8 * 1024 ** 3,
                mtime_ns=int(timestamp.timestamp() * 1_000_000_000),
                fingerprint=f"fingerprint-{rel_path}",
                duration_seconds=2_700.0,
                video_codec="h264",
                audio_track_count=1,
                subtitle_track_count=1,
                english_audio_count=1,
                english_subtitle_count=1,
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                content_version_changed_at=timestamp_text,
                status="discovered",
                priority_score=50.0,
                recommendation="review_encode",
                recommendation_reason="test",
                last_scan_id="scan-1",
                discovered_at=timestamp_text,
                last_seen_at=timestamp_text,
                updated_at=timestamp_text,
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _insert_series_metadata(
            connection: DBClient,
            prefix: str,
            *,
            status: str,
            in_production: bool,
    ) -> None:
        observed_at = NOW.isoformat(timespec="seconds")
        connection.execute(
            series_metadata.insert().values(
                series_prefix=prefix,
                plex_guids_json="[]",
                tmdb_status=status,
                tmdb_in_production=1 if in_production else 0,
                tmdb_observed_at=observed_at,
                updated_at=observed_at,
            )
        )

    @staticmethod
    def _insert_plex_metadata(
            connection: DBClient,
            item_id: int,
            *,
            added_at: datetime,
            season_index: int,
    ) -> None:
        connection.execute(
            plex_item_metadata.insert().values(
                library_item_id=item_id,
                plex_server_id="plex-1",
                plex_item_rating_key=str(item_id),
                plex_show_rating_key=f"show-{item_id}",
                plex_season_index=season_index,
                plex_added_at=added_at.isoformat(timespec="seconds"),
                plex_part_path=f"/plex/{item_id}.mkv",
                observed_at=NOW.isoformat(timespec="seconds"),
            )
        )


if __name__ == "__main__":
    unittest.main()
