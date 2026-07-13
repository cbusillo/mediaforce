import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy import select

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items, plex_item_metadata, staged_artifacts
from mediaforce.library.candidate_selection import candidate_rank_key, project_candidates, workflow_eligibility
from mediaforce.library.media_scopes import ScopeDomain, media_group_scope_for_rel_path, resolve_media_scope
from mediaforce.library.movie_library import load_movie_library_payload, load_movie_scope_payload
from mediaforce.library.movie_workflow import classify_movie_path, movie_item_included
from mediaforce.library.representatives import load_representative_selection
from mediaforce.library.workflow_state import build_folder_workflow_state
from mediaforce.web.runtime.folder_actions import _promotion_conflict_response
from mediaforce.web.runtime.folder_actions import _reset_stale_prefix_encoding_items_for_requeue
from mediaforce.web.runtime.folder_actions import promote_folder_outputs_action, queue_folder_encode_action, \
    validate_folder_outputs_action
from mediaforce.web.runtime.folder_cards import list_folder_cards


class MovieWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = self._config(extras="exclude")

    def tearDown(self) -> None:
        reset_engine_cache()
        self.temp_dir.cleanup()

    def test_classifies_features_editions_extras_and_uncertain_members(self) -> None:
        feature = classify_movie_path(
            "films/Example/Example - Director's Cut.mkv",
            root="films",
        )
        extra = classify_movie_path("films/Example/Featurettes/Making Of.mkv", root="films")
        direct_trailer = classify_movie_path("films/Example/Example-trailer.mkv", root="films")
        uncertain = classify_movie_path("films/Example/Disc 2/Alternate.mkv", root="films")
        single_file = classify_movie_path("films/Loose Feature.mkv", root="films")

        self.assertEqual((feature.role, feature.edition_label), ("feature", "Director's Cut"))
        self.assertEqual((extra.role, extra.extra_category), ("extra", "Featurettes"))
        self.assertEqual((direct_trailer.role, direct_trailer.extra_category), ("extra", "Trailers"))
        self.assertEqual(uncertain.role, "uncertain")
        self.assertEqual((single_file.role, single_file.scope_mode), ("feature", "single_file"))
        self.assertFalse(movie_item_included(extra, {"extras": "exclude"}, explicit_exact=False)[0])
        self.assertTrue(movie_item_included(extra, {"extras": "exclude"}, explicit_exact=True)[0])

    def test_classifies_release_named_bonus_and_extras_files_without_misreading_the_title(self) -> None:
        bonus = classify_movie_path(
            "movies/Bohemian Rhapsody (2018)/"
            "Bohemian.Rhapsody.2018.BONUS.Complete.Live.Aid.Performance.1080p.mkv",
            root="movies",
        )
        extras = classify_movie_path(
            "movies/Jurassic World Rebirth (2025)/"
            "Jurassic World Rebirth 2025 Extras 1080p BluRay Remux.mkv",
            root="movies",
        )
        titled_extra = classify_movie_path(
            "movies/The Extras (2005)/The Extras 2005 1080p BluRay.mkv",
            root="movies",
        )

        self.assertEqual((bonus.role, bonus.extra_category), ("extra", "Bonus features"))
        self.assertEqual((extras.role, extras.extra_category), ("extra", "Extras"))
        self.assertEqual(titled_extra.role, "feature")

    def test_title_words_that_match_extra_tokens_remain_features_without_filename_years(self) -> None:
        feature_paths = (
            "movies/The Extras (2005)/The Extras.mkv",
            "movies/Short Term 12 (2013)/Short Term 12.mkv",
            "movies/Interview with the Vampire (1994)/Interview with the Vampire.mkv",
            "movies/Bonus (2024)/Bonus.mkv",
            "movies/Trailer Park Boys The Movie (2006)/Trailer Park Boys The Movie.mkv",
        )

        memberships = [classify_movie_path(path, root="movies") for path in feature_paths]

        self.assertTrue(all(membership is not None for membership in memberships))
        self.assertEqual([membership.role for membership in memberships], ["feature"] * len(feature_paths))

    def test_custom_typed_root_resolves_movie_scopes(self) -> None:
        scope = media_group_scope_for_rel_path(
            "films/Example/Example.mkv",
            library_types={"films": "movie"},
        )
        self.assertEqual((scope.domain, scope.kind, scope.scope_label), ("movie", "movie_title", "Title"))

        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Loose Feature.mkv")
            exact = resolve_media_scope(
                connection,
                "films/Loose Feature.mkv",
                library_types=self.config.library_type_map,
            )
        self.assertEqual((exact.domain, exact.kind, exact.match), ("movie", "movie_file", "exact_item"))

    def test_title_scope_excludes_extras_and_uncertain_members_but_exact_scope_allows_them(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            feature_id = self._insert_item(connection, "films/Example/Example.mkv")
            edition_id = self._insert_item(connection, "films/Example/Example - Director's Cut.mkv")
            extra_id = self._insert_item(connection, "films/Example/Extras/Trailer.mkv")
            uncertain_id = self._insert_item(connection, "films/Example/Disc 2/Alternate.mkv")

            title_decisions = project_candidates(connection, self.config, prefixes=["films/Example"])
            exact_extra = project_candidates(
                connection,
                self.config,
                prefixes=["films/Example/Extras/Trailer.mkv"],
            )

        decisions = {decision.item_id: decision for decision in title_decisions}
        self.assertTrue(decisions[feature_id].eligible)
        self.assertTrue(decisions[edition_id].eligible)
        self.assertFalse(decisions[extra_id].eligible)
        self.assertIn("Extras are excluded", decisions[extra_id].production_blocker)
        self.assertFalse(decisions[uncertain_id].eligible)
        self.assertIn("explicit exact-file", decisions[uncertain_id].production_blocker)
        self.assertEqual([decision.item_id for decision in exact_extra if decision.eligible], [extra_id])

    def test_representative_selection_ignores_extras_for_title_scope(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            feature_id = self._insert_item(
                connection,
                "films/Example/Example.mkv",
                size_bytes=8 * 1024**3,
            )
            self._insert_item(
                connection,
                "films/Example/Extras/Trailer.mkv",
                size_bytes=200 * 1024**2,
            )
            selection = load_representative_selection(connection, self.config, "films/Example")

        self.assertIsNotNone(selection)
        self.assertEqual(selection.primary_item()["library_item_id"], feature_id)
        self.assertEqual(selection.public_payload()["coverage"]["candidate_item_count"], 1)

    def test_movie_ranking_can_prefer_largest_or_oldest_plex_item(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            older_id = self._insert_item(connection, "films/Older.mkv", size_bytes=2_000)
            larger_id = self._insert_item(connection, "films/Larger.mkv", size_bytes=9_000)
            self._insert_plex_age(connection, older_id, "2010-01-01T00:00:00+00:00")
            self._insert_plex_age(connection, larger_id, "2020-01-01T00:00:00+00:00")

            oldest = project_candidates(connection, self.config, prefixes=[])
            largest_config = self._config(extras="exclude", ranking="largest_first")
            largest = project_candidates(connection, largest_config, prefixes=[])

        oldest_sorted = sorted(
            oldest,
            key=lambda decision: candidate_rank_key(
                decision,
                recommendation_score=0,
                size_bytes=int(decision.row["size_bytes"]),
            ),
        )
        largest_sorted = sorted(
            largest,
            key=lambda decision: candidate_rank_key(
                decision,
                recommendation_score=0,
                size_bytes=int(decision.row["size_bytes"]),
            ),
        )
        self.assertEqual(oldest_sorted[0].item_id, older_id)
        self.assertEqual(oldest_sorted[0].age.source, "plex")
        self.assertEqual(largest_sorted[0].item_id, larger_id)

    def test_typed_movie_root_can_enter_production_while_other_and_spatial_remain_blocked(self) -> None:
        config = self._config(
            extras="exclude",
            additional_libraries=[
                {
                    "key": "other",
                    "path": str(self.root / "other"),
                    "type": "other",
                    "availability": "production",
                },
                {
                    "key": "vr",
                    "path": str(self.root / "vr"),
                    "type": "spatial",
                    "availability": "production",
                },
            ],
        )
        self.assertEqual(set(config.source_root_map), {"films"})

    def test_movie_library_payload_keeps_editions_extras_age_and_conflicts_visible(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            first_id = self._insert_item(connection, "films/Example/Example.mp4")
            second_id = self._insert_item(connection, "films/Example/Example.mkv")
            trailer_id = self._insert_item(connection, "films/Example/Example-trailer.mkv")
            self._insert_plex_age(connection, first_id, "2020-01-01T00:00:00+00:00")
            self._insert_stage(connection, first_id, "films/Example/Example.mp4")
            self._insert_stage(connection, second_id, "films/Example/Example.mkv")
            payload = load_movie_library_payload(
                connection,
                self.config,
                include_details=True,
                metrics_by_prefix={
                    "films/Example": {
                        "projected_reclaim_bytes": 512,
                        "estimated_savings_bytes": 512,
                    }
                },
            )

        title = payload["titles"][0]
        members = {member["item_id"]: member for member in title["members"]}
        self.assertEqual(title["edition_count"], 2)
        self.assertEqual(title["extra_count"], 1)
        self.assertEqual(title["savings_confidence"], "estimated")
        self.assertEqual(title["age"]["source"], "plex")
        self.assertFalse(members[trailer_id]["included_by_default"])
        self.assertEqual(title["promotion_conflicts"][0]["kind"], "duplicate_destination")

    def test_movie_library_payload_uses_mediaforce_discovery_age_fallback(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Example/Example.mkv")
            payload = load_movie_library_payload(
                connection,
                self.config,
                include_details=True,
            )

        self.assertEqual(payload["titles"][0]["age"]["source"], "mediaforce_discovered")

    def test_movie_folder_cards_ignore_tv_history_and_report_unavailable_estimate(self) -> None:
        config = self._config(
            extras="exclude",
            additional_libraries=[
                {
                    "key": "shows",
                    "label": "TV",
                    "path": str(self.root / "shows"),
                    "type": "tv",
                    "availability": "production",
                    "default_profile": "tv_balanced",
                    "policy": {},
                },
            ],
        )
        with open_db(config.paths.db_path) as connection:
            movie_id = self._insert_item(connection, "films/Example/Example.mkv", size_bytes=1_000)
            tv_id = self._insert_item(
                connection,
                "shows/Example/Season 1/Example S01E01.mkv",
                size_bytes=1_000,
                status="promoted",
            )
            connection.execute(
                library_items.update()
                .where(library_items.c.id.in_((movie_id, tv_id)))
                .values(video_codec="hevc")
            )
            self._insert_stage(connection, tv_id, "shows/Example/Season 1/Example S01E01.mkv")
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == tv_id)
                .values(
                    source_rel_path="shows/Example/Season 1/Example S01E01.mkv",
                    source_video_codec="hevc",
                    source_size_bytes=1_000,
                    bytes_saved=950,
                    promoted_at="2026-07-13T00:00:00+00:00",
                )
            )
            decisions = project_candidates(connection, config, prefixes=[])
            estimate_savings = Mock(return_value=160)

            with patch("mediaforce.web.runtime.folder_cards._apply_lifecycle") as lifecycle_mock, patch(
                    "mediaforce.web.runtime.folder_cards._apply_folder_workflow_states"
            ) as workflow_mock:
                cards = list_folder_cards(
                    connection,
                    config=config,
                    minimum_recommended_savings_bytes=None,
                    folder_group=lambda rel_path: self._movie_group(config, rel_path),
                    age_days=lambda _path: 0.0,
                    estimate_savings_bytes=estimate_savings,
                    review_badge_for_prefix=lambda _prefix: {"label": None, "tone": None, "detail": None},
                    candidate_decisions=decisions,
                    include_lifecycle=False,
                    include_workflow_states=False,
                )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].estimated_savings_bytes, 0)
        self.assertEqual(cards[0].estimate_unavailable_count, 1)
        estimate_savings.assert_not_called()
        lifecycle_mock.assert_not_called()
        workflow_mock.assert_not_called()

    def test_movie_library_payload_preserves_unavailable_reclaim_as_null(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Example/Example.mkv")
            payload = load_movie_library_payload(
                connection,
                self.config,
                include_details=True,
                metrics_by_prefix={
                    "films/Example": {
                        "projected_reclaim_bytes": 0,
                        "estimated_savings_bytes": 0,
                        "estimate_unavailable_count": 1,
                    }
                },
            )

        title = payload["titles"][0]
        self.assertIsNone(title["projected_reclaim_bytes"])
        self.assertIsNone(title["estimated_savings_bytes"])
        self.assertEqual(title["known_saved_bytes"], 0)
        self.assertEqual(title["savings_confidence"], "unavailable")

    def test_movie_scope_payload_preserves_unavailable_reclaim_as_null(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Example/Example.mkv")
            decisions = project_candidates(connection, self.config, prefixes=["films/Example"])
            payload = load_movie_scope_payload(
                connection,
                self.config,
                "films/Example",
                metrics_by_prefix={
                    "films/Example": {
                        "projected_reclaim_bytes": 0,
                        "estimated_savings_bytes": 0,
                        "estimate_unavailable_count": 1,
                    }
                },
                candidate_decisions=decisions,
            )
            payload_without_metrics = load_movie_scope_payload(
                connection,
                self.config,
                "films/Example",
                candidate_decisions=decisions,
            )

        self.assertIsNotNone(payload)
        self.assertIsNone(payload["projected_reclaim_bytes"])
        self.assertIsNone(payload["estimated_savings_bytes"])
        self.assertEqual(payload["savings_confidence"], "unavailable")
        self.assertIsNotNone(payload_without_metrics)
        self.assertIsNone(payload_without_metrics["projected_reclaim_bytes"])

    def test_tv_folder_cards_ignore_movie_codec_history(self) -> None:
        config = self._config(
            extras="exclude",
            additional_libraries=[
                {
                    "key": "shows",
                    "label": "TV",
                    "path": str(self.root / "shows"),
                    "type": "tv",
                    "availability": "production",
                    "default_profile": "tv_balanced",
                    "policy": {},
                },
            ],
        )
        with open_db(config.paths.db_path) as connection:
            pending_id = self._insert_item(
                connection,
                "shows/Example/Season 1/Example S01E01.mkv",
                size_bytes=1_000,
            )
            movie_id = self._insert_item(
                connection,
                "films/Example/Example.mkv",
                size_bytes=1_000,
                status="promoted",
            )
            connection.execute(
                library_items.update()
                .where(library_items.c.id.in_((pending_id, movie_id)))
                .values(video_codec="h264")
            )
            self._insert_stage(connection, movie_id, "films/Example/Example.mkv")
            connection.execute(
                staged_artifacts.update()
                .where(staged_artifacts.c.library_item_id == movie_id)
                .values(
                    source_rel_path="films/Example/Example.mkv",
                    source_video_codec="h264",
                    source_size_bytes=1_000,
                    bytes_saved=900,
                    promoted_at="2026-07-13T00:00:00+00:00",
                )
            )
            decisions = project_candidates(
                connection,
                config,
                prefixes=[],
                manual_override_prefix="shows/Example/Season 1",
            )
            estimate_savings = Mock(return_value=420)
            cards = list_folder_cards(
                connection,
                config=config,
                minimum_recommended_savings_bytes=None,
                folder_group=lambda rel_path: self._domain_group(config, rel_path, "tv"),
                age_days=lambda _path: 0.0,
                estimate_savings_bytes=estimate_savings,
                review_badge_for_prefix=lambda _prefix: {"label": None, "tone": None, "detail": None},
                candidate_decisions=decisions,
                include_lifecycle=False,
                include_workflow_states=False,
            )

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].estimated_savings_bytes, 420)
        estimate_savings.assert_called_once()

    def test_movie_library_payload_reuses_precomputed_candidate_projection(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Example/Example.mkv")
            decisions = project_candidates(connection, self.config, prefixes=[])
            with patch("mediaforce.library.movie_library.project_candidates") as projection_mock:
                payload = load_movie_library_payload(
                    connection,
                    self.config,
                    include_details=True,
                    candidate_decisions=decisions,
                )

        projection_mock.assert_not_called()
        self.assertEqual(payload["titles"][0]["workflow_state"]["state"], "encode_candidates")

    def test_promotion_preflight_blocks_duplicate_output_destinations(self) -> None:
        conflict = _promotion_conflict_response(
            self.config,
            [
                {"source_path": str(self.root / "Cut.mp4"), "rel_path": "films/Cut.mp4"},
                {"source_path": str(self.root / "Cut.avi"), "rel_path": "films/Cut.avi"},
            ],
        )

        self.assertIsNotNone(conflict)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["conflicts"][0]["kind"], "duplicate_destination")

    def test_promotion_preflight_blocks_case_equivalent_destinations(self) -> None:
        conflict = _promotion_conflict_response(
            self.config,
            [
                {"source_path": str(self.root / "Cut.mp4"), "rel_path": "films/Cut.mp4"},
                {"source_path": str(self.root / "cut.avi"), "rel_path": "films/cut.avi"},
            ],
        )

        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["conflicts"][0]["kind"], "duplicate_destination")

    def test_browse_only_movie_library_blocks_validation_and_promotion_actions(self) -> None:
        browse_config = self._config(extras="exclude", availability="browse_only")

        validate_result = validate_folder_outputs_action(
            browse_config,
            "films/Example",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: self.fail("staged items should not load"),
            validate_manifest_items_fn=lambda *_args, **_kwargs: self.fail("validation should not run"),
        )
        promote_result = promote_folder_outputs_action(
            browse_config,
            "films/Example",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: self.fail("staged items should not load"),
            promote_manifest_items_fn=lambda *_args, **_kwargs: self.fail("promotion should not run"),
        )

        self.assertEqual(validate_result["code"], "library_not_production")
        self.assertEqual(promote_result["code"], "library_not_production")

    def test_typed_libraries_block_stale_physical_root_prefixes(self) -> None:
        result = validate_folder_outputs_action(
            self.config,
            "Movies on Disk/Example",
            load_folder_staged_items_fn=lambda *_args, **_kwargs: self.fail("staged items should not load"),
            validate_manifest_items_fn=lambda *_args, **_kwargs: self.fail("validation should not run"),
        )

        self.assertEqual(result["code"], "library_not_configured")

    def test_title_retry_reset_leaves_extras_and_uncertain_members_untouched(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            feature_id = self._insert_item(
                connection,
                "films/Example/Example.mkv",
                status="encoding",
            )
            extra_id = self._insert_item(
                connection,
                "films/Example/Extras/Trailer.mkv",
                status="encoding",
            )
            uncertain_id = self._insert_item(
                connection,
                "films/Example/Disc 2/Alternate.mkv",
                status="encoding",
            )
            for item_id, rel_path in (
                (feature_id, "films/Example/Example.mkv"),
                (extra_id, "films/Example/Extras/Trailer.mkv"),
                (uncertain_id, "films/Example/Disc 2/Alternate.mkv"),
            ):
                self._insert_stage(connection, item_id, rel_path)

            _reset_stale_prefix_encoding_items_for_requeue(
                connection,
                self.config,
                "films/Example",
                now_iso=lambda: "2026-07-13T00:00:00+00:00",
            )
            statuses = dict(connection.execute(
                select(library_items.c.id, library_items.c.status)
                .where(library_items.c.id.in_((feature_id, extra_id, uncertain_id)))
            ).fetchall())
            staged_ids = set(connection.execute(select(staged_artifacts.c.library_item_id)).scalars().all())

        self.assertEqual(statuses[feature_id], "planned")
        self.assertEqual(statuses[extra_id], "encoding")
        self.assertEqual(statuses[uncertain_id], "encoding")
        self.assertNotIn(feature_id, staged_ids)
        self.assertIn(extra_id, staged_ids)
        self.assertIn(uncertain_id, staged_ids)

    def test_title_workflow_hides_exact_extra_delivery_until_the_exact_scope_is_opened(self) -> None:
        extra_prefix = "films/Example/Extras/Trailer.mkv"
        with open_db(self.config.paths.db_path) as connection:
            extra_id = self._insert_item(connection, extra_prefix, status="encoded")
            self._insert_stage(connection, extra_id, extra_prefix)
            title_decisions = project_candidates(connection, self.config, prefixes=["films/Example"])
            exact_decisions = project_candidates(connection, self.config, prefixes=[extra_prefix])
            title_workflow = build_folder_workflow_state(
                connection,
                "films/Example",
                candidate_eligibility=workflow_eligibility(title_decisions),
                library_types=self.config.library_type_map,
            )
            exact_workflow = build_folder_workflow_state(
                connection,
                extra_prefix,
                candidate_eligibility=workflow_eligibility(exact_decisions),
                library_types=self.config.library_type_map,
            )

        self.assertEqual(title_workflow.items[0].lane, "none")
        self.assertIn("Extras are excluded", title_workflow.items[0].blocker)
        self.assertEqual(exact_workflow.items[0].lane, "validate")

    def test_exact_extra_retry_resets_only_that_explicit_file(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            extra_id = self._insert_item(
                connection,
                "films/Example/Extras/Trailer.mkv",
                status="encoding",
            )
            self._insert_stage(connection, extra_id, "films/Example/Extras/Trailer.mkv")

            _reset_stale_prefix_encoding_items_for_requeue(
                connection,
                self.config,
                "films/Example/Extras/Trailer.mkv",
                now_iso=lambda: "2026-07-13T00:00:00+00:00",
            )
            status = connection.execute(
                select(library_items.c.status).where(library_items.c.id == extra_id)
            ).scalar_one()
            staged = connection.execute(
                select(staged_artifacts.c.library_item_id)
                .where(staged_artifacts.c.library_item_id == extra_id)
            ).scalar_one_or_none()

        self.assertEqual(status, "planned")
        self.assertIsNone(staged)

    def test_extras_only_queue_failure_does_not_persist_a_profile_override(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Extras Only/Featurettes/Making Of.mkv")
        persisted: list[str] = []

        with self.assertRaises(HTTPException) as raised:
            queue_folder_encode_action(
                self.config,
                "films/Extras Only",
                "",
                False,
                now_iso=lambda: "2026-07-13T00:00:00+00:00",
                load_job_state=lambda *_args: None,
                load_calibration_state=lambda *_args: {"policy": {}},
                review_gate=lambda _calibration: {"can_confirm_full": True, "message": "Ready"},
                upsert_override=lambda *_args: persisted.append("persisted"),
                load_active_encode_job_for_prefix_fn=lambda *_args: None,
                clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
                prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None,
                save_encode_job=lambda *_args: None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Extras are excluded", str(raised.exception.detail))
        self.assertEqual(persisted, [])

    def test_title_queue_does_not_recover_an_active_exact_extra_job(self) -> None:
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, "films/Example/Extras/Trailer.mkv", status="encoding")

        active_job = {
            "job_id": "exact-extra",
            "prefix": "films/Example/Extras/Trailer.mkv",
            "job_kind": "folder",
            "status": "running",
        }
        with patch("mediaforce.web.runtime.folder_actions._recover_active_folder_encode_job") as recover:
            result = queue_folder_encode_action(
                self.config,
                "films/Example",
                "",
                False,
                now_iso=lambda: "2026-07-13T00:00:00+00:00",
                load_job_state=lambda *_args: None,
                load_calibration_state=lambda *_args: {"policy": {}},
                review_gate=lambda _calibration: {"can_confirm_full": True, "message": "Ready"},
                upsert_override=lambda *_args: None,
                load_active_encode_job_for_prefix_fn=lambda *_args: active_job,
                clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
                prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None,
                save_encode_job=lambda *_args: None,
            )

        self.assertFalse(result["ok"])
        self.assertIn("Extras/Trailer.mkv", result["message"])
        recover.assert_not_called()

    def test_title_retry_preserves_artifacts_from_an_old_manifest_with_extras(self) -> None:
        feature_prefix = "films/Example/Feature.mkv"
        extra_prefix = "films/Example/Extras/Trailer.mkv"
        with open_db(self.config.paths.db_path) as connection:
            self._insert_item(connection, feature_prefix)
        manifest_path = self.root / "old-movie-manifest.json"
        manifest_path.write_text(
            '{"items":[{"library_item_id":99,"rel_path":"films/Example/Extras/Trailer.mkv"}]}'
        )
        terminal_job = {
            "job_id": "old-title-job",
            "prefix": "films/Example",
            "job_kind": "single",
            "status": "failed",
            "manifest_path": str(manifest_path),
            "manifest_indexes": [0],
        }
        prepared: list[str] = []

        with patch(
            "mediaforce.web.runtime.folder_actions.load_latest_terminal_encode_job_for_prefix",
            return_value=terminal_job,
        ), self.assertRaises(HTTPException) as raised:
            queue_folder_encode_action(
                self.config,
                "films/Example",
                "",
                False,
                now_iso=lambda: "2026-07-13T00:00:00+00:00",
                load_job_state=lambda *_args: None,
                load_calibration_state=lambda *_args: {"policy": {}},
                review_gate=lambda _calibration: {"can_confirm_full": True, "message": "Ready"},
                upsert_override=lambda *_args: None,
                load_active_encode_job_for_prefix_fn=lambda *_args: None,
                clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
                prepare_terminal_encode_job_for_requeue_fn=lambda *_args: prepared.append("prepared"),
                save_encode_job=lambda *_args: None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn(extra_prefix, str(raised.exception.detail))
        self.assertEqual(prepared, [])

    def _config(
            self,
            *,
            extras: str,
            ranking: str = "oldest_added_first",
            availability: str = "production",
            additional_libraries: list[dict[str, object]] | None = None,
    ) -> MediaforceConfig:
        libraries: list[dict[str, object]] = [
            {
                "key": "films",
                "label": "Movies",
                "path": str(self.root / "films"),
                "type": "movie",
                "availability": availability,
                "default_profile": "movie_balanced",
                "policy": {
                    "grouping": "title",
                    "editions": "separate",
                    "extras": extras,
                    "ranking": ranking,
                },
            },
            *(additional_libraries or []),
        ]
        return MediaforceConfig(
            raw={
                "media": {
                    "libraries": libraries,
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
    def _movie_group(config: MediaforceConfig, rel_path: str) -> tuple[str, str, str, str] | None:
        return MovieWorkflowTests._domain_group(config, rel_path, "movie")

    @staticmethod
    def _domain_group(
            config: MediaforceConfig,
            rel_path: str,
            domain: ScopeDomain,
    ) -> tuple[str, str, str, str] | None:
        scope = media_group_scope_for_rel_path(rel_path, library_types=config.library_type_map)
        if scope is None or scope.domain != domain:
            return None
        return scope.group_tuple()

    @staticmethod
    def _insert_item(
            connection: DBClient,
            rel_path: str,
            *,
            size_bytes: int = 1024,
            status: str = "discovered",
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
                fingerprint=f"movie-{rel_path}",
                duration_seconds=7_200.0,
                video_codec="h264",
                audio_summary_json="[]",
                subtitle_summary_json="[]",
                status=status,
                priority_score=50,
                recommendation="priority_encode",
                recommendation_reason="Movie workflow fixture.",
                last_scan_id="movie-scan",
                discovered_at=timestamp,
                last_seen_at=timestamp,
                updated_at=timestamp,
            )
        )
        return int(result.inserted_primary_key[0])

    @staticmethod
    def _insert_stage(connection: DBClient, item_id: int, rel_path: str) -> None:
        timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
        connection.execute(
            staged_artifacts.insert().values(
                library_item_id=item_id,
                staging_path=f"/staging/{rel_path}.mkv",
                staging_size_bytes=512,
                bytes_saved=512,
                validated_at=timestamp,
                updated_at=timestamp,
            )
        )

    @staticmethod
    def _insert_plex_age(connection: DBClient, item_id: int, added_at: str) -> None:
        connection.execute(
            plex_item_metadata.insert().values(
                library_item_id=item_id,
                plex_server_id="server",
                plex_item_rating_key=f"movie-{item_id}",
                plex_part_path=f"/media/{item_id}.mkv",
                plex_added_at=added_at,
                observed_at=added_at,
            )
        )


if __name__ == "__main__":
    unittest.main()
