import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from sqlalchemy import select, update

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_item_source_evidence, library_items, staged_artifacts
from mediaforce.core.models import ProbeSummary
from mediaforce.encoding import staging as staging_module
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EvidenceKind, evidence_summary_column, \
    sync_library_item_evidence_state, sync_library_item_evidence_states
from mediaforce.library.scanner import scan_library
from mediaforce.library.source_evidence import canonical_summary_json, load_source_evidence_summaries, \
    record_source_evidence_before_publish, restore_published_source_evidence

SOURCE_SUMMARIES: dict[EvidenceKind, dict[str, Any]] = {
    CADENCE_EVIDENCE_KIND: {"decision": {"classification": "telecine"}, "schema_version": 1},
    MEDIA_FINGERPRINT_EVIDENCE_KIND: {"analysis": {"aggregate": {"luma_mean": 38.5}}, "schema_version": 1},
}
OUTPUT_SUMMARIES: dict[EvidenceKind, dict[str, Any]] = {
    CADENCE_EVIDENCE_KIND: {"decision": {"classification": "progressive"}, "schema_version": 1},
    MEDIA_FINGERPRINT_EVIDENCE_KIND: {"analysis": {"aggregate": {"luma_mean": 61.0}}, "schema_version": 1},
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


def _store_measurements(connection: DBClient, item_id: int, summaries: dict[EvidenceKind, dict[str, Any]]) -> None:
    """Store measurements of the current file the way the evidence worker commits them."""
    for evidence_kind, summary in summaries.items():
        connection.execute(
            update(library_items)
            .where(library_items.c.id == item_id)
            .values({evidence_summary_column(evidence_kind): canonical_summary_json(summary)})
        )
        item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
        sync_library_item_evidence_state(
            connection, item, evidence_kind, preserve_source_identity=False, preserve_policy_identity=False,
        )


def _publish(connection: DBClient, root: Path, item_id: int) -> Path:
    source = root / "tv" / "Show" / "S01E01.mkv"
    output = root / "staging" / "S01E01.mkv"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"encoded output")
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id,
        staging_path=str(output),
        validation_json=json.dumps({"passed": True}),
        validated_at="2026-10-22T10:00:00+00:00",
        updated_at="2026-10-22T10:00:00+00:00",
    ))
    connection.commit()
    config = SimpleNamespace(
        output_container="mkv",
        archive_root=root / "archive",
        source_root_map={"tv": root / "tv"},
    )
    return staging_module.promote_one_item(
        connection,
        cast(MediaforceConfig, config),
        {"library_item_id": item_id, "source_path": str(source), "rel_path": "tv/Show/S01E01.mkv",
         "media_root": "tv"},
        force=False,
        probe_media=lambda _path: _probe("av1"),
        file_fingerprint=lambda *_args: "output-stat",
        timestamp=lambda: "2026-10-22T10:05:00+00:00",
        record_event=lambda *_args: None,
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


def test_source_measurements_survive_publish_output_measurement_and_rescan(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    episode = tmp_path / "tv" / "Show" / "S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"original source")
    try:
        with open_db(config.paths.db_path) as connection:
            _scan(connection, config, "h264")
            item_id = connection.execute(select(library_items.c.id)).scalar_one()
            _store_measurements(connection, item_id, SOURCE_SUMMARIES)
            connection.commit()
            assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES

            assert _publish(connection, tmp_path, item_id) == episode
            # The evidence worker later measures the output into the catalog row, and the catalog is rescanned.
            _store_measurements(connection, item_id, OUTPUT_SUMMARIES)
            connection.commit()
            _scan(connection, config, "av1")

            assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES
            current = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
            assert {
                evidence_kind: json.loads(current[evidence_summary_column(evidence_kind)])
                for evidence_kind in EVIDENCE_KINDS
            } == OUTPUT_SUMMARIES

            # A new original downloaded over the published copy is a new source with nothing measured yet.
            episode.write_bytes(b"a better download replacing the published copy")
            _scan(connection, config, "h264")
            assert _source_summaries(connection, item_id) == dict.fromkeys(EVIDENCE_KINDS)
    finally:
        reset_engine_cache()


def test_publishing_over_our_own_output_keeps_the_original_source(tmp_path: Path) -> None:
    config = _scan_config(tmp_path)
    episode = tmp_path / "tv" / "Show" / "S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"original source")
    try:
        with open_db(config.paths.db_path) as connection:
            _scan(connection, config, "h264")
            item_id = connection.execute(select(library_items.c.id)).scalar_one()
            _store_measurements(connection, item_id, SOURCE_SUMMARIES)
            _publish(connection, tmp_path, item_id)
            _store_measurements(connection, item_id, OUTPUT_SUMMARIES)

            record_source_evidence_before_publish(
                connection, item_id, published_fingerprint="second-output", recorded_at="2026-10-22T11:00:00+00:00",
            )

            assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES
    finally:
        reset_engine_cache()


def _insert_published_item(
        connection: DBClient,
        item_id: int,
        *,
        stored: dict[EvidenceKind, dict[str, Any]],
        measured_fingerprint: str,
        manifest_path: Path | None = None,
) -> None:
    """A published item whose catalog row holds ``stored``, measured on the file named ``measured_fingerprint``."""
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
        content_version_fingerprint=measured_fingerprint,
        audio_summary_json="[]",
        subtitle_summary_json="[]",
        status="validated",
        last_scan_id="scan-1",
        discovered_at="2026-10-01T00:00:00+00:00",
        last_seen_at="2026-10-01T00:00:00+00:00",
        updated_at="2026-10-01T00:00:00+00:00",
        **{evidence_summary_column(kind): canonical_summary_json(summary) for kind, summary in stored.items()},
    ))
    item = connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one()
    sync_library_item_evidence_states(connection, item, preserve_source_identity=False)
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


