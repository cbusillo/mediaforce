import json
import shutil
from contextlib import nullcontext
from importlib import import_module
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, inspect, select, update
from sqlalchemy.exc import IntegrityError

from mediaforce import cli
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_migrations import _alembic_config, _alembic_script_location
from mediaforce.core.db_tables import encode_jobs, evidence_queue_state, item_events, library_item_evidence_state, library_items, metadata, motion_keep_decisions, run_manifests, source_admissions, \
    source_generations, source_measurements, source_moves, source_transitions, source_versions, staged_artifacts, target_production_outcomes
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp
from mediaforce.encoding.source_publication import apply_source_moves, path_in_pending_move, prepare_archive_removal, prepare_source_publish, \
    reconcile_source_publish
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.encoding.staging import promote_one_item
from mediaforce.encoding.put_back import PUT_BACK_EVENT, PutBackRefused, PutBackRestoreError, put_original_back, \
    reconcile_unfinished_put_backs
from mediaforce.library.motion_keep_state import library_item_ids_kept_original
from mediaforce.web.runtime.episode_progress import put_back_rel_paths
from mediaforce.library.evidence_acquisition import load_fingerprint_acquisition_items, plan_fingerprint_acquisition
from mediaforce.library.evidence_state import EvidenceKind
from mediaforce.library.source_evidence import append_measurement, ensure_active_source_version, json_text, load_source_evidence, \
    known_output_check, observe_file, record_current_measurement, record_generated_output, source_trait_coverage, version_location
from mediaforce.library.source_evidence_backfill import restore_published_source_evidence
from mediaforce.library.evidence_queue import load_evidence_work_claim
from mediaforce.library.evidence_worker import _commit_evidence_summary
from mediaforce.tuning.content_intent_observations import content_intent_replay_context
from mediaforce.tuning.production_lineage import seal_lineage
from mediaforce.core.evidence import stable_json_hash
from mediaforce.library.run_manifests import select_candidates
from mediaforce.web.runtime.archive_cleanup import clear_archive_cleanup_action


@pytest.fixture
def connection() -> Iterator[DBClient]:
    engine = create_engine("sqlite://")
    with engine.connect() as database:
        database.exec_driver_sql("PRAGMA foreign_keys=ON")
        metadata.create_all(database)
        database.commit()
        yield database
    engine.dispose()


def _config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(raw={"media": {"source_roots": {"tv": str(root / "tv")},
                                           "staging_root": str(root / "staging"), "archive_root": str(root / "archive"),
                                           "output_container": "mkv"}},
                            paths=ConfigPaths(project_root=root, config_path=root / "config.toml", db_path=root / "db.sqlite3",
                                              run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
                                              runtime_settings_path=root / "settings.json"))


def _insert_item(connection: DBClient, path: Path) -> int:
    now = timestamp()
    result = connection.execute(library_items.insert().values(
        source_path=str(path), rel_path="tv/Show/episode.mkv", media_root="tv", parent_dir="tv/Show", file_name=path.name,
        container=".mkv", size_bytes=path.stat().st_size, mtime_ns=path.stat().st_mtime_ns,
        fingerprint=file_fingerprint(path, path.stat(), 60), content_version_fingerprint=content_version_fingerprint(path, path.stat()),
        video_codec="av1", width=640, height=360, duration_seconds=60, audio_summary_json="[]", subtitle_summary_json="[]",
        last_scan_id="fixture", discovered_at=now, last_seen_at=now, updated_at=now,
    ))
    assert result.inserted_primary_key is not None
    return int(result.inserted_primary_key[0])


def _history(connection: DBClient, root: Path, *, codec: str = "h264", removed: bool = True,
             fingerprint: bool = True) -> dict[str, Any]:
    original = root / "original.mkv"
    original.write_bytes(b"source material" * 100)
    sampled = content_version_fingerprint(original, original.stat())
    path = root / "tv/Show/episode.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"output A" * 10)
    item_id = _insert_item(connection, path)
    archive = root / "archive/tv/Show/episode.mkv"
    original_size = original.stat().st_size
    original.unlink()
    plan_item = {"library_item_id": item_id, "source_path": str(path), "rel_path": "tv/Show/episode.mkv",
                 "source_fingerprint": "path-based-identity", "source_size_bytes": original_size,
                 "content_version_fingerprint": sampled, "video_codec": codec, "width": 1920, "height": 1080,
                 "duration_seconds": 60, "audio_summary": [],
                 "cadence_summary": {"schema_version": 1, "decision": {"status": "measured", "classification": "progressive"}}}
    if fingerprint:
        plan_item["media_fingerprint"] = {"schema_version": 1, "decision": {"status": "measured", "traits": ["grain"]}}
    plan_path = root / "plan.json"
    plan = {"run_id": "run-1", "items": [plan_item]}
    plan_path.write_text(json_text(plan))
    now = timestamp()
    connection.execute(run_manifests.insert().values(run_id="run-1", created_at=now, output_path=str(plan_path), selection_json="{}", item_count=1))
    connection.execute(encode_jobs.insert().values(job_id="job-1", prefix="tv/Show", status="completed", manifest_path=str(plan_path),
                                                   manifest_indexes_json="[0]", host_json="{}", created_at=now, updated_at=now))
    stage = {"library_item_id": item_id, "manifest_run_id": "run-1", "manifest_path": str(plan_path), "item_index": 0,
             "encode_job_id": "job-1", "source_path": str(path), "source_size_bytes": original_size,
             "source_video_codec": codec, "source_fingerprint": "path-based-identity", "encode_completed_at": now,
             "staging_path": str(root / "staging/episode.mkv"), "staging_size_bytes": path.stat().st_size,
             "promoted_at": now, "promoted_path": str(path), "archived_source_path": str(archive),
             "encode_command_json": '["ffmpeg","-c:v","libsvtav1"]', "updated_at": now}
    connection.execute(staged_artifacts.insert().values(**stage))
    event = {"manifest": str(plan_path), "item_index": 0, "encode_job_id": "job-1", "source_size_bytes": original_size,
             "source_path": str(path), "source_video_codec": codec, "staging_path": stage["staging_path"],
             "staging_size_bytes": stage["staging_size_bytes"], "encode_completed_at": now}
    connection.execute(item_events.insert().values(library_item_id=item_id, event_type="encoding_completed", details_json=json_text(event), created_at=now))
    if removed:
        connection.execute(item_events.insert().values(library_item_id=item_id, event_type="originals_removed_confirmed",
                                                       details_json=json_text({"archived_source_path": str(archive)}), created_at=now))
    connection.commit()
    return {"id": item_id, "path": path, "archive": archive, "plan": plan, "plan_path": plan_path, "stage": stage}


@pytest.mark.parametrize("codec", ["h264", "hevc"])
@pytest.mark.parametrize("fingerprint", [True, False])
def test_removed_source_recovers_without_media_reads_and_never_rebinds(connection: DBClient, tmp_path: Path,
                                                                      codec: str, fingerprint: bool) -> None:
    history = _history(connection, tmp_path, codec=codec, fingerprint=fingerprint)
    before = connection.execute(select(func.count()).select_from(source_measurements)).scalar_one()
    with patch("mediaforce.library.source_evidence.observe_file", side_effect=AssertionError("historical recovery cannot read media")):
        stats = restore_published_source_evidence(connection)
        assert stats.historical_probable["cadence_analysis"] == 1
        assert connection.execute(select(func.count()).select_from(source_measurements)).scalar_one() == before
        restore_published_source_evidence(connection, apply=True)
    record = load_source_evidence(connection, history["id"])
    assert record.traits["cadence_analysis"].tier == "probable"
    assert record.traits["cadence_analysis"].basis == "historical-plan"
    assert record.traits["cadence_analysis"].binding == "historical-plan-sampled"
    assert record.traits["media_fingerprint"].tier == ("probable" if fingerprint else "unknown")
    assert record.profile["width"] == 1920
    assert record.availability == "unavailable"
    history["archive"].parent.mkdir(parents=True)
    history["archive"].write_bytes(b"source material" * 100)
    assert load_source_evidence(connection, history["id"]).traits == record.traits
    measurements = connection.execute(select(source_measurements)).mappings().all()
    count = len(measurements)
    restore_published_source_evidence(connection, apply=True)
    assert connection.execute(select(func.count()).select_from(source_measurements)).scalar_one() == count
    assert all(row["measured_at"] is None for row in measurements)
    connection.execute(update(library_items).values(cadence_summary_json='{"output":true}', media_fingerprint_json='{"output":true}'))
    assert load_source_evidence(connection, history["id"]).traits == record.traits
    source = connection.execute(select(source_versions).where(source_versions.c.version_id == record.original_version_id)).mappings().one()
    assert source["sha256"] is None
    frozen = json.loads(next(row["provenance_json"] for row in measurements if row["binding"] == "historical-plan-sampled"))
    assert frozen["plan_sha256"]
    history["plan"]["items"][0]["cadence_summary"] = {"different": True}
    history["plan_path"].write_text(json_text(history["plan"]))
    assert load_source_evidence(connection, history["id"]).traits == record.traits
    restore_published_source_evidence(connection, apply=True)
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == "unknown"
    assert connection.execute(select(source_measurements.c.summary_json).where(
        source_measurements.c.measurement_id == record.traits["cadence_analysis"].measurement_ids[0],
    )).scalar_one() == json_text({"schema_version": 1, "decision": {"status": "measured", "classification": "progressive"}})


