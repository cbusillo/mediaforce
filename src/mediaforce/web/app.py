#!/usr/bin/env python3
# mypy: ignore-errors
# pyright: reportMissingImports=false, reportOptionalOperand=false, reportAttributeAccessIssue=false
"""
Mediaforce web interface for managing encoding queues and monitoring progress.
"""

import asyncio
import csv
import io
import json
import os
import pathlib
import platform as platform_mod
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, Callable, Any, Iterable, Iterator, Sequence
from mediaforce.config.logging import configure_logging, env_log_config

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from mediaforce.config.settings import AppSettings, LibrarySettings, load_app_settings, save_app_settings, ENGINE
from mediaforce.db.repository.session import session_scope
from mediaforce.db.repository.media import MediaRepository
from mediaforce.db.repository.queue import QueueRepository
from mediaforce.db.repository.base import Pagination
from mediaforce.db.repository.stats import StatsRepository
from mediaforce.db.repository.encode import EncodeRepository
from mediaforce.db.repository.progress import ProgressRepository
from mediaforce.config.paths import iter_libraries_for_current_host, normalize_path
from mediaforce.config.settings import INVENTORY_DB
from mediaforce.domain.types import OutlierResult, QualityMetrics
from mediaforce.services.encoder import record_encode_result
from mediaforce.services.progress import finish_progress_tracking, start_progress_tracking, update_progress
from mediaforce.services.queue import claim_next_file, release_claim
from mediaforce.services.watch import watch_libraries
from mediaforce.services.notifications import send_notifications
from mediaforce.core import ensure_active_profile_settings
from mediaforce.services.promote import (
    promote_encoded_file_atomic,
    rollback_from_manifest,
    rollback_promote,
)
from mediaforce.services.quality_feedback import flag_profile_choice
from mediaforce.services.quality_loop import (
    VmafSampleResult,
    VmafThresholds,
    extract_thresholds,
    finalize_profile_evaluation,
    start_profile_evaluation,
)
from mediaforce.domain.types import TierSettings
from mediaforce.db import (
    MediaItem,
    EncodeResult,
    ShowOverride,
    WorkerRegistry,
    ProfileEvaluation,
    ProfileSettingsSource,
    VmafSample,
    ProfileChoiceFeedback,
    RetrainingCandidate,
    EncodeProgress,
    now_iso,
    ensure_schema,
)
from sqlalchemy import func, desc
from sqlmodel import select, Session
from dataclasses import asdict
from mediaforce.web.charts import sparkline_svg

IS_MAC = platform_mod.system() == "Darwin"
# Ensure the base logger is configured for shared service logs that emit events
# against the default "mediaforce" component.
configure_logging(env_log_config(component="mediaforce"))
logger = configure_logging(env_log_config(component="mediaforce.web"))
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _iter_csv_bytes(header: Sequence[str], rows: Iterable[Sequence[object]]) -> Iterator[bytes]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(header))
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow(list(row))
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


def _csv_response(*, filename: str, header: Sequence[str], rows: Iterable[Sequence[object]]) -> StreamingResponse:
    response = StreamingResponse(
        _iter_csv_bytes(header, rows),
        media_type="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def get_default_library_path() -> str:
    """Return the primary library path for this host.

    We use the first configured library from the shared settings file,
    falling back to the historical tv path if needed. This keeps the web
    UI aligned with the CLI and watcher configuration while staying
    compatible with existing deployments.
    """

    settings: AppSettings = load_app_settings()
    libraries = iter_libraries_for_current_host(settings)
    if libraries:
        # Use the first configured library for now (typically TV).
        _lib, root = libraries[0]
        return str(root)

    return "/Volumes/media/tv" if IS_MAC else "/mnt/media/tv"


DEFAULT_LIBRARY = get_default_library_path()


def get_library_status(session: Session | None = None) -> list[dict]:
    """Return library list with last_scan timestamp per library."""

    if session is None:
        with session_scope() as scoped:
            return get_library_status(scoped)

    settings = load_app_settings()
    libs = []
    media_repo = MediaRepository(session)

    for lib, root in iter_libraries_for_current_host(settings):
        db = INVENTORY_DB
        running = SCAN_STATUS.get(str(root)) == "running"
        last_scan = None
        if db.exists():
            try:
                last_scan = media_repo.last_scan_ts(library_id=lib.id)
            except Exception:
                last_scan = None
        libs.append({
            "lib": lib,
            "root": str(root),
            "db": str(db),
            "last_scan": last_scan,
            "running": running,
        })
    return libs


def get_worker_status(library_root: Optional[str] = None) -> list[dict]:
    """Return active workers based on encode_progress entries."""
    _ = library_root
    try:
        with session_scope() as session:
            return ProgressRepository(session).list_workers()
    except Exception:
        return []


def _watch_status_snapshot() -> dict:
    """Return a lightweight, read-only copy of the current watch status."""

    return {
        "running": WATCH_STATUS.get("running", False),
        "paused": WATCH_STATUS.get("paused", False),
        "libraries": WATCH_STATUS.get("libraries", []),
        "message": WATCH_STATUS.get("message", "idle"),
    }


def _parse_size_param(value: Optional[str]) -> Optional[int]:
    """Convert a size parameter in MB/GB suffix to bytes (None if invalid)."""

    if value is None:
        return None
    try:
        text = value.strip().lower()
        if not text:
            return None
        multiplier = 1024 * 1024
        if text.endswith("gb"):
            multiplier *= 1024
            text = text[:-2]
        elif text.endswith("mb"):
            text = text[:-2]
        num = float(text)
        if num < 0:
            return None
        return int(num * multiplier)
    except Exception:
        return None


def _nav_status() -> dict:
    """Build navigation status badges (scan + watch) with last-scan details."""

    libs = get_library_status()
    running_scans = [entry["root"] for entry in libs if entry.get("running")]
    last_scan_map = {entry["lib"].name: entry.get("last_scan") for entry in libs}
    latest_scan = None
    for ts in last_scan_map.values():
        if ts:
            latest_scan = ts if latest_scan is None else max(latest_scan, ts)

    return {
        "scan_running": bool(running_scans),
        "scan_libraries": running_scans,
        "scan_last": last_scan_map,
        "scan_latest": latest_scan,
        "watch_running": WATCH_STATUS.get("running", False),
        "watch_paused": WATCH_STATUS.get("paused", False),
        "watch_message": WATCH_STATUS.get("message", "idle"),
    }


app = FastAPI(title="Mediaforce", description="Content-aware media encoding management")
STATIC_DIR = PROJECT_ROOT / "src" / "mediaforce" / "web" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Setup Jinja2 templates
templates_dir = pathlib.Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))


# In-process watch task state
WATCH_TASK: Optional[asyncio.Task] = None
WATCH_STATUS: dict = {
    "running": False,
    "paused": False,
    "libraries": [],
    "message": "idle",
}

# Track web-triggered scan status
SCAN_STATUS: dict[str, str] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    try:
        ensure_schema(ENGINE)
    except Exception:
        logger.warning("Failed to ensure DB schema", exc_info=True)

    try:
        if _get_watch_libraries():
            await _start_watch_task()
    except Exception:
        WATCH_STATUS.update({"running": False, "message": "watch start failed"})

    try:
        yield
    finally:
        try:
            await _stop_watch_task(message="shutdown", paused=False)
        except Exception:
            WATCH_STATUS.update({"running": False, "message": "shutdown failed"})


app.router.lifespan_context = _lifespan


def _require_worker_api_auth(request: Request) -> None:
    required = os.getenv("MEDIAFORCE_API_TOKEN")
    if not required:
        return

    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        provided = auth.split(" ", 1)[1].strip()
    else:
        provided = request.headers.get("x-mediaforce-token") or ""

    if provided != required:
        raise HTTPException(status_code=401, detail="unauthorized")


ALLOWED_RAW_FILES: dict[str, str] = {
    "__init__.py": "src/mediaforce/__init__.py",
    "core.py": "src/mediaforce/core.py",
    "db/__init__.py": "src/mediaforce/db/__init__.py",
    "db/models.py": "src/mediaforce/db/models.py",
    "web/app.py": "src/mediaforce/web/app.py",
    "cli/__init__.py": "src/mediaforce/cli/__init__.py",
    "cli/main.py": "src/mediaforce/cli/main.py",
}


@app.get("/raw/{filename:path}", response_class=PlainTextResponse)
async def raw_file(filename: str):
    """Serve whitelisted source files for remote workers (pull-based autoupdate).

    Restricts to a small allowlist and prevents path traversal.
    """
    if filename not in ALLOWED_RAW_FILES:
        return HTMLResponse("not found", status_code=404)

    target = PROJECT_ROOT / ALLOWED_RAW_FILES[filename]
    try:
        text = target.read_text()
    except Exception:
        return HTMLResponse("not found", status_code=404)
    return text


@app.get("/raw/manifest.json")
async def raw_manifest():
    """Expose a simple manifest with version + sha256 hashes for allowed raw files."""
    manifest = {"version": datetime.utcnow().isoformat() + "Z", "files": {}}
    base = PROJECT_ROOT
    for export_name, rel_path in ALLOWED_RAW_FILES.items():
        p = base / rel_path
        if not p.exists() or not p.is_file():
            continue
        import hashlib

        h = hashlib.sha256()
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        manifest["files"][export_name] = {
            "sha256": h.hexdigest(),
            "size": p.stat().st_size,
        }

    return manifest

QUEUE_CACHE: dict[str, dict] = {}  # reserved for future use
QUEUE_CACHE_TTL = 10  # seconds
QUEUE_TOTALS_CACHE: dict[str, dict] = {}
QUEUE_TOTALS_TTL = 15  # seconds


def _get_watch_libraries() -> list[str]:
    """Return the library roots configured to be watched on this host."""
    settings = load_app_settings()
    libs = []
    for lib, root in iter_libraries_for_current_host(settings):
        if lib.watch:
            libs.append(str(root))
    return libs


async def _start_watch_task() -> dict:
    global WATCH_TASK

    if WATCH_TASK and not WATCH_TASK.done():
        return WATCH_STATUS

    watch_roots = _get_watch_libraries()
    if not watch_roots:
        WATCH_STATUS.update({
            "running": False,
            "paused": False,
            "libraries": [],
            "message": "No watch-enabled libraries on this host",
        })
        return WATCH_STATUS

    async def runner():
        WATCH_STATUS.update({
            "running": True,
            "paused": False,
            "libraries": watch_roots,
            "message": "watching",
        })
        try:
            await watch_libraries()
            WATCH_STATUS.update({
                "running": False,
                "paused": False,
                "message": "stopped",
            })
        except asyncio.CancelledError:
            WATCH_STATUS.update({
                "running": False,
                "paused": True,
                "message": "stopped",
            })
            raise
        except Exception as exc:  # pragma: no cover
            WATCH_STATUS.update({
                "running": False,
                "paused": False,
                "message": f"error: {exc}",
            })
    # Mark running before returning so API callers see immediate state
    WATCH_STATUS.update({
        "running": True,
        "paused": False,
        "libraries": watch_roots,
        "message": "watching",
    })
    WATCH_TASK = asyncio.create_task(runner())
    return WATCH_STATUS


