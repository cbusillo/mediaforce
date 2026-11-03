import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from mediaforce import state_cleanup
from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.core.db_tables import encode_jobs
from mediaforce.library.run_manifests import build_run_manifest, write_manifest

DAY = 86400


@pytest.fixture
def config(tmp_path: Path) -> Iterator[MediaforceConfig]:
    support = tmp_path / "support"
    yield MediaforceConfig(
        raw={
            "state": {"cleanup": {"transient_artifact_retention_days": 14}},
            "media": {
                "staging_root": str(tmp_path / "staging"),
                "source_roots": {},
                "output_container": "mkv",
            },
            "video": {},
            "audio": {},
            "subtitle": {},
            "planning": {},
        },
        paths=ConfigPaths(
            project_root=tmp_path / "repo",
            config_path=tmp_path / "config.toml",
            db_path=support / "library.sqlite3",
            run_manifest_dir=support / "runs",
            web_state_dir=support / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=support / "runtime.json",
            runtime_reservation_dir=tmp_path / "reservations",
        ),
    )


def _work_folder(root: Path, name: str, *, age_days: float, finder_opened_now: bool = False) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    backup = folder / "before.sqlite"
    backup.write_text("database copy")
    stamp = time.time() - age_days * DAY
    os.utime(backup, (stamp, stamp))
    if finder_opened_now:
        (folder / ".DS_Store").write_text("finder")
    else:
        os.utime(folder, (stamp, stamp))
    return folder


def _sweep(config: MediaforceConfig) -> None:
    with patch.object(
            state_cleanup,
            "_cleanup_process_snapshot",
            return_value=state_cleanup.CleanupProcessSnapshot((), ()),
    ):
        state_cleanup.purge_transient_artifacts(config, force=True)


def _old_run_plan(config: MediaforceConfig, trait_column: str | None = None) -> Path:
    row = {
        "id": 1,
        "source_path": str(config.paths.project_root / "episode.mkv"),
        "rel_path": "tv/Show/episode.mkv",
        "media_root": "tv",
        "fingerprint": "original-source",
        "size_bytes": 1_000_000,
        "duration_seconds": 60.0,
        "video_codec": "h264",
        "container": ".mkv",
        "status": "discovered",
        "audio_track_count": 1,
        "english_audio_count": 1,
        "english_subtitle_count": 0,
        "audio_summary_json": "[]",
        "subtitle_summary_json": "[]",
    }
    if trait_column:
        analysis = (
            {"sampled_frames": 120, "aggregate": {"dark_frame_fraction": 0.8}}
            if trait_column == "media_fingerprint_json"
            else {"sampled_frames": 120, "progressive_frames": 120, "repeated_neither": 120}
        )
        row[trait_column] = json.dumps({"analysis": analysis})
    manifest = build_run_manifest([row], config)
    with open_db(config.paths.db_path) as connection:
        path = write_manifest(connection, config, manifest)
    stamp = time.time() - 60 * DAY
    os.utime(path, (stamp, stamp))
    return path


@pytest.mark.parametrize("trait_column", ["media_fingerprint_json", "cadence_summary_json"])
def test_old_run_plan_with_source_traits_is_kept(
    config: MediaforceConfig, trait_column: str,
) -> None:
    path = _old_run_plan(config, trait_column)

    _sweep(config)

    assert path.exists()


def test_old_run_plan_without_source_traits_is_removed(config: MediaforceConfig) -> None:
    path = _old_run_plan(config)

    _sweep(config)

    assert not path.exists()


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_retained_problem_jobs_keep_their_file_membership_evidence(config: MediaforceConfig, status: str) -> None:
    path = _old_run_plan(config)
    with open_db(config.paths.db_path) as connection:
        connection.execute(encode_jobs.insert().values(
            job_id="retained-question", prefix="tv/Show", status=status, job_kind="shard", manifest_path=str(path),
            host_json="{}", created_at="before-cleanup", updated_at="before-cleanup",
        ))

    _sweep(config)

    assert path.exists()


@pytest.mark.parametrize("contents", [b"{broken", b"\xff", b"[]", b'{"items": [null]}'])
def test_malformed_old_run_plan_is_kept(config: MediaforceConfig, contents: bytes) -> None:
    path = _old_run_plan(config)
    path.write_bytes(contents)
    stamp = time.time() - 60 * DAY
    os.utime(path, (stamp, stamp))

    _sweep(config)

    assert path.exists()


def test_keeps_only_the_newest_deploy_backups(config: MediaforceConfig) -> None:
    deployments = config.paths.db_path.parent / "deployments"
    newest = _work_folder(deployments, "main-c07882d1-20261007", age_days=6)
    previous = _work_folder(deployments, "main-4b9ce65b-20261006", age_days=7)
    oldest = [
        _work_folder(deployments, f"6{index}0-old", age_days=20 + index, finder_opened_now=True)
        for index in range(3)
    ]

    _sweep(config)

    assert newest.exists() and previous.exists()
    assert not any(folder.exists() for folder in oldest)


def test_opening_old_deploys_in_finder_does_not_make_them_newest(config: MediaforceConfig) -> None:
    deployments = config.paths.db_path.parent / "deployments"
    kept = [_work_folder(deployments, f"recent-{index}", age_days=index) for index in range(2)]
    touched = _work_folder(deployments, "september", age_days=30, finder_opened_now=True)

    _sweep(config)

    assert all(folder.exists() for folder in kept)
    assert not touched.exists()