@pytest.mark.parametrize("damage", ["run", "index", "job", "completion", "output", "source_size", "source_fingerprint",
                                    "av1", "unknown_codec", "missing_sample", "stamp", "conflict", "corrupt", "missing_required",
                                    "conflicting_event", "nonfinite"])
def test_historical_recovery_requires_consistent_complete_edge(connection: DBClient, tmp_path: Path, damage: str) -> None:
    history = _history(connection, tmp_path)
    plan = history["plan"]
    if damage in {"run", "index", "job", "output", "source_size", "source_fingerprint"}:
        fields = {"run": ("manifest_run_id", "other"), "index": ("item_index", 4), "job": ("encode_job_id", "other"),
                  "output": ("staging_size_bytes", 2), "source_size": ("source_size_bytes", 2),
                  "source_fingerprint": ("source_fingerprint", "other")}
        key, value = fields[damage]
        connection.execute(update(staged_artifacts).values({key: value}))
    elif damage == "completion":
        connection.execute(update(item_events).where(item_events.c.event_type == "encoding_completed").values(details_json='{}'))
    elif damage in {"av1", "unknown_codec", "missing_sample", "stamp"}:
        item = plan["items"][0]
        if damage in {"av1", "unknown_codec"}:
            item["video_codec"] = "av1" if damage == "av1" else None
        elif damage == "missing_sample":
            item.pop("content_version_fingerprint")
        else:
            item["probe"] = {"streams": [{"tags": {"MEDIAFORCE_ENCODED_BY": ""}}]}
        history["plan_path"].write_text(json_text(plan))
    elif damage == "conflict":
        other = {**plan, "items": [{**plan["items"][0], "content_version_fingerprint": "b" * 40}]}
        path = tmp_path / "other.json"
        path.write_text(json_text(other))
        connection.execute(run_manifests.insert().values(run_id="other", created_at=timestamp(), output_path=str(path), selection_json="{}", item_count=1))
    elif damage == "corrupt":
        history["plan_path"].write_text("{")
    elif damage == "nonfinite":
        plan["items"][0]["cadence_summary"]["analysis"] = {"score": float("nan")}
        history["plan_path"].write_text(json.dumps(plan))
    elif damage == "conflicting_event":
        connection.execute(item_events.insert().values(library_item_id=history["id"], event_type="encoding_started",
                                                       details_json='{"source_size_bytes":2,"source_video_codec":"hevc"}',
                                                       created_at=timestamp()))
    else:
        history["plan_path"].unlink()
    assert restore_published_source_evidence(connection).historical_probable["cadence_analysis"] == 0


def test_missing_unrelated_plan_and_duplicate_sources_do_not_veto(connection: DBClient, tmp_path: Path) -> None:
    history = _history(connection, tmp_path)
    for name in ("missing", "duplicate"):
        path = tmp_path / f"{name}.json"
        if name == "duplicate":
            path.write_text(json_text({**history["plan"], "run_id": name}))
        connection.execute(run_manifests.insert().values(run_id=name, output_path=str(path), created_at=timestamp(), selection_json="{}", item_count=1))
    stats = restore_published_source_evidence(connection, apply=True)
    assert stats.historical_probable["cadence_analysis"] == 1
    assert str(tmp_path / "missing.json") in stats.plan_gaps
    source = load_source_evidence(connection, history["id"])
    assert source_trait_coverage([source, source])["cadence_analysis"]["total"] == 1


def test_publication_is_not_unavailability_and_legacy_candidates_are_unknown(connection: DBClient, tmp_path: Path) -> None:
    history = _history(connection, tmp_path, removed=False)
    stats = restore_published_source_evidence(connection, apply=True)
    assert stats.unknown["cadence_analysis"]["source_unavailability_not_established"] == 1
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].summary is None
    assert {row.binding for row in connection.execute(select(source_measurements.c.binding))} == {"unbound"}
    stats = restore_published_source_evidence(connection, apply=True, unavailable_sources={history["id"]: "archive share unavailable"})
    assert stats.historical_probable["cadence_analysis"] == 1


@pytest.mark.parametrize("output,expected", [({"size_bytes": 2}, "clear"), ({"video_codec": "av1"}, "clear"),
                                            ({}, "unresolved"), ({"size_bytes": 10, "video_codec": "h264"}, "unresolved"),
                                            ({"sha256": "different"}, "clear"),
                                            ({"sha256": "source", "size_bytes": 2, "video_codec": "av1"}, "match")])
def test_known_output_digest_gaps_respect_size_or_codec_exclusion(output: dict[str, Any], expected: str) -> None:
    assert known_output_check({"sha256": "source", "size_bytes": 10, "video_codec": "h264"}, [output]) == expected


def _probe() -> SimpleNamespace:
    return SimpleNamespace(duration_seconds=60, video_codec="av1", video_bitrate=400, width=640, height=360, pix_fmt="yuv420p10le",
                           audio_track_count=1, subtitle_track_count=0, english_audio_count=1, english_subtitle_count=0,
                           default_audio_language="eng", default_subtitle_language=None, audio_summary_json="[]",
                           subtitle_summary_json="[]", attachment_summary_json="[]")


def test_real_publishes_preserve_source_measurements_and_backup_locations(connection: DBClient, tmp_path: Path) -> None:
    config = _config(tmp_path)
    source_path = tmp_path / "tv/Show/episode.mkv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"S" * 1600)
    item_id = _insert_item(connection, source_path)
    original_id = ensure_active_source_version(connection, connection.execute(select(library_items)).mappings().one())
    source_summaries: dict[EvidenceKind, dict[str, Any]] = {"cadence_analysis": {"decision": {"classification": "source cadence"}},
                        "media_fingerprint": {"decision": {"traits": ["source grain"]}}}
    for kind, summary in source_summaries.items():
        append_measurement(connection, version_id=original_id, kind=kind, binding="observed", summary=summary,
                           provenance={"observation": observe_file(source_path)}, measured_at=timestamp(), recorded_at=timestamp())
    active_versions = []
    for output in (b"A" * 800, b"B" * 400, b"C" * 200):
        staging = tmp_path / "staging/episode.mkv"
        staging.parent.mkdir(exist_ok=True)
        staging.write_bytes(output)
        connection.execute(staged_artifacts.delete())
        connection.execute(staged_artifacts.insert().values(library_item_id=item_id, staging_path=str(staging),
                                                           validation_json='{"passed":true}', validated_at=timestamp(), updated_at=timestamp()))
        connection.commit()
        promote_one_item(connection, config, {"library_item_id": item_id, "source_path": str(source_path),
                                             "rel_path": "tv/Show/episode.mkv", "media_root": "tv"}, force=False,
                         probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                         record_event=lambda *_args: None, reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                             allowed=True, waiting_reason=None, required_by_volume={}))
        item = connection.execute(select(library_items)).mappings().one()
        active_versions.append(item["active_source_version_id"])
        for kind in source_summaries:
            record_current_measurement(connection, item, kind=kind, summary={"output": output.decode()[0]},
                                       provenance={"observation": observe_file(source_path)}, binding="observed")
        connection.commit()
        assert ensure_active_source_version(connection, item) == active_versions[-1]
    assert len(set(active_versions)) == 3
    original_records = connection.execute(select(source_measurements).where(source_measurements.c.version_id == original_id)).mappings().all()
    assert {row["kind"]: json.loads(row["summary_json"]) for row in original_records} == source_summaries
    first_publish = connection.execute(select(source_transitions).where(source_transitions.c.kind == "publish")
                                       .order_by(source_transitions.c.expected_revision)).mappings().first()
    assert first_publish is not None
    source = connection.execute(select(source_versions).where(source_versions.c.version_id == first_publish["before_version_id"])).mappings().one()
    source_location = version_location(connection, source)
    assert source_location is not None
    location = Path(source_location)
    assert "promotion-backup" in location.name
    assert location.read_bytes() == b"S" * 1600
    assert (config.archive_root / "tv/Show/episode.mkv").read_bytes() == b"B" * 400
    assert load_source_evidence(connection, item_id).traits["cadence_analysis"].tier == "unknown"