async def _stop_watch_task(message: str = "stopped", paused: bool = False) -> dict:
    global WATCH_TASK
    if WATCH_TASK and not WATCH_TASK.done():
        WATCH_TASK.cancel()
        try:
            await WATCH_TASK
        except asyncio.CancelledError:
            pass
    WATCH_STATUS.update({
        "running": False,
        "paused": paused,
        "message": message,
    })
    return WATCH_STATUS


class BulkPromoteRequest(BaseModel):
    ids: list[int]


class BulkRejectRequest(BaseModel):
    ids: list[int]
    new_tier: Optional[str] = None


class RejectRequest(BaseModel):
    new_tier: Optional[str] = None
    apply_to_show: bool = False


class BumpRequest(BaseModel):
    id: Optional[int] = None
    path: Optional[str] = None


class ShowOverrideRequest(BaseModel):
    show_name: str
    tier: Optional[str] = None


class ApplyTierRequest(BaseModel):
    show_name: str
    tier: str
    set_override: bool = False


class WorkerClaimRequest(BaseModel):
    machine: str


class WorkerReleaseRequest(BaseModel):
    id: int
    machine: str
    success: bool
    error: Optional[str] = None


class WorkerProgressStartRequest(BaseModel):
    source_id: int
    source_path: str
    output_path: str
    machine: str
    tier: str
    duration_sec: float
    total_frames: Optional[int] = None


class WorkerProgressUpdateRequest(BaseModel):
    progress_id: int
    frame: int = 0
    fps: float = 0.0
    speed: float = 0.0
    bitrate_kbps: Optional[float] = None
    size_bytes: int = 0
    time_encoded_sec: float = 0.0
    duration_sec: Optional[float] = None
    phase: Optional[str] = None
    phase_detail: Optional[str] = None


class WorkerMetricsPayload(BaseModel):
    ssim: Optional[float] = None
    psnr: Optional[float] = None
    vmaf: Optional[float] = None
    sample_duration_sec: Optional[float] = None
    sample_start_sec: Optional[float] = None


class WorkerOutlierPayload(BaseModel):
    is_outlier: bool
    reasons: list[str] = []


class WorkerEncodeReportRequest(BaseModel):
    source_id: int
    source_path: str
    tier: str
    crf: int
    preset: int
    film_grain: int
    denoise: Optional[str] = None
    output_path: str
    output_size_bytes: int
    output_bitrate_kbps: Optional[int] = None
    source_size_bytes: int
    machine: str
    started_at: str
    success: bool
    error_message: Optional[str] = None
    metrics: Optional[WorkerMetricsPayload] = None
    outlier: Optional[WorkerOutlierPayload] = None
    profile_eval_id: Optional[int] = None
    progress_id: Optional[int] = None


class LibrarySettingsModel(BaseModel):
    """Pydantic model mirroring LibrarySettings for the API layer."""

    id: str
    name: str
    media_type: str
    mac_path: str
    linux_path: str
    watch: bool = True
    max_height: Optional[int] = None
    weight: float = 1.0


class SettingsUpdateRequest(BaseModel):
    libraries: list[LibrarySettingsModel]
    global_max_height: Optional[int] = None
    max_concurrency: Optional[int] = None
    offpeak_enabled: bool = False
    offpeak_start: str = "00:00"
    offpeak_end: str = "05:00"


class WatchToggleRequest(BaseModel):
    action: str  # 'start' or 'stop'


class ScanRequest(BaseModel):
    path: str


class WorkerStatus(BaseModel):
    id: str
    host: str
    role: str
    state: str
    current: Optional[str] = None
    last_heartbeat: Optional[str] = None


class FlagProfileRequest(BaseModel):
    decision: str = "bad"
    reason: str


class EvaluationStartRequest(BaseModel):
    media_id: int
    initial_profile: str
    sample_length: float = 8.0


class EvaluationSamplePayload(BaseModel):
    kind: str
    start_sec: float
    duration_sec: float
    weight: float
    vmaf: float


class EvaluationSubmitSamplesRequest(BaseModel):
    samples: list[EvaluationSamplePayload]
    target_height: Optional[int] = None
    target_height_reason: Optional[str] = None


class QueueAddRequest(BaseModel):
    path: str
    library: Optional[str] = None


class QueueMoveRequest(BaseModel):
    delta: int = 1


def _serialize_eval(ev: ProfileEvaluation) -> dict:
    return {
        "id": ev.id,
        "media_id": ev.media_id,
        "encode_result_id": ev.encode_result_id,
        "selected_profile": ev.selected_profile,
        "sample_strategy": ev.sample_strategy,
        "sample_count": ev.sample_count,
        "sample_length": ev.sample_length,
        "weighted_vmaf": ev.weighted_vmaf,
        "median_vmaf": ev.median_vmaf,
        "min_vmaf": ev.min_vmaf,
        "max_vmaf": ev.max_vmaf,
        "threshold_min": ev.threshold_min,
        "threshold_median": ev.threshold_median,
        "threshold_max": ev.threshold_max,
        "decision": ev.decision,
        "status": ev.status,
        "note": ev.note,
        "reason_json": ev.reason_json,
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
    }


def _serialize_sample(s: VmafSample) -> dict:
    return {
        "id": s.id,
        "evaluation_id": s.evaluation_id,
        "kind": s.sample_kind,
        "start_sec": s.start_sec,
        "duration_sec": s.duration_sec,
        "vmaf": s.vmaf,
        "weight": s.weight,
        "log_path": s.log_path,
        "created_at": s.created_at,
    }


def _compute_stats(session: Session) -> dict:
    totals = session.exec(
        select(func.sum(MediaItem.size_bytes), func.sum(EncodeResult.output_size_bytes))
        .join(EncodeResult, EncodeResult.source_id == MediaItem.id)
        .where(EncodeResult.output_size_bytes.is_not(None), EncodeResult.output_size_bytes > 0)  # type: ignore[union-attr]
    ).one_or_none()
    total_in, total_out = totals if totals else (0, 0)
    saved_bytes = (total_in or 0) - (total_out or 0)

    encodes = session.exec(
        select(func.count(EncodeResult.id)).where(EncodeResult.output_size_bytes.is_not(None))  # type: ignore[union-attr]
    ).one()
    active = session.exec(
        select(func.count())
        .select_from(EncodeProgress)
        .where(EncodeProgress.percent_complete < 100)
    ).one()
    return {
        "saved_bytes": saved_bytes,
        "encodes": encodes if encodes is not None else 0,
        "active_encodes": active if active is not None else 0,
    }


def _serialize_feedback(fb: ProfileChoiceFeedback) -> dict:
    return {
        "id": fb.id,
        "evaluation_id": fb.evaluation_id,
        "decision": fb.decision,
        "reason": fb.reason_text,
        "flagged_at": fb.flagged_at,
    }


def resolve_existing_library_root() -> Optional[str]:
    """Return first existing library root for this host, or None."""
    settings = load_app_settings()
    for _, root in iter_libraries_for_current_host(settings):
        if pathlib.Path(root).exists():
            return str(root)
    return None


def format_size(bytes_val: float | int | None) -> str:
    if bytes_val is None:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def format_duration(seconds: float) -> str:
    if seconds is None:
        return "?"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def extract_show_name(path: str) -> Optional[str]:
    parts = pathlib.Path(path).parts
    for i, part in enumerate(parts):
        if part.lower().startswith("season"):
            if i > 0:
                return parts[i - 1]
            break
    return None


# Add custom filters to Jinja2
templates.env.filters["format_size"] = format_size
templates.env.filters["format_duration"] = format_duration


