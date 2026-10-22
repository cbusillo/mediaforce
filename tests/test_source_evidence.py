import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import delete, select, update

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import item_events, library_item_source_evidence, library_items, staged_artifacts
from mediaforce.core.models import ProbeSummary
from mediaforce.encoding import staging as staging_module
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EvidenceKind, evidence_summary_column, \
    library_item_source_fingerprint, sync_library_item_evidence_state, sync_library_item_evidence_states
from mediaforce.library.scanner import scan_library
from mediaforce.library.source_evidence import canonical_summary_json, load_source_evidence_summaries, \
    restore_published_source_evidence

SOURCE_SUMMARIES: dict[EvidenceKind, dict[str, Any]] = {
    CADENCE_EVIDENCE_KIND: {"decision": {"classification": "telecine"}, "schema_version": 1},
    MEDIA_FINGERPRINT_EVIDENCE_KIND: {"analysis": {"aggregate": {"luma_mean": 38.5}}, "schema_version": 1},
}
NO_SUMMARIES: dict[str, None] = dict.fromkeys(EVIDENCE_KINDS)


def _output_summaries(generation: int) -> dict[EvidenceKind, dict[str, Any]]:
    return {
        CADENCE_EVIDENCE_KIND: {"decision": {"classification": f"progressive-{generation}"}, "schema_version": 1},
        MEDIA_FINGERPRINT_EVIDENCE_KIND: {"analysis": {"aggregate": {"luma_mean": 60.0 + generation}},
                                          "schema_version": 1},
    }


def _probe(video_codec: str) -> ProbeSummary:
    return ProbeSummary(
        duration_seconds=2700.0,
        video_codec=video_codec,
        video_bitrate=1000,
        width=1920,
        height=1080,
        pix_fmt="yuv420p10le",
        audio_track_count=0,
        subtitle_track_count=0,
        english_audio_count=0,
        english_subtitle_count=0,
        default_audio_language=None,
        default_subtitle_language=None,
        audio_summary_json="[]",
        subtitle_summary_json="[]",
    )


def _scan_config(root: Path) -> MediaforceConfig:
    paths = ConfigPaths(
        project_root=root,
        config_path=root / "config.toml",
        db_path=root / "library.sqlite3",
        run_manifest_dir=root / "runs",
        web_state_dir=root / "web",
        review_dir=root / "review",
        runtime_settings_path=root / "runtime-settings.json",
    )
    return MediaforceConfig(
        raw={
            "media": {
                "libraries": [
                    {"key": "tv", "label": "TV", "path": str(root / "tv"), "type": "other",
                     "availability": "browse_only"},
                ]
            },
            "video": {}, "audio": {}, "subtitle": {}, "planning": {}, "validation": {},
            "overrides": [], "remote_hosts": [],
        },
        paths=paths,
    )


def _scan(connection: DBClient, config: MediaforceConfig, video_codec: str) -> None:
    with patch("mediaforce.library.scanner.probe_media", return_value=_probe(video_codec)):
        scan_library(connection, config)
    connection.commit()


def _item(connection: DBClient, item_id: int) -> dict[str, Any]:
    return dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())


def _store_measurements(connection: DBClient, item_id: int, summaries: dict[EvidenceKind, dict[str, Any]]) -> None:
    """Store measurements of the current file the way the evidence worker commits them."""
    for evidence_kind, summary in summaries.items():
        connection.execute(
            update(library_items)
            .where(library_items.c.id == item_id)
            .values({evidence_summary_column(evidence_kind): canonical_summary_json(summary)})
        )
        sync_library_item_evidence_state(
            connection, _item(connection, item_id), evidence_kind,
            preserve_source_identity=False, preserve_policy_identity=False,
        )
    connection.commit()


def _record_event(connection: DBClient, item_id: int, event_type: str, details: dict[str, Any]) -> None:
    connection.execute(item_events.insert().values(
        library_item_id=item_id, event_type=event_type, details_json=json.dumps(details),
        created_at="2026-10-22T10:05:00+00:00",
    ))


def _publish(connection: DBClient, root: Path, item_id: int, output_bytes: bytes) -> None:
    """Encode and publish the item's current file, as a first publish or a remake of a published one."""
    output = root / "staging" / "S01E01.mkv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_bytes)
    connection.execute(delete(staged_artifacts).where(staged_artifacts.c.library_item_id == item_id))
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id,
        staging_path=str(output),
        validation_json=json.dumps({"passed": True}),
        validated_at="2026-10-22T10:00:00+00:00",
        updated_at="2026-10-22T10:00:00+00:00",
    ))
    connection.commit()
    config = SimpleNamespace(output_container="mkv", archive_root=root / "archive", source_root_map={"tv": root / "tv"})
    staging_module.promote_one_item(
        connection,
        cast(MediaforceConfig, config),
        {"library_item_id": item_id, "source_path": str(root / "tv" / "Show" / "S01E01.mkv"),
         "rel_path": "tv/Show/S01E01.mkv", "media_root": "tv"},
        force=False,
        probe_media=lambda _path: _probe("av1"),
        file_fingerprint=lambda *_args: f"output-stat-{len(output_bytes)}",
        timestamp=lambda: "2026-10-22T10:05:00+00:00",
        record_event=_record_event,
        active_reserve_waiting_reason=lambda *_args: None,
        reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
            allowed=True, waiting_reason=None, required_by_volume={}
        ),
    )