@pytest.mark.parametrize("fingerprint", [True, False])
def test_remakes_and_output_analysis_preserve_historical_original_reads(connection: DBClient, tmp_path: Path,
                                                                      fingerprint: bool) -> None:
    history = _history(connection, tmp_path, fingerprint=fingerprint)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    versions = [original.active_version_id]
    for label in ("B", "C"):
        staged = tmp_path / f"output-{label}.mkv"
        staged.write_bytes(label.encode() * 20)
        connection.execute(update(staged_artifacts).values(staging_path=str(staged), promoted_at=None,
                                                           validated_at=timestamp(), validation_json='{"passed":true}'))
        assert load_source_evidence(connection, history["id"]).traits == original.traits
        promote_one_item(connection, _config(tmp_path), {"library_item_id": history["id"], "source_path": str(history["path"]),
                                                         "rel_path": "tv/Show/episode.mkv", "media_root": "tv"}, force=False,
                         probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                         record_event=lambda *_args: None, reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                             allowed=True, waiting_reason=None, required_by_volume={}))
        item = connection.execute(select(library_items)).mappings().one()
        for kind in original.traits:
            record_current_measurement(connection, item, kind=kind, summary={"output": label},
                                       provenance={"observation": observe_file(history["path"])}, binding="observed")
        result = load_source_evidence(connection, history["id"])
        assert result.traits == original.traits
        assert result.original_version_id == original.original_version_id
        versions.append(result.active_version_id)
    assert len(set(versions)) == 3


@pytest.mark.parametrize("boundary", range(7))
def test_journal_recovers_crash_before_and_after_each_move(connection: DBClient, tmp_path: Path, boundary: int) -> None:
    source, archive, staging, backup = [tmp_path / name for name in ("source", "archive", "staging", ".backup")]
    source.write_bytes(b"A" * 20)
    archive.write_bytes(b"S" * 40)
    staging.write_bytes(b"B" * 10)
    item_id = _insert_item(connection, source)
    connection.commit()
    operation = prepare_source_publish(connection, item_id, source_path=source, staging_path=staging, archive_path=archive,
                                       destination_path=source, backup_path=backup)
    prepared = dict(connection.execute(select(source_transitions).where(source_transitions.c.operation_id == operation)).mappings().one())
    with pytest.raises(IntegrityError):
        connection.execute(source_transitions.insert().values(**{**prepared, "operation_id": "second-pending"}))
    connection.rollback()
    move = shutil.move
    calls = 0

    def interrupted_move(old: str, new: str) -> str:
        nonlocal calls
        if calls * 2 == boundary:
            raise KeyboardInterrupt()
        result = move(old, new)
        calls += 1
        if calls * 2 - 1 == boundary:
            raise KeyboardInterrupt()
        return result

    try:
        with patch("mediaforce.encoding.source_publication.shutil.move", side_effect=interrupted_move):
            apply_source_moves(connection, operation)
    except KeyboardInterrupt:
        pass
    connection.rollback()
    assert reconcile_source_publish(connection, operation)
    assert source.read_bytes() == b"A" * 20
    assert archive.read_bytes() == b"S" * 40
    assert staging.read_bytes() == b"B" * 10
    assert not backup.exists()


@pytest.mark.parametrize("problem", ["missing", "ambiguous"])
def test_journal_retains_unresolved_locations(connection: DBClient, tmp_path: Path, problem: str) -> None:
    source, archive, staging, backup = [tmp_path / name for name in ("source", "archive", "staging", ".backup")]
    source.write_bytes(b"A" * 20)
    archive.write_bytes(b"S" * 40)
    staging.write_bytes(b"B" * 10)
    item_id = _insert_item(connection, source)
    operation = prepare_source_publish(connection, item_id, source_path=source, staging_path=staging, archive_path=archive,
                                       destination_path=source, backup_path=backup)
    if problem == "missing":
        apply_source_moves(connection, operation)
        backup.unlink()
    else:
        shutil.copyfile(archive, backup)
    assert not reconcile_source_publish(connection, operation)
    assert source.read_bytes() == b"A" * 20
    assert staging.read_bytes() == b"B" * 10
    assert connection.execute(select(source_transitions.c.outcome).where(source_transitions.c.operation_id == operation)).scalar_one() == "unresolved"


@pytest.mark.parametrize("after_commit", [False, True])
def test_publish_commit_failure_reconciles_durable_outcome(connection: DBClient, tmp_path: Path, after_commit: bool) -> None:
    source = tmp_path / "tv/Show/episode.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"S" * 40)
    staged = tmp_path / "staging.mkv"
    staged.write_bytes(b"A" * 20)
    item_id = _insert_item(connection, source)
    connection.execute(staged_artifacts.insert().values(library_item_id=item_id, staging_path=str(staged),
                                                       validation_json='{"passed":true}', validated_at=timestamp(), updated_at=timestamp()))
    commit = connection.commit
    failed = False

    def interrupted_commit() -> None:
        nonlocal failed
        final = connection.execute(select(source_transitions.c.operation_id).where(
            source_transitions.c.kind == "publish", source_transitions.c.outcome == "committed",
        )).first()
        if final and not failed:
            failed = True
            if after_commit:
                commit()
            raise RuntimeError("interrupted final commit")
        commit()

    item = {"library_item_id": item_id, "source_path": str(source), "rel_path": "tv/Show/episode.mkv", "media_root": "tv"}
    kwargs = {"force": False, "probe_media": lambda _path: _probe(), "file_fingerprint": file_fingerprint,
              "timestamp": timestamp, "record_event": lambda *_args: None,
              "reserve_preflight": lambda *_args, **_kwargs: ReservePreflight(allowed=True, waiting_reason=None, required_by_volume={})}
    with patch.object(connection, "commit", side_effect=interrupted_commit), pytest.raises(RuntimeError, match="final commit"):
        promote_one_item(connection, _config(tmp_path), item, **kwargs)
    assert source.read_bytes() == (b"A" * 20 if after_commit else b"S" * 40)
    assert staged.exists() is not after_commit
    if after_commit:
        assert promote_one_item(connection, _config(tmp_path), item, **kwargs) == source
        assert (tmp_path / "archive/tv/Show/episode.mkv").read_bytes() == b"S" * 40


def test_catalog_removal_preserves_receipts_and_reused_id_inherits_nothing(connection: DBClient, tmp_path: Path) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    before = {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
              for table in (source_generations, source_versions, source_admissions, source_measurements)}
    connection.execute(library_items.delete().where(library_items.c.id == history["id"]))
    assert before == {table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                      for table in (source_generations, source_versions, source_admissions, source_measurements)}
    assert connection.execute(select(source_transitions.c.library_item_id)).scalar_one() is None
    new_id = _insert_item(connection, history["path"])
    assert load_source_evidence(connection, new_id).generation_id is None


def test_forged_admission_never_upgrades_history(connection: DBClient, tmp_path: Path) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    connection.execute(source_admissions.insert().values(admission_id="forged", generation_id=original.generation_id,
                                                        version_id=original.original_version_id, tier="proven", basis="owner",
                                                        confirmation_ref="forged receipt", checks_json="{}", recorded_at=timestamp()))
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == "unknown"


def test_grouping_uses_probable_sources_and_own_technical_profile(connection: DBClient, tmp_path: Path) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    _scope, items, measured = load_fingerprint_acquisition_items(connection, _config(tmp_path), "tv/Show")
    assert len(items) == 1
    assert items[0]["width"] == 1920
    assert items[0]["media_fingerprint_decision"]["traits"] == ["grain"]
    coverage = plan_fingerprint_acquisition(items, measured_source_ids=measured).coverage
    assert coverage.source_traits["media_fingerprint"]["probable:historical-plan"] == 1
    assert coverage.source_traits["media_fingerprint"]["proven"] == 0
    with pytest.raises(ValueError, match="requires"):
        content_intent_replay_context({**items[0], "content_version_fingerprint": items[0]["source_fingerprint"]})
    assert connection.execute(select(staged_artifacts.c.promoted_at)).scalar_one() == history["stage"]["promoted_at"]


