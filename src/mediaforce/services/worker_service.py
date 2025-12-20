import asyncio
import hashlib
import json
import logging
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import queue
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Mapping, Optional, TYPE_CHECKING, TypedDict

from sqlmodel import Session

from mediaforce.config.logging import log_event
from mediaforce.config.paths import (
    canonicalize_mount_prefix_for_current_host,
    default_transcode_root,
    get_db_path,
    get_library_root,
    get_media_roots,
    get_transcode_output_path,
    normalize_path,
    resolve_target_height_for_path,
)
from mediaforce.config.settings import ENGINE, load_app_settings, init_db
from mediaforce.domain.types import ClassificationResult, QualityMetrics, SourceTier, TierSettings
from mediaforce.services.classification import TIER_SETTINGS, classify_source
from mediaforce.services.encoder import build_ffmpeg_command
from mediaforce.services.media_probe import probe_media, probe_media_with_interlace_detection
from mediaforce.services.metrics import (
    compute_vmaf_score,
    encode_sample_clip,
    verify_encode_quality,
    window_bitrate,
)
from mediaforce.services.notifications import send_notifications
from mediaforce.services.outlier_detection import check_for_outliers, OutlierResult
from mediaforce.services.progress import (
    finish_progress_tracking,
    start_progress_tracking,
    update_progress,
)
from mediaforce.services.queue import claim_next_file, release_claim
from mediaforce.services.encoder import (
    parse_ffmpeg_progress,
    run_ffmpeg_with_progress,
    record_encode_result,
)
from mediaforce.services.remote_settings import load_remote_settings, ensure_active_profile_settings
from mediaforce.services.watch import watch_libraries
from mediaforce.services.worker_api import (
    EvaluationSubmitResponse,
    WorkerApiClient,
    WorkerApiError,
    WorkerEncodeReportPayload,
    WorkerMetricsPayload,
    WorkerOutlierPayload,
)
from mediaforce.services.quality_loop import (
    build_motion_weighted_plan,
    run_profile_quality_loop,
    QualityLoopResult,
)
from mediaforce.db import ProfileEvaluation, now_iso

if TYPE_CHECKING:
    from watchfiles import Change, awatch  # type: ignore
else:
    try:
        from watchfiles import Change, awatch
    except ImportError:
        Change = None
        awatch = None

AUTOUPDATE_FILES: list[str] = []

STALE_CLAIM_SECONDS = 8 * 60 * 60
REMOTE_SETTINGS_URL_GLOBAL: str | None = None


class VmafSamplePayload(TypedDict):
    kind: str
    start_sec: float
    duration_sec: float
    weight: float
    vmaf: float


class EvaluationSummaryPayload(TypedDict):
    weighted: Optional[float]
    minimum: Optional[float]
    median: Optional[float]