def build_pagination_url(request: Request) -> Callable[[int], str]:
    def pagination_url(page: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(page)
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{request.url.path}?{query_string}"
    return pagination_url

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard with overview stats."""
    host_name = platform_mod.node()
    library_root = resolve_existing_library_root()
    if not library_root:
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "title": "Dashboard",
            "active": "dashboard",
            "stats": {"pending": 0, "encoding": 0, "encoded": 0, "completed": 0, "space_saved_gb": "0"},
            "active_encodes": [],
            "recent_completions": [],
            "tier_counts": {},
            "lib_status": get_library_status(),
            "watch_status": _watch_status_snapshot(),
            "workers": [],
            "host_name": host_name,
            "nav_status": _nav_status(),
            "error": "No accessible library root found. Mount /Volumes or /mnt media shares.",
        })

    with session_scope() as session:
        media_repo = MediaRepository(session)
        encode_repo = EncodeRepository(session)
        progress_repo = ProgressRepository(session)

        status_counts = media_repo.count_by_status()

        space_saved_bytes = encode_repo.space_saved_bytes()
        space_saved_gb = space_saved_bytes / 1024 / 1024 / 1024

        active_rows = progress_repo.list_active()
        active_encodes = []
        for row in active_rows:
            prog = row[0]
            filename = pathlib.Path(prog.source_path).name if prog.source_path else "Unknown"
            show_name = extract_show_name(prog.source_path) if prog.source_path else None
            eta_display = format_duration(prog.eta_seconds) if prog.eta_seconds and prog.eta_seconds > 0 else None
            active_encodes.append({
                "filename": filename,
                "path": prog.source_path,
                "show_name": show_name,
                "machine": prog.machine,
                "tier": prog.tier,
                "started_at": prog.started_at[:16] if prog.started_at else None,
                "percent_complete": prog.percent_complete or 0,
                "speed": f"{prog.speed:.2f}x" if prog.speed else "0x",
                "eta": eta_display,
                "phase": prog.phase or "encoding",
            })

        recent_rows = encode_repo.recent_completions(limit=10)
        recent_completions = []
        for row in recent_rows:
            path, size_bytes, out_bytes, completed_at, tier, encode_id = row
            reduction = 0
            if size_bytes and out_bytes:
                reduction = int((1 - out_bytes / size_bytes) * 100)
            recent_completions.append({
                "id": encode_id,
                "path": path,
                "filename": pathlib.Path(path).name,
                "source_size": format_size(size_bytes),
                "output_size": format_size(out_bytes),
                "reduction": reduction,
                "tier": tier,
                "completed_at": completed_at[:16] if completed_at else "?",
            })

        tier_counts = media_repo.pending_tier_counts()

        lib_status = get_library_status(session)
        workers = progress_repo.list_workers()

    stats = {
        "pending": status_counts.get("pending", 0),
        "encoding": status_counts.get("encoding", 0),
        "encoded": status_counts.get("encoded", 0),
        "completed": status_counts.get("completed", 0),
        "space_saved_gb": f"{space_saved_gb:.1f}",
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "title": "Dashboard",
        "active": "dashboard",
        "stats": stats,
        "active_encodes": active_encodes,
        "recent_completions": recent_completions,
        "tier_counts": tier_counts,
        "lib_status": lib_status,
        "workers": workers,
        "watch_status": _watch_status_snapshot(),
        "nav_status": _nav_status(),
        "library_root": library_root,
        "host_name": host_name,
    })


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    days: int = Query(30, ge=7, le=365),
):
    library_root = resolve_existing_library_root()
    if not library_root:
        return templates.TemplateResponse(request, "stats.html", {
            "request": request,
            "title": "Stats",
            "active": "stats",
            "window_days": days,
            "window_totals": {"encodes": 0},
            "window_saved": "0",
            "window_avg_reduction": "-",
            "window_avg_speed": "-",
            "daily": [],
            "tiers": [],
            "all_time_totals": {"encodes": 0},
            "all_time_saved": "0",
            "saved_spark": "",
            "encodes_spark": "",
            "reduction_spark": "",
            "speed_spark": "",
            "nav_status": _nav_status(),
            "error": "No accessible library root found. Mount /Volumes or /mnt media shares.",
        })

    since = datetime.now() - timedelta(days=days - 1)

    with session_scope() as session:
        repo = StatsRepository(session)
        window_totals = repo.totals(since=since)
        all_time_totals = repo.totals()
        daily_stats = repo.daily(days=days)
        tier_stats = repo.reduction_by_tier(since=since)

    daily_rows = []
    for row in daily_stats:
        daily_rows.append({
            "day": row.day.isoformat(),
            "encodes": row.encodes,
            "saved_human": format_size(row.saved_bytes),
            "avg_reduction_human": f"{row.avg_reduction * 100:.1f}%" if row.avg_reduction is not None else "-",
            "avg_speed_human": f"{row.avg_speed:.2f}x" if row.avg_speed is not None else "-",
        })

    tier_rows = []
    for row in tier_stats:
        tier_rows.append({
            "tier": row.tier,
            "encodes": row.encodes,
            "saved_human": format_size(row.saved_bytes),
            "avg_reduction_human": f"{row.avg_reduction * 100:.1f}%" if row.avg_reduction is not None else "-",
        })

    saved_series = [float(row.saved_bytes) / 1024 / 1024 / 1024 for row in daily_stats]
    encodes_series = [float(row.encodes) for row in daily_stats]
    reduction_series = [float(row.avg_reduction or 0.0) * 100.0 for row in daily_stats]
    speed_series = [float(row.avg_speed or 0.0) for row in daily_stats]

    window_saved = format_size(window_totals.saved_bytes)
    window_avg_reduction = f"{window_totals.avg_reduction * 100:.1f}%" if window_totals.avg_reduction is not None else "-"
    window_avg_speed = f"{window_totals.avg_speed:.2f}x" if window_totals.avg_speed is not None else "-"

    return templates.TemplateResponse(request, "stats.html", {
        "request": request,
        "title": "Stats",
        "active": "stats",
        "window_days": days,
        "window_totals": window_totals,
        "window_saved": window_saved,
        "window_avg_reduction": window_avg_reduction,
        "window_avg_speed": window_avg_speed,
        "daily": daily_rows,
        "tiers": tier_rows,
        "all_time_totals": all_time_totals,
        "all_time_saved": format_size(all_time_totals.saved_bytes),
        "saved_spark": sparkline_svg(saved_series, stroke="#38bdf8", fill="rgba(56, 189, 248, 0.16)"),
        "encodes_spark": sparkline_svg(encodes_series, stroke="#a855f7", fill="rgba(168, 85, 247, 0.16)"),
        "reduction_spark": sparkline_svg(reduction_series, stroke="#22c55e", fill="rgba(34, 197, 94, 0.16)"),
        "speed_spark": sparkline_svg(speed_series, stroke="#f59e0b", fill="rgba(245, 158, 11, 0.16)"),
        "nav_status": _nav_status(),
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Application settings page (library configuration)."""

    settings: AppSettings = load_app_settings()
    libraries = settings.libraries
    global_max_height = settings.global_max_height

    # Library status (last scan time per library)
    lib_status = get_library_status()

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "title": "Settings",
        "active": "settings",
        "libraries": libraries,
        "watch_status": _watch_status_snapshot(),
        "is_mac": IS_MAC,
        "global_max_height": global_max_height,
        "settings": settings,
        "lib_status": lib_status,
        "nav_status": _nav_status(),
    })


def parse_media_path(path: str, library_root: str = DEFAULT_LIBRARY) -> dict:
    """Parse media path into show/season/episode components.

    Works for both TV (Show/Season/File) and flat movies (Title/File).
    """
    p = pathlib.Path(path)
    try:
        rel_path = p.relative_to(library_root)
    except Exception:
        rel_path = p

    parts = rel_path.parts
    if len(parts) >= 3 and parts[1].lower().startswith("season"):
        # TV structure
        return {
            "show_name": parts[0],
            "season": parts[1],
            "filename": parts[-1],
            "is_show": True,
        }
    elif len(parts) >= 2:
        # Flat movie structure: Title/File
        return {
            "show_name": parts[0],
            "season": "Files",
            "filename": parts[-1],
            "is_show": False,
        }
    else:
        return {
            "show_name": parts[0] if parts else p.name,
            "season": None,
            "filename": parts[-1] if parts else p.name,
            "is_show": False,
        }


def _queue_cache_get(library_root: str):
    entry = QUEUE_CACHE.get(library_root)
    if not entry:
        return None
    if entry.get("expires", 0) < datetime.utcnow().timestamp():
        QUEUE_CACHE.pop(library_root, None)
        return None
    return entry.get("shows")


def _queue_cache_set(library_root: str, shows: list[dict]):
    QUEUE_CACHE[library_root] = {
        "expires": datetime.utcnow().timestamp() + QUEUE_CACHE_TTL,
        "shows": shows,
    }


def _queue_totals_get(cache_key: str):
    entry = QUEUE_TOTALS_CACHE.get(cache_key)
    if not entry:
        return None
    if entry.get("expires", 0) < datetime.utcnow().timestamp():
        QUEUE_TOTALS_CACHE.pop(cache_key, None)
        return None
    return entry.get("totals")


def _queue_totals_set(cache_key: str, totals: dict):
    QUEUE_TOTALS_CACHE[cache_key] = {
        "expires": datetime.utcnow().timestamp() + QUEUE_TOTALS_TTL,
        "totals": totals,
    }


def _aggregate_queue_shows(conn, library_root: str) -> list[dict]:
    # Deprecated; shows are now fetched with ordered/limited SQL in queue_shows_view
    return []


def _resolve_library(request: Request) -> str:
    lib_param = request.query_params.get("library")
    settings = load_app_settings()
    libs = [str(root) for _, root in iter_libraries_for_current_host(settings)]
    existing = [lib for lib in libs if pathlib.Path(lib).exists()]

    if lib_param and pathlib.Path(lib_param).exists():
        return lib_param
    if existing:
        return existing[0]
    if libs:
        return libs[0]
    return DEFAULT_LIBRARY


@app.get("/queue", response_class=HTMLResponse)
async def queue(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50),
    show: Optional[str] = Query(None, description="Filter by show name (contains)"),
    tier: Optional[str] = Query(None, description="Filter by detected tier"),
    size_min: Optional[str] = Query(None, description="Minimum size (e.g., 500MB, 5GB)"),
    size_max: Optional[str] = Query(None, description="Maximum size (e.g., 2GB)"),
):
    """View encoding queue with hierarchical in-page drill-down."""
    if per_page not in [25, 50, 100, 200]:
        per_page = 50

    library_root = _resolve_library(request)
    libs = [str(root) for _, root in iter_libraries_for_current_host(load_app_settings())]

    with session_scope() as session:
        repo = QueueRepository(session)
        return await queue_shows_view(
            request,
            repo,
            page,
            per_page,
            library_root,
            libs,
            show,
            tier,
            _parse_size_param(size_min),
            _parse_size_param(size_max),
        )


async def queue_shows_view(
    request: Request,
    repo: QueueRepository,
    page: int,
    per_page: int,
    library_root: str,
    libraries: list[str],
    show_filter: Optional[str] = None,
    tier_filter: Optional[str] = None,
    size_min_bytes: Optional[int] = None,
    size_max_bytes: Optional[int] = None,
):
    """View grouped by shows with filter/sort support."""
    sort = request.query_params.get("sort", "priority")
    direction = request.query_params.get("order", "desc").lower()
    direction = "desc" if direction not in ["asc", "desc"] else direction

    shows, total, total_files, total_savings = repo.list_shows(
        library_root=library_root,
        show_filter=show_filter,
        tier_filter=tier_filter,
        size_min=size_min_bytes,
        size_max=size_max_bytes,
        per_page=per_page,
        page=page,
        sort=sort,
        direction=direction,
    )

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start_page = max(1, page - 2)
    end_page = min(total_pages, page + 2)
    page_range = list(range(start_page, end_page + 1))

    return templates.TemplateResponse("queue.html", {
        "request": request,
        "title": "Queue",
        "active": "queue",
        "view_mode": "shows",
        "library_root": library_root,
        "libraries": libraries,
        "shows": shows,
        "total": total,
        "total_files": total_files,
        "total_savings": format_size(total_savings),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "page_range": page_range,
        "breadcrumbs": [],
        "pagination_url": build_pagination_url(request),
        "lib_status": get_library_status(),
        "workers": get_worker_status(library_root),
        "watch_status": _watch_status_snapshot(),
        "nav_status": _nav_status(),
        "sort": sort,
        "order": direction,
    })


async def queue_seasons_view(request: Request, repo: QueueRepository, show: str, page: int, per_page: int, library_root: str):
    """View seasons for a specific show via repository."""

    seasons_all = repo.list_seasons(library_root, show)
    total = len(seasons_all)
    total_files = sum(s.get("file_count", 0) for s in seasons_all)
    total_savings = sum(s.get("total_savings", 0) for s in seasons_all)

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    seasons = seasons_all[offset:offset + per_page]

    page_range = list(range(max(1, page - 2), min(total_pages + 1, page + 3)))

    return templates.TemplateResponse("queue.html", {
        "request": request,
        "title": f"Queue - {show}",
        "active": "queue",
        "view_mode": "seasons",
        "show_name": show,
        "seasons": seasons,
        "total": total,
        "total_files": total_files,
        "total_savings": format_size(total_savings),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "page_range": page_range,
        "breadcrumbs": [{"name": "All Shows", "url": "/queue"}],
        "pagination_url": build_pagination_url(request),
        "workers": get_worker_status(library_root),
        "watch_status": _watch_status_snapshot(),
        "nav_status": _nav_status(),
    })