def _remake_history(connection: DBClient, root: Path, history: dict[str, Any]) -> None:
    item = connection.execute(select(library_items).where(library_items.c.id == history["id"])).mappings().one()
    plan_item = {**history["plan"]["items"][0], "source_size_bytes": item["size_bytes"],
                 "content_version_fingerprint": item["content_version_fingerprint"], "source_fingerprint": item["fingerprint"],
                 "video_codec": "av1", "cadence_summary": {"output": "A"}, "media_fingerprint": {"output": "A"}}
    path = root / "remake.json"
    path.write_text(json_text({"run_id": "remake", "items": [plan_item]}))
    now = timestamp()
    staged = root / "output-B.mkv"
    staged.write_bytes(b"output B")
    connection.execute(encode_jobs.insert().values(job_id="remake", prefix="tv/Show", status="completed", manifest_path=str(path),
                                                   host_json="{}", created_at=now, updated_at=now))
    stage = {**history["stage"], "manifest_run_id": "remake", "manifest_path": str(path), "encode_job_id": "remake",
             "source_size_bytes": item["size_bytes"], "source_fingerprint": item["fingerprint"], "source_video_codec": "av1",
             "staging_path": str(staged), "staging_size_bytes": staged.stat().st_size, "encode_completed_at": now,
             "promoted_at": None, "validated_at": now, "validation_json": '{"passed":true}'}
    connection.execute(update(staged_artifacts).where(staged_artifacts.c.library_item_id == history["id"]).values(**stage))
    connection.execute(item_events.insert().values(library_item_id=history["id"], event_type="encoding_completed", created_at=now,
        details_json=json_text({"manifest": str(path), "item_index": 0, "encode_job_id": "remake", "source_path": str(history["path"]),
                               "source_size_bytes": item["size_bytes"], "source_video_codec": "av1", "staging_path": str(staged),
                               "staging_size_bytes": staged.stat().st_size, "encode_completed_at": now})))
    promote_one_item(connection, _config(root), {"library_item_id": history["id"], "source_path": str(history["path"]),
                                               "rel_path": "tv/Show/episode.mkv", "media_root": "tv"}, force=False,
                     probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                     record_event=lambda *_args: None, reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                         allowed=True, waiting_reason=None, required_by_volume={}))


@pytest.mark.parametrize("later_evidence", ["published_remake", "different_summary", "operation_input", "possible_output"])
def test_restore_rechecks_admitted_source_instead_of_later_inputs(connection: DBClient, tmp_path: Path, later_evidence: str) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    admission = connection.execute(select(source_admissions)).mappings().one()
    output_a = connection.execute(select(library_items)).mappings().one()
    _remake_history(connection, tmp_path, history)
    if later_evidence == "different_summary":
        later = json.loads((tmp_path / "remake.json").read_text())
        later["items"][0]["cadence_summary"] = {"different": "output A"}
        (tmp_path / "remake.json").write_text(json_text(later))
    elif later_evidence == "operation_input":
        connection.execute(item_events.insert().values(library_item_id=history["id"], event_type="encoding_started",
            created_at=timestamp(), details_json=json_text({"manifest": str(tmp_path / "remake.json"), "item_index": 0,
                                                           "encode_job_id": "remake", "source_size_bytes": 80, "source_video_codec": "av1"})))
    elif later_evidence == "possible_output":
        connection.execute(source_versions.insert().values(version_id="A-again", generation_id=original.generation_id,
            role="output", sample_fingerprint=output_a["content_version_fingerprint"], size_bytes=output_a["size_bytes"],
            video_codec="av1", identity_json="{}", profile_json="{}", observed_at=timestamp()))
    assert connection.execute(select(func.count()).select_from(target_production_outcomes)).scalar_one() == 0
    stats = restore_published_source_evidence(connection, apply=True)
    assert stats.historical_probable["cadence_analysis"] == 1
    assert load_source_evidence(connection, history["id"]).traits == original.traits
    assert connection.execute(select(source_admissions)).mappings().all() == [admission]
    restore_published_source_evidence(connection, apply=True)
    assert load_source_evidence(connection, history["id"]).original_version_id == original.original_version_id


@pytest.mark.parametrize("contradiction,reason", [("sampled_output", "possible_output_match"),
                                                ("source_operation", "conflicting_operation_input"),
                                                ("source_plan", "historical_plan_changed")])
def test_real_source_conflict_after_remake_appends_one_correction(connection: DBClient, tmp_path: Path,
                                                               contradiction: str, reason: str) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    admission = connection.execute(select(source_admissions)).mappings().one()
    measurements = connection.execute(select(source_measurements)).mappings().all()
    _remake_history(connection, tmp_path, history)
    if contradiction == "sampled_output":
        source = history["plan"]["items"][0]
        connection.execute(source_versions.insert().values(version_id="matching-output", generation_id=original.generation_id,
            role="output", sample_fingerprint=source["content_version_fingerprint"], size_bytes=source["source_size_bytes"],
            video_codec=source["video_codec"], identity_json="{}", profile_json="{}", observed_at=timestamp()))
    elif contradiction == "source_operation":
        connection.execute(item_events.insert().values(library_item_id=history["id"], event_type="encoding_started",
            created_at=timestamp(), details_json=json_text({"manifest": str(history["plan_path"]), "item_index": 0,
                                                           "encode_job_id": "job-1", "source_size_bytes": 2, "source_video_codec": "hevc"})))
    else:
        history["plan"]["items"][0]["cadence_summary"] = {"changed": "S"}
        history["plan_path"].write_text(json_text(history["plan"]))
    restore_published_source_evidence(connection, apply=True)
    correction = connection.execute(select(source_admissions).where(source_admissions.c.supersedes_id == admission["admission_id"])).mappings().one()
    assert correction["tier"] == "unknown"
    assert correction["version_id"] == original.original_version_id
    assert json.loads(correction["checks_json"])["reason"] == reason
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == "unknown"
    assert all(row in connection.execute(select(source_measurements)).mappings().all() for row in measurements)
    restore_published_source_evidence(connection, apply=True)
    assert connection.execute(select(source_admissions)).mappings().all() == [admission, correction]


@pytest.mark.parametrize("admission", ["none", "correction", "missing_fingerprint"])
def test_unpublished_acquisition_keeps_current_profile_and_measurements(connection: DBClient, tmp_path: Path, admission: str) -> None:
    from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_SCHEMA_VERSION, MEDIA_FINGERPRINT_TOOL_NAME, MEDIA_FINGERPRINT_TOOL_VERSION
    if admission == "none":
        path = tmp_path / "source.mkv"
        path.write_bytes(b"unpublished source")
        item_id = _insert_item(connection, path)
    else:
        history = _history(connection, tmp_path, fingerprint=admission != "missing_fingerprint")
        path, item_id = history["path"], history["id"]
        history["plan"]["items"][0]["cadence_summary"]["decision"]["classification"] = "tff"
        history["plan_path"].write_text(json_text(history["plan"]))
        restore_published_source_evidence(connection, apply=True)
        if admission == "correction":
            original = load_source_evidence(connection, item_id)
            source = history["plan"]["items"][0]
            connection.execute(source_versions.insert().values(version_id="contrary-output", generation_id=original.generation_id,
                role="output", sample_fingerprint=source["content_version_fingerprint"], size_bytes=source["source_size_bytes"],
                video_codec=source["video_codec"], identity_json="{}", profile_json="{}", observed_at=timestamp()))
            restore_published_source_evidence(connection, apply=True)
            assert connection.execute(select(source_admissions.c.tier).where(
                source_admissions.c.supersedes_id.is_not(None))).scalar_one() == "unknown"
    summary = {"schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
               "analysis": {"sampled_frames": 120, "aggregate": {"grain": 0.5},
                            "tool": {"name": MEDIA_FINGERPRINT_TOOL_NAME, "version": MEDIA_FINGERPRINT_TOOL_VERSION}},
               "decision": {"status": "measured", "traits": ["grain"]}}
    from mediaforce.encoding.cadence import analyze_cadence
    cadence = analyze_cadence(path, video_stream={"field_order": "progressive", "avg_frame_rate": "24/1",
                                               "r_frame_rate": "24/1", "time_base": "1/1000"}, duration_seconds=60)
    connection.execute(update(library_items).where(library_items.c.id == item_id).values(
        video_codec="h264", width=1280, height=720, media_fingerprint_json=json_text(summary),
        cadence_summary_json=json_text(cadence),
        audio_summary_json='[{"codec":"aac"}]'))
    _scope, items, measured = load_fingerprint_acquisition_items(connection, _config(tmp_path), "tv/Show")
    assert len(items) == 1
    item = items[0]
    assert (item["video_codec"], item["width"], item["height"], item["source_size_bytes"], item["duration_seconds"]) == (
        "h264", 1280, 720, path.stat().st_size, 60)
    assert item["cadence_class"] == cadence["decision"]["classification"]
    assert item["trait_use"] == "direct-input"
    assert item["source_fingerprint"] == content_version_fingerprint(path, path.stat())
    assert item["audio_summary"] == [{"codec": "aac"}]
    assert item["media_fingerprint_decision"] == summary["decision"]
    plan = plan_fingerprint_acquisition(items, measured_source_ids=measured)
    assert plan.coverage.measured_item_count == 1
    assert plan.candidate_ids == ()
    assert load_source_evidence(connection, item_id).traits["media_fingerprint"].tier == "unknown"


