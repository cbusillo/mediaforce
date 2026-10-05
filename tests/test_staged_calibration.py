from __future__ import annotations

import json
import hashlib
import os
import subprocess
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import open_db
from mediaforce.encoding.encode_queue import load_encode_job, save_encode_job
from mediaforce.tuning.calibration_jobs import save_job
from mediaforce.web.runtime import encode_runtime
from mediaforce.core.process_control import ManagedProcessController, ProcessCancelledError
from mediaforce.encoding import staged_host
from mediaforce.encoding.quality import QualitySearchResult, SampleEncodeResult
from mediaforce.execution import resolve_stream_budget_ledger
from mediaforce.review import encode_preview_clips, render_source_review_clips
from mediaforce.web.runtime import calibration_runtime as runtime


@pytest.fixture
def sample_run(tmp_path: Path) -> dict[str, Any]:
    source = tmp_path / "Episode.MP4"
    source.write_bytes(b"seekable source" * 100)
    scratch = tmp_path / "scratch"
    host = {"key": "scratch-worker", "host": "scratch-worker", "mode": "ssh", "media_access": "stream"}
    policy = {"video": {"encoder": "libsvtav1", "pixel_format": "yuv420p10le", "sample_every": "8m", "sample_duration": "20s"}, "audio": {}, "subtitle": {}}
    item = {"source_path": str(source), "rel_path": "tv/show/Episode.MP4", "source_size_bytes": source.stat().st_size,
            "video_codec": "h264", "duration_seconds": 120.0, "resolved_policy": policy,
            "audio_summary": [], "subtitle_summary": []}
    config = MediaforceConfig(raw={"media": {"staging_root": str(tmp_path / "staging")},
                                  "remote_hosts": [{**host, "scratch_root": str(scratch)}]},
                             paths=ConfigPaths(project_root=tmp_path, config_path=tmp_path / "config.toml",
                                               db_path=tmp_path / "library.sqlite3", run_manifest_dir=tmp_path / "runs",
                                               web_state_dir=tmp_path / "web", review_dir=tmp_path / "review",
                                               runtime_settings_path=tmp_path / "settings.json",
                                               runtime_reservation_dir=tmp_path / "reservations"))
    deps = runtime.CalibrationRunDeps(**{field.name: Mock() for field in fields(runtime.CalibrationRunDeps)})
    deps.now_iso = lambda: "2026-10-04T00:00:00+00:00"
    deps.effective_video_preset = lambda *_args, **_kwargs: 6
    deps.resolve_stream_budget_ledger = resolve_stream_budget_ledger
    deps.build_svt_params = lambda *_args: []
    deps.detect_video_crop = Mock(return_value=None)
    deps.select_quality_metric = lambda _metric: ("vmaf", 95.0)
    deps.search_quality_for_source = Mock(return_value=QualitySearchResult(crf=28, metric="VMAF", target=95, score=96, stdout="quality"))
    deps.run_sample_encode = Mock(return_value=SampleEncodeResult(metric="VMAF", score=96, predicted_encode_percent=10,
                                                                predicted_encode_seconds=30, predicted_encode_size_bytes=100, stdout="sample"))
    deps.recommend_review_moments = None
    deps.default_review_timestamps = lambda *_args: [12.0]
    deps.quality_toolchain_identity = None
    deps.plan_av1_cold_start = None
    deps.secure_review_artifacts = None
    deps.review_url = lambda _config, path: str(path)
    deps.encode_preview_clips = Mock(return_value=[])
    deps.render_source_review_clips = Mock(return_value=[])
    controller = ManagedProcessController()
    progress = Mock()
    return {"config": config, "prefix": "tv/show", "action": "baseline", "host_data": host,
            "notes": "", "policy": policy, "seed_metadata": None, "sample_item": item,
            "calibration_run_id": "sample-run", "process_controller": controller, "deps": deps,
            "progress_callback": progress}