def test_the_number_of_kept_deploys_is_a_setting(config: MediaforceConfig) -> None:
    config.raw["state"]["cleanup"]["kept_deploy_backups"] = 3
    deployments = config.paths.db_path.parent / "deployments"
    folders = [_work_folder(deployments, f"deploy-{index}", age_days=index) for index in range(4)]

    _sweep(config)

    assert [folder.exists() for folder in folders] == [True, True, True, False]


def test_old_operation_evidence_goes_and_recent_evidence_stays(config: MediaforceConfig) -> None:
    operations = config.paths.db_path.parent / "operations"
    recent = _work_folder(operations, "20261010-recent-recovery", age_days=3)
    old = _work_folder(operations, "20260917-house-requeue", age_days=40, finder_opened_now=True)

    _sweep(config)

    assert recent.exists()
    assert not old.exists()


def test_missing_support_folders_are_fine(config: MediaforceConfig) -> None:
    _sweep(config)

    assert not (config.paths.db_path.parent / "deployments").exists()


@pytest.mark.parametrize("location", ["deployments", "operations", "web", "runs", "review", "staging"])
def test_cleanup_refuses_registered_runtime_and_rollback(config: MediaforceConfig, location: str) -> None:
    import json

    candidates = {
        "deployments": config.paths.db_path.parent / "deployments",
        "operations": config.paths.db_path.parent / "operations",
        "web": config.paths.web_state_dir,
        "runs": config.paths.run_manifest_dir,
        "review": config.paths.review_dir,
        "staging": config.staging_root,
    }
    active = _work_folder(candidates[location], "retained-runtime", age_days=60)
    registry = config.paths.db_path.parent / "runtime/protected-roots.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps([str(active)]))
    _work_folder(config.paths.db_path.parent / "deployments", "new", age_days=1)
    _work_folder(config.paths.db_path.parent / "deployments", "previous", age_days=2)

    _sweep(config)

    assert (active / "before.sqlite").exists()
    assert not (config.paths.web_state_dir / ".cleanup-stamp").exists()


def test_cleanup_refuses_unreadable_runtime_ownership(config: MediaforceConfig) -> None:
    registry = config.paths.db_path.parent / "runtime/protected-roots.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("broken registry")
    retained = _work_folder(config.paths.db_path.parent / "operations", "old", age_days=60)

    _sweep(config)

    assert retained.exists()


def test_cleanup_cannot_race_a_runtime_switch(config: MediaforceConfig) -> None:
    from mediaforce.ops.runtime_install import runtime_activity

    retained = _work_folder(config.paths.db_path.parent / "operations", "old", age_days=60)
    with runtime_activity(config.paths.db_path.parent) as acquired:
        assert acquired
        _sweep(config)
        assert retained.exists()
    _sweep(config)
    assert not retained.exists()


def test_failed_attempts_cannot_age_out_the_active_rollback(config: MediaforceConfig) -> None:
    from mediaforce.ops.runtime_install import retain_deploy_stage

    support = config.paths.db_path.parent
    active = _work_folder(support / "deployments", "active-rollback", age_days=60)
    retain_deploy_stage(support, active)
    _work_folder(support / "deployments", "newer-attempt", age_days=1)
    _work_folder(support / "deployments", "latest-attempt", age_days=0)

    _sweep(config)

    assert (active / "before.sqlite").exists()



def test_kind_evidence_outlives_transient_review_cleanup_until_the_kind_is_stopped(config: MediaforceConfig) -> None:
    from mediaforce.web.runtime.kind_approvals import APPROVABLE_KINDS, kind_approval_file, stop_kind_approval

    watched = _work_folder(config.paths.review_dir, "watched-sample", age_days=30)
    unused = _work_folder(config.paths.review_dir, "unused-sample", age_days=30)
    kind = APPROVABLE_KINDS[0]
    approval_file = kind_approval_file(config, kind)
    approval_file.parent.mkdir(parents=True)
    clip = f"/review-media/{watched.name}/before.sqlite"
    approval_file.write_text(json.dumps({
        "kind": kind, "browser_review_ready": True,
        "review_pairs": [{"source_clip": {"path": clip}, "preview_clip": {"path": clip}}],
    }))

    _sweep(config)

    assert watched.exists() and not unused.exists()
    assert stop_kind_approval(config, kind)
    _sweep(config)
    assert not watched.exists()


@pytest.mark.parametrize("contents", ['{broken', '[]'])
def test_unreadable_kind_evidence_preserves_review_files(config: MediaforceConfig, contents: str) -> None:
    from mediaforce.web.runtime.kind_approvals import APPROVABLE_KINDS, kind_approval_file

    watched = _work_folder(config.paths.review_dir, "watched-sample", age_days=30)
    approval_file = kind_approval_file(config, APPROVABLE_KINDS[0])
    approval_file.parent.mkdir(parents=True)
    approval_file.write_text(contents)
    unrelated_scratch = _work_folder(config.staging_root / "_calibration", "scratch", age_days=30)

    _sweep(config)

    assert watched.exists()
    assert not unrelated_scratch.exists()