@pytest.mark.parametrize("encoder", ["libsvtav1", "av1_videotoolbox", "libaom-av1", "librav1e", "av1"])
@pytest.mark.parametrize("record", ["stage", "receipt", "version"])
def test_all_av1_encoders_preserve_historical_rule(connection: DBClient, tmp_path: Path, encoder: str, record: str) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    command = json_text(["ffmpeg", "-c:v", encoder])
    if record == "stage":
        connection.execute(update(staged_artifacts).values(encode_command_json=command))
    elif record == "receipt":
        payload = {"encode_command_json": command}
        connection.execute(target_production_outcomes.insert().values(
            receipt_id="output", library_item_id=history["id"], observation_id="observation", manifest_run_id="run",
            item_index=0, encode_job_id="job", promoted_content_fingerprint="f" * 40,
            payload_json=json_text(payload), payload_sha256=stable_json_hash(payload), recorded_at=timestamp()))
    else:
        record_generated_output(connection, history["id"], identity={"size_bytes": 8, "sample_fingerprint": "f" * 40},
                                profile={"video_codec": encoder}, operation_key={"test": encoder}, recorded_at=timestamp())
    assert load_source_evidence(connection, history["id"]).traits == original.traits
    restore_published_source_evidence(connection, apply=True)
    assert connection.execute(select(source_admissions.c.tier)).scalars().all() == ["probable"]


@pytest.mark.parametrize("input_codec", ["h264", "hevc"])
@pytest.mark.parametrize("output_kind", ["library", "fixture", "sample"])
def test_non_av1_rule_only_invalidates_affected_library_assessments(connection: DBClient, tmp_path: Path,
                                                                 input_codec: str, output_kind: str) -> None:
    history = _history(connection, tmp_path, codec=input_codec)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    admission = connection.execute(select(source_admissions)).mappings().one()
    if output_kind == "library":
        record_generated_output(connection, history["id"], identity={"size_bytes": 5, "sample_fingerprint": "f" * 40},
                                profile={"video_codec": "hevc"}, operation_key={"run": "non-av1"}, recorded_at=timestamp())
    elif output_kind == "fixture":
        connection.execute(source_versions.insert().values(version_id="fixture", generation_id=original.generation_id,
            role="output", video_codec="hevc", identity_json="{}", profile_json="{}", observed_at=timestamp()))
    else:
        connection.execute(update(staged_artifacts).values(encode_origin="sample", encode_command_json='["ffmpeg","-c:v","libx265"]'))
    affected = output_kind == "library" and input_codec == "hevc"
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == ("unknown" if affected else "probable")
    restore_published_source_evidence(connection, apply=True)
    corrections = connection.execute(select(source_admissions).where(source_admissions.c.supersedes_id == admission["admission_id"])).mappings().all()
    assert len(corrections) == int(affected)
    if affected:
        assert corrections[0]["tier"] == "unknown"
        assert json.loads(corrections[0]["checks_json"])["reason"] == "historical_output_conflict"
    else:
        assert load_source_evidence(connection, history["id"]).traits == original.traits


@pytest.mark.parametrize("encoder,codec", [("hevc_videotoolbox", "hevc"), ("hevc_nvenc", "hevc"),
    ("h264_videotoolbox", "h264"), ("h264_nvenc", "h264"), ("libx265", "hevc"), ("libx264", "h264"),
    ("libvpx-vp9", "vp9"), ("libvpx", "vp8"), ("prores_ks", "prores"), ("mystery_encoder", None)])
@pytest.mark.parametrize("record", ["stage", "receipt", "version"])
@pytest.mark.parametrize("origin", ["queue", "sample", "fixture"])
@pytest.mark.parametrize("affected_codec", [True, False])
def test_non_av1_encoder_families_fail_closed(connection: DBClient, tmp_path: Path, encoder: str,
                                             codec: str | None, record: str, origin: str, affected_codec: bool) -> None:
    input_codec = (codec or "h264") if affected_codec else "mpeg2video"
    history = _history(connection, tmp_path, codec=input_codec)
    restore_published_source_evidence(connection, apply=True)
    command = json_text(["ffmpeg", "-c:v", encoder])
    if record == "stage":
        connection.execute(update(staged_artifacts).values(encode_origin=origin, encode_command_json=command))
    elif record == "receipt":
        payload = {"encode_origin": origin, "encode_command_json": command}
        connection.execute(target_production_outcomes.insert().values(
            receipt_id="contrary", library_item_id=history["id"], observation_id="o", manifest_run_id="r",
            item_index=0, encode_job_id="j", promoted_content_fingerprint="f" * 40,
            payload_json=json_text(payload), payload_sha256=stable_json_hash(payload), recorded_at=timestamp()))
    else:
        record_generated_output(connection, history["id"], identity={"size_bytes": 5, "sample_fingerprint": "f" * 40},
            profile={"video_codec": encoder}, operation_key={"encode_origin": origin}, recorded_at=timestamp())
    affected = origin == "queue" and (affected_codec or codec is None)
    expected = "unknown" if affected else "probable"
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == expected
    restore_published_source_evidence(connection, apply=True)
    assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].tier == expected
    # An unrecognized encoder hides traits on read but must not invalidate them permanently.
    corrected = affected and codec is not None
    assert connection.execute(select(source_admissions.c.tier)).scalars().all() == (
        ["probable", "unknown"] if corrected else ["probable"])


@pytest.mark.parametrize("change", ["revision", "sample", "size"])
def test_finished_encode_checks_identity_instead_of_revision(connection: DBClient, tmp_path: Path, change: str) -> None:
    history = _history(connection, tmp_path)
    planned = connection.execute(select(library_items)).mappings().one()
    restore_published_source_evidence(connection, apply=True)
    if change == "sample":
        connection.execute(update(library_items).values(content_version_fingerprint="e" * 40))
    elif change == "size":
        connection.execute(update(library_items).values(size_bytes=planned["size_bytes"] + 1))
    kwargs = {"identity": {"size_bytes": 12, "sample_fingerprint": "f" * 40}, "profile": {"video_codec": "av1"},
              "operation_key": {"input_sample": planned["content_version_fingerprint"], "input_size_bytes": planned["size_bytes"],
                                "source_revision": planned["source_revision"]}, "recorded_at": timestamp()}
    if change != "revision":
        with pytest.raises(RuntimeError, match="Encode input changed"):
            record_generated_output(connection, history["id"], **kwargs)
        return
    output = record_generated_output(connection, history["id"], **kwargs)
    transition = connection.execute(select(source_transitions).where(source_transitions.c.kind == "encode")).mappings().one()
    assert transition["after_version_id"] == output
    assert transition["outcome"] == "committed"


@pytest.mark.parametrize("change", ["revision", "sample", "size"])
def test_worker_commit_checks_identity_instead_of_revision(connection: DBClient, tmp_path: Path, change: str) -> None:
    from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_SCHEMA_VERSION, MEDIA_FINGERPRINT_TOOL_NAME, MEDIA_FINGERPRINT_TOOL_VERSION
    history = _history(connection, tmp_path)
    item = connection.execute(select(library_items)).mappings().one()
    connection.execute(evidence_queue_state.insert().values(queue_name="evidence", batch_id="batch", updated_at=timestamp()))
    connection.execute(library_item_evidence_state.insert().values(
        library_item_id=history["id"], evidence_kind="media_fingerprint", state="analysis_required", policy_hash="policy",
        work_batch_id="batch", work_status="running", work_source_fingerprint=item["content_version_fingerprint"],
        worker_id="worker", updated_at=timestamp()))
    claim = load_evidence_work_claim(connection, library_item_id=history["id"], evidence_kind="media_fingerprint")
    assert claim is not None
    observation = observe_file(history["path"])
    restore_published_source_evidence(connection, apply=True)
    if change == "sample":
        connection.execute(update(library_items).values(content_version_fingerprint="e" * 40))
    elif change == "size":
        connection.execute(update(library_items).values(size_bytes=item["size_bytes"] + 1))
    summary = {"schema_version": MEDIA_FINGERPRINT_SCHEMA_VERSION,
               "analysis": {"sampled_frames": 120, "aggregate": {"grain": 0.5},
                            "tool": {"name": MEDIA_FINGERPRINT_TOOL_NAME, "version": MEDIA_FINGERPRINT_TOOL_VERSION}},
               "decision": {"status": "measured", "traits": ["grain"]}}
    with patch("mediaforce.library.evidence_worker.open_db", return_value=nullcontext(connection)):
        _commit_evidence_summary(_config(tmp_path), claim, summary, actual_fingerprint=claim.expected_source_fingerprint,
                                 classification_only=False, observation={"before": observation, "after": observation})
    expected = "completed" if change == "revision" else "failed"
    assert connection.execute(select(library_item_evidence_state.c.work_status)).scalar_one() == expected
    assert connection.execute(select(library_items.c.media_fingerprint_json)).scalar_one() == (json_text(summary) if change == "revision" else None)