def _source_summaries(connection: DBClient, item_id: int) -> dict[str, dict[str, Any] | None]:
    return {
        evidence_kind: load_source_evidence_summaries(connection, [item_id], evidence_kind).get(item_id)
        for evidence_kind in EVIDENCE_KINDS
    }


def _source_rows(connection: DBClient) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        select(library_item_source_evidence).order_by(
            library_item_source_evidence.c.library_item_id, library_item_source_evidence.c.evidence_kind,
        )
    ).mappings()]


def _scanned_episode(tmp_path: Path, connection: DBClient, config: MediaforceConfig) -> tuple[Path, int]:
    episode = tmp_path / "tv" / "Show" / "S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"original source")
    _scan(connection, config, "h264")
    return episode, connection.execute(select(library_items.c.id)).scalar_one()


def test_source_measurements_survive_publish_output_measurement_and_rescan(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    try:
        with open_db(config.paths.db_path) as connection:
            episode, item_id = _scanned_episode(tmp_path, connection, config)
            _store_measurements(connection, item_id, SOURCE_SUMMARIES)
            assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES

            _publish(connection, tmp_path, item_id, b"encoded output")
            # The evidence worker later measures the output into the catalog row, and the catalog is rescanned.
            _store_measurements(connection, item_id, _output_summaries(1))
            _scan(connection, config, "av1")

            assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES
            current = _item(connection, item_id)
            assert {
                evidence_kind: json.loads(current[evidence_summary_column(evidence_kind)])
                for evidence_kind in EVIDENCE_KINDS
            } == _output_summaries(1)

            # A new original downloaded over the published copy is a new source with nothing measured yet.
            episode.write_bytes(b"a better download replacing the published copy")
            _scan(connection, config, "h264")
            assert _source_summaries(connection, item_id) == NO_SUMMARIES
    finally:
        reset_engine_cache()


def test_each_publish_over_our_output_keeps_the_source_and_moves_the_output_forward(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    try:
        with open_db(config.paths.db_path) as connection:
            _, item_id = _scanned_episode(tmp_path, connection, config)
            _store_measurements(connection, item_id, SOURCE_SUMMARIES)
            for generation, output_bytes in enumerate((b"output A", b"output B, remade", b"output C, remade again"),
                                                      start=1):
                _publish(connection, tmp_path, item_id, output_bytes)
                _store_measurements(connection, item_id, _output_summaries(generation))
                _scan(connection, config, "av1")

                rows = _source_rows(connection)
                assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES
                assert {row["published_fingerprint"] for row in rows} == {
                    library_item_source_fingerprint(_item(connection, item_id))
                }
    finally:
        reset_engine_cache()


def test_a_publish_over_an_unrecorded_earlier_output_keeps_no_measurements(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    try:
        with open_db(config.paths.db_path) as connection:
            _, item_id = _scanned_episode(tmp_path, connection, config)
            # Published before source measurements were kept: the catalog now holds the output's measurements.
            _record_event(connection, item_id, "promotion_completed", {})
            _store_measurements(connection, item_id, _output_summaries(1))

            _publish(connection, tmp_path, item_id, b"remade output")

            assert _source_summaries(connection, item_id) == NO_SUMMARIES
            assert {row["published_fingerprint"] for row in _source_rows(connection)} == {
                library_item_source_fingerprint(_item(connection, item_id))
            }
    finally:
        reset_engine_cache()


def test_a_publish_over_an_unscanned_new_download_keeps_no_measurements(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    try:
        with open_db(config.paths.db_path) as connection:
            episode, item_id = _scanned_episode(tmp_path, connection, config)
            _store_measurements(connection, item_id, SOURCE_SUMMARIES)
            episode.write_bytes(b"a new download that no scan has seen")

            _publish(connection, tmp_path, item_id, b"encoded output")

            assert _source_summaries(connection, item_id) == NO_SUMMARIES
    finally:
        reset_engine_cache()


def _insert_published_item(
        connection: DBClient,
        item_id: int,
        *,
        stored: dict[EvidenceKind, dict[str, Any]],
        measured_on: str,
        manifest_path: Path | None,
        publish_count: int = 1,
) -> None:
    """A published item whose catalog row holds ``stored``, measured on the file ``measured_on``."""
    connection.execute(library_items.insert().values(
        id=item_id,
        source_path=f"/library/tv/Show/S01E0{item_id}.mkv",
        rel_path=f"tv/Show/S01E0{item_id}.mkv",
        media_root="tv",
        parent_dir="tv/Show",
        file_name=f"S01E0{item_id}.mkv",
        container=".mkv",
        size_bytes=100,
        mtime_ns=1,
        fingerprint=f"source-stat-{item_id}",
        content_version_fingerprint=measured_on,
        audio_summary_json="[]",
        subtitle_summary_json="[]",
        status="validated",
        last_scan_id="scan-1",
        discovered_at="2026-10-01T00:00:00+00:00",
        last_seen_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
        **{evidence_summary_column(kind): canonical_summary_json(summary) for kind, summary in stored.items()},
    ))
    sync_library_item_evidence_states(connection, _item(connection, item_id), preserve_source_identity=False)
    connection.execute(
        update(library_items)
        .where(library_items.c.id == item_id)
        .values(fingerprint=f"output-stat-{item_id}", content_version_fingerprint=f"output-{item_id}",
                status="promoted")
    )
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id,
        manifest_path=str(manifest_path) if manifest_path else None,
        item_index=0,
        source_fingerprint=f"source-stat-{item_id}",
        staging_path=f"/staging/S01E0{item_id}.mkv",
        promoted_at="2026-10-02T00:00:00+00:00",
        updated_at="2026-10-02T00:00:00+00:00",
    ))
    for _ in range(publish_count):
        _record_event(connection, item_id, "promotion_completed", {})


def _run_plan(path: Path, item_id: int, summaries: dict[EvidenceKind, dict[str, Any]]) -> Path:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"run_id": path.stem, "items": [{
        "library_item_id": item_id,
        "source_fingerprint": f"source-stat-{item_id}",
        "content_version_fingerprint": f"source-{item_id}",
        "cadence_summary": summaries[CADENCE_EVIDENCE_KIND],
        "media_fingerprint": summaries[MEDIA_FINGERPRINT_EVIDENCE_KIND],
    }]}))
    return path