async def queue_episodes_view(request: Request, conn, show: str, season: str, page: int, per_page: int, library_root: str):
    """View episodes for a specific show/season."""
    if season == "Files":
        like_pattern = f"{library_root}/{show}/%"
    else:
        like_pattern = f"{library_root}/{show}/{season}/%"

    sort = request.query_params.get("sort", "priority")
    direction = request.query_params.get("order", "desc").lower()
    direction = "desc" if direction not in ["asc", "desc"] else direction

    sort_map = {
        "file": "filename COLLATE NOCASE",
        "size": "size_bytes",
        "savings": "potential_savings_bytes",
        "tier": "detected_tier",
        "priority": "priority_score",
        "bitrate": "bitrate_kbps",
        "duration": "duration_sec",
    }
    sort_expr = sort_map.get(sort, "priority_score")

    # Total count and savings
    total_row = conn.execute(
        """
        SELECT COUNT(*) as total, SUM(potential_savings_bytes) as total_savings,
               MIN(pe.status) as eval_status,
               MIN(pe.median_vmaf) as eval_median,
               MIN(pe.min_vmaf) as eval_min,
               MIN(pe.id) as eval_id
        FROM media_inventory mi
        LEFT JOIN profile_evaluations pe ON pe.media_id = mi.id
        WHERE mi.status = 'pending' AND mi.path LIKE ?
        """,
        (like_pattern,),
    ).fetchone()
    total = total_row["total"] or 0
    total_savings = total_row["total_savings"] or 0

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    page_range = list(range(max(1, page - 2), min(total_pages + 1, page + 3)))

    cursor = conn.execute(
        f"""
        SELECT mi.id, mi.path, mi.size_bytes, mi.detected_tier, mi.priority_score, mi.bitrate_kbps,
               mi.duration_sec, mi.is_interlaced, mi.potential_savings_bytes,
               mi.video_codec, mi.video_profile, mi.resolution, mi.width, mi.height,
               mi.bit_depth, mi.frame_rate, mi.is_hdr, mi.hdr_format, mi.audio_tracks, mi.subtitle_tracks,
               mi.tier_reasoning,
               pe.id as eval_id,
               pe.status as eval_status,
               pe.median_vmaf as eval_median,
               pe.min_vmaf as eval_min
        FROM media_inventory mi
        LEFT JOIN profile_evaluations pe ON pe.media_id = mi.id
        WHERE mi.status = 'pending' AND mi.path LIKE ?
        ORDER BY {sort_expr} {direction}
        LIMIT ? OFFSET ?
        """,
        (like_pattern, per_page, offset),
    )

    episodes = []
    total_savings = 0
    for row in cursor.fetchall():
        savings = row["potential_savings_bytes"] or 0
        total_savings += savings

        # Parse audio tracks
        audio_info = "?"
        if row["audio_tracks"]:
            try:
                tracks = json.loads(row["audio_tracks"])
                audio_info = ", ".join(
                    f"{t.get('codec', '?')} {t.get('channels', '?')}ch"
                    for t in tracks[:3]  # Show first 3
                )
                if len(tracks) > 3:
                    audio_info += f" (+{len(tracks) - 3})"
            except Exception:
                pass

        # Parse subtitle tracks
        sub_count = 0
        if row["subtitle_tracks"]:
            try:
                sub_count = len(json.loads(row["subtitle_tracks"]))
            except Exception:
                pass

        eval_obj = None
        if row["profile_eval_id"]:
            eval_obj = conn.execute(
                "SELECT * FROM profile_evaluations WHERE id = ?", (row["profile_eval_id"],)
            ).fetchone()

        episodes.append({
            "id": row["id"],
            "path": row["path"],
            "filename": pathlib.Path(row["path"]).name,
            "size": format_size(row["size_bytes"]),
            "size_bytes": row["size_bytes"],
            "detected_tier": row["detected_tier"],
            "priority_score": row["priority_score"],
            "bitrate": f"{row['bitrate_kbps']}k" if row["bitrate_kbps"] else "?",
            "bitrate_kbps": row["bitrate_kbps"],
            "duration": format_duration(row["duration_sec"]),
            "is_interlaced": row["is_interlaced"],
            "savings": format_size(savings) if savings else "?",
            "savings_bytes": savings,
            # Expanded details
            "video_codec": row["video_codec"] or "?",
            "video_profile": row["video_profile"] or "",
            "resolution": row["resolution"] or f"{row['width']}x{row['height']}" if row["width"] else "?",
            "bit_depth": row["bit_depth"],
            "frame_rate": row["frame_rate"] or "?",
            "is_hdr": row["is_hdr"],
            "hdr_format": row["hdr_format"],
            "audio_info": audio_info,
            "subtitle_count": sub_count,
            "tier_reasoning": row["tier_reasoning"] or "",
            "eval_status": eval_obj["status"] if eval_obj else None,
            "eval_median": eval_obj["median_vmaf"] if eval_obj else None,
            "eval_min": eval_obj["min_vmaf"] if eval_obj else None,
            "eval_id": eval_obj["id"] if eval_obj else None,
        })

    return templates.TemplateResponse("queue.html", {
        "request": request,
        "title": f"Queue - {show} - {season}",
        "active": "queue",
        "view_mode": "episodes",
        "show_name": show,
        "season_name": season,
        "episodes": episodes,
        "total": total,
        "total_files": total,
        "total_savings": format_size(total_savings),
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "page_range": page_range,
        "breadcrumbs": [
            {"name": "All Shows", "url": "/queue"},
            {"name": show, "url": f"/queue?show={show}"},
        ],
        "pagination_url": build_pagination_url(request),
        "workers": get_worker_status(library_root),
        "sort": sort,
        "order": direction,
        "watch_status": _watch_status_snapshot(),
        "nav_status": _nav_status(),
    })


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: Optional[str] = Query("", description="Search term"),
    status: Optional[str] = Query(None, description="Filter by status"),
    tier: Optional[str] = Query(None, description="Filter by tier"),
):
    """Simple cross-page search across inventory/encodes."""

    library_root = resolve_existing_library_root()
    if not library_root:
        return templates.TemplateResponse("search.html", {
            "request": request,
            "title": "Search",
            "active": "search",
            "query": q,
            "status": status,
            "tier": tier,
            "results": [],
            "total": 0,
            "nav_status": _nav_status(),
            "watch_status": _watch_status_snapshot(),
        })

    filters = []
    if q:
        filters.append(func.lower(MediaItem.path).like(f"%{q.lower()}%"))
    if status:
        filters.append(MediaItem.status == status)
    if tier:
        filters.append(MediaItem.detected_tier == tier)

    with session_scope() as session:
        stmt = (
            select(
                MediaItem.id,
                MediaItem.path,
                MediaItem.status,
                MediaItem.detected_tier,
                MediaItem.size_bytes,
                MediaItem.updated_at,
                func.coalesce(EncodeResult.output_size_bytes, 0).label("output_size_bytes"),
                EncodeResult.completed_at,
            )
            .select_from(MediaItem)
            .join(EncodeResult, EncodeResult.source_id == MediaItem.id, isouter=True)
        )
        if filters:
            stmt = stmt.where(*filters)
        stmt = stmt.order_by(MediaItem.updated_at.desc()).limit(200)  # type: ignore[attr-defined]
        rows = session.exec(stmt).all()

    results = []
    for row in rows:
        reduction = 0
        output_size = row.output_size_bytes if hasattr(row, "output_size_bytes") else row[6]
        size_bytes = row.size_bytes if hasattr(row, "size_bytes") else row[4]
        updated_at = row.updated_at if hasattr(row, "updated_at") else row[5]
        if output_size and size_bytes:
            reduction = int((1 - output_size / size_bytes) * 100)
        results.append({
            "path": row.path,
            "filename": pathlib.Path(row.path).name,
            "status": row.status,
            "tier": row.detected_tier,
            "size": format_size(size_bytes),
            "reduction": reduction,
            "updated_at": updated_at[:16] if updated_at else None,
        })

    return templates.TemplateResponse("search.html", {
        "request": request,
        "title": "Search",
        "active": "search",
        "query": q,
        "status": status,
        "tier": tier,
        "results": results,
        "total": len(results),
        "nav_status": _nav_status(),
        "watch_status": _watch_status_snapshot(),
    })