@pytest.mark.parametrize("occupied_archive", [False, True])
@pytest.mark.parametrize("missing_at", ["before", "observation"])
def test_publish_missing_source_keeps_previous_success_behavior(connection: DBClient, tmp_path: Path,
                                                               occupied_archive: bool, missing_at: str) -> None:
    history = _history(connection, tmp_path)
    connection.execute(update(library_items).where(library_items.c.id == history["id"]).values(missing_since=timestamp()))
    if missing_at == "before":
        history["path"].unlink()
    if occupied_archive:
        history["archive"].parent.mkdir(parents=True)
        history["archive"].write_bytes(b"older rollback")
    staging = tmp_path / "ready.mkv"
    staging.write_bytes(b"ready output")
    connection.execute(update(staged_artifacts).values(staging_path=str(staging), promoted_at=None,
                                                       validated_at=timestamp(), validation_json='{"passed":true}'))
    recorded: list[tuple[str, dict[str, Any]]] = []
    vanished = False

    def observe(path: Path, **kwargs: Any) -> dict[str, Any]:
        nonlocal vanished
        if path == history["path"] and missing_at == "observation" and not vanished:
            path.unlink()
            vanished = True
        return observe_file(path, **kwargs)

    with patch("mediaforce.encoding.source_publication.observe_file", side_effect=observe):
        published = promote_one_item(connection, _config(tmp_path), {"library_item_id": history["id"], "source_path": str(history["path"]),
                                                               "rel_path": "tv/Show/episode.mkv", "media_root": "tv"}, force=False,
                                 probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                                 record_event=lambda _connection, _item, event, details: recorded.append((event, details)),
                                 reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                                     allowed=True, waiting_reason=None, required_by_volume={}))
    assert published.read_bytes() == b"ready output"
    assert connection.execute(select(library_items.c.status)).scalar_one() == "promoted"
    assert connection.execute(select(library_items.c.missing_since)).scalar_one() is None
    assert history["archive"].exists() == occupied_archive
    if occupied_archive:
        assert history["archive"].read_bytes() == b"older rollback"
    assert [event for event, _details in recorded] == ["promotion_completed"]
    assert recorded[0][1]["retained_archive_backup_path"] is None


def test_cleanup_tolerates_file_vanishing_after_enumeration(connection: DBClient, tmp_path: Path) -> None:
    archived = _config(tmp_path).archive_root / "tv/Show/episode.mkv"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"rollback")
    observed = observe_file

    def vanished(path: Path) -> dict[str, Any]:
        path.unlink()
        return observed(path)

    with patch("mediaforce.web.runtime.archive_cleanup.open_db", return_value=nullcontext(connection)), \
            patch("mediaforce.encoding.source_publication.observe_file", side_effect=vanished):
        result = clear_archive_cleanup_action(_config(tmp_path))
    assert result["ok"]
    assert result["archive_cleanup"]["file_count"] == 0


@pytest.mark.parametrize("action", ["cleanup", "publish"])
def test_archive_removal_queries_only_identity_candidates(connection: DBClient, tmp_path: Path, action: str) -> None:
    archived = tmp_path / "archived"
    archived.write_bytes(b"rollback")
    source = tmp_path / "source"
    source.write_bytes(b"current source")
    staging = tmp_path / "staging"
    staging.write_bytes(b"new output")
    item_id = _insert_item(connection, source)
    ensure_active_source_version(connection, connection.execute(select(library_items)).mappings().one())
    version = connection.execute(select(source_versions)).mappings().one()
    identity = observe_file(archived)
    for index in range(20):
        for matching in (True, False):
            connection.execute(source_versions.insert().values(version_id=f"unrelated-{matching}-{index}",
                generation_id=version["generation_id"], role="unknown",
                size_bytes=identity["size_bytes"] if matching else 1000 + index,
                sample_fingerprint=identity["sample_fingerprint"] if matching else "e" * 40,
                identity_json=json_text({"path": str(tmp_path / f"other-{index}")}), profile_json="{}", observed_at=timestamp()))
    from mediaforce.library.source_evidence import append_version, version_locations
    archived_id = append_version(connection, generation_id=version["generation_id"], role="unknown",
        identity=identity, profile={}, recorded_at=timestamp())
    execute = connection.execute
    with patch.object(connection, "execute", wraps=execute) as queries, patch(
            "mediaforce.encoding.source_publication.version_locations", wraps=version_locations) as locations:
        if action == "cleanup":
            operations = prepare_archive_removal(connection, archived)
            assert len(operations) == 1
        else:
            operation = prepare_source_publish(connection, item_id, source_path=source, staging_path=staging,
                archive_path=archived, destination_path=source, backup_path=tmp_path / "backup")
            assert connection.execute(select(source_moves.c.version_id).where(
                source_moves.c.operation_id == operation, source_moves.c.ordinal == 0)).scalar_one() == archived_id
    assert queries.call_count < (10 if action == "cleanup" else 25)
    assert len(locations.call_args.args[1]) == 21


@pytest.mark.parametrize("interruption", ["unlink_error", "before_unlink", "after_unlink"])
def test_interrupted_owner_cleanup_reconciles_on_next_cleanup(connection: DBClient, tmp_path: Path, interruption: str) -> None:
    config = _config(tmp_path)
    config.paths.db_path.touch()
    archived = config.archive_root / "original.mkv"
    archived.parent.mkdir(parents=True)
    archived.write_bytes(b"rollback")
    _insert_item(connection, archived)
    version_id = ensure_active_source_version(connection, connection.execute(select(library_items)).mappings().one())
    version = connection.execute(select(source_versions).where(source_versions.c.version_id == version_id)).mappings().one()
    with patch("mediaforce.web.runtime.archive_cleanup.open_db", side_effect=lambda _path: nullcontext(connection)):
        if interruption == "unlink_error":
            with patch("mediaforce.web.runtime.archive_cleanup.safe_unlink", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError):
                    clear_archive_cleanup_action(config)
        else:
            prepare_archive_removal(connection, archived)
            connection.rollback()
            if interruption == "after_unlink":
                archived.unlink()
        operation = connection.execute(select(source_transitions.c.operation_id).where(
            source_transitions.c.kind == "owner-cleanup")).scalar_one()
        assert version_location(connection, version) is None
        # The next owner action must reconcile even when there are no files left to enumerate.
        result = clear_archive_cleanup_action(config)
        assert result["ok"]
        assert not archived.exists()
        assert connection.execute(select(source_transitions.c.outcome).where(
            source_transitions.c.operation_id == operation)).scalar_one() == (
                "committed" if interruption == "after_unlink" else "rolled_back")
        assert version_location(connection, version) is None
        assert not connection.execute(select(source_transitions).where(
            source_transitions.c.outcome.in_(("prepared", "unresolved")))).mappings().all()
        assert clear_archive_cleanup_action(config)["ok"]


@pytest.mark.parametrize("reader", ["acquisition", "selection", "restore"])
def test_global_source_evidence_scans_are_once_per_pass(connection: DBClient, tmp_path: Path, reader: str) -> None:
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = connection.execute(select(library_items)).mappings().one()
    for index in range(1, 5):
        copied = {**original, "id": index + 1, "source_path": str(tmp_path / f"copy-{index}"), "rel_path": f"tv/Show/copy-{index}.mkv"}
        connection.execute(library_items.insert().values(**copied))
    execute = connection.execute
    with patch.object(connection, "execute", wraps=execute) as queries:
        if reader == "acquisition":
            load_fingerprint_acquisition_items(connection, _config(tmp_path), "tv/Show")
        elif reader == "selection":
            select_candidates(connection, _config(tmp_path), statuses=[original["status"]], prefixes=[], limit=None)
        else:
            plan = history["plan"]
            connection.execute(update(encode_jobs).values(manifest_indexes_json=None))
            event = json.loads(connection.execute(select(item_events.c.details_json).where(
                item_events.c.event_type == "encoding_completed")).scalar_one())
            for index in range(1, 5):
                plan["items"].append({**plan["items"][0], "library_item_id": index + 1})
                connection.execute(staged_artifacts.insert().values(**{**history["stage"], "library_item_id": index + 1, "item_index": index,
                                                                       "promoted_path": str(tmp_path / f"copy-{index}")}))
                connection.execute(item_events.insert().values(library_item_id=index + 1, event_type="encoding_completed",
                    details_json=json_text({**event, "item_index": index}), created_at=timestamp()))
                connection.execute(item_events.insert().values(library_item_id=index + 1, event_type="originals_removed_confirmed",
                    details_json=json_text({"archived_source_path": history["stage"]["archived_source_path"]}), created_at=timestamp()))
            history["plan_path"].write_text(json_text(plan))
            restore_published_source_evidence(connection)
    statements = [str(call.args[0]) for call in queries.call_args_list if call.args and str(call.args[0]).startswith("SELECT")]
    output_scans = [sql for sql in statements if "source_versions.role =" in sql]
    command_scans = [sql for sql in statements if "staged_artifacts.encode_command_json" in sql and "library_item_id =" not in sql]
    receipt_scans = [sql for sql in statements if "FROM target_production_outcomes" in sql and "library_item_id =" not in sql]
    assert len(output_scans) <= 2
    assert len(command_scans) <= 2
    assert len(receipt_scans) <= 1