def local_stage(run: dict[str, Any], *, fault: str | None = None, free_kib: int = 10000000) -> Any:
    """Use real keeper/copy shells, pinned capacity and synthetic source bytes; never SSH."""
    real_stage = staged_host.staged_job

    def remote(_host: dict[str, Any], command: list[str], _timeout: int, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        controller = _kwargs.get("process_controller")
        if controller is not None:
            controller.throw_if_cancelled()
        if "df -Pk" in command[-1]:
            return subprocess.CompletedProcess(command, 0, f"{free_kib}\n", "")
        if "sha256sum" in command[-1]:
            scratch = Path(run["config"].remote_hosts[0]["scratch_root"])
            source = next(scratch.glob(f"{staged_host.SCRATCH_DIR_PREFIX}*/source.*"))
            digest = "wrong digest" if fault == "digest" else hashlib.sha256(source.read_bytes()).hexdigest()
            return subprocess.CompletedProcess(command, 0, digest, "")
        return subprocess.run(command, capture_output=True, text=True, timeout=10)

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        script = argv[-1]
        if script.startswith("cat >") and fault in {"full", "disconnect"}:
            detail = "No space left on device" if fault == "full" else "Connection reset by peer"
            script = f"cat >/dev/null; echo '{detail}' >&2; exit 1"
        process = subprocess.Popen(["sh", "-c", script], **kwargs)
        if script.startswith("cat >") and fault == "cancelcopy":
            pipe = process.stdin
            assert pipe is not None
            cancelled_pipe = Mock(wraps=pipe)

            def cancelled_write(_chunk: bytes) -> None:
                run["process_controller"].cancel()
                cancelled_pipe.close()
                raise BrokenPipeError("copy was stopped")

            cancelled_pipe.write.side_effect = cancelled_write
            process.stdin = cancelled_pipe
        return process

    def stage(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(run_remote_command=remote, popen=popen)
        return real_stage(*args, **kwargs)

    return patch("mediaforce.web.runtime.calibration_runtime.staged_job", side_effect=stage)


def assert_scratch_empty(run: dict[str, Any]) -> None:
    scratch = Path(run["config"].remote_hosts[0]["scratch_root"])
    assert not list(scratch.glob(f"{staged_host.SCRATCH_DIR_PREFIX}*"))
    assert Path(run["sample_item"]["source_path"]).read_bytes() == b"seekable source" * 100


@pytest.mark.parametrize("suffix", [".MP4", ".M4V", ".MOV", ".mkv"])
def test_sample_stages_once_and_routes_all_work_to_seekable_remote_source(sample_run: dict[str, Any], suffix: str) -> None:
    source = Path(sample_run["sample_item"]["source_path"])
    renamed = source.with_suffix(suffix)
    source.rename(renamed)
    sample_run["sample_item"]["source_path"] = str(renamed)
    deps = sample_run["deps"]

    def search(path: Path, _policy: dict[str, Any], **kwargs: Any) -> QualitySearchResult:
        assert path.read_bytes() == renamed.read_bytes()
        assert path.suffix == suffix.lower()
        assert kwargs["host"]["mode"] == "ssh"
        assert kwargs["host"]["media_access"] == "mounted"
        assert kwargs["quality_temp_dir"] == path.parent
        assert kwargs["process_controller"] is sample_run["process_controller"]
        return QualitySearchResult(crf=28, metric="VMAF", target=95, score=96, stdout="quality")

    deps.search_quality_for_source.side_effect = search
    with local_stage(sample_run) as staging:
        payload, cleanup = runtime.run_sampled_calibration(**sample_run)
    assert staging.call_count == 1
    quality_path = deps.search_quality_for_source.call_args.args[0]
    for work in (deps.detect_video_crop, deps.run_sample_encode):
        assert work.call_args.args[0] == quality_path
        assert work.call_args.kwargs["host"]["mode"] == "ssh"
    for work in (deps.encode_preview_clips, deps.render_source_review_clips):
        assert work.call_args.kwargs["source_path"] == quality_path
        assert work.call_args.kwargs["host"]["mode"] == "ssh"
    deps.recommend_review_timestamps.assert_not_called()
    deps.generate_compare_clips_from_review_pairs.assert_not_called()
    assert payload["host"] == sample_run["host_data"]
    assert staged_host.STAGED_JOB_KEY not in json.dumps(payload)
    assert str(quality_path) not in json.dumps(payload)
    assert cleanup is None
    sample_run["progress_callback"].assert_any_call("preparing_source")
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("fault", ["full", "disconnect", "digest"])
def test_bad_copy_never_starts_search_and_releases_scratch(sample_run: dict[str, Any], fault: str) -> None:
    with local_stage(sample_run, fault=fault), pytest.raises(staged_host.StagedScratchError):
        runtime.run_sampled_calibration(**sample_run)
    sample_run["deps"].search_quality_for_source.assert_not_called()
    sample_run["deps"].encode_preview_clips.assert_not_called()
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("phase,error", [
    ("search_quality_for_source", ProcessCancelledError()),
    ("search_quality_for_source", RuntimeError("quality search failed")),
    ("encode_preview_clips", RuntimeError("Connection reset by peer")),
    ("render_source_review_clips", ProcessCancelledError()),
])
def test_failed_sample_work_releases_scratch(sample_run: dict[str, Any], phase: str, error: Exception) -> None:
    getattr(sample_run["deps"], phase).side_effect = error
    with local_stage(sample_run), pytest.raises(type(error)):
        runtime.run_sampled_calibration(**sample_run)
    assert_scratch_empty(sample_run)


def test_sample_sweep_preserves_live_and_unrelated_work(sample_run: dict[str, Any]) -> None:
    scratch = Path(sample_run["config"].remote_hosts[0]["scratch_root"])
    live = scratch / f"{staged_host.SCRATCH_DIR_PREFIX}live"
    orphan = scratch / f"{staged_host.SCRATCH_DIR_PREFIX}orphan"
    unrelated = scratch / "other-data"
    for path in (live, orphan, unrelated):
        path.mkdir(parents=True)
    (live / staged_host.KEEPER_PID_FILE).write_text(str(os.getpid()))
    old = time.time() - (staged_host.ORPHAN_GRACE_MINUTES + 5) * 60
    os.utime(orphan, (old, old))
    with local_stage(sample_run):
        runtime.run_sampled_calibration(**sample_run)
    assert not orphan.exists()
    assert live.exists() and unrelated.exists()


def test_source_override_is_staged_without_replacing_saved_identity(sample_run: dict[str, Any], tmp_path: Path) -> None:
    override = tmp_path / "override.mov"
    override.write_bytes(b"alternate synthetic source")
    sample_run["source_path_override"] = override
    with local_stage(sample_run) as staging:
        payload, _ = runtime.run_sampled_calibration(**sample_run)
    assert staging.call_args.args[1] == override
    assert payload["sample_item"]["source_path"] == sample_run["sample_item"]["source_path"]
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("source_review", [False, True])
def test_review_render_and_pull_share_keeper_and_cancellation(sample_run: dict[str, Any], source_review: bool) -> None:
    deps = sample_run["deps"]
    work = render_source_review_clips if source_review else encode_preview_clips
    setattr(deps, "render_source_review_clips" if source_review else "encode_preview_clips", work)
    render = Mock()
    transfers: list[Path] = []

    def pull(job: staged_host.StagedJob, path: Path, **kwargs: Any) -> None:
        assert job.output_path.is_relative_to(job.scratch_dir)
        assert Path(str(job.source_path)).exists()
        assert kwargs["process_controller"] is sample_run["process_controller"]
        path.write_bytes(b"returned review clip")
        transfers.append(path)

    render_target = "mediaforce.review._render_source_review_clip_remote" if source_review else "mediaforce.review._render_encoded_preview_clip_remote"
    with local_stage(sample_run), patch(
        "mediaforce.review.run_remote_command", return_value=subprocess.CompletedProcess([], 0, "", "")
    ), patch(render_target, render), patch(
        "mediaforce.reviewing.clips.pull_output", side_effect=pull
    ), patch("mediaforce.review.copy_remote_file_to_local", side_effect=AssertionError("staged copies must be managed")):
        payload, _ = runtime.run_sampled_calibration(**sample_run)
    assert len(transfers) == 1
    assert transfers[0].read_bytes() == b"returned review clip"
    assert render.call_args.kwargs["process_controller"] is sample_run["process_controller"]
    rendered_job = staged_host.staged_job_for_host(render.call_args.kwargs["host"])
    assert rendered_job is not None
    assert render.call_args.kwargs["remote_output_path"].parent.parent == Path(str(rendered_job.scratch_dir))
    assert payload["source_clips" if source_review else "preview_clips"]
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("source_review", [False, True])
def test_stop_reaches_real_remote_renderer_and_cleans_keeper(sample_run: dict[str, Any], source_review: bool) -> None:
    deps = sample_run["deps"]
    setattr(deps, "render_source_review_clips" if source_review else "encode_preview_clips",
            render_source_review_clips if source_review else encode_preview_clips)
    observed: list[list[str]] = []

    def remote(_host: dict[str, Any], command: list[str], _timeout: int, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(command)
        if "-i" in command:
            assert kwargs["process_controller"] is sample_run["process_controller"]
            Path(command[-1]).write_bytes(b"unfinished review render")
            sample_run["process_controller"].cancel()
            sample_run["process_controller"].throw_if_cancelled()
        if command[0] == "mkdir":
            return subprocess.run(command, capture_output=True, text=True, timeout=10)
        return subprocess.CompletedProcess(command, 0, "", "")

    with local_stage(sample_run), patch("mediaforce.review.run_remote_command", side_effect=remote), patch(
        "mediaforce.review.run_command", side_effect=AssertionError("no controller render")
    ), patch("mediaforce.reviewing.clips.pull_output") as pull, patch(
        "mediaforce.review.ffmpeg_binary", return_value="ffmpeg"
    ), patch("mediaforce.review.ffmpeg_hwaccel_input_args", return_value=[]):
        with pytest.raises(ProcessCancelledError):
            runtime.run_sampled_calibration(**sample_run)
    assert any("-i" in command for command in observed)
    assert not any(command[:2] == ["rm", "-rf"] for command in observed)
    pull.assert_not_called()
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("source_review", [False, True])
def test_stop_cancels_real_review_pull_process_and_removes_local_clip(sample_run: dict[str, Any], source_review: bool) -> None:
    setattr(sample_run["deps"], "render_source_review_clips" if source_review else "encode_preview_clips",
            render_source_review_clips if source_review else encode_preview_clips)
    readers: list[subprocess.Popen[bytes]] = []
    local_paths: list[Path] = []
    attached = threading.Event()
    controller = sample_run["process_controller"]
    original_attach = controller.attach

    def attach(process: subprocess.Popen[bytes], **kwargs: Any) -> None:
        original_attach(process, **kwargs)
        attached.set()

    def stop_after_attachment() -> None:
        if attached.wait(timeout=5):
            controller.cancel()

    stopper = threading.Thread(target=stop_after_attachment)

    def pull(job: staged_host.StagedJob, path: Path, **kwargs: Any) -> None:
        def popen(_argv: list[str], **process_kwargs: Any) -> subprocess.Popen[bytes]:
            reader = subprocess.Popen([sys.executable, "-c",
                                       "import sys,time; sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush(); time.sleep(30)"],
                                      **process_kwargs)
            readers.append(reader)
            return reader

        local_paths.append(path)
        stopper.start()
        with patch.object(staged_host.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "100", "")), patch.object(
            controller, "attach", side_effect=attach
        ):
            staged_host.pull_output(job, path, popen=popen, **kwargs)

    renderer = "mediaforce.review._render_source_review_clip_remote" if source_review else "mediaforce.review._render_encoded_preview_clip_remote"
    try:
        with local_stage(sample_run), patch("mediaforce.review.run_remote_command") as remote, patch(renderer), patch(
            "mediaforce.reviewing.clips.pull_output", side_effect=pull
        ), pytest.raises(ProcessCancelledError):
            runtime.run_sampled_calibration(**sample_run)
    finally:
        if stopper.ident is not None:
            stopper.join(timeout=6)
    assert not stopper.is_alive()
    assert len(readers) == 1
    assert readers[0].returncode is not None and readers[0].returncode < 0
    assert not local_paths[0].exists()
    assert not any(call.args[1][:2] == ["rm", "-rf"] for call in remote.call_args_list)
    assert_scratch_empty(sample_run)


@pytest.mark.parametrize("source_review", [False, True])
def test_interrupted_review_download_removes_partial_clip(sample_run: dict[str, Any], source_review: bool) -> None:
    setattr(sample_run["deps"], "render_source_review_clips" if source_review else "encode_preview_clips",
            render_source_review_clips if source_review else encode_preview_clips)
    partial_paths: list[Path] = []

    def interrupted(_job: staged_host.StagedJob, path: Path, **_kwargs: Any) -> None:
        path.write_bytes(b"partial")
        partial_paths.append(path)
        raise staged_host.StagedScratchError("Copying the encoded output back from the encode host failed.")

    render_target = "mediaforce.review._render_source_review_clip_remote" if source_review else "mediaforce.review._render_encoded_preview_clip_remote"
    with local_stage(sample_run), patch(
        "mediaforce.review.run_remote_command", return_value=subprocess.CompletedProcess([], 0, "", "")
    ), patch(render_target), patch("mediaforce.reviewing.clips.pull_output", side_effect=interrupted):
        with pytest.raises(staged_host.StagedScratchError):
            runtime.run_sampled_calibration(**sample_run)
    assert partial_paths and all(not path.exists() for path in partial_paths)
    assert_scratch_empty(sample_run)


def test_low_space_refuses_sample_before_copy_or_search(sample_run: dict[str, Any]) -> None:
    def remote(_host: dict[str, Any], command: list[str], _timeout: int, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, "0\n", "")

    real_stage = staged_host.staged_job
    processes = Mock(side_effect=AssertionError("low space must not start a copy"))

    def stage(*args: Any, **kwargs: Any) -> Any:
        kwargs.update(run_remote_command=remote, popen=processes)
        return real_stage(*args, **kwargs)

    with patch("mediaforce.web.runtime.calibration_runtime.staged_job", side_effect=stage), pytest.raises(
        staged_host.StagedScratchError, match="scratch folder has"
    ):
        runtime.run_sampled_calibration(**sample_run)
    processes.assert_not_called()
    sample_run["deps"].search_quality_for_source.assert_not_called()
    assert_scratch_empty(sample_run)


def test_stop_during_source_copy_is_cancellation(sample_run: dict[str, Any]) -> None:
    with local_stage(sample_run, fault="cancelcopy"), pytest.raises(ProcessCancelledError):
        runtime.run_sampled_calibration(**sample_run)
    sample_run["deps"].search_quality_for_source.assert_not_called()
    assert_scratch_empty(sample_run)


def running_encode(sample_run: dict[str, Any], tmp_path: Path) -> int:
    manifest = tmp_path / "encode.json"
    manifest.write_text(json.dumps({"items": [dict(sample_run["sample_item"])]}))
    with open_db(sample_run["config"].paths.db_path) as connection:
        save_encode_job(connection, {"job_id": "production", "prefix": "tv/approved", "status": "running",
                                     "manifest_path": str(manifest), "host": sample_run["config"].remote_hosts[0],
                                     "created_at": "2026-10-04T00:00:00+00:00", "updated_at": "2026-10-04T00:00:00+00:00"})
    return staged_host.required_scratch_bytes(sample_run["sample_item"]["source_size_bytes"])


def test_sample_preserves_scratch_reserved_by_running_encode_and_recovers_after_finish(sample_run: dict[str, Any], tmp_path: Path) -> None:
    from sqlalchemy import update
    from mediaforce.core.db_tables import encode_jobs

    budget = running_encode(sample_run, tmp_path)
    free_kib = (budget + 1023) // 1024
    with local_stage(sample_run, free_kib=free_kib), pytest.raises(staged_host.StagedScratchError, match="reserved work"):
        runtime.run_sampled_calibration(**sample_run)
    sample_run["deps"].search_quality_for_source.assert_not_called()
    assert_scratch_empty(sample_run)
    with open_db(sample_run["config"].paths.db_path) as connection:
        connection.execute(update(encode_jobs).where(encode_jobs.c.job_id == "production").values(status="completed"))
    with local_stage(sample_run, free_kib=free_kib):
        runtime.run_sampled_calibration(**sample_run)
    sample_run["deps"].search_quality_for_source.assert_called_once()
    assert_scratch_empty(sample_run)


def test_sample_can_share_computer_when_space_covers_both_reservations(sample_run: dict[str, Any], tmp_path: Path) -> None:
    budget = running_encode(sample_run, tmp_path)
    with local_stage(sample_run, free_kib=(2 * budget + 1023) // 1024) as stage:
        runtime.run_sampled_calibration(**sample_run)
    assert stage.call_args.kwargs["reserved_bytes"] == budget
    assert_scratch_empty(sample_run)


def test_encode_reservations_include_active_samples_and_exclude_finished_or_own_sample(sample_run: dict[str, Any]) -> None:
    config = sample_run["config"]
    budget = staged_host.required_scratch_bytes(sample_run["sample_item"]["source_size_bytes"])
    with open_db(config.paths.db_path) as connection:
        for job_id, status in (("own-sample", "running"), ("other-sample", "starting"), ("review-ready", "pending_review")):
            save_job(connection, {"job_id": job_id, "prefix": f"tv/{job_id}", "status": status, "lane": "sample",
                                  "action": "baseline", "host": sample_run["host_data"], "sample_item": sample_run["sample_item"],
                                  "created_at": "2026-10-04T00:00:00+00:00", "updated_at": "2026-10-04T00:00:00+00:00"})
        reservations = encode_runtime.running_staged_scratch_reservations(connection, config)
        assert reservations["scratch-worker"] == 2 * budget
        reservations = encode_runtime.running_staged_scratch_reservations(connection, config, exclude_calibration_job_id="own-sample")
        assert reservations["scratch-worker"] == budget
    sample_run["calibration_job_id"] = "own-sample"
    with local_stage(sample_run, free_kib=(2 * budget + 1023) // 1024) as stage:
        runtime.run_sampled_calibration(**sample_run)
    assert stage.call_args.kwargs["reserved_bytes"] == budget
    assert_scratch_empty(sample_run)


def test_unknown_active_sample_size_holds_other_staged_work(sample_run: dict[str, Any]) -> None:
    with open_db(sample_run["config"].paths.db_path) as connection:
        save_job(connection, {"job_id": "unknown", "prefix": "tv/unknown", "status": "running", "lane": "sample",
                              "action": "baseline", "host": sample_run["host_data"], "sample_item": {},
                              "created_at": "2026-10-04T00:00:00+00:00", "updated_at": "2026-10-04T00:00:00+00:00"})
        assert encode_runtime.running_staged_scratch_reservations(connection, sample_run["config"])["scratch-worker"] is None
    with local_stage(sample_run) as stage, pytest.raises(staged_host.StagedScratchError, match="active work"):
        runtime.run_sampled_calibration(**sample_run)
    stage.assert_not_called()
    sample_run["deps"].search_quality_for_source.assert_not_called()


def test_active_sample_protects_computer_from_encode_and_admission_cleanup(sample_run: dict[str, Any]) -> None:
    from sqlalchemy import update
    from mediaforce.core.db_tables import calibration_jobs

    config = sample_run["config"]
    host = config.remote_hosts[0]
    with open_db(config.paths.db_path) as connection:
        save_job(connection, {"job_id": "using-computer", "prefix": "tv/active", "status": "running", "lane": "sample",
                              "action": "baseline", "host": sample_run["host_data"], "sample_item": sample_run["sample_item"],
                              "created_at": "2026-10-04T00:00:00+00:00", "updated_at": "2026-10-04T00:00:00+00:00"})
    prepared = {**host, "scratch_admission_started": True}
    deps = Mock()
    import threading
    deps.scratch_admission_lock = threading.Lock()
    deps.scratch_ready_hosts = {"prepared": prepared}
    assert encode_runtime._host_has_other_running_jobs(config, "finished-encode", host)
    with patch.object(encode_runtime, "_launch_scratch_admission_task") as lifecycle:
        encode_runtime._stop_unused_scratch_preparations(config, deps)
        lifecycle.assert_not_called()
        with open_db(config.paths.db_path) as connection:
            connection.execute(update(calibration_jobs).where(calibration_jobs.c.job_id == "using-computer")
                               .values(status="pending_review"))
        assert not encode_runtime._host_has_other_running_jobs(config, "finished-encode", host)
        encode_runtime._stop_unused_scratch_preparations(config, deps)
        lifecycle.assert_called_once_with(config, deps, prepared, stop=True)


def test_encode_claim_rechecks_changed_sample_budget(sample_run: dict[str, Any], tmp_path: Path) -> None:
    from sqlalchemy import update
    from mediaforce.core.db_tables import encode_jobs

    config = sample_run["config"]
    running_encode(sample_run, tmp_path)
    sample = {"job_id": "changing-sample", "prefix": "tv/changing", "status": "running", "lane": "sample",
              "action": "baseline", "host": sample_run["host_data"], "sample_item": sample_run["sample_item"],
              "created_at": "2026-10-04T00:00:00+00:00", "updated_at": "2026-10-04T00:00:00+00:00"}
    with open_db(config.paths.db_path) as connection:
        save_job(connection, sample)
        connection.execute(update(encode_jobs).where(encode_jobs.c.job_id == "production").values(status="queued"))
        connection.commit()
        selected = load_encode_job(connection, "production")
        assert selected is not None

        def select_while_sample_changes(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            with open_db(config.paths.db_path) as sample_connection:
                sample_connection.exec_driver_sql("BEGIN IMMEDIATE")
                save_job(sample_connection, {**sample, "sample_item": {**sample["sample_item"], "source_size_bytes": 999999}})
            return selected

        deps = Mock(encode_job_lease_seconds=60, now_iso=sample_run["deps"].now_iso)
        with patch.object(encode_runtime, "load_next_runnable_encode_job", side_effect=select_while_sample_changes):
            assert encode_runtime.claim_next_runnable_encode_job(connection, config, deps) is None
        queued = load_encode_job(connection, "production")
        assert queued is not None
        assert queued["status"] == "queued" and queued["attempt_count"] == 0