@app.get("/completed", response_class=HTMLResponse)
async def completed(request: Request):
    """View completed (promoted) files."""
    with session_scope() as session:
        rows = session.exec(
            select(
                EncodeResult.id,
                MediaItem.path,
                MediaItem.size_bytes,
                MediaItem.detected_tier,
                EncodeResult.output_size_bytes,
                EncodeResult.vmaf,
                EncodeResult.promoted_at,
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(
                MediaItem.status == "completed",
                EncodeResult.output_size_bytes.is_not(None),  # type: ignore[attr-defined]
                EncodeResult.output_size_bytes > 0,
            )
            .order_by(EncodeResult.promoted_at.desc())  # type: ignore[attr-defined]
            .limit(100)
        ).all()

    encodes = []
    for row in rows:
        rid, path, size_bytes, tier, out_size, vmaf, promoted_at = row
        reduction = 0
        if size_bytes and out_size:
            reduction = int((1 - out_size / size_bytes) * 100)
        encodes.append({
            "id": rid,
            "source_path": path,
            "filename": pathlib.Path(path).name,
            "source_size": format_size(size_bytes),
            "output_size": format_size(out_size),
            "reduction": reduction,
            "tier": tier,
            "vmaf": f"{vmaf:.1f}" if vmaf else None,
            "promoted_at": promoted_at[:16] if promoted_at else None,
        })

    return templates.TemplateResponse("completed.html", {
        "request": request,
        "title": "Completed",
        "active": "completed",
        "encodes": encodes,
        "nav_status": _nav_status(),
    })


@app.get("/export/completed.csv")
async def export_completed_csv(
    limit: int = Query(5000, ge=1, le=50_000),
):
    """Export completed (promoted) encodes as CSV."""

    with session_scope() as session:
        rows = session.exec(
            select(
                EncodeResult.id,
                MediaItem.library_id,
                MediaItem.path,
                MediaItem.size_bytes,
                EncodeResult.output_path,
                EncodeResult.output_size_bytes,
                EncodeResult.tier,
                EncodeResult.vmaf,
                EncodeResult.machine,
                EncodeResult.promoted_at,
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(
                MediaItem.status == "completed",
                EncodeResult.output_size_bytes.is_not(None),  # type: ignore[attr-defined]
                EncodeResult.output_size_bytes > 0,
            )
            .order_by(desc(EncodeResult.promoted_at))  # type: ignore[attr-defined]
            .limit(limit)
        ).all()

    header = (
        "encode_id",
        "library_id",
        "filename",
        "source_path",
        "output_path",
        "source_bytes",
        "output_bytes",
        "saved_bytes",
        "reduction_pct",
        "tier",
        "vmaf",
        "machine",
        "promoted_at",
    )

    def iter_rows() -> Iterator[Sequence[object]]:
        for rid, library_id, source_path, source_bytes, output_path, output_bytes, tier, vmaf, machine, promoted_at in rows:
            saved_bytes = None
            reduction_pct = None
            if source_bytes and output_bytes:
                saved_bytes = int(source_bytes - output_bytes)
                if source_bytes > 0:
                    reduction_pct = round((1.0 - (output_bytes / source_bytes)) * 100.0, 3)

            yield (
                rid,
                library_id,
                pathlib.Path(source_path).name,
                source_path,
                output_path,
                int(source_bytes or 0),
                int(output_bytes or 0),
                saved_bytes if saved_bytes is not None else "",
                reduction_pct if reduction_pct is not None else "",
                tier or "",
                round(float(vmaf), 3) if vmaf is not None else "",
                machine or "",
                promoted_at or "",
            )

    return _csv_response(filename="mediaforce-completed.csv", header=header, rows=iter_rows())


@app.get("/export/stats/daily.csv")
async def export_stats_daily_csv(
    days: int = Query(30, ge=7, le=365),
):
    """Export daily stats (encodes + savings) as CSV."""

    since = datetime.now() - timedelta(days=days - 1)
    with session_scope() as session:
        repo = StatsRepository(session)
        window_totals = repo.totals(since=since)
        daily_stats = repo.daily(days=days)

    header = (
        "day",
        "encodes",
        "source_bytes",
        "output_bytes",
        "saved_bytes",
        "avg_reduction_pct",
        "avg_speed_x",
    )

    def iter_rows() -> Iterator[Sequence[object]]:
        for row in daily_stats:
            yield (
                row.day.isoformat(),
                row.encodes,
                row.source_bytes,
                row.output_bytes,
                row.saved_bytes,
                round(float(row.avg_reduction) * 100.0, 4) if row.avg_reduction is not None else "",
                round(float(row.avg_speed), 4) if row.avg_speed is not None else "",
            )

        yield ()
        yield (
            "WINDOW_TOTAL",
            window_totals.encodes,
            window_totals.source_bytes,
            window_totals.output_bytes,
            window_totals.saved_bytes,
            round(float(window_totals.avg_reduction) * 100.0, 4) if window_totals.avg_reduction is not None else "",
            round(float(window_totals.avg_speed), 4) if window_totals.avg_speed is not None else "",
        )

    return _csv_response(
        filename=f"mediaforce-stats-daily-{days}d.csv",
        header=header,
        rows=iter_rows(),
    )


@app.get("/export/stats/tiers.csv")
async def export_stats_tiers_csv(
    days: int = Query(30, ge=7, le=365),
):
    """Export tier stats (encodes + savings) as CSV."""

    since = datetime.now() - timedelta(days=days - 1)
    with session_scope() as session:
        repo = StatsRepository(session)
        tier_stats = repo.reduction_by_tier(since=since)

    header = (
        "tier",
        "encodes",
        "saved_bytes",
        "avg_reduction_pct",
    )

    def iter_rows() -> Iterator[Sequence[object]]:
        for row in tier_stats:
            yield (
                row.tier,
                row.encodes,
                row.saved_bytes,
                round(float(row.avg_reduction) * 100.0, 4) if row.avg_reduction is not None else "",
            )

    return _csv_response(
        filename=f"mediaforce-stats-tiers-{days}d.csv",
        header=header,
        rows=iter_rows(),
    )


@app.get("/review", response_class=HTMLResponse)
async def review(request: Request):
    """Review encodes pending promotion."""
    with session_scope() as session:
        encodes = []
        stmt = (
            select(EncodeResult, MediaItem, ProfileEvaluation, RetrainingCandidate)
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .outerjoin(ProfileEvaluation, ProfileEvaluation.id == EncodeResult.profile_eval_id)
            .outerjoin(RetrainingCandidate, RetrainingCandidate.evaluation_id == ProfileEvaluation.id)
            .where(MediaItem.status == "encoded", EncodeResult.output_size_bytes > 0)
            .order_by(desc(EncodeResult.completed_at))
        )
        rows = session.exec(stmt).all()
        for enc, media, eval_obj, retrain in rows:
            reduction_pct = 0
            size_increase_pct = 0
            media_size = media.size_bytes or 0
            enc_size = enc.output_size_bytes or 0
            if media_size and enc_size:
                reduction_pct = (1 - enc_size / media_size) * 100
                if reduction_pct < 0:
                    size_increase_pct = abs(reduction_pct)

            show_name = extract_show_name(media.path)
            encodes.append({
                "id": enc.id,
                "source_path": media.path,
                "filename": pathlib.Path(media.path).name,
                "show_name": show_name,
                "source_size": format_size(media.size_bytes),
                "output_size": format_size(enc.output_size_bytes),
                "reduction": f"{reduction_pct:.0f}",
                "reduction_pct": reduction_pct,
                "size_increase_pct": f"{size_increase_pct:.0f}",
                "tier": media.detected_tier,
                "vmaf": f"{enc.vmaf:.1f}" if enc.vmaf else None,
                "is_outlier": bool(enc.is_outlier),
                "eval_status": eval_obj.status if eval_obj else None,
                "eval_median": eval_obj.median_vmaf if eval_obj else None,
                "eval_min": eval_obj.min_vmaf if eval_obj else None,
                "eval_id": eval_obj.id if eval_obj else None,
                "eval_note": eval_obj.note if eval_obj else None,
                "eval_weighted": eval_obj.weighted_vmaf if eval_obj else None,
                "eval_thresh_min": eval_obj.threshold_min if eval_obj else None,
                "eval_thresh_med": eval_obj.threshold_median if eval_obj else None,
                "eval_thresh_max": eval_obj.threshold_max if eval_obj else None,
                "retrain_status": retrain.status if retrain else None,
            })

        stats = _compute_stats(session)

    return templates.TemplateResponse("review.html", {
        "request": request,
        "title": "Review",
        "active": "review",
        "encodes": encodes,
        "nav_status": _nav_status(),
        "stats": stats,
    })


@app.get("/compare/{encode_id}", response_class=HTMLResponse)
async def compare(request: Request, encode_id: int):
    """Side-by-side video comparison."""
    with session_scope() as session:
        row = session.exec(
            select(
                EncodeResult.output_size_bytes,
                EncodeResult.crf,
                EncodeResult.preset,
                EncodeResult.vmaf,
                EncodeResult.ssim,
                MediaItem.path.label("source_path"),  # type: ignore[attr-defined]
                MediaItem.size_bytes.label("source_size"),  # type: ignore[attr-defined]
                MediaItem.video_codec,
                MediaItem.detected_tier,
                MediaItem.is_interlaced,
            )
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(EncodeResult.id == encode_id)
        ).first()

        if not row:
            return HTMLResponse("Encode not found", status_code=404)

        (
            output_size,
            crf,
            preset,
            vmaf,
            ssim,
            source_path,
            source_size,
            video_codec,
            detected_tier,
            is_interlaced,
        ) = row

        reduction = 0
        if source_size and output_size:
            reduction = int((1 - output_size / source_size) * 100)

        response_payload = {
            "request": request,
            "title": "Compare",
            "active": "review",
            "encode_id": encode_id,
            "filename": pathlib.Path(source_path).name,
            "source_codec": video_codec or "?",
            "source_size": format_size(source_size),
            "output_size": format_size(output_size),
            "reduction": reduction,
            "tier": detected_tier,
            "crf": crf,
            "preset": preset,
            "vmaf": f"{vmaf:.1f}" if vmaf else None,
            "ssim": f"{ssim:.4f}" if ssim else None,
            "deinterlaced": is_interlaced,
            "nav_status": _nav_status(),
        }

    return templates.TemplateResponse("compare.html", response_payload)


@app.get("/shows", response_class=HTMLResponse)
async def shows(request: Request):
    """Show/Series management page."""
    with session_scope() as session:
        rows = session.exec(
            select(MediaItem.path, MediaItem.status, MediaItem.detected_tier)
            .where(MediaItem.path.like("%/Season %"))
        ).all()

        show_data: dict[str, dict[str, Any]] = {}
        for path, status, detected_tier in rows:
            show_name = extract_show_name(path)
            if not show_name:
                continue

            if show_name not in show_data:
                show_data[show_name] = {
                    "name": show_name,
                    "total": 0,
                    "pending": 0,
                    "encoding": 0,
                    "encoded": 0,
                    "completed": 0,
                    "tiers": {},
                }

            show_data[show_name]["total"] += 1
            if status in ["pending", "encoding", "encoded", "completed"]:
                show_data[show_name][status] += 1

            if detected_tier:
                show_data[show_name]["tiers"][detected_tier] = show_data[show_name]["tiers"].get(detected_tier, 0) + 1

        for show in show_data.values():
            if show["tiers"]:
                show["detected_tier"] = max(show["tiers"].items(), key=lambda x: x[1])[0]
            else:
                show["detected_tier"] = None
            del show["tiers"]

        overrides_rows = session.exec(select(ShowOverride.show_name, ShowOverride.default_tier)).all()
        overrides = {row[0]: row[1] for row in overrides_rows}

        for show in show_data.values():
            show["override_tier"] = overrides.get(show["name"])

    shows_list = sorted(show_data.values(), key=lambda x: x["name"].lower())

    return templates.TemplateResponse("shows.html", {
        "request": request,
        "title": "Shows",
        "active": "shows",
        "shows": shows_list,
        "nav_status": _nav_status(),
    })


@app.get("/video/{video_type}/{encode_id}")
async def serve_video(video_type: str, encode_id: int):
    """Serve video files for comparison."""
    with session_scope() as session:
        row = session.exec(
            select(EncodeResult.output_path, MediaItem.path.label("source_path"))
            .join(MediaItem, EncodeResult.source_id == MediaItem.id)
            .where(EncodeResult.id == encode_id)
        ).first()

    if not row:
        return HTMLResponse("Not found", status_code=404)

    output_path, source_path = row

    if video_type == "source":
        video_path = pathlib.Path(source_path)
    elif video_type == "encoded":
        video_path = pathlib.Path(output_path)
    else:
        return HTMLResponse("Invalid video type", status_code=400)

    if not video_path.exists():
        return HTMLResponse("Video file not found", status_code=404)

    return FileResponse(video_path)


@app.post("/api/promote/{encode_id}")
async def api_promote(encode_id: int):
    """Promote an encode (replace original with encoded version)."""
    rollback_state = None
    try:
        with session_scope() as session:
            row = session.exec(
                select(EncodeResult, MediaItem)
                .join(MediaItem, EncodeResult.source_id == MediaItem.id)
                .where(EncodeResult.id == encode_id)
            ).first()
            if not row:
                return {"success": False, "error": "Encode not found"}
            encode, item = row
            if not encode.output_path:
                return {"success": False, "error": "Encoded file path missing"}

            output_path = normalize_path(pathlib.Path(encode.output_path))
            source_path = normalize_path(pathlib.Path(item.path))

            if not output_path.exists():
                return {"success": False, "error": "Encoded file not found"}

            dest_path = source_path.parent / output_path.name
            result, rollback_state = promote_encoded_file_atomic(
                source_path=source_path,
                encoded_path=output_path,
                dest_path=dest_path,
                dry_run=False,
                move_original_to_backup=True,
                rename_sidecars=True,
                verify=True,
                logger=logger,
            )

            now_str = now_iso()
            item.status = "completed"
            item.path = str(result.dest_path)
            item.updated_at = now_str
            encode.promoted = True
            encode.promoted_at = now_str
            encode.promoted_path = str(result.dest_path)
            encode.source_backup_path = str(result.backup_source_path) if result.backup_source_path else None
            encode.promote_manifest_json = result.manifest.to_json()
            encode.output_path = str(result.dest_path)
            session.add(item)
            session.add(encode)

        return {"success": True, "dest_path": str(result.dest_path)}
    except Exception as exc:
        if rollback_state:
            try:
                rollback_promote(rollback_state)
            except Exception:
                logger.exception("Failed to rollback promotion")
        return {"success": False, "error": str(exc)}


@app.post("/api/reject/{encode_id}")
async def api_reject(encode_id: int, data: RejectRequest):
    """Reject an encode."""
    try:
        with session_scope() as session:
            row = session.exec(
                select(EncodeResult, MediaItem)
                .join(MediaItem, EncodeResult.source_id == MediaItem.id)
                .where(EncodeResult.id == encode_id)
            ).first()
            if not row:
                return {"success": False, "error": "Encode not found"}
            encode, item = row
            output_path = pathlib.Path(encode.output_path)
            source_path = pathlib.Path(item.path)

            if output_path.exists():
                output_path.unlink()

            try:
                parent = output_path.parent
                while parent.name and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
            except (OSError, StopIteration):
                pass

            show_name = extract_show_name(str(source_path))

            if data.new_tier and data.apply_to_show and show_name:
                session.merge(
                    ShowOverride(
                        show_name=show_name,
                        default_tier=data.new_tier,
                        notes="Set from review UI",
                        updated_at=now_iso(),
                    )
                )

            item.status = "pending"
            if data.new_tier:
                item.detected_tier = data.new_tier
            item.updated_at = now_iso()
            session.add(item)
            session.delete(encode)

        return {"success": True, "show_name": show_name}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/bump")
async def api_bump(data: BumpRequest):
    """Bump an item to the front of the queue by lowering manual_priority."""
    if data.id is None and (not data.path):
        return {"success": False, "error": "id or path required"}
    with session_scope() as session:
        current_min = session.exec(select(func.min(MediaItem.manual_priority))).first() or 0
        new_priority = current_min - 1
        now_str = now_iso()

        if data.id is not None:
            item = session.get(MediaItem, data.id)
        else:
            item = session.exec(select(MediaItem).where(MediaItem.path == data.path)).first()
        if not item:
            return {"success": False, "error": "Item not found"}
        item.manual_priority = new_priority
        item.updated_at = now_str
        session.add(item)

    return {"success": True, "manual_priority": new_priority}


class SendToWorkerRequest(BaseModel):
    id: int
    worker: str


@app.post("/api/send-to-worker")
async def api_send_to_worker(data: SendToWorkerRequest):
    """Hint a specific worker to take a pending item by bumping it and setting claimed_by."""
    if not data.worker:
        return {"success": False, "error": "worker required"}
    with session_scope() as session:
        current_min = session.exec(select(func.min(MediaItem.manual_priority))).first() or 0
        new_priority = current_min - 1
        now_str = now_iso()

        item = session.get(MediaItem, data.id)
        if not item:
            return {"success": False, "error": "Item not found"}

        item.manual_priority = new_priority
        item.claimed_by = data.worker
        item.status = "pending"
        item.updated_at = now_str
        session.add(item)

    return {"success": True, "manual_priority": new_priority}


@app.post("/api/bulk-promote")
async def api_bulk_promote(data: BulkPromoteRequest):
    """Bulk promote multiple encodes."""
    if not data.ids:
        return {"success": False, "error": "No IDs provided"}

    promoted = 0
    failed = 0

    with session_scope() as session:
        for encode_id in data.ids:
            try:
                enc = session.get(EncodeResult, encode_id)
                if not enc:
                    failed += 1
                    continue
                item = session.get(MediaItem, enc.source_id)
                if not item:
                    failed += 1
                    continue

                if not enc.output_path:
                    failed += 1
                    continue

                output_path = normalize_path(pathlib.Path(enc.output_path))
                source_path = normalize_path(pathlib.Path(item.path))

                if not output_path.exists():
                    failed += 1
                    continue

                rollback_state = None
                dest_path = source_path.parent / output_path.name
                result, rollback_state = promote_encoded_file_atomic(
                    source_path=source_path,
                    encoded_path=output_path,
                    dest_path=dest_path,
                    dry_run=False,
                    move_original_to_backup=True,
                    rename_sidecars=True,
                    verify=True,
                    logger=logger,
                )

                try:
                    now_str = now_iso()
                    item.status = "completed"
                    item.path = str(result.dest_path)
                    item.updated_at = now_str
                    enc.promoted = True
                    enc.promoted_at = now_str
                    enc.promoted_path = str(result.dest_path)
                    enc.source_backup_path = (
                        str(result.backup_source_path) if result.backup_source_path else None
                    )
                    enc.promote_manifest_json = result.manifest.to_json()
                    enc.output_path = str(result.dest_path)
                    session.add(item)
                    session.add(enc)
                    session.commit()
                except Exception as db_exc:
                    session.rollback()
                    if rollback_state:
                        rollback_promote(rollback_state)
                    raise db_exc

                promoted += 1
            except Exception:
                session.rollback()
                failed += 1

    return {"success": True, "promoted": promoted, "failed": failed}


@app.post("/api/rollback/{encode_id}")
async def api_rollback(encode_id: int):
    """Rollback a previous promotion when a backup/manifest is available."""
    try:
        with session_scope() as session:
            row = session.exec(
                select(EncodeResult, MediaItem)
                .join(MediaItem, EncodeResult.source_id == MediaItem.id)
                .where(EncodeResult.id == encode_id)
            ).first()
            if not row:
                return {"success": False, "error": "Encode not found"}

            encode, item = row
            if not encode.promote_manifest_json:
                return {"success": False, "error": "No promote manifest available"}

            rollback_from_manifest(encode.promote_manifest_json)
            now_str = now_iso()
            item.path = encode.source_path
            item.status = "pending"
            item.updated_at = now_str
            encode.promoted = False
            encode.promoted_at = None
            encode.promoted_path = None
            session.add(item)
            session.add(encode)

        return {"success": True}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@app.post("/api/bulk-reject")
async def api_bulk_reject(data: BulkRejectRequest):
    """Bulk reject multiple encodes."""
    if not data.ids:
        return {"success": False, "error": "No IDs provided"}

    rejected = 0
    failed = 0

    with session_scope() as session:
        for encode_id in data.ids:
            try:
                enc = session.get(EncodeResult, encode_id)
                if not enc:
                    failed += 1
                    continue
                item = session.get(MediaItem, enc.source_id)
                if not item:
                    failed += 1
                    continue

                output_path = pathlib.Path(enc.output_path)
                if output_path.exists():
                    output_path.unlink()

                try:
                    parent = output_path.parent
                    while parent.name and not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                except (OSError, StopIteration):
                    pass

                now_str = now_iso()
                item.status = "pending"
                if data.new_tier:
                    item.detected_tier = data.new_tier
                item.updated_at = now_str
                session.add(item)
                session.delete(enc)
                session.commit()
                rejected += 1
            except Exception:
                session.rollback()
                failed += 1

    return {"success": True, "rejected": rejected, "failed": failed}


@app.post("/api/show-override")
async def api_show_override(data: ShowOverrideRequest):
    """Set or clear a tier override for a show."""
    if not data.show_name:
        return {"success": False, "error": "show_name required"}
    with session_scope() as session:
        try:
            if data.tier:
                session.merge(
                    ShowOverride(
                        show_name=data.show_name,
                        default_tier=data.tier,
                        updated_at=now_iso(),
                    )
                )
            else:
                existing = session.get(ShowOverride, data.show_name)
                if existing:
                    session.delete(existing)
            session.commit()
            return {"success": True}
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}


@app.post("/api/apply-tier-to-show")
async def api_apply_tier_to_show(data: ApplyTierRequest):
    """Apply a tier to all pending episodes of a show."""
    if not data.show_name or not data.tier:
        return {"success": False, "error": "show_name and tier required"}

    with session_scope() as session:
        try:
            items = session.exec(
                select(MediaItem).where(
                    MediaItem.status == "pending",
                    MediaItem.path.like(f"%/{data.show_name}/Season %"),
                )
            ).all()
            now_str = now_iso()
            updated = 0
            for item in items:
                item.detected_tier = data.tier
                item.updated_at = now_str
                session.add(item)
                updated += 1

            if data.set_override:
                session.merge(
                    ShowOverride(
                        show_name=data.show_name,
                        default_tier=data.tier,
                        updated_at=now_str,
                    )
                )

            session.commit()
            return {"success": True, "updated": updated}
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}