def _coerce_float(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _coerce_str(value: object, default: str) -> str:
    if value is None:
        return default
    return str(value)


def _parse_evaluation_submit_response(
    response: EvaluationSubmitResponse,
    *,
    fallback_tier: str,
) -> tuple[str, str, str, EvaluationSummaryPayload]:
    initial_profile = _coerce_str(response.initial_profile, fallback_tier)
    selected_profile = _coerce_str(response.selected_profile, fallback_tier)
    decision = _coerce_str(response.decision, "keep")

    summary_payload: EvaluationSummaryPayload = {
        "weighted": None,
        "minimum": None,
        "median": None,
    }
    if response.summary is not None:
        summary_payload["weighted"] = _coerce_float(response.summary.weighted)
        summary_payload["minimum"] = _coerce_float(response.summary.min)
        summary_payload["median"] = _coerce_float(response.summary.median)

    return initial_profile, selected_profile, decision, summary_payload


def _download_file(url: str, dest: pathlib.Path, expected_sha256: str | None = None) -> bool:
    try:
        with urllib.request.urlopen(url) as resp:
            data = resp.read()
    except Exception:
        return False

    if expected_sha256:
        h = hashlib.sha256(data).hexdigest()
        if h != expected_sha256:
            return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return True


def maybe_autoupdate(base_url: str, files: list[str]) -> bool:
    if not base_url.endswith('/'):
        base_url += '/'

    manifest_url = base_url + 'manifest.json'
    try:
        with urllib.request.urlopen(manifest_url) as resp:
            manifest = json.loads(resp.read().decode())
    except Exception:
        return False

    changed = False
    file_info = manifest.get('files', {}) if isinstance(manifest, dict) else {}
    base_dir = pathlib.Path(__file__).parent.parent  # src/mediaforce

    if files:
        selected = list(files)
    else:
        selected = sorted([str(k) for k in file_info.keys()])

    for fname in selected:
        info = file_info.get(fname)
        if not info:
            continue
        target = base_dir / fname
        expected = info.get('sha256')
        
        local_hash = None
        if target.exists():
            h = hashlib.sha256()
            with target.open('rb') as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            local_hash = h.hexdigest()
        if local_hash == expected:
            continue

        if _download_file(base_url + fname, target, expected_sha256=expected):
            changed = True

    return changed


def _resolve_machine_name() -> str:
    override = (os.getenv("MEDIAFORCE_MACHINE_NAME") or "").strip()
    if override:
        return override

    hostname = socket.gethostname().strip()
    if "." in hostname:
        hostname = hostname.split(".", 1)[0]
    return hostname


def parse_until_time(until_str: str) -> Optional[datetime]:
    try:
        hour, minute = map(int, until_str.split(":"))
        now = datetime.now()
        until = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if until <= now:
            until = until.replace(day=until.day + 1)
        return until
    except (ValueError, AttributeError):
        return None


def within_offpeak(settings: Any) -> bool:
    if not settings.offpeak_enabled:
        return True
    try:
        start_h, start_m = map(int, settings.offpeak_start.split(":"))
        end_h, end_m = map(int, settings.offpeak_end.split(":"))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23 and 0 <= start_m <= 59 and 0 <= end_m <= 59):
            return True
    except Exception:
        return True
    now = datetime.now()
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def run_ffmpeg_with_progress_api(
    cmd: list[str],
    api_client: WorkerApiClient,
    machine: str,
    progress_id: int,
    duration_sec: float,
) -> subprocess.CompletedProcess:
    cmd_with_progress = cmd.copy()
    try:
        idx = cmd_with_progress.index("-hide_banner") + 1
    except ValueError:
        idx = 1
    cmd_with_progress.insert(idx, "-progress")
    cmd_with_progress.insert(idx + 1, "pipe:1")
    cmd_with_progress.insert(idx, "-nostats")

    process = subprocess.Popen(
        cmd_with_progress,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    stdout_lines: queue.Queue[str] = queue.Queue()

    def _drain_stdout() -> None:
        try:
            if process.stdout is None:
                return
            for line in process.stdout:
                stdout_lines.put(line)
        except Exception:
            return

    threading.Thread(target=_drain_stdout, daemon=True).start()

    accumulated: dict[str, float | int] = {}
    last_update = time.time()
    last_control_check = time.time()

    while True:
        try:
            line = stdout_lines.get(timeout=1.0)
        except queue.Empty:
            line = ""

        if not line and process.poll() is not None and stdout_lines.empty():
            break

        if line:
            parsed = parse_ffmpeg_progress(line)
            accumulated.update(parsed)

        now = time.time()

        if now - last_control_check >= 5:
            try:
                control = api_client.control(machine=machine)
                if bool(control.stop_now):
                    try:
                        api_client.ack(machine=machine, action="stop_now")
                    except WorkerApiError:
                        pass
                    try:
                        process.terminate()
                        process.wait(timeout=3)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                    return subprocess.CompletedProcess(
                        args=cmd_with_progress,
                        returncode=255,
                        stdout="",
                        stderr="stopped_now",
                    )
            except WorkerApiError:
                pass
            last_control_check = now

        if now - last_update >= 2 and accumulated:
            try:
                api_client.progress_update(
                    progress_id=progress_id,
                    frame=int(accumulated.get("frame", 0) or 0),
                    fps=float(accumulated.get("fps", 0) or 0.0),
                    speed=float(accumulated.get("speed", 0) or 0.0),
                    bitrate_kbps=accumulated.get("bitrate_kbps"),
                    size_bytes=int(accumulated.get("size_bytes", 0) or 0),
                    time_encoded_sec=float(accumulated.get("time_encoded_sec", 0) or 0.0),
                    duration_sec=duration_sec,
                )
            except WorkerApiError:
                pass
            last_update = now

    _, stderr = process.communicate()

    return subprocess.CompletedProcess(
        args=cmd_with_progress,
        returncode=process.returncode,
        stdout="",
        stderr=stderr,
    )


def run_worker_loop(
    path_str: str,
    output_dir_str: str,
    until: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    verify: bool = False,
    verify_duration: int = 60,
    sample_vmaf: bool = True,
    sample_count: int = 3,
    sample_length: float = 8.0,
    sample_motion_aware: bool = True,
    hw_decode: bool = True,
    hw_encode: bool = False,
    offpeak_enabled: bool = False,
    offpeak_start: Optional[str] = None,
    offpeak_end: Optional[str] = None,
    autoupdate_url: Optional[str] = None,
    autoupdate_interval: int = 0,
    api_url: Optional[str] = None,
    settings_url: Optional[str] = None,
    profile_settings_url: Optional[str] = None,
    max_concurrency: Optional[int] = None,
) -> int:
    global REMOTE_SETTINGS_URL_GLOBAL
    if profile_settings_url:
        REMOTE_SETTINGS_URL_GLOBAL = profile_settings_url

    raw_path = pathlib.Path(path_str)
    try:
        path = raw_path.resolve()
    except Exception:
        path = raw_path

    library_root = get_library_root(path)

    api_url = api_url or os.getenv("MEDIAFORCE_API_URL")
    use_api = bool(api_url)
    api_client: Optional[WorkerApiClient] = None
    db_path: Optional[pathlib.Path] = None

    if use_api:
        assert api_url is not None
        api_client = WorkerApiClient(api_url)
        log_event(20, "worker_api_enabled", url=api_url)
    else:
        try:
            path.stat()
        except PermissionError:
            pass
        except FileNotFoundError:
            log_event(40, "run_path_missing", path=str(path))
            return 1

        db_path = get_db_path(library_root)
        if not db_path.exists():
            log_event(40, "run_db_missing", db=str(db_path))
            return 1

    until_time = None
    if until:
        until_time = parse_until_time(until)
        if until_time is None:
            log_event(40, "run_until_invalid", value=until)
            return 1
        log_event(20, "run_until_set", until=until)

    if autoupdate_url:
        if maybe_autoupdate(autoupdate_url, AUTOUPDATE_FILES):
            log_event(20, "autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    if use_api and not settings_url and api_url:
        settings_url = f"{api_url.rstrip('/')}/api/settings/current"
    
    app_settings = None
    if settings_url:
        app_settings = load_remote_settings(settings_url)
        if app_settings is None:
            log_event(30, "remote_settings_load_failed", url=settings_url)
    if app_settings is None:
        app_settings = load_app_settings()

    transcode_root_str = output_dir_str
    if use_api and app_settings.transcode_root:
        try:
            default_root = default_transcode_root()
        except Exception:
            default_root = ""
        if output_dir_str == default_root:
            transcode_root_str = app_settings.transcode_root

    transcode_root = canonicalize_mount_prefix_for_current_host(pathlib.Path(transcode_root_str).expanduser())
    transcode_root = normalize_path(transcode_root)
    transcode_root.mkdir(parents=True, exist_ok=True)

    machine = _resolve_machine_name()
    log_event(20, "run_start", machine=machine, library=str(library_root), output=str(transcode_root))

    if max_concurrency:
        app_settings.max_concurrency = max(max_concurrency, 1)
    if offpeak_enabled:
        app_settings.offpeak_enabled = True
    if offpeak_start:
        app_settings.offpeak_start = offpeak_start or app_settings.offpeak_start
    if offpeak_end:
        app_settings.offpeak_end = offpeak_end or app_settings.offpeak_end

    last_update_check = time.time()

    def check_autoupdate():
        nonlocal last_update_check
        if not autoupdate_url or autoupdate_interval <= 0:
            return
        now = time.time()
        if now - last_update_check < autoupdate_interval:
            return
        last_update_check = now
        if maybe_autoupdate(autoupdate_url, AUTOUPDATE_FILES):
            log_event(20, "autoupdate_restart")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    session: Optional[Session] = None
    if not use_api:
        assert db_path is not None
        session = init_db(db_path)

    encoded_count = 0
    error_count = 0
    outlier_count = 0

    max_concurrent_jobs = int(os.getenv("MEDIAFORCE_MAX_CONCURRENCY", app_settings.max_concurrency or 1))
    if max_concurrency:
        max_concurrent_jobs = max_concurrency
    if max_concurrent_jobs < 1:
        max_concurrent_jobs = 1
    active_slots = 0

    while True:
        check_autoupdate()
        library_available = True
        library_status_message = None
        if use_api:
            try:
                path.stat()
            except PermissionError:
                library_available = True
            except FileNotFoundError:
                library_available = False
                library_status_message = f"Library not mounted: {path}"

        if until_time and datetime.now() >= until_time:
            log_event(20, "run_until_reached", until=until)
            break

        if app_settings.offpeak_enabled and not within_offpeak(app_settings):
            log_event(20, "offpeak_pause", window=f"{app_settings.offpeak_start}-{app_settings.offpeak_end}")
            time.sleep(300)
            continue

        if active_slots >= max_concurrent_jobs:
            time.sleep(1)
            continue

        claimed: Optional[dict] = None
        override_tier: Optional[str] = None
        
        if use_api and api_client is not None:
            try:
                claim_result = api_client.claim(
                    machine=machine,
                    available=library_available,
                    sample_path=(str(path) if not library_available else None),
                    status_message=library_status_message,
                )
            except WorkerApiError as e:
                log_event(40, "worker_api_claim_failed", error=str(e))
                break

            if claim_result is None:
                log_event(20, "queue_empty")
                if dry_run:
                    break
                time.sleep(10)
                continue

            claim_obj = claim_result.claim
            if claim_obj is None:
                if not library_available:
                    log_event(30, "run_library_unavailable", path=str(path))
                    time.sleep(30)
                    continue
                if claim_result.control_mode == "stop":
                    log_event(20, "worker_stopped", machine=machine)
                else:
                    event = "worker_paused" if claim_result.control_mode == "drain" else "queue_empty"
                    log_event(20, event)
                if dry_run:
                    break
                time.sleep(10)
                continue

            claimed = {"id": claim_obj.id, "path": claim_obj.path}
            override_tier = claim_obj.override_tier
        else:
            assert session is not None
            claimed = claim_next_file(session, machine, stale_seconds=STALE_CLAIM_SECONDS, now_iso=now_iso)
            if claimed is None:
                log_event(20, "queue_empty")
                break

        source_path = normalize_path(pathlib.Path(claimed["path"]))
        log_event(20, "encode_start", index=encoded_count + 1, file=str(source_path))

        log_event(20, "detect_interlace", file=str(source_path))
        info = probe_media_with_interlace_detection(source_path)
        if info is None:
            log_event(40, "probe_failed", file=str(source_path))
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                except WorkerApiError as e:
                    log_event(30, "worker_api_release_failed", error=str(e))
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=False, now_iso=now_iso)
            error_count += 1
            continue

        classification = classify_source(info, override_tier)
        settings = classification.recommended_settings
        tier = classification.tier.value

        log_event(
            20,
            "classification",
            file=str(source_path),
            tier=tier,
            crf=settings.crf,
            preset=settings.preset,
            denoise=settings.denoise or "none",
        )
        if info.is_interlaced:
            log_event(20, "interlaced_detected", file=str(source_path))

        output_path = get_transcode_output_path(source_path, transcode_root)
        if not output_path:
             source_str = str(source_path)
             rel_path = None
             for root in get_media_roots():
                 if source_str.startswith(root):
                     rel_path = source_path.relative_to(pathlib.Path(root))
                     break
             else:
                 rel_path = pathlib.Path(source_path.name)
             
             output_dir = transcode_root / rel_path.parent
             output_dir.mkdir(parents=True, exist_ok=True)
             stem = source_path.stem
             for marker in [".x264", ".x265", ".h264", ".h265", ".HEVC", ".AVC"]:
                 stem = stem.replace(marker, "")
             output_path = output_dir / f"{stem}.AV1.mp4"

        if output_path.exists() and not force:
            log_event(20, "skip_output_exists", output=str(output_path))
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=True)
                except WorkerApiError as e:
                    log_event(30, "worker_api_release_failed", error=str(e))
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=True, now_iso=now_iso)
            continue

        target_height, target_height_reason = resolve_target_height_for_path(source_path, app_settings)

        eval_obj_id: Optional[int] = None
        
        if sample_vmaf:
            loop_result: Optional[Any] = None
            if session is not None:
                active_settings_source = ensure_active_profile_settings(
                    session, remote_url=REMOTE_SETTINGS_URL_GLOBAL
                )
                def measure(item) -> Optional[float]:
                    enc_path, enc_size = encode_sample_clip(
                        source_path,
                        settings,
                        info,
                        item.start_sec,
                        item.duration_sec,
                        target_height,
                    )
                    if not enc_path:
                        return None
                    vmaf = compute_vmaf_score(
                        source_path,
                        enc_path,
                        item.start_sec,
                        item.duration_sec,
                        encoded_size=enc_size,
                    )
                    try:
                        enc_path.unlink(missing_ok=True)
                        enc_path.parent.rmdir()
                    except OSError:
                        pass
                    return vmaf

                loop_result = run_profile_quality_loop(
                    session,
                    media_id=claimed["id"],
                    source_path=source_path,
                    duration_seconds=float(info.duration_seconds or 0.0),
                    initial_profile=tier,
                    settings_source=active_settings_source,
                    sample_length=sample_length,
                    motion_aware=sample_motion_aware,
                    measure_vmaf=measure,
                    window_bitrate=window_bitrate,
                    target_height=target_height,
                    target_height_reason=target_height_reason,
                )
                eval_obj_id = loop_result.evaluation_id

            elif use_api and api_client is not None:
                duration_seconds = float(info.duration_seconds or 0.0)
                if duration_seconds > 0.0 and sample_length > 0:
                    try:
                        eval_id, _thresholds = api_client.evaluation_start(
                            media_id=int(claimed["id"]),
                            initial_profile=tier,
                            sample_length=float(sample_length),
                        )
                        eval_obj_id = eval_id

                        plan = build_motion_weighted_plan(
                            source_path=source_path,
                            duration_seconds=duration_seconds,
                            sample_length=float(sample_length),
                            motion_aware=bool(sample_motion_aware),
                            window_bitrate=window_bitrate,
                        )

                        samples_payload: list[VmafSamplePayload] = []
                        for item in plan:
                            enc_path, enc_size = encode_sample_clip(
                                source_path,
                                settings,
                                info,
                                item.start_sec,
                                item.duration_sec,
                                target_height,
                            )
                            if not enc_path:
                                continue
                            vmaf = compute_vmaf_score(
                                source_path,
                                enc_path,
                                item.start_sec,
                                item.duration_sec,
                                encoded_size=enc_size,
                            )
                            try:
                                enc_path.unlink(missing_ok=True)
                                enc_path.parent.rmdir()
                            except OSError:
                                pass
                            if vmaf is None:
                                continue
                            samples_payload.append(
                                {
                                    "kind": item.kind,
                                    "start_sec": item.start_sec,
                                    "duration_sec": item.duration_sec,
                                    "weight": item.weight,
                                    "vmaf": float(vmaf),
                                }
                            )

                        resp = api_client.evaluation_submit_samples(
                            evaluation_id=eval_id,
                            samples=samples_payload,
                            target_height=target_height,
                            target_height_reason=target_height_reason,
                        )
                        initial_profile, selected_profile, decision, eval_summary = (
                            _parse_evaluation_submit_response(resp, fallback_tier=tier)
                        )
                        loop_result = SimpleNamespace(
                            evaluation_id=eval_id,
                            initial_profile=initial_profile,
                            selected_profile=selected_profile,
                            decision=decision,
                            summary=SimpleNamespace(
                                weighted=eval_summary.get("weighted"),
                                minimum=eval_summary.get("minimum"),
                                median=eval_summary.get("median"),
                            ),
                            thresholds=SimpleNamespace(
                                min_vmaf=_thresholds.min,
                                median_vmaf=_thresholds.median,
                            ),
                        )
                    except WorkerApiError as e:
                        log_event(30, "quality_loop_api_failed", error=str(e))
                        loop_result = None

            if loop_result is not None and loop_result.selected_profile != tier:
                try:
                    new_tier = SourceTier(loop_result.selected_profile)
                    classification = ClassificationResult(
                        tier=new_tier,
                        confidence=classification.confidence,
                        reasons=classification.reasons
                        + [
                            f"quality_loop:{tier}->{loop_result.selected_profile}"
                            f" (weighted={loop_result.summary.weighted})"
                        ],
                        recommended_settings=TIER_SETTINGS[new_tier],
                    )
                    settings = classification.recommended_settings
                    tier = classification.tier.value
                    
                    log_event(
                        20,
                        "quality_loop_result",
                        file=str(source_path),
                        eval_id=loop_result.evaluation_id,
                        initial=loop_result.initial_profile,
                        selected=loop_result.selected_profile,
                        decision=loop_result.decision,
                        weighted=loop_result.summary.weighted,
                    )

                    try:
                        send_notifications(
                            event="quality_loop_adjustment",
                            summary=f"Quality Loop: Adjusted {source_path.name} from {tier} to {loop_result.selected_profile} (VMAF: {loop_result.summary.weighted:.1f})",
                            data={
                                "source_path": str(source_path),
                                "initial_tier": tier,
                                "selected_tier": loop_result.selected_profile,
                                "vmaf_weighted": loop_result.summary.weighted,
                                "machine": machine,
                            }
                        )
                    except Exception:
                        pass
                except ValueError:
                    pass

        cmd = build_ffmpeg_command(
            source_path,
            output_path,
            settings,
            info,
            max_height=target_height,
            hw_decode=hw_decode,
            hw_encode=hw_encode,
        )
        started_at = datetime.now().isoformat()

        if dry_run:
            log_event(20, "dry_run", output=str(output_path))
            if use_api and api_client is not None:
                try:
                    api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                except WorkerApiError:
                    pass
            else:
                assert session is not None
                release_claim(session, claimed["id"], success=False, now_iso=now_iso)
            encoded_count += 1
            continue

        log_event(20, "encode_launch", output=output_path.name)
        active_slots += 1

        total_frames = None
        if info.video_framerate and info.duration_seconds:
             total_frames = int(info.video_framerate * info.duration_seconds)

        duration_sec = float(info.duration_seconds or 0.0)
        
        progress_id = 0
        if use_api and api_client is not None:
             progress_id = api_client.progress_start(
                 source_id=int(claimed["id"]),
                 source_path=str(source_path),
                 output_path=str(output_path),
                 machine=machine,
                 tier=tier,
                 duration_sec=duration_sec,
                 total_frames=total_frames,
             )
        else:
             assert session is not None
             progress_id = start_progress_tracking(
                 session,
                 claimed["id"],
                 str(source_path),
                 str(output_path),
                 machine,
                 tier,
                 duration_sec,
                 total_frames=total_frames,
             )

        try:
            if use_api and api_client is not None:
                result = run_ffmpeg_with_progress_api(cmd, api_client, machine, progress_id, duration_sec)
            else:
                assert session is not None
                result = run_ffmpeg_with_progress(cmd, session, progress_id, duration_sec, update_progress)
            
            if result.returncode != 0 and hw_decode:
                 stderr = result.stderr or ""
                 if any(x in stderr for x in ("cuInit(0) failed", "CUDA_ERROR")):
                      log_event(30, "hw_decode_failed_fallback", machine=machine)
                      cmd = build_ffmpeg_command(source_path, output_path, settings, info, max_height=target_height, hw_decode=False, hw_encode=hw_encode)
                      if use_api and api_client is not None:
                           result = run_ffmpeg_with_progress_api(cmd, api_client, machine, progress_id, duration_sec)
                      else:
                           assert session is not None
                           result = run_ffmpeg_with_progress(cmd, session, progress_id, duration_sec, update_progress)

            if result.returncode != 0:
                raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)

            try:
                output_size = output_path.stat().st_size
            except FileNotFoundError:
                output_size = 0
            try:
                source_size = source_path.stat().st_size
            except FileNotFoundError:
                source_size = 0
            
            output_info = probe_media(output_path)
            output_bitrate = output_info.video_bitrate_kbps if output_info else None
            
            log_event(20, "encode_complete", output_mb=output_size//1024//1024)

            metrics = None
            outlier_result = None

            if verify:
                 if use_api and api_client is not None:
                      try:
                           api_client.progress_update(progress_id=progress_id, duration_sec=duration_sec, phase="verifying", phase_detail="Quality checks")
                      except WorkerApiError:
                           pass
                 else:
                      assert session is not None
                      update_progress(session, progress_id, phase="verifying", phase_detail="Quality checks")
                 
                 log_event(20, "verify_start", file=str(source_path))
                 try:
                      metrics = verify_encode_quality(source_path, output_path, sample_duration_sec=verify_duration)
                      if metrics:
                           log_event(20, "verify_metrics", vmaf=metrics.vmaf)
                           outlier_result = check_for_outliers(source_path, output_path, metrics=metrics)
                           if outlier_result.is_outlier:
                                log_event(30, "outlier_flagged", reasons=", ".join(outlier_result.reasons))
                           outlier_count += 1
                 except Exception as e:
                      log_event(40, "verify_exception", error=str(e))

            if use_api and api_client is not None:
                 metrics_payload = None
                 if metrics:
                      metrics_payload = WorkerMetricsPayload(
                           ssim=metrics.ssim,
                           psnr=metrics.psnr,
                           vmaf=metrics.vmaf,
                           sample_duration_sec=metrics.sample_duration_sec,
                           sample_start_sec=metrics.sample_start_sec,
                      )
                 outlier_payload = None
                 if outlier_result:
                      outlier_payload = WorkerOutlierPayload(
                           is_outlier=outlier_result.is_outlier,
                           reasons=list(outlier_result.reasons or []),
                      )

                 payload = WorkerEncodeReportPayload(
                      source_id=int(claimed["id"]),
                      source_path=str(source_path),
                      tier=tier,
                      crf=settings.crf,
                      preset=settings.preset,
                      film_grain=settings.film_grain,
                      denoise=settings.denoise,
                      output_path=str(output_path),
                      output_size_bytes=int(output_size),
                      output_bitrate_kbps=output_bitrate,
                      source_size_bytes=int(source_size),
                      machine=machine,
                      started_at=started_at,
                      success=True,
                      profile_eval_id=eval_obj_id,
                      progress_id=progress_id,
                      metrics=metrics_payload,
                      outlier=outlier_payload,
                 )
                 try:
                     api_client.report_encode_result(payload=payload)
                 except WorkerApiError:
                     try:
                         api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                     except WorkerApiError:
                         pass
            else:
                 assert session is not None
                 finish_progress_tracking(session, progress_id, success=True)
                 result_id = record_encode_result(
                     session, claimed["id"], str(source_path), tier, settings,
                     str(output_path), output_size, output_bitrate, source_size,
                     machine, started_at,
                     metrics=metrics,
                     outlier_result=outlier_result,
                     profile_eval_id=eval_obj_id,
                 )
                 if eval_obj_id:
                      eval_obj = session.get(ProfileEvaluation, eval_obj_id)
                      if eval_obj:
                           eval_obj.encode_result_id = result_id
                           eval_obj.updated_at = datetime.now().isoformat()
                           session.add(eval_obj)
                           session.commit()
                 release_claim(session, claimed["id"], success=True, now_iso=now_iso)

                 size_increase = output_size > source_size if source_size > 0 else False
                 saved_bytes = max(0, source_size - output_size) if source_size > 0 else 0
                 reduction_pct = (1 - (output_size / source_size)) * 100 if source_size > 0 else None
                 event = "encode_size_increase" if size_increase else "encode_completed"
                 summary = (
                     f"{event}: {source_path.name}"
                     + (f" ({saved_bytes // 1024 // 1024} MB saved)" if saved_bytes else "")
                     + (" (size increased!)" if size_increase else "")
                 )
                 try:
                     send_notifications(
                         event=event,
                         summary=summary,
                         data={
                             "encode_result_id": result_id,
                             "success": True,
                             "source_path": str(source_path),
                             "tier": tier,
                             "machine": machine,
                             "saved_bytes": int(saved_bytes),
                             "reduction_pct": reduction_pct,
                             "vmaf": metrics.vmaf if metrics else None,
                         }
                     )
                 except Exception:
                     pass
            
            encoded_count += 1

        except subprocess.CalledProcessError as e:
            error_msg = str(e.stderr)[:500] if e.stderr else str(e)
            log_event(40, "encode_failed", error=error_msg)
            
            if use_api and api_client is not None:
                 payload = WorkerEncodeReportPayload(
                      source_id=int(claimed["id"]),
                      source_path=str(source_path),
                      tier=tier,
                      crf=settings.crf,
                      preset=settings.preset,
                      film_grain=settings.film_grain,
                      denoise=settings.denoise,
                      output_path=str(output_path),
                      output_size_bytes=0,
                      output_bitrate_kbps=None,
                      source_size_bytes=int(source_path.stat().st_size),
                      machine=machine,
                      started_at=started_at,
                      success=False,
                      error_message=error_msg,
                      profile_eval_id=eval_obj_id,
                      progress_id=progress_id,
                 )
                 try:
                      api_client.report_encode_result(payload=payload)
                 except WorkerApiError:
                      try:
                           api_client.release(machine=machine, source_id=int(claimed["id"]), success=False)
                      except WorkerApiError:
                           pass
            else:
                 assert session is not None
                 finish_progress_tracking(session, progress_id, success=False, error_msg=error_msg)
                 result_id = record_encode_result(
                     session, claimed["id"], str(source_path), tier, settings,
                     str(output_path), 0, None, source_path.stat().st_size,
                     machine, started_at, error_msg=error_msg, profile_eval_id=eval_obj_id,
                 )
                 if eval_obj_id:
                      eval_obj = session.get(ProfileEvaluation, eval_obj_id)
                      if eval_obj:
                           eval_obj.encode_result_id = result_id
                           eval_obj.status = "failed"
                           eval_obj.updated_at = datetime.now().isoformat()
                           session.add(eval_obj)
                           session.commit()
                 release_claim(session, claimed["id"], success=False, now_iso=now_iso)

                 try:
                     send_notifications(
                         event="encode_failed",
                         summary=f"encode_failed: {source_path.name} on {machine}",
                         data={
                             "source_path": str(source_path),
                             "machine": machine,
                             "error": error_msg,
                         }
                     )
                 except Exception:
                     pass
            
            error_count += 1
            if output_path.exists():
                 output_path.unlink()
            time.sleep(5)
        finally:
            active_slots = max(active_slots - 1, 0)

    if session is not None:
        session.close()

    log_event(20, "run_complete", encoded=encoded_count, errors=error_count, outliers=outlier_count)
    return 0 if error_count == 0 else 1


def run_watch(
    autoupdate_url: Optional[str] = None,
    autoupdate_interval: int = 3600,
    settings_url: Optional[str] = None,
) -> int:
    if awatch is None or Change is None:
        log_event(40, "watch_unavailable", error="watchfiles_not_installed")
        return 1

    if autoupdate_url:
        autoupdate_url_str = autoupdate_url
        updated = maybe_autoupdate(autoupdate_url_str, AUTOUPDATE_FILES)
        if updated:
            log_event(20, "autoupdate_restart", component="watch")
            os.execv(sys.executable, [sys.executable] + sys.argv)

    env_settings_url = os.getenv("MEDIAFORCE_REMOTE_SETTINGS_URL")
    effective_settings_url = settings_url or env_settings_url
    settings = load_remote_settings(effective_settings_url) if effective_settings_url else load_app_settings()
    if settings is None:
        log_event(40, "settings_load_failed", url=effective_settings_url)
        return 1

    async def runner():
        if autoupdate_url:
            autoupdate_url_str = autoupdate_url
            async def updater():
                while True:
                    await asyncio.sleep(float(autoupdate_interval))
                    if maybe_autoupdate(autoupdate_url_str, AUTOUPDATE_FILES):
                        log_event(20, "autoupdate_restart", component="watch")
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            asyncio.create_task(updater())
        await watch_libraries(settings)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        log_event(20, "watch_stop")
    return 0