def test_migration_materializes_catalog_before_same_connection_writes(connection: DBClient, tmp_path: Path) -> None:
    migration = import_module("mediaforce.core.db_migration_scripts.versions.20261022_0027_source_evidence")
    for index in range(3):
        path = tmp_path / f"source-{index}"
        path.write_bytes(b"source")
        _insert_item(connection, path)
    execute = connection.execute
    reading = False

    class MutationSensitiveRows:
        def __init__(self, result: Any) -> None:
            self.result = result

        def __iter__(self) -> Iterator[Any]:
            nonlocal reading
            reading = True
            try:
                yield from self.result
            finally:
                reading = False

        def all(self) -> list[Any]:
            return list(self)

    def guarded_execute(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if reading:
            raise RuntimeError("SQLite catalog cursor is still active during a same-connection write")
        result = execute(statement, *args, **kwargs)
        if str(statement).startswith("SELECT * FROM library_items"):
            return SimpleNamespace(mappings=lambda: MutationSensitiveRows(result.mappings()))
        return result

    with patch.object(migration.op, "get_bind", return_value=connection), \
            patch.object(migration.op, "execute", side_effect=connection.exec_driver_sql), \
            patch.object(connection, "execute", side_effect=guarded_execute):
        migration.upgrade()
    assert connection.execute(select(func.count()).select_from(source_versions)).scalar_one() == 3
    assert connection.execute(select(func.count()).select_from(library_items).where(library_items.c.active_source_version_id.is_not(None))).scalar_one() == 3


def test_pending_moves_reject_late_worker_commits_and_remakes_leave_original_reads(connection: DBClient, tmp_path: Path) -> None:
    from contextlib import nullcontext
    history = _history(connection, tmp_path)
    restore_published_source_evidence(connection, apply=True)
    original = load_source_evidence(connection, history["id"])
    connection.execute(update(staged_artifacts).values(promoted_at=None))
    assert load_source_evidence(connection, history["id"]).traits == original.traits
    connection.execute(evidence_queue_state.insert().values(queue_name="evidence", batch_id="batch", updated_at=timestamp()))
    connection.execute(library_item_evidence_state.insert().values(
        library_item_id=history["id"], evidence_kind="cadence_analysis", state="analysis_required", policy_hash="policy",
        work_batch_id="batch", work_status="running", work_source_fingerprint=original.profile["content_version_fingerprint"],
        worker_id="worker", updated_at=timestamp(),
    ))
    claim = load_evidence_work_claim(connection, library_item_id=history["id"], evidence_kind="cadence_analysis")
    assert claim is not None
    staged = tmp_path / "next.mkv"
    staged.write_bytes(b"next output")
    connection.commit()
    operation = prepare_source_publish(connection, history["id"], source_path=history["path"], staging_path=staged,
                                       archive_path=history["archive"], destination_path=history["path"], backup_path=None)
    with patch("mediaforce.library.evidence_worker.open_db", return_value=nullcontext(connection)):
        _commit_evidence_summary(_config(tmp_path), claim, {"late": True},
                                 actual_fingerprint=claim.expected_source_fingerprint, classification_only=False)
    assert connection.execute(select(library_items.c.cadence_summary_json)).scalar_one() is None
    assert connection.execute(select(library_item_evidence_state.c.work_status)).scalar_one() == "failed"
    assert reconcile_source_publish(connection, operation)
    assert load_source_evidence(connection, history["id"]).traits == original.traits


@pytest.mark.parametrize("missing_endpoint", [False, True])
def test_older_receipt_can_link_source_to_a_later_encode_but_paths_cannot(connection: DBClient, tmp_path: Path,
                                                                       missing_endpoint: bool) -> None:
    history = _history(connection, tmp_path)
    source_item = history["plan"]["items"][0]
    output_a_sample = content_version_fingerprint(history["path"], history["path"].stat())
    output_a_size = history["path"].stat().st_size
    identity = {"library_item_id": history["id"], "source_size_bytes": source_item["source_size_bytes"]}
    if missing_endpoint:
        identity.pop("source_size_bytes")
    observation = {"status": "available", "item_identity": identity,
                   "content_fingerprint": source_item["content_version_fingerprint"], "source_fingerprint": source_item["source_fingerprint"]}
    operation = {"library_item_id": history["id"], "manifest_run_id": "run-1", "item_index": 0, "encode_job_id": "job-1"}
    payload = {"lineage": seal_lineage({"identity_before": observation, "identity_after": observation,
                                        "capsule": seal_lineage({"manifest_run_id": "run-1", "item_index": 0})}),
               "encode_identity": operation, "promoted_content_fingerprint": output_a_sample,
               "promoted_size_bytes": output_a_size}
    connection.execute(target_production_outcomes.insert().values(
        receipt_id="older", **operation, observation_id="boundary", promoted_content_fingerprint=output_a_sample,
        payload_json=json_text(payload), payload_sha256=stable_json_hash(payload), recorded_at=timestamp(),
    ))
    later = {**source_item, "source_fingerprint": "A-path", "source_size_bytes": output_a_size,
             "content_version_fingerprint": output_a_sample, "video_codec": "av1", "cadence_summary": {"output": "A"}}
    path = tmp_path / "later.json"
    path.write_text(json_text({"run_id": "run-2", "items": [later]}))
    connection.execute(run_manifests.insert().values(run_id="run-2", output_path=str(path), created_at=timestamp(), selection_json="{}", item_count=1))
    connection.execute(encode_jobs.insert().values(job_id="job-2", prefix="tv/Show", status="completed", manifest_path=str(path),
                                                   host_json="{}", created_at=timestamp(), updated_at=timestamp()))
    history["path"].write_bytes(b"output B")
    output_b_size = history["path"].stat().st_size
    connection.execute(update(library_items).values(size_bytes=output_b_size,
                                                    content_version_fingerprint=content_version_fingerprint(history["path"], history["path"].stat())))
    stage = {**history["stage"], "manifest_run_id": "run-2", "manifest_path": str(path), "encode_job_id": "job-2",
             "source_fingerprint": "A-path", "source_size_bytes": output_a_size, "source_video_codec": "av1", "staging_size_bytes": output_b_size}
    connection.execute(update(staged_artifacts).values(**stage))
    completion = {"manifest": str(path), "item_index": 0, "encode_job_id": "job-2", "source_path": str(history["path"]),
                  "source_size_bytes": output_a_size, "source_video_codec": "av1", "staging_path": stage["staging_path"],
                  "staging_size_bytes": output_b_size, "encode_completed_at": stage["encode_completed_at"]}
    connection.execute(item_events.insert().values(library_item_id=history["id"], event_type="encoding_completed",
                                                   created_at=timestamp(), details_json=json_text(completion)))
    stats = restore_published_source_evidence(connection, apply=True, unavailable_sources={history["id"]: "S was removed"})
    assert stats.historical_probable["cadence_analysis"] == (0 if missing_endpoint else 1)
    if not missing_endpoint:
        assert load_source_evidence(connection, history["id"]).traits["cadence_analysis"].summary == source_item["cadence_summary"]


def test_dry_run_cli_uses_readonly_path_without_lock_or_cleanup(connection: DBClient, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _history(connection, tmp_path)
    from contextlib import nullcontext
    args = cli.build_parser().parse_args(["evidence", "restore-source"])
    with patch.object(cli, "open_readonly_db", return_value=nullcontext(connection)), \
            patch.object(cli, "exclusive_mediaforce_runtime_lock", side_effect=AssertionError("dry run must not lock")), \
            patch.object(cli, "purge_transient_artifacts", side_effect=AssertionError("dry run must not clean up")):
        assert cli._main(args, _config(tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert connection.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 0


def test_migration_graph_preserves_unbound_candidates_and_enforces_version_keys(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite3"
    with _alembic_script_location() as location:
        scripts = ScriptDirectory(location)
        head = scripts.get_current_head()
        assert head is not None
        evidence_revision = next(revision for revision in scripts.walk_revisions()
                                 if Path(revision.path).stem.endswith("_source_evidence"))
        previous = evidence_revision.down_revision
        assert isinstance(previous, str)
        config = _alembic_config(path, location)
        command.upgrade(config, previous)
        engine = create_engine(f"sqlite:///{path}")
        with engine.begin() as database:
            input_path = tmp_path / "source"
            input_path.write_bytes(b"fixture")
            columns = {column["name"] for column in inspect(database).get_columns("library_items")}
            database.exec_driver_sql("INSERT INTO library_items (source_path,rel_path,media_root,parent_dir,file_name,container,size_bytes,mtime_ns,"
                                     "fingerprint,audio_summary_json,subtitle_summary_json,last_scan_id,discovered_at,last_seen_at,updated_at,cadence_summary_json) "
                                     "VALUES ('source','tv/Show/e.mkv','tv','tv/Show','e.mkv','.mkv',7,1,'fp','[]','[]','scan','time','time','time',?)",
                                     ('{"legacy":true}',))
            assert "active_source_version_id" not in columns
        command.upgrade(config, head)
        with engine.connect() as database:
            database.exec_driver_sql("PRAGMA foreign_keys=ON")
            stored = database.execute(select(source_measurements)).mappings().one()
            assert stored["binding"] == "unbound" and json.loads(stored["summary_json"]) == {"legacy": True}
            assert load_source_evidence(database, 1).traits["cadence_analysis"].tier == "unknown"
            with pytest.raises(IntegrityError):
                database.execute(update(library_items).values(active_source_version_id="not-a-version"))
            database.rollback()
            with pytest.raises(IntegrityError):
                database.execute(update(source_measurements).values(summary_json='{}'))
            database.rollback()
            with pytest.raises(IntegrityError):
                database.execute(source_measurements.insert().values(
                    measurement_id="null-sentinel", kind="cadence_analysis", binding="unbound", summary_json=None,
                    summary_sha256="", provenance_json="{}", recorded_at=timestamp(),
                ))
        engine.dispose()


PUT_BACK_ORIGINAL = b"S" * 1600
PUT_BACK_COMPRESSED = b"C" * 200


def _published(connection: DBClient, root: Path, original_name: str) -> dict[str, Any]:
    """One episode compressed and published the normal way, its original kept as a backup."""
    source = root / "tv/Show/Season 1" / original_name
    source.parent.mkdir(parents=True)
    source.write_bytes(PUT_BACK_ORIGINAL)
    now = timestamp()
    connection.execute(library_items.insert().values(
        source_path=str(source), rel_path=f"tv/Show/Season 1/{original_name}", media_root="tv", parent_dir="tv/Show/Season 1",
        file_name=source.name, container=source.suffix, size_bytes=source.stat().st_size, mtime_ns=source.stat().st_mtime_ns,
        fingerprint=file_fingerprint(source, source.stat(), 60),
        content_version_fingerprint=content_version_fingerprint(source, source.stat()),
        video_codec="h264", width=1920, height=1080, duration_seconds=60, audio_summary_json="[]", subtitle_summary_json="[]",
        last_scan_id="fixture", discovered_at=now, last_seen_at=now, updated_at=now,
    ))
    item_id = connection.execute(select(library_items.c.id).where(library_items.c.source_path == str(source))).scalar_one()
    staging = root / "staging/tv/Show/Season 1/episode.mkv"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(PUT_BACK_COMPRESSED)
    connection.execute(staged_artifacts.insert().values(
        library_item_id=item_id, staging_path=str(staging), source_path=str(source), validation_json='{"passed":true}',
        validated_at=now, bytes_saved=len(PUT_BACK_ORIGINAL) - len(PUT_BACK_COMPRESSED), updated_at=now,
    ))
    connection.commit()
    promote_one_item(connection, _config(root), {"library_item_id": item_id, "source_path": str(source),
                                                 "rel_path": f"tv/Show/Season 1/{original_name}", "media_root": "tv"},
                     force=False, probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
                     record_event=lambda *_args: None,
                     reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(allowed=True, waiting_reason=None,
                                                                                  required_by_volume={}))
    return {"id": item_id, "source": source, "staging": staging, "library": source.with_suffix(".mkv"),
            "archive": root / "archive/tv/Show/Season 1" / original_name}


def _put_back(connection: DBClient, item_id: int, events: list[tuple[str, dict[str, Any]]] | None = None) -> Path:
    return put_original_back(
        connection, item_id, probe_media=lambda _path: _probe(), file_fingerprint=file_fingerprint,
        content_fingerprint=content_version_fingerprint, timestamp=timestamp,
        record_event=lambda _connection, _item, kind, details: (events if events is not None else []).append((kind, details)),
    )


@pytest.mark.parametrize("original_name", ["episode.mp4", "episode.mkv"])
def test_put_back_returns_the_original_and_keeps_it(connection: DBClient, tmp_path: Path, original_name: str) -> None:
    published = _published(connection, tmp_path, original_name)
    assert published["library"].read_bytes() == PUT_BACK_COMPRESSED
    events: list[tuple[str, dict[str, Any]]] = []

    assert _put_back(connection, published["id"], events) == published["source"]

    assert published["source"].read_bytes() == PUT_BACK_ORIGINAL
    assert not published["archive"].exists()
    assert not published["staging"].exists()
    if published["library"] != published["source"]:
        assert not published["library"].exists()
    item = connection.execute(select(library_items)).mappings().one()
    assert (item["source_path"], item["rel_path"]) == (str(published["source"]), f"tv/Show/Season 1/{original_name}")
    assert (item["size_bytes"], item["status"]) == (len(PUT_BACK_ORIGINAL), "discovered")
    assert connection.execute(select(staged_artifacts)).first() is None
    # Kept for the source that came back, so queueing, dispatch and publishing leave it alone.
    keep = connection.execute(select(motion_keep_decisions)).mappings().one()
    assert (keep["reason_code"], keep["source_fingerprint"]) == ("put_back", item["content_version_fingerprint"])
    assert library_item_ids_kept_original(connection, [published["id"]]) == {published["id"]}
    assert [kind for kind, _details in events] == [PUT_BACK_EVENT]
    # The episode list words it as the owner's put-back, not a motion-pattern choice.
    assert put_back_rel_paths(connection, [item["rel_path"], "tv/Show/Season 1/other.mkv"]) == {item["rel_path"]}


def test_put_back_refuses_when_the_backup_is_gone(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    published["archive"].unlink()

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "backup_gone"
    assert published["library"].read_bytes() == PUT_BACK_COMPRESSED
    assert connection.execute(select(staged_artifacts.c.promoted_at)).scalar_one() is not None


def test_put_back_refuses_a_file_never_replaced(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    _put_back(connection, published["id"])

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "not_replaced"
    assert published["source"].read_bytes() == PUT_BACK_ORIGINAL


def test_interrupted_put_back_returns_every_file(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    move = shutil.move
    calls = 0

    def second_move_fails(old: str, new: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk went away")
        return move(old, new)

    with patch("mediaforce.encoding.source_publication.shutil.move", side_effect=second_move_fails):
        with pytest.raises(OSError, match="disk went away"):
            _put_back(connection, published["id"])

    assert published["library"].read_bytes() == PUT_BACK_COMPRESSED
    assert published["archive"].read_bytes() == PUT_BACK_ORIGINAL
    assert not published["staging"].exists()
    assert connection.execute(select(staged_artifacts.c.promoted_at)).scalar_one() is not None
    assert connection.execute(select(motion_keep_decisions)).first() is None


@pytest.mark.parametrize("killed_after_moves", [1, 2])
def test_put_back_killed_partway_is_returned_then_retried(connection: DBClient, tmp_path: Path, killed_after_moves: int) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    move = shutil.move
    calls = 0

    def killed(old: str, new: str) -> str:
        nonlocal calls
        result = move(old, new)
        calls += 1
        if calls == killed_after_moves:
            raise KeyboardInterrupt()
        return result

    with patch("mediaforce.encoding.source_publication.shutil.move", side_effect=killed):
        with patch("mediaforce.encoding.put_back.reconcile_source_publish", return_value=False):
            with pytest.raises((KeyboardInterrupt, PutBackRestoreError)):
                _put_back(connection, published["id"])
    # A scan meanwhile leaves the files of the unfinished move alone.
    assert path_in_pending_move(connection, str(published["staging"]))
    assert path_in_pending_move(connection, str(published["source"]))

    assert reconcile_unfinished_put_backs(connection) == 0
    assert published["library"].read_bytes() == PUT_BACK_COMPRESSED
    assert published["archive"].read_bytes() == PUT_BACK_ORIGINAL
    assert _put_back(connection, published["id"]) == published["source"]
    assert published["source"].read_bytes() == PUT_BACK_ORIGINAL


def test_put_back_refuses_a_backup_that_is_not_the_recorded_original(connection: DBClient, tmp_path: Path) -> None:
    published = _published(connection, tmp_path, "episode.mp4")
    published["archive"].write_bytes(b"X" * 1600)

    with pytest.raises(PutBackRefused) as refused:
        _put_back(connection, published["id"])

    assert refused.value.code == "backup_changed"
    assert published["library"].read_bytes() == PUT_BACK_COMPRESSED
    assert published["archive"].read_bytes() == b"X" * 1600
    assert not path_in_pending_move(connection, str(published["source"]))
