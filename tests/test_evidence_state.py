import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, insert, select, update

from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, analyze_cadence, unavailable_cadence_summary
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND, unavailable_media_fingerprint_summary
from mediaforce.library.evidence_state import EVIDENCE_REASON_MALFORMED, EVIDENCE_REASON_MISSING, \
    EVIDENCE_REASON_POLICY_CHANGED, EVIDENCE_REASON_RETRY_REQUIRED, EVIDENCE_REASON_SCHEMA_CHANGED, \
    EVIDENCE_REASON_SOURCE_CHANGED, EVIDENCE_REASON_TOOL_CHANGED, EVIDENCE_REASON_UNKNOWN, \
    EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, \
    load_library_item_evidence_states, project_evidence_state, rebuild_library_item_evidence_states, \
    summarize_evidence_states


class EvidenceStateProjectionTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_engine_cache()

    def test_projection_distinguishes_noncurrent_cadence_states(self) -> None:
        valid = self._cadence_summary()
        old_tool = json.loads(valid)
        old_tool["analysis"]["tool"]["version"] = "0"
        old_schema = json.loads(valid)
        old_schema["schema_version"] = 0
        invalid_shape = json.loads(valid)
        invalid_shape["decision"] = {}
        retry = unavailable_cadence_summary("fixture failure")
        retry["retry_required"] = True
        cases = (
            (None, EVIDENCE_REASON_MISSING),
            ("not-json", EVIDENCE_REASON_MALFORMED),
            (json.dumps(invalid_shape), EVIDENCE_REASON_MALFORMED),
            (json.dumps(old_schema), EVIDENCE_REASON_SCHEMA_CHANGED),
            (json.dumps(old_tool), EVIDENCE_REASON_TOOL_CHANGED),
            (json.dumps(retry), EVIDENCE_REASON_RETRY_REQUIRED),
            (json.dumps(unavailable_cadence_summary("fixture unknown")), EVIDENCE_REASON_UNKNOWN),
        )

        for summary_json, reason in cases:
            with self.subTest(reason=reason):
                projection = project_evidence_state(
                    CADENCE_EVIDENCE_KIND,
                    summary_json,
                    source_fingerprint="content-1",
                )

                self.assertEqual(projection.state, EVIDENCE_STATE_ANALYSIS_REQUIRED)
                self.assertEqual(projection.reason, reason)

        current = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            valid,
            source_fingerprint="content-1",
        )
        self.assertEqual(current.state, EVIDENCE_STATE_CURRENT)
        self.assertIsNone(current.reason)

    def test_cadence_and_fingerprint_states_are_independent(self) -> None:
        cadence = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            self._cadence_summary(),
            source_fingerprint="content-1",
        )
        fingerprint = project_evidence_state(
            MEDIA_FINGERPRINT_EVIDENCE_KIND,
            json.dumps(unavailable_media_fingerprint_summary("fixture unknown")),
            source_fingerprint="content-1",
        )

        self.assertEqual(cadence.state, EVIDENCE_STATE_CURRENT)
        self.assertEqual(fingerprint.state, EVIDENCE_STATE_ANALYSIS_REQUIRED)
        self.assertEqual(fingerprint.reason, EVIDENCE_REASON_UNKNOWN)

    def test_source_and_policy_changes_require_different_work(self) -> None:
        original = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            self._cadence_summary(),
            source_fingerprint="content-1",
            current_policy_hash="sha256:policy-1",
            updated_at="2026-07-19T12:00:00+00:00",
        )
        previous = original.to_values(1)

        source_changed = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            self._cadence_summary(),
            source_fingerprint="content-2",
            previous_state=previous,
            current_policy_hash="sha256:policy-1",
        )
        policy_changed = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            self._cadence_summary(),
            source_fingerprint="content-1",
            previous_state=previous,
            current_policy_hash="sha256:policy-2",
        )

        self.assertEqual(source_changed.state, EVIDENCE_STATE_ANALYSIS_REQUIRED)
        self.assertEqual(source_changed.reason, EVIDENCE_REASON_SOURCE_CHANGED)
        self.assertEqual(source_changed.source_fingerprint, "content-1")
        self.assertEqual(policy_changed.state, EVIDENCE_STATE_CLASSIFICATION_REQUIRED)
        self.assertEqual(policy_changed.reason, EVIDENCE_REASON_POLICY_CHANGED)
        self.assertEqual(policy_changed.policy_hash, "sha256:policy-1")

    def test_summary_hash_tracks_exact_canonical_text(self) -> None:
        compact = self._cadence_summary()
        spaced = f" {compact} "

        first = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            compact,
            source_fingerprint="content-1",
        )
        second = project_evidence_state(
            CADENCE_EVIDENCE_KIND,
            spaced,
            source_fingerprint="content-1",
        )

        self.assertNotEqual(first.summary_sha256, second.summary_sha256)

    def test_rebuild_preserves_identities_and_detects_source_and_policy_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path) as connection:
                library_item_id = int(
                    connection.execute(
                        insert(library_items).values(
                            **self._item_values(
                                1,
                                cadence_summary_json=self._cadence_summary(),
                                media_fingerprint_json=self._fingerprint_summary(),
                            )
                        )
                    ).inserted_primary_key[0]
                )
                rebuild_library_item_evidence_states(connection)
                connection.execute(
                    update(library_item_evidence_state)
                    .where(library_item_evidence_state.c.library_item_id == library_item_id)
                    .values(policy_hash="sha256:old-policy")
                )

                refreshed = rebuild_library_item_evidence_states(connection)
                policy_states = load_library_item_evidence_states(connection, library_item_id)
                connection.execute(
                    update(library_items)
                    .where(library_items.c.id == library_item_id)
                    .values(content_version_fingerprint="content-2")
                )
                source_refresh = rebuild_library_item_evidence_states(connection)
                source_states = load_library_item_evidence_states(connection, library_item_id)

        self.assertEqual(refreshed.classification_required_count, 2)
        self.assertEqual(
            [state["reason"] for state in policy_states.values()],
            [EVIDENCE_REASON_POLICY_CHANGED, EVIDENCE_REASON_POLICY_CHANGED],
        )
        self.assertEqual(source_refresh.analysis_required_count, 2)
        self.assertEqual(
            [state["reason"] for state in source_states.values()],
            [EVIDENCE_REASON_SOURCE_CHANGED, EVIDENCE_REASON_SOURCE_CHANGED],
        )

    def test_rebuild_is_media_free_preserves_retry_state_and_supports_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "library.sqlite3"
            with open_db(db_path) as connection:
                first_id = int(
                    connection.execute(
                        insert(library_items).values(
                            **self._item_values(
                                1,
                                cadence_summary_json=self._cadence_summary(),
                                media_fingerprint_json=self._fingerprint_summary(),
                            )
                        )
                    ).inserted_primary_key[0]
                )
                second_id = int(
                    connection.execute(
                        insert(library_items).values(
                            **self._item_values(
                                2,
                                cadence_summary_json="not-json",
                                media_fingerprint_json=None,
                            )
                        )
                    ).inserted_primary_key[0]
                )
                with patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")), patch(
                    "subprocess.Popen",
                    side_effect=AssertionError("unexpected subprocess"),
                ):
                    first_stats = rebuild_library_item_evidence_states(connection)
                connection.execute(
                    update(library_item_evidence_state)
                    .where(
                        library_item_evidence_state.c.library_item_id == first_id,
                        library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                    )
                    .values(
                        attempt_count=3,
                        retry_not_before="2026-07-20T00:00:00+00:00",
                        last_attempt_at="2026-07-19T12:00:00+00:00",
                        last_error="fixture",
                    )
                )
                second_stats = rebuild_library_item_evidence_states(connection)
                first_states = load_library_item_evidence_states(connection, first_id)
                summary = summarize_evidence_states(connection)
                canonical = connection.execute(
                    select(
                        library_items.c.cadence_summary_json,
                        library_items.c.media_fingerprint_json,
                    ).where(library_items.c.id == first_id)
                ).mappings().one()

                self.assertEqual(first_stats.item_count, 2)
                self.assertEqual(first_stats.row_count, 4)
                self.assertEqual(first_stats.current_count, 2)
                self.assertEqual(first_stats.analysis_required_count, 2)
                self.assertEqual(second_stats, first_stats)
                self.assertEqual(first_states[CADENCE_EVIDENCE_KIND]["attempt_count"], 3)
                self.assertEqual(summary["total"], 4)
                self.assertEqual(canonical["cadence_summary_json"], self._cadence_summary())
                self.assertEqual(canonical["media_fingerprint_json"], self._fingerprint_summary())

                connection.execute(delete(library_items).where(library_items.c.id == second_id))
                remaining = connection.execute(
                    select(library_item_evidence_state.c.library_item_id)
                    .where(library_item_evidence_state.c.library_item_id == second_id)
                ).fetchall()
                self.assertEqual(remaining, [])

    @staticmethod
    def _cadence_summary() -> str:
        summary = analyze_cadence(
            Path("unused.mkv"),
            video_stream={
                "field_order": "progressive",
                "avg_frame_rate": "24/1",
                "r_frame_rate": "24/1",
                "time_base": "1/1000",
            },
            duration_seconds=60.0,
        )
        return json.dumps(summary, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def _fingerprint_summary() -> str:
        return json.dumps(
            {
                "schema_version": 1,
                "analysis": {
                    "sampled_frames": 120,
                    "coverage": 1.0,
                    "ranges": [],
                    "aggregate": {"dark_frame_fraction": 0.05},
                    "audio_probe": {"track_count": 0},
                    "tool": {
                        "name": "mediaforce.media_fingerprint",
                        "version": "1",
                        "ffmpeg_version": "ffmpeg fixture",
                    },
                },
                "decision": {"status": "measured"},
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _item_values(
            index: int,
            *,
            cadence_summary_json: str | None,
            media_fingerprint_json: str | None,
    ) -> dict[str, object]:
        now = "2026-07-19T12:00:00+00:00"
        return {
            "source_path": f"/media/item-{index}.mkv",
            "rel_path": f"tv/show/item-{index}.mkv",
            "media_root": "tv",
            "parent_dir": "tv/show",
            "file_name": f"item-{index}.mkv",
            "container": ".mkv",
            "size_bytes": 1000,
            "mtime_ns": index,
            "fingerprint": f"file-{index}",
            "duration_seconds": 60.0,
            "video_codec": "h264",
            "audio_track_count": 1,
            "subtitle_track_count": 0,
            "english_audio_count": 1,
            "english_subtitle_count": 0,
            "audio_summary_json": "[]",
            "subtitle_summary_json": "[]",
            "cadence_summary_json": cadence_summary_json,
            "media_fingerprint_json": media_fingerprint_json,
            "content_version_changed_at": now,
            "content_version_fingerprint": f"content-{index}",
            "status": "discovered",
            "priority_score": 0,
            "last_scan_id": "fixture",
            "discovered_at": now,
            "last_seen_at": now,
            "updated_at": now,
        }


if __name__ == "__main__":
    unittest.main()