def test_restore_keeps_only_source_measurements_proven_by_the_publish_run_plan(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    try:
        with open_db(tmp_path / "library.sqlite3") as connection:
            # Proven: published once, the catalog is still bound to the source, and the run plan encoded it.
            _insert_published_item(connection, 1, stored=SOURCE_SUMMARIES, measured_on="source-1",
                                   manifest_path=_run_plan(runs / "run-1.json", 1, SOURCE_SUMMARIES))
            # The run plan was pruned, so nothing proves the bound file was the original that was encoded.
            _insert_published_item(connection, 2, stored=SOURCE_SUMMARIES, measured_on="source-2",
                                   manifest_path=runs / "pruned.json")
            # The run plan survives, but the catalog already holds the output's measurements.
            _insert_published_item(connection, 3, stored=_output_summaries(1), measured_on="output-3",
                                   manifest_path=_run_plan(runs / "run-3.json", 3, SOURCE_SUMMARIES))
            # The run plan carries other measurements than the ones bound to the encoded file.
            _insert_published_item(connection, 4, stored=SOURCE_SUMMARIES, measured_on="source-4",
                                   manifest_path=_run_plan(runs / "run-4.json", 4, _output_summaries(2)))
            # A remake: the file the last plan encoded may itself have been an earlier output.
            _insert_published_item(connection, 5, stored=SOURCE_SUMMARIES, measured_on="source-5",
                                   manifest_path=_run_plan(runs / "run-5.json", 5, SOURCE_SUMMARIES),
                                   publish_count=2)
            connection.commit()

            counted = restore_published_source_evidence(connection, recorded_at="2026-10-22T12:00:00+00:00",
                                                        apply=False)
            assert _source_rows(connection) == []
            restored = restore_published_source_evidence(connection, recorded_at="2026-10-22T12:00:00+00:00")
            connection.commit()
            rows = _source_rows(connection)
            repeated = restore_published_source_evidence(connection, recorded_at="2026-10-22T13:00:00+00:00")

            assert counted.to_payload() == restored.to_payload() == {
                "published_item_count": 5,
                "restored_from_run_plan": dict.fromkeys(EVIDENCE_KINDS, 1),
                "already_recorded": {},
                "unrecoverable": dict.fromkeys(EVIDENCE_KINDS, {
                    "catalog_not_bound_to_source": 1,
                    "no_run_plan": 1,
                    "published_more_than_once": 1,
                    "run_plan_mismatch": 1,
                }),
            }
            assert repeated.already_recorded == dict.fromkeys(EVIDENCE_KINDS, 1)
            assert repeated.restored_from_run_plan == {}
            assert _source_rows(connection) == rows
            assert {(row["library_item_id"], row["source_fingerprint"], row["published_fingerprint"])
                    for row in rows} == {(1, "source-1", "output-1")}
            assert _source_summaries(connection, 1) == SOURCE_SUMMARIES
            for item_id in (2, 3, 4, 5):
                assert _source_summaries(connection, item_id) == NO_SUMMARIES
    finally:
        reset_engine_cache()