def _source_rows(connection: DBClient) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(
        select(library_item_source_evidence).order_by(
            library_item_source_evidence.c.library_item_id, library_item_source_evidence.c.evidence_kind,
        )
    ).mappings()]


def test_restore_recovers_source_measurements_once_from_run_plans_and_the_catalog(tmp_path: Path) -> None:
    plan_path = tmp_path / "runs" / "run-1.json"
    plan_path.parent.mkdir()
    plan_path.write_text(json.dumps({"run_id": "1", "items": [{
        "library_item_id": 1,
        "source_fingerprint": "source-stat-1",
        "content_version_fingerprint": "source-1",
        "cadence_summary": SOURCE_SUMMARIES[CADENCE_EVIDENCE_KIND],
        "media_fingerprint": SOURCE_SUMMARIES[MEDIA_FINGERPRINT_EVIDENCE_KIND],
    }]}))
    try:
        with open_db(tmp_path / "library.sqlite3") as connection:
            # The run plan survives, but the catalog already holds the output's measurements.
            _insert_published_item(connection, 1, stored=OUTPUT_SUMMARIES, measured_fingerprint="output-1",
                                   manifest_path=plan_path)
            # The run plan was pruned, but the catalog row still holds measurements of the source.
            _insert_published_item(connection, 2, stored=SOURCE_SUMMARIES, measured_fingerprint="source-2",
                                   manifest_path=tmp_path / "runs" / "pruned.json")
            # Nothing of the source survives anywhere.
            _insert_published_item(connection, 3, stored=OUTPUT_SUMMARIES, measured_fingerprint="output-3")
            for item_id in (1, 3):
                _store_measurements(connection, item_id, OUTPUT_SUMMARIES)
            connection.commit()

            counted = restore_published_source_evidence(connection, recorded_at="2026-10-22T12:00:00+00:00",
                                                        apply=False)
            assert _source_rows(connection) == []
            restored = restore_published_source_evidence(connection, recorded_at="2026-10-22T12:00:00+00:00")
            connection.commit()
            rows = _source_rows(connection)
            repeated = restore_published_source_evidence(connection, recorded_at="2026-10-22T13:00:00+00:00")

            per_kind = dict.fromkeys(EVIDENCE_KINDS, 1)
            assert counted.to_payload() == restored.to_payload() == {
                "published_item_count": 3,
                "restored_from_run_plan": per_kind,
                "restored_from_catalog": per_kind,
                "already_recorded": {},
                "unrecoverable": per_kind,
            }
            assert repeated.already_recorded == dict.fromkeys(EVIDENCE_KINDS, 2)
            assert repeated.restored_from_run_plan == repeated.restored_from_catalog == {}
            assert _source_rows(connection) == rows
            assert {(row["library_item_id"], row["origin"], row["source_fingerprint"]) for row in rows} == {
                (1, "run_plan", "source-1"),
                (2, "catalog", "source-2"),
            }
            for item_id in (1, 2):
                assert _source_summaries(connection, item_id) == SOURCE_SUMMARIES
            assert _source_summaries(connection, 3) == dict.fromkeys(EVIDENCE_KINDS)
    finally:
        reset_engine_cache()