@app.post("/api/watch")
async def api_watch_toggle(data: WatchToggleRequest):
    """Start or stop the in-process library watcher."""

    try:
        if data.action == "start":
            status = await _start_watch_task()
        elif data.action == "pause":
            status = await _stop_watch_task("paused by user", paused=True)
        elif data.action == "stop":
            status = await _stop_watch_task()
        else:
            return {"success": False, "error": "action must be 'start', 'pause', or 'stop'"}
        return {"success": True, "status": status}
    except Exception as exc:  # pragma: no cover - defensive
        return {"success": False, "error": str(exc)}


@app.post("/api/scan")
async def api_scan_library(data: ScanRequest):
    """Kick off a scan for a specific library path."""

    lib_path = data.path.strip()
    if not lib_path:
        return {"success": False, "error": "path is required"}

    # Run scan as a subprocess to reuse the CLI logic safely.
    repo_dir = pathlib.Path(__file__).parent
    cmd = [sys.executable, "-m", "mediaforce", "scan", lib_path]

    try:
        SCAN_STATUS[lib_path] = "running"

        async def runner():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(repo_dir),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await proc.communicate()
                output = stdout.decode().strip()
                return {
                    "success": proc.returncode == 0,
                    "code": proc.returncode,
                    "output": output[-2000:],
                }
            finally:
                SCAN_STATUS[lib_path] = "idle"

        asyncio.create_task(runner())
        return {"success": True, "message": "scan started"}
    except FileNotFoundError:
        return {"success": False, "error": "mediaforce package not found"}
    except Exception as exc:  # pragma: no cover
        return {"success": False, "error": str(exc)}


