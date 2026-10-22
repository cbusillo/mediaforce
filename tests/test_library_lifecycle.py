import copy
import json
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, plex_item_metadata, series_metadata, staged_artifacts
from mediaforce.encoding.staging import FAR_BELOW_PREDICTION_CHECK
from mediaforce.library.candidate_selection import (
    older_season_override_selection,
    project_candidates,
    scope_lifecycle_payload,
    season_identity,
)
from mediaforce.library.representatives import load_representative_selection
from mediaforce.library.scanner import _failed_probe_summary, scan_library
from mediaforce.library.run_manifests import build_run_manifest, create_folder_manifest, select_encode_candidates
from mediaforce.library.workflow_state import EncodeEligibility, build_folder_workflow_state, \
    build_folder_workflow_states, derive_item_workflow_state
from mediaforce.web.runtime.episode_progress import load_season_episode_progress
from mediaforce.web.routes.folders import _request_flag
from mediaforce.web.runtime.folder_cards import FolderCard, list_folder_cards


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

    def test_season_identity_uses_typed_root_authority(self) -> None:
        custom_tv = season_identity(
            {"rel_path": "shows/Example/Season 1/Episode.mkv"},
            library_types={"shows": "tv"},
        )
        movie_named_tv = season_identity(
            {"rel_path": "tv/Example/Season 1.mkv"},
            library_types={"tv": "movie"},
        )

        self.assertEqual(custom_tv.season_prefix, "shows/Example/Season 1")
        self.assertIsNone(movie_named_tv)

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

    def test_a_season_or_episode_loads_only_its_show_and_still_finds_the_current_season(self) -> None:
        from unittest.mock import patch

        from mediaforce.library import candidate_selection

        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Big Brother/Season 26/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Big Brother/Season 27/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Other Show/Season 1/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Big Brother", status="Returning Series", in_production=True)
            loaded: list[str] = []
            load_rows = candidate_selection._load_rows

            def recording_load_rows(*args, **kwargs):
                rows = load_rows(*args, **kwargs)
                loaded.extend(str(row["rel_path"]) for row in rows)
                return rows

            with patch.object(candidate_selection, "_load_rows", side_effect=recording_load_rows):
                season = project_candidates(connection, config, prefixes=["tv/Big Brother/Season 27"], now=NOW)
                episode = project_candidates(
                    connection, config, prefixes=["tv/Big Brother/Season 27/Episode 01.mkv"], now=NOW,
                )

        self.assertFalse(any(rel_path.startswith("tv/Other Show/") for rel_path in loaded))
        for decisions in (season, episode):
            current = [decision for decision in decisions if decision.season and decision.season.season_number == 27]
            self.assertEqual([reason.code for reason in current[0].hold_reasons], ["current_season"])

    def test_exact_episode_scope_can_override_parent_season_holds(self) -> None:
        config = self._config()
        rel_path = "tv/Show/Season 1/Episode 01.mkv"
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, rel_path, age_days=5)
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)

            lifecycle = scope_lifecycle_payload(connection, config, rel_path, now=NOW)

        self.assertEqual(lifecycle["held_candidate_count"], 1)
        self.assertTrue(lifecycle["can_override_holds"])
        self.assertEqual(lifecycle["seasons"][0]["prefix"], "tv/Show/Season 1")

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

    def test_ended_series_stays_released_when_its_metadata_is_stale(self) -> None:
        config = self._config()
        long_ago = NOW - timedelta(days=60)
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Ended/Season 8/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Active/Season 2/Episode 01.mkv", age_days=100)
            self._insert_series_metadata(connection, "tv/Ended", status="Ended", in_production=False, observed_at=long_ago)
            self._insert_series_metadata(
                connection, "tv/Active", status="Returning Series", in_production=True, observed_at=long_ago,
            )

            ended = project_candidates(connection, config, prefixes=["tv/Ended"], now=NOW)
            active = project_candidates(connection, config, prefixes=["tv/Active"], now=NOW)

        self.assertEqual(ended[0].provider_state, "ended")
        self.assertTrue(ended[0].eligible)
        self.assertEqual(active[0].provider_state, "stale")
        self.assertEqual([reason.code for reason in active[0].hold_reasons], ["current_season"])

    def test_first_discovery_is_not_activity_when_plex_knows_the_arrival_date(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            long_held = self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=100)
            self._insert_plex_metadata(connection, long_held, added_at=NOW - timedelta(days=900), season_index=3)
            replaced = self._insert_item(connection, "tv/Other/Season 3/Episode 01.mkv", age_days=100)
            self._insert_plex_metadata(connection, replaced, added_at=NOW - timedelta(days=900), season_index=3)
            connection.execute(
                library_items.update()
                .where(library_items.c.id == replaced)
                .values(content_version_changed_at=(NOW - timedelta(days=40)).isoformat(timespec="seconds"))
            )
            for prefix in ("tv/Show", "tv/Other"):
                self._insert_series_metadata(connection, prefix, status="Returning Series", in_production=True)

            settled = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)
            recently_replaced = project_candidates(connection, config, prefixes=["tv/Other"], now=NOW)

        self.assertTrue(settled[0].eligible)
        # A replacement 40 days ago is past the acquisition hold and is not a new episode, so nothing holds the season.
        self.assertTrue(recently_replaced[0].eligible)

    def test_a_replaced_file_restarts_the_acquisition_hold_but_not_the_current_season_hold(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            item = self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=100)
            self._insert_plex_metadata(connection, item, added_at=NOW - timedelta(days=900), season_index=3)
            connection.execute(
                library_items.update()
                .where(library_items.c.id == item)
                .values(content_version_changed_at=(NOW - timedelta(days=3)).isoformat(timespec="seconds"))
            )
            self._insert_series_metadata(connection, "tv/Show", status="Returning Series", in_production=True)

            decisions = project_candidates(connection, config, prefixes=["tv/Show"], now=NOW)

        self.assertEqual([reason.code for reason in decisions[0].hold_reasons], ["recent_acquisition"])

    def test_a_file_that_goes_missing_and_returns_unchanged_keeps_its_stage(self) -> None:
        config = self._config()
        season = "tv/Show/Season 1"
        with open_db(config.paths.db_path) as connection, self._scan_clock() as scan_at:
            item_ids = self._scan_staged_season(connection, config, scan_at, season)
            before = self._library_rows(connection)
            gone = [self.root / "source" / season / f"{stage}.mkv" for stage in ("encoded", "validated", "promoted")]
            for path in gone:
                path.rename(path.with_suffix(".away"))

            scan_at[0] = NOW - timedelta(days=2)
            scan_library(connection, config)
            while_missing = self._library_rows(connection)
            missing_candidates = select_encode_candidates(connection, config, prefixes=["tv"], limit=None, now=NOW)
            missing_decisions = project_candidates(connection, config, prefixes=["tv"], now=NOW)
            missing_workflow = build_folder_workflow_state(connection, season)
            missing_cards = self._season_cards(connection, config)

            for path in gone:
                path.with_suffix(".away").rename(path)
            scan_at[0] = NOW - timedelta(days=1)
            scan_library(connection, config)
            after = self._library_rows(connection)
            candidates = select_encode_candidates(connection, config, prefixes=["tv"], limit=None, now=NOW)
            decisions = {decision.item_id: decision for decision in project_candidates(
                connection, config, prefixes=["tv"], now=NOW,
            )}

        returned = [item_ids[stage] for stage in ("encoded", "validated", "promoted")]
        for item_id in returned:
            self.assertIsNotNone(while_missing[item_id]["missing_since"])
        # Missing files stay out of the candidates and decisions, and the season shows them as missing.
        self.assertEqual([row["item_id"] for row in missing_candidates], [item_ids["waiting"]])
        self.assertEqual({decision.item_id for decision in missing_decisions}, {item_ids["waiting"]})
        self.assertEqual(missing_workflow.counts["items"], 4)
        self.assertEqual(missing_workflow.counts["ready_to_validate"], 0)
        self.assertEqual(missing_workflow.counts["ready_to_promote"], 0)
        self.assertEqual([(card.item_count, card.statuses) for card in missing_cards], [(1, {"discovered": 1})])

        for stage in ("encoded", "validated", "promoted"):
            item_id = item_ids[stage]
            with self.subTest(stage=stage):
                self.assertIsNone(after[item_id]["missing_since"])
                self.assertEqual(after[item_id]["status"], stage)
                self.assertEqual(
                    after[item_id]["content_version_changed_at"], before[item_id]["content_version_changed_at"],
                )
                self.assertNotEqual(decisions[item_id].workflow_lane, "encode")
        self.assertEqual([row["item_id"] for row in candidates], [item_ids["waiting"]])
        self.assertEqual(decisions[item_ids["waiting"]].hold_reasons, ())

    def test_a_file_that_returns_with_different_content_is_a_replacement(self) -> None:
        config = self._config()
        season = "tv/Show/Season 1"
        with open_db(config.paths.db_path) as connection, self._scan_clock() as scan_at:
            item_ids = self._scan_staged_season(connection, config, scan_at, season)
            published = self.root / "source" / season / "promoted.mkv"
            published.rename(published.with_suffix(".away"))
            scan_at[0] = NOW - timedelta(days=2)
            scan_library(connection, config)

            published.with_suffix(".away").unlink()
            published.write_bytes(b"a different recording of this episode")
            returned_at = NOW - timedelta(days=1)
            scan_at[0] = returned_at
            scan_library(connection, config)
            after = self._library_rows(connection)
            decisions = {decision.item_id: decision for decision in project_candidates(
                connection, config, prefixes=["tv"], now=NOW,
            )}

        replaced = after[item_ids["promoted"]]
        self.assertIsNone(replaced["missing_since"])
        self.assertEqual(replaced["status"], "discovered")
        self.assertEqual(replaced["content_version_changed_at"], returned_at.isoformat(timespec="seconds"))
        self.assertEqual(
            [reason.code for reason in decisions[item_ids["waiting"]].hold_reasons], ["recent_acquisition"],
        )

    @contextmanager
    def _scan_clock(self) -> Iterator[list[datetime]]:
        """Scans stamp the time held in the returned list, so each scan can happen on a chosen day."""
        scan_at = [NOW - timedelta(days=100)]
        with (
            patch("mediaforce.library.scanner.timestamp", side_effect=lambda: scan_at[0].isoformat(timespec="seconds")),
            patch(
                "mediaforce.library.scanner.probe_media",
                return_value=_failed_probe_summary(RuntimeError("fixture probe")),
            ),
        ):
            yield scan_at

    def _scan_staged_season(
            self, connection: DBClient, config: MediaforceConfig, scan_at: list[datetime], season: str,
    ) -> dict[str, int]:
        """A long-held, ended season with a file at each finished stage and one still waiting to encode."""
        season_dir = self.root / "source" / season
        season_dir.mkdir(parents=True)
        stages = ("encoded", "validated", "promoted", "waiting")
        for stage in stages:
            (season_dir / f"{stage}.mkv").write_bytes(f"{stage} episode".encode())
        scan_library(connection, config)
        item_ids = {
            Path(str(row["rel_path"])).stem: int(row["id"])
            for row in connection.execute(library_items.select()).mappings()
        }
        stage_at = (scan_at[0] + timedelta(days=10)).isoformat(timespec="seconds")
        for stage in stages:
            item_id = item_ids[stage]
            self._insert_plex_metadata(connection, item_id, added_at=scan_at[0], season_index=1)
            if stage == "waiting":
                continue
            connection.execute(
                library_items.update().where(library_items.c.id == item_id).values(status=stage)
            )
            connection.execute(staged_artifacts.insert().values(
                library_item_id=item_id,
                staging_path=str(self.root / "staging" / f"{stage}.mkv"),
                promoted_at=stage_at if stage == "promoted" else None,
                updated_at=stage_at,
            ))
        self._insert_series_metadata(connection, "tv/Show", status="Ended", in_production=False)
        connection.commit()
        return item_ids

    @staticmethod
    def _season_cards(connection: DBClient, config: MediaforceConfig) -> list[FolderCard]:
        return list_folder_cards(
            connection,
            config=config,
            minimum_recommended_savings_bytes=None,
            folder_group=lambda _path: ("tv/Show/Season 1", "Season 1", "Show", "Season"),
            age_days=lambda _path: 0.0,
            estimate_savings_bytes=lambda **_kwargs: 123,
            review_badge_for_prefix=lambda _prefix: {"label": None, "tone": None, "detail": None},
            rel_path_root="tv/",
        )

    @staticmethod
    def _library_rows(connection: DBClient) -> dict[int, dict[str, object]]:
        return {
            int(row["id"]): dict(row)
            for row in connection.execute(library_items.select()).mappings()
        }

    def test_publishing_a_compressed_copy_does_not_restart_the_acquisition_hold(self) -> None:
        config = self._config()
        published_at = NOW - timedelta(days=3)
        with open_db(config.paths.db_path) as connection:
            # Promotion can record the content change a few seconds before the publish itself.
            waiting = self._insert_published_season(
                connection, "tv/Show", published_at=published_at, changed_at=published_at - timedelta(seconds=9),
            )
            replaced_later = self._insert_published_season(
                connection, "tv/Other", published_at=published_at, changed_at=published_at + timedelta(days=1),
            )

            decisions = project_candidates(connection, config, prefixes=["tv/Show", "tv/Other"], now=NOW)

        by_id = {decision.row["item_id"]: decision for decision in decisions}
        self.assertEqual(by_id[waiting].hold_reasons, ())
        self.assertTrue(by_id[waiting].eligible)
        # A replacement after the publish is new content and still restarts the hold.
        self.assertEqual([reason.code for reason in by_id[replaced_later].hold_reasons], ["recent_acquisition"])

    def _insert_published_season(
            self, connection: DBClient, show: str, *, published_at: datetime, changed_at: datetime,
    ) -> int:
        """A long-held season with one published episode and one still waiting; returns the waiting one."""
        published = self._insert_item(connection, f"{show}/Season 3/Episode 01.mkv", age_days=100)
        waiting = self._insert_item(connection, f"{show}/Season 3/Episode 02.mkv", age_days=100)
        for item_id in (published, waiting):
            self._insert_plex_metadata(connection, item_id, added_at=NOW - timedelta(days=900), season_index=3)
        connection.execute(
            library_items.update()
            .where(library_items.c.id == published)
            .values(status="promoted", content_version_changed_at=changed_at.isoformat(timespec="seconds"))
        )
        connection.execute(staged_artifacts.insert().values(
            library_item_id=published,
            staging_path=str(self.root / "staging" / f"{published}.mkv"),
            promoted_at=published_at.isoformat(timespec="seconds"),
            updated_at=published_at.isoformat(timespec="seconds"),
        ))
        self._insert_series_metadata(connection, show, status="Ended", in_production=False)
        return waiting

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

    def test_older_season_override_excludes_latest_even_when_policy_is_off(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            self._insert_item(connection, "tv/Show/Season 1/Episode 01.mkv", age_days=5)
            self._insert_item(connection, "tv/Show/Season 2/Episode 01.mkv", age_days=100)
            self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=5)
            self._insert_item(connection, "tv/Show/Specials/Aftershow.mkv", age_days=5)

            selection = older_season_override_selection(
                project_candidates(connection, config, prefixes=["tv/Show"], now=NOW),
                "tv/Show",
            )

        self.assertTrue(selection.available)
        self.assertEqual(selection.latest_season_number, 3)
        self.assertEqual(selection.latest_season_prefixes, ("tv/Show/Season 3",))
        self.assertEqual(
            selection.included_season_prefixes,
            ("tv/Show/Season 1", "tv/Show/Season 2"),
        )
        self.assertEqual(selection.overridden_season_prefixes, ("tv/Show/Season 1",))
        self.assertEqual(selection.candidate_count, 2)
        self.assertEqual(selection.overridden_candidate_count, 1)

    def test_older_season_override_excludes_ambiguous_and_unknown_age_seasons(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            unknown_id = self._insert_item(
                connection,
                "tv/Show/Season 1/Episode 01.mkv",
                age_days=100,
            )
            ambiguous_id = self._insert_item(
                connection,
                "tv/Show/Season 2/Episode 01.mkv",
                age_days=100,
            )
            self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=100)
            connection.execute(
                library_items.update()
                .where(library_items.c.id == unknown_id)
                .values(discovered_at="unknown", content_version_changed_at=None)
            )
            self._insert_plex_metadata(
                connection,
                ambiguous_id,
                added_at=NOW - timedelta(days=100),
                season_index=9,
            )

            selection = older_season_override_selection(
                project_candidates(connection, config, prefixes=["tv/Show"], now=NOW),
                "tv/Show",
            )

        self.assertFalse(selection.available)
        self.assertEqual(selection.latest_season_number, 3)
        self.assertEqual(selection.included_season_prefixes, ())

    def test_selection_ranks_plex_age_then_mediaforce_discovery(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            plex_item = self._insert_item(
                connection,
                "tv/A Plex Show/Season 1/Episode 01.mkv",
                age_days=200,
            )
            self._insert_item(
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
                now=NOW,
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

    def test_older_season_manifest_freezes_exact_scope_and_override_provenance(self) -> None:
        config = self._config(mode="off")
        with open_db(config.paths.db_path) as connection:
            first_id = self._insert_item(
                connection,
                "tv/Show/Season 1/Episode 01.mkv",
                age_days=5,
            )
            second_id = self._insert_item(
                connection,
                "tv/Show/Season 2/Episode 01.mkv",
                age_days=100,
            )
            self._insert_item(connection, "tv/Show/Season 3/Episode 01.mkv", age_days=5)
            self._insert_item(connection, "tv/Show/Specials/Aftershow.mkv", age_days=5)
            selection = older_season_override_selection(
                project_candidates(connection, config, prefixes=["tv/Show"], now=NOW),
                "tv/Show",
            )

            manifest, _ = create_folder_manifest(
                connection,
                config,
                prefix="tv/Show",
                older_season_override=selection,
            )

        self.assertEqual(
            [item["library_item_id"] for item in manifest["items"]],
            [second_id, first_id],
        )
        override = manifest["selection"]["lifecycle_override"]
        self.assertEqual(
            override["included_season_prefixes"],
            ["tv/Show/Season 1", "tv/Show/Season 2"],
        )
        self.assertEqual(
            override["overridden_season_prefixes"],
            ["tv/Show/Season 1"],
        )
        provenance_by_season = {
            item["selection_provenance"]["season_number"]: item["selection_provenance"]
            for item in manifest["items"]
        }
        self.assertTrue(provenance_by_season[1]["manual_override"])
        self.assertTrue(provenance_by_season[1]["override_applied"])
        self.assertFalse(provenance_by_season[2]["manual_override"])
        self.assertFalse(provenance_by_season[2]["override_applied"])

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

    def test_failed_check_agrees_across_season_show_and_episode_without_holding_other_files(self) -> None:
        config = self._config()
        season = "tv/Show/Season 1"
        other_season = "tv/Show/Season 2"
        with open_db(config.paths.db_path) as connection:
            failed_id = self._insert_item(connection, f"{season}/Episode 01.mkv", age_days=100)
            ready_id = self._insert_item(connection, f"{season}/Episode 02.mkv", age_days=100)
            other_id = self._insert_item(connection, f"{other_season}/Episode 01.mkv", age_days=100)
            for item_id in (failed_id, ready_id, other_id):
                connection.execute(library_items.update().where(library_items.c.id == item_id).values(status="encoded"))
                connection.execute(staged_artifacts.insert().values(
                    library_item_id=item_id,
                    staging_path=str(self.root / "staging" / f"{item_id}.mkv"),
                    validation_json=json.dumps({"passed": False}) if item_id == failed_id else None,
                    updated_at=NOW.isoformat(),
                ))

            single = build_folder_workflow_state(connection, season)
            bulk = build_folder_workflow_states(connection, ["tv/Show", season, other_season])
            episodes = load_season_episode_progress(connection, single.items, [])

        self.assertEqual(single.state, "blocked")
        self.assertEqual(single.primary_lane, "blocked")
        self.assertEqual(single.counts["blocked"], 1)
        self.assertEqual(single.counts["ready_to_validate"], 1)
        self.assertEqual(single.to_payload(), bulk[season].to_payload())
        self.assertEqual(bulk["tv/Show"].state, "blocked")
        self.assertEqual(bulk[other_season].state, "ready_to_validate")
        self.assertEqual({episode["stage"] for episode in episodes}, {"needs_you", "checking"})

        with open_db(config.paths.db_path) as connection:
            connection.execute(staged_artifacts.update().where(
                staged_artifacts.c.library_item_id == failed_id,
            ).values(validation_json=json.dumps({"passed": True}), validated_at=NOW.isoformat()))
            connection.execute(library_items.update().where(library_items.c.id == failed_id).values(status="validated"))
            settled = build_folder_workflow_state(connection, season)

        self.assertFalse(settled.blockers)
        self.assertEqual(settled.counts["ready_to_promote"], 1)

    def test_staged_check_attention_precedes_policy_hold_but_not_active_or_published_file(self) -> None:
        row = {
            "item_id": 1,
            "rel_path": "tv/Show/Season 1/Episode 01.mkv",
            "status": "encoded",
            "staged_library_item_id": 1,
            "staging_path": "staging/episode.mkv",
            "promoted_at": None,
            "validation_json": json.dumps({"passed": False}),
        }
        state = derive_item_workflow_state(row, encode_eligible=False, policy_blocker="Recent season")
        self.assertEqual(state.state, "blocked")
        self.assertTrue(state.blocker)
        for status, expected in (("encoding", "encoding"), ("promoted", "complete")):
            with self.subTest(status=status):
                self.assertEqual(derive_item_workflow_state({**row, "status": status}).state, expected)
        self.assertEqual(derive_item_workflow_state({**row, "missing_since": NOW.isoformat()}).state, "missing")
        size_held = derive_item_workflow_state({
            **row,
            "validation_json": json.dumps({
                "passed": False,
                "size_prediction": {"held": True},
                "checks": [{"passed": False, "message": FAR_BELOW_PREDICTION_CHECK}],
            }),
        })
        self.assertEqual(size_held.state, "blocked")
        self.assertNotEqual(size_held.blocker, state.blocker)
        self.assertEqual(derive_item_workflow_state({**row, "promoted_at": NOW.isoformat()}).state, "complete")
        self.assertEqual(derive_item_workflow_state({**row, "staging_path": None}).state, "blocked")
        # The encoder's own final-size miss still waits for the file's normal check.
        encoder_size_miss = json.dumps({
            "passed": False,
            "target_size_verification": {"status": "over_target", "passed": False, "retry_allowed": False},
        })
        for validation in (None, "invalid-json", json.dumps({"passed": True}), encoder_size_miss):
            with self.subTest(validation=validation):
                self.assertEqual(derive_item_workflow_state({**row, "validation_json": validation}).state, "ready_to_validate")

    def test_removed_remake_copy_returns_the_file_to_the_encode_lane(self) -> None:
        validation = {"passed": False, "remake_intent": {"prefix": "tv/Show", "mode": "older_seasons", "state": "removed"}}
        row = {
            "item_id": 1,
            "rel_path": "tv/Show/Season 1/Episode 01.mkv",
            "status": "planned",
            "staged_library_item_id": 1,
            "staging_path": "staging/episode.mkv",
            "promoted_at": None,
            "validation_json": json.dumps(validation),
        }
        removed = derive_item_workflow_state(row)
        self.assertEqual((removed.state, removed.lane), ("encode_candidate", "encode"))
        self.assertFalse(removed.has_staged_output)
        requested = derive_item_workflow_state({**row, "validation_json": json.dumps(
            {**validation, "remake_intent": {**validation["remake_intent"], "state": "requested"}})})
        self.assertEqual(requested.lane, "blocked")

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
                    eligible_item_id: EncodeEligibility(eligible=True),
                    held_item_id: EncodeEligibility(
                        eligible=False,
                        blocker="This season is still receiving episodes.",
                    ),
                },
            )

        self.assertEqual(state.state, "encode_candidates")
        self.assertEqual(state.counts["encode_candidates"], 1)
        self.assertEqual(state.counts["held"], 1)
        self.assertEqual(state.blockers, [])

    def test_sample_comes_from_seasons_production_covers(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_house_seasons(connection)

            selection = load_representative_selection(connection, config, "tv/House", now=NOW)

        self.assertIsNotNone(selection)
        payload = selection.public_payload()
        primary = selection.primary_item()
        self.assertNotIn("/Season 8/", primary["rel_path"])
        self.assertEqual(primary["video_codec"], "h264")
        self.assertEqual(payload["population"], {"basis": "production", "excluded_count": 6, "reason": None})
        self.assertEqual(payload["coverage"]["candidate_item_count"], 5)
        self.assertEqual(primary["representative_selection"]["population"], payload["population"])
        self.assertEqual(payload["measured_source_ids"], [payload["primary_source_id"]])
        self.assertIn("Not yet tested", payload["coverage_summary"])
        self.assertNotIn("Season 8", payload["coverage_summary"])
        self.assertNotIn("HEVC", payload["coverage_summary"])
        self.assertNotEqual(payload["confidence"]["level"], "high")

    def test_held_season_alone_falls_back_to_every_file_with_a_reason(self) -> None:
        config = self._config()
        with open_db(config.paths.db_path) as connection:
            self._insert_house_seasons(connection)

            held = load_representative_selection(connection, config, "tv/House/Season 8", now=NOW)
            released = load_representative_selection(connection, config, "tv/House/Season 3", now=NOW)

        self.assertIsNotNone(held)
        self.assertEqual(held.payload["population"]["basis"], "all_items")
        self.assertEqual(held.payload["population"]["excluded_count"], 0)
        self.assertTrue(held.payload["population"]["reason"])
        self.assertIn("/Season 8/", held.primary_item()["rel_path"])
        self.assertIsNotNone(released)
        self.assertEqual(released.payload["population"]["basis"], "production")

    def _insert_house_seasons(self, connection: DBClient) -> None:
        for season in (1, 3, 4, 6, 7):
            self._insert_item(connection, f"tv/House/Season {season}/Episode 01.mkv", age_days=100)
        for episode in range(1, 7):
            self._insert_item(
                connection,
                f"tv/House/Season 8/Episode {episode:02d}.mkv",
                age_days=100,
                video_codec="hevc",
            )
        self._insert_series_metadata(connection, "tv/House", status="Returning Series", in_production=True)

    def _config(self, *, mode: str | None = None) -> MediaforceConfig:
        with (Path(__file__).parent / "fixtures/defaults.toml").open("rb") as handle:
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

    def _insert_item(
            self,
            connection: DBClient,
            rel_path: str,
            *,
            age_days: int,
            video_codec: str = "h264",
    ) -> int:
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
                video_codec=video_codec,
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
            observed_at: datetime = NOW,
    ) -> None:
        observed_text = observed_at.isoformat(timespec="seconds")
        connection.execute(
            series_metadata.insert().values(
                series_prefix=prefix,
                plex_guids_json="[]",
                tmdb_status=status,
                tmdb_in_production=1 if in_production else 0,
                tmdb_observed_at=observed_text,
                updated_at=observed_text,
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