@app.post("/api/settings")
async def api_update_settings(data: SettingsUpdateRequest):
    """Update global application settings (libraries and watches)."""

    try:
        # Convert Pydantic models back into our dataclasses for persistence.
        libs: list[LibrarySettings] = []
        for lib in data.libraries:
            libs.append(
                LibrarySettings(
                    id=lib.id.strip() or lib.name.strip().lower().replace(" ", "-"),
                    name=lib.name.strip() or lib.id,
                    media_type=lib.media_type.strip() or "generic",
                    mac_path=lib.mac_path.strip(),
                    linux_path=lib.linux_path.strip(),
                    watch=lib.watch,
                    max_height=lib.max_height,
                    weight=lib.weight,
                )
            )

        settings = AppSettings(
            libraries=libs,
            global_max_height=data.global_max_height,
            max_concurrency=data.max_concurrency or 1,
            offpeak_enabled=data.offpeak_enabled,
            offpeak_start=data.offpeak_start,
            offpeak_end=data.offpeak_end,
        )
        save_app_settings(settings)
        return {"success": True}
    except Exception as exc:  # pragma: no cover - defensive
        return {"success": False, "error": str(exc)}


@app.get("/api/workers")
async def api_workers(request: Request):
    """Return active worker list for the current library."""
    library_root = request.query_params.get('library') or _resolve_library(request)
    return {"success": True, "workers": get_worker_status(library_root)}


@app.post("/api/worker/claim", dependencies=[Depends(_require_worker_api_auth)])
async def api_worker_claim(data: WorkerClaimRequest):
    """Claim the next pending item for a worker without direct DB access."""

    machine = (data.machine or "").strip()
    if not machine:
        return {"success": False, "error": "machine required"}

    with session_scope() as session:
        session.merge(WorkerRegistry(machine=machine, role="encoder", last_seen=now_iso()))
        claimed = claim_next_file(session, machine)
        if not claimed:
            return {"success": True, "claimed": None}

        session.merge(
            WorkerRegistry(
                machine=machine,
                role="encoder",
                last_seen=now_iso(),
                sample_path=claimed.get("path"),
            )
        )

        show_name = extract_show_name(claimed.get("path") or "")
        override_tier = None
        if show_name:
            existing = session.get(ShowOverride, show_name)
            if existing and existing.default_tier:
                override_tier = existing.default_tier

        return {
            "success": True,
            "claimed": claimed,
            "show_name": show_name,
            "override_tier": override_tier,
        }


@app.post("/api/worker/release", dependencies=[Depends(_require_worker_api_auth)])
async def api_worker_release(data: WorkerReleaseRequest):
    """Release a claimed item back to the queue (or mark encoded)."""

    machine = (data.machine or "").strip()
    if not machine:
        return {"success": False, "error": "machine required"}

    with session_scope() as session:
        release_claim(session, int(data.id), bool(data.success))
    return {"success": True}


@app.post("/api/worker/progress/start", dependencies=[Depends(_require_worker_api_auth)])
async def api_worker_progress_start(data: WorkerProgressStartRequest):
    """Create a progress row for an active encode."""

    machine = (data.machine or "").strip()
    if not machine:
        return {"success": False, "error": "machine required"}

    with session_scope() as session:
        pid = start_progress_tracking(
            session,
            int(data.source_id),
            data.source_path,
            data.output_path,
            machine,
            data.tier,
            float(data.duration_sec or 0.0),
            total_frames=data.total_frames,
        )
        return {"success": True, "progress_id": pid}


@app.post("/api/worker/progress/update", dependencies=[Depends(_require_worker_api_auth)])
async def api_worker_progress_update(data: WorkerProgressUpdateRequest):
    """Update encode progress for an active encode."""

    with session_scope() as session:
        update_progress(
            session,
            int(data.progress_id),
            frame=int(data.frame or 0),
            fps=float(data.fps or 0.0),
            speed=float(data.speed or 0.0),
            bitrate_kbps=data.bitrate_kbps,
            size_bytes=int(data.size_bytes or 0),
            time_encoded_sec=float(data.time_encoded_sec or 0.0),
            duration_sec=float(data.duration_sec) if data.duration_sec is not None else None,
            phase=data.phase,
            phase_detail=data.phase_detail,
        )
        return {"success": True}


@app.post("/api/worker/report", dependencies=[Depends(_require_worker_api_auth)])
async def api_worker_report(data: WorkerEncodeReportRequest, background_tasks: BackgroundTasks):
    """Record an encode result from a worker and transition DB state safely."""

    machine = (data.machine or "").strip()
    if not machine:
        return {"success": False, "error": "machine required"}

    with session_scope() as session:
        if data.progress_id is not None:
            finish_progress_tracking(
                session,
                int(data.progress_id),
                success=bool(data.success),
                error_msg=data.error_message,
            )

        settings = TierSettings(
            crf=int(data.crf),
            preset=int(data.preset),
            film_grain=int(data.film_grain),
            denoise=data.denoise,
        )

        metrics_obj: Optional[QualityMetrics] = None
        if data.metrics is not None:
            metrics_obj = QualityMetrics(
                ssim=data.metrics.ssim,
                psnr=data.metrics.psnr,
                vmaf=data.metrics.vmaf,
                sample_duration_sec=data.metrics.sample_duration_sec,
                sample_start_sec=data.metrics.sample_start_sec,
            )

        outlier_obj: Optional[OutlierResult] = None
        if data.outlier is not None:
            outlier_obj = OutlierResult(
                is_outlier=bool(data.outlier.is_outlier),
                reasons=list(data.outlier.reasons or []),
                metrics=metrics_obj,
            )

        error_msg = data.error_message if not data.success else None
        result_id = record_encode_result(
            session,
            int(data.source_id),
            data.source_path,
            data.tier,
            settings,
            data.output_path,
            int(data.output_size_bytes),
            data.output_bitrate_kbps,
            int(data.source_size_bytes),
            machine,
            data.started_at,
            error_msg,
            metrics=metrics_obj,
            outlier_result=outlier_obj,
            profile_eval_id=data.profile_eval_id,
        )

        if data.profile_eval_id:
            eval_obj = session.get(ProfileEvaluation, int(data.profile_eval_id))
            if eval_obj:
                eval_obj.encode_result_id = result_id
                eval_obj.updated_at = datetime.now().isoformat()
                if not data.success:
                    eval_obj.status = "failed"
                session.add(eval_obj)
                session.commit()

        release_claim(session, int(data.source_id), bool(data.success))

        try:
            source_size = int(data.source_size_bytes)
            output_size = int(data.output_size_bytes or 0)
        except Exception:
            source_size = 0
            output_size = 0

        saved_bytes = max(0, source_size - output_size) if data.success else 0
        size_increase = output_size > source_size if data.success and source_size > 0 else False
        reduction_pct = (
            (1 - (output_size / source_size)) * 100
            if data.success and source_size > 0 and output_size > 0
            else None
        )

        event = "encode_completed" if data.success else "encode_failed"
        if size_increase:
            event = "encode_size_increase"

        summary = (
            f"{event}: {data.source_path}"
            + (f" ({saved_bytes} bytes saved)" if saved_bytes else "")
            + (" (size increased)" if size_increase else "")
        )
        payload = {
            "encode_result_id": result_id,
            "success": bool(data.success),
            "source_id": int(data.source_id),
            "source_path": data.source_path,
            "output_path": data.output_path,
            "tier": data.tier,
            "machine": machine,
            "source_size_bytes": source_size,
            "output_size_bytes": output_size,
            "saved_bytes": saved_bytes,
            "reduction_pct": reduction_pct,
            "error_message": data.error_message,
            "vmaf": data.metrics.vmaf if data.metrics else None,
            "outlier": bool(data.outlier.is_outlier) if data.outlier else None,
            "outlier_reasons": list(data.outlier.reasons or []) if data.outlier else [],
        }

        background_tasks.add_task(
            send_notifications,
            event=event,
            summary=summary,
            data=payload,
            logger=logger,
        )

    return {"success": True, "encode_result_id": result_id}


@app.get("/api/settings/current")
async def api_get_settings():
    """Return current settings (libraries and global max height)."""
    try:
        settings: AppSettings = load_app_settings()
        return {
            "success": True,
            "settings": {
                "global_max_height": settings.global_max_height,
                "max_concurrency": settings.max_concurrency,
                "offpeak_enabled": settings.offpeak_enabled,
                "offpeak_start": settings.offpeak_start,
                "offpeak_end": settings.offpeak_end,
                "libraries": [asdict(lib) for lib in settings.libraries],
            },
        }
    except Exception as exc:  # pragma: no cover
        return {"success": False, "error": str(exc)}


@app.get("/api/active-encodes")
async def api_active_encodes(request: Request):
    """Return live encode progress for dashboard polling."""
    library_root = request.query_params.get("library") or resolve_existing_library_root()
    if not library_root:
        return {"success": True, "encodes": []}
    with session_scope() as session:
        rows = session.exec(
            select(EncodeProgress, MediaItem.size_bytes, MediaItem.video_codec)
            .select_from(EncodeProgress)
            .join(MediaItem, EncodeProgress.source_id == MediaItem.id, isouter=True)
            .order_by(EncodeProgress.started_at.desc())
        ).all()

    encodes = []
    for row in rows:
        prog = row[0]
        eta_display = format_duration(prog.eta_seconds) if prog.eta_seconds and prog.eta_seconds > 0 else None
        encodes.append({
            "filename": pathlib.Path(prog.source_path).name if prog.source_path else "Unknown",
            "path": prog.source_path,
            "show_name": extract_show_name(prog.source_path) if prog.source_path else None,
            "machine": prog.machine,
            "tier": prog.tier,
            "started_at": prog.started_at[:16] if prog.started_at else None,
            "percent_complete": prog.percent_complete or 0,
            "speed": prog.speed or 0,
            "eta": eta_display,
            "phase": prog.phase or "encoding",
            "frame": prog.frame or 0,
            "total_frames": prog.total_frames,
            "fps": prog.fps or 0,
        })

    return {"success": True, "encodes": encodes}


@app.post("/api/profile-settings/refresh")
async def api_refresh_profile_settings():
    with session_scope() as session:
        src = ensure_active_profile_settings(session)
        return {
            "success": True,
            "source": {
                "id": src.id if src else None,
                "name": src.name if src else None,
                "fetched_at": src.fetched_at if src else None,
                "applied_at": src.applied_at if src else None,
            },
        }


@app.get("/api/evaluations/{eval_id}")
async def api_get_evaluation(eval_id: int):
    with session_scope() as session:
        ev = session.get(ProfileEvaluation, eval_id)
        if not ev:
            return {"success": False, "error": "not found"}
        samples = session.exec(select(VmafSample).where(VmafSample.evaluation_id == eval_id)).all()
        feedback = session.exec(select(ProfileChoiceFeedback).where(ProfileChoiceFeedback.evaluation_id == eval_id)).all()
        retrain = session.exec(select(RetrainingCandidate).where(RetrainingCandidate.evaluation_id == eval_id)).first()
        retrain_payload = None
        if retrain:
            retrain_payload = {
                "id": retrain.id,
                "status": retrain.status,
                "reason_text": retrain.reason_text,
                "created_at": retrain.created_at,
                "processed_at": retrain.processed_at,
            }
    return {
        "success": True,
        "evaluation": _serialize_eval(ev),
        "samples": [_serialize_sample(s) for s in samples],
        "feedback": [_serialize_feedback(fb) for fb in feedback],
        "retraining": retrain_payload,
    }


@app.post("/api/evaluations/start", dependencies=[Depends(_require_worker_api_auth)])
async def api_start_evaluation(data: EvaluationStartRequest):
    with session_scope() as session:
        settings_source = ensure_active_profile_settings(session)
        thresholds = extract_thresholds(settings_source)
        ev = start_profile_evaluation(
            session,
            media_id=int(data.media_id),
            initial_profile=data.initial_profile,
            thresholds=thresholds,
            settings_source=settings_source,
            sample_length=float(data.sample_length),
        )
        return {
            "success": True,
            "evaluation_id": ev.id,
            "thresholds": {
                "min": thresholds.min_vmaf,
                "median": thresholds.median_vmaf,
                "max": thresholds.max_vmaf,
            },
            "settings_source_id": settings_source.id if settings_source else None,
        }


@app.post("/api/evaluations/{eval_id}/samples", dependencies=[Depends(_require_worker_api_auth)])
async def api_submit_evaluation_samples(eval_id: int, data: EvaluationSubmitSamplesRequest):
    with session_scope() as session:
        ev = session.get(ProfileEvaluation, int(eval_id))
        if not ev:
            return {"success": False, "error": "not found"}

        settings_source = session.get(ProfileSettingsSource, ev.settings_source_id) if ev.settings_source_id else None
        thresholds = VmafThresholds(
            min_vmaf=float(ev.threshold_min or 82.0),
            median_vmaf=float(ev.threshold_median or 92.0),
            max_vmaf=float(ev.threshold_max) if ev.threshold_max is not None else None,
        )
        sample_results = [
            VmafSampleResult(
                kind=s.kind,
                start_sec=float(s.start_sec),
                duration_sec=float(s.duration_sec),
                weight=float(s.weight),
                vmaf=float(s.vmaf),
            )
            for s in data.samples
        ]

        try:
            result = finalize_profile_evaluation(
                session,
                evaluation_id=int(eval_id),
                initial_profile=ev.selected_profile,
                thresholds=thresholds,
                settings_source=settings_source,
                sample_results=sample_results,
                target_height=data.target_height,
                target_height_reason=data.target_height_reason,
            )
        except ValueError:
            session.rollback()
            return {"success": False, "error": "not found"}

        return {
            "success": True,
            "selected_profile": result.selected_profile,
            "initial_profile": result.initial_profile,
            "decision": result.decision,
            "status": result.status,
            "note": result.note,
            "summary": {
                "weighted": result.summary.weighted,
                "median": result.summary.median,
                "min": result.summary.minimum,
                "max": result.summary.maximum,
            },
        }


@app.post("/api/evaluations/{eval_id}/flag")
async def api_flag_evaluation(eval_id: int, data: FlagProfileRequest):
    with session_scope() as session:
        try:
            flag_profile_choice(
                session,
                evaluation_id=eval_id,
                decision=data.decision,
                reason=data.reason,
            )
            return {"success": True}
        except ValueError:
            session.rollback()
            return {"success": False, "error": "not found"}


@app.post("/api/queue/{media_id}/bump")
async def api_queue_bump(media_id: int, data: QueueMoveRequest):
    with session_scope() as session:
        repo = MediaRepository(session)
        repo.bump_priority(media_id, delta=data.delta)
        return {"success": True}


@app.post("/api/queue/{media_id}/pause")
async def api_queue_pause(media_id: int):
    with session_scope() as session:
        repo = MediaRepository(session)
        item = repo.get(media_id)
        if not item:
            return {"success": False, "error": "not found"}
        item.status = "paused"
        session.add(item)
        session.commit()
        return {"success": True}


@app.post("/api/queue/{media_id}/resume")
async def api_queue_resume(media_id: int):
    with session_scope() as session:
        repo = MediaRepository(session)
        item = repo.get(media_id)
        if not item:
            return {"success": False, "error": "not found"}
        item.status = "pending"
        item.skip_reason = None
        session.add(item)
        session.commit()
        return {"success": True}


@app.post("/api/queue/add")
async def api_queue_add(data: QueueAddRequest):
    path = pathlib.Path(data.path).resolve()
    with session_scope() as session:
        repo = MediaRepository(session)
        existing = session.exec(select(MediaItem).where(MediaItem.path == str(path))).first()
        if existing:
            existing.status = "pending"
            existing.skip_reason = None
            session.add(existing)
            session.commit()
            return {"success": True, "id": existing.id, "message": "already existed; resumed"}
        item = MediaItem(
            path=str(path),
            library_id=None,
            status="pending",
            skip_reason=None,
        )
        repo.add(item)
        session.commit()
        return {"success": True, "id": item.id}


@app.get("/api/queue/skipped")
async def api_queue_skipped(page: int = 1, per_page: int = 50):
    with session_scope() as session:
        repo = MediaRepository(session)
        page_obj = repo.list_skipped(pagination=Pagination(limit=per_page, offset=(page - 1) * per_page))
        items = [
            {
                "id": m.id,
                "path": m.path,
                "skip_reason": m.skip_reason,
                "updated_at": m.updated_at,
            }
            for m in page_obj.items
        ]
        return {
            "success": True,
            "items": items,
            "total": page_obj.total,
            "page": page,
            "per_page": per_page,
        }


@app.post("/api/queue/{media_id}/force-rescan")
async def api_queue_force_rescan(media_id: int):
    with session_scope() as session:
        repo = MediaRepository(session)
        item = repo.get(media_id)
        if not item:
            return {"success": False, "error": "not found"}
        item.status = "pending"
        item.skip_reason = None
        session.add(item)
        session.commit()
        return {"success": True}


@app.post("/api/queue/{media_id}/reset-skip")
async def api_queue_reset_skip(media_id: int):
    with session_scope() as session:
        repo = MediaRepository(session)
        item = repo.get(media_id)
        if not item:
            return {"success": False, "error": "not found"}
        item.skip_reason = None
        session.add(item)
        session.commit()
        return {"success": True}


@app.get("/api/queue/seasons/{show_name}")
async def api_queue_seasons(show_name: str, request: Request):
    """Get seasons for a specific show."""
    library_root = request.query_params.get('library') or _resolve_library(request)
    with session_scope() as session:
        repo = QueueRepository(session)
        seasons_raw = repo.list_seasons(library_root, show_name)
        seasons = [
            {
                "season_name": s["season_name"],
                "file_count": s["file_count"],
                "total_size": format_size(s["total_size"]),
                "total_savings": format_size(s["total_savings"]) if s["total_savings"] else "?",
                "total_savings_bytes": s["total_savings"],
                "max_priority": s["max_priority"],
            }
            for s in seasons_raw
        ]
        seasons = sorted(seasons, key=lambda x: -x["max_priority"])

    return {"seasons": seasons}


@app.get("/api/queue/episodes/{show_name}/{season_name}")
async def api_queue_episodes(show_name: str, season_name: str, request: Request):
    """Get episodes for a specific show/season."""
    library_root = request.query_params.get('library') or _resolve_library(request)
    with session_scope() as session:
        repo = QueueRepository(session)
        eps = repo.list_episodes(library_root, show_name, season_name)
        episodes = []
        for item in eps:
            savings = item.potential_savings_bytes or 0
            sub_count = len(json.loads(item.subtitle_tracks)) if item.subtitle_tracks else 0
            episodes.append({
                "id": item.id,
                "path": item.path,
                "filename": pathlib.Path(item.path).name,
                "size_bytes": item.size_bytes,
                "size": format_size(item.size_bytes),
                "detected_tier": item.detected_tier,
                "priority_score": item.priority_score,
                "bitrate_kbps": item.bitrate_kbps,
                "bitrate": f"{item.bitrate_kbps}k" if item.bitrate_kbps else "?",
                "duration": format_duration(item.duration_sec or 0),
                "duration_sec": item.duration_sec,
                "is_interlaced": item.is_interlaced,
                "savings": format_size(savings) if savings else "?",
                "savings_bytes": savings,
                "video_codec": item.video_codec or "?",
                "video_profile": item.video_profile or "",
                "resolution": item.resolution or (f"{item.width}x{item.height}" if item.width else "?"),
                "width": item.width,
                "height": item.height,
                "bit_depth": item.bit_depth,
                "frame_rate": item.frame_rate or "?",
                "is_hdr": item.is_hdr,
                "hdr_format": item.hdr_format,
                "audio_info": item.audio_tracks or "",
                "subtitle_count": sub_count,
                "tier_reasoning": item.tier_reasoning or "",
            })

    return {"episodes": episodes}


@app.get("/api/stats")
async def api_stats():
    """Get current stats as JSON."""
    with session_scope() as session:
        return MediaRepository(session).count_by_status()


@app.get("/api/stats/summary")
async def api_stats_summary(days: int = Query(30, ge=7, le=365)):
    since = datetime.now() - timedelta(days=days - 1)
    with session_scope() as session:
        repo = StatsRepository(session)
        totals = repo.totals(since=since)
        daily = repo.daily(days=days)
        tiers = repo.reduction_by_tier(since=since)

    return {
        "window_days": days,
        "totals": {
            "encodes": totals.encodes,
            "source_bytes": totals.source_bytes,
            "output_bytes": totals.output_bytes,
            "saved_bytes": totals.saved_bytes,
            "avg_reduction": totals.avg_reduction,
            "avg_speed": totals.avg_speed,
        },
        "daily": [
            {
                "day": row.day.isoformat(),
                "encodes": row.encodes,
                "source_bytes": row.source_bytes,
                "output_bytes": row.output_bytes,
                "saved_bytes": row.saved_bytes,
                "avg_reduction": row.avg_reduction,
                "avg_speed": row.avg_speed,
            }
            for row in daily
        ],
        "tiers": [
            {
                "tier": row.tier,
                "encodes": row.encodes,
                "saved_bytes": row.saved_bytes,
                "avg_reduction": row.avg_reduction,
            }
            for row in tiers
        ],
    }

def main():
    """CLI entry point for `mediaforce-web`."""
    from mediaforce.config.dotenv import load_dotenv_if_present

    load_dotenv_if_present()
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Mediaforce Web Interface")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", "-p", type=int, default=5555, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")

    args = parser.parse_args()

    logger.info("Starting Mediaforce Web UI", extra={"host": args.host, "port": args.port})
    uvicorn.run(
        "mediaforce.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
