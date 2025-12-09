#!/usr/bin/env python3
"""
Mediaforce web interface for managing encoding queues and monitoring progress.
"""

import asyncio
import json
import pathlib
import platform as platform_mod
import shutil
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Callable, Any
import logging

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Import functions from mediaforce
from mediaforce.core import (
    AppSettings,
    LibrarySettings,
    get_db_path,
    get_library_root,
    init_db_shim,
    iter_libraries_for_current_host,
    load_app_settings,
    save_app_settings,
    _watch_libraries,
)
from mediaforce.db import MediaItem, EncodeResult, ShowOverride, now_iso
from sqlalchemy import func
from sqlmodel import select
from dataclasses import asdict

# Configuration
IS_MAC = platform_mod.system() == "Darwin"
logger = logging.getLogger("mediaforce.web")
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]


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


def get_library_status() -> list[dict]:
    """Return library list with last_scan timestamp (if DB present)."""
    settings = load_app_settings()
    libs = []
    for lib, root in iter_libraries_for_current_host(settings):
        db = get_db_path(root)
        last_scan = None
        running = SCAN_STATUS.get(str(root)) == "running"
        if db.exists():
            try:
                conn = init_db_shim(db)
                row = conn.execute("SELECT MAX(scanned_at) FROM media_inventory").fetchone()
                last_scan = row[0] if row and row[0] else None
                conn.close()
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
    try:
        root = library_root or resolve_existing_library_root()
        if not root:
            return []
        with get_db_connection(root) as conn:
            rows = conn.execute(
                """
                SELECT machine, COUNT(*) as active, MAX(updated_at) as updated_at,
                       MAX(percent_complete) as percent_complete,
                       MAX(tier) as tier,
                       MAX(source_path) as sample_path
                FROM encode_progress
                GROUP BY machine
                ORDER BY machine
                """
            ).fetchall()
            workers = []
            for row in rows:
                workers.append({
                    "machine": row["machine"],
                    "active": row["active"],
                    "percent_complete": row["percent_complete"] or 0,
                    "tier": row["tier"],
                    "sample_path": row["sample_path"],
                    "updated_at": row["updated_at"],
                })
            return workers
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


@app.on_event("startup")
async def _startup_watch():
    """Ensure watcher starts automatically when configured libraries exist."""
    try:
        if _get_watch_libraries():
            await _start_watch_task()
    except Exception:
        # Don't fail app startup if watch cannot start
        WATCH_STATUS.update({"running": False, "message": "watch start failed"})


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
            await _watch_libraries()
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


# =============================================================================
# Pydantic Models for API
# =============================================================================


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


# =============================================================================
# Database Helpers
# =============================================================================


@contextmanager
def get_db_connection(library_path: str = DEFAULT_LIBRARY):
    """Get database connection for the library.

    Raises FileNotFoundError if the library root does not exist (e.g., missing mount).
    """
    library_root = get_library_root(pathlib.Path(library_path))
    if not library_root.exists():
        raise FileNotFoundError(f"Library root not found: {library_root}")
    db_path = get_db_path(library_root)
    conn = init_db_shim(db_path)
    try:
        yield conn
    finally:
        conn.close()


def resolve_existing_library_root() -> Optional[str]:
    """Return first existing library root for this host, or None."""
    settings = load_app_settings()
    for _, root in iter_libraries_for_current_host(settings):
        if pathlib.Path(root).exists():
            return str(root)
    return None


def format_size(bytes_val: float | int | None) -> str:
    """Format bytes as human-readable size."""
    if bytes_val is None:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(bytes_val) < 1024:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} PB"


def format_duration(seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    if seconds is None:
        return "?"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def extract_show_name(path: str) -> Optional[str]:
    """Extract show name from path (assumes /Show Name/Season X/...)."""
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
    """Create a pagination URL builder for the current request."""
    def pagination_url(page: int) -> str:
        params = dict(request.query_params)
        params["page"] = str(page)
        query_string = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{request.url.path}?{query_string}"
    return pagination_url


# =============================================================================
# HTML Page Routes
# =============================================================================


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

    with get_db_connection(library_root) as conn:
        # Get status counts
        cursor = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM media_inventory
            GROUP BY status
        """)
        status_counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

        # Get space saved
        cursor = conn.execute("""
            SELECT
                COALESCE(SUM(m.size_bytes), 0) as source_bytes,
                COALESCE(SUM(e.output_size_bytes), 0) as output_bytes
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE e.output_size_bytes > 0
        """)
        row = cursor.fetchone()
        space_saved_gb = 0
        if row and row[0] and row[1]:
            space_saved_gb = (row[0] - row[1]) / 1024 / 1024 / 1024

        # Active encodes (with progress)
        cursor = conn.execute("""
            SELECT p.*, m.size_bytes as source_size_bytes, m.video_codec
            FROM encode_progress p
            LEFT JOIN media_inventory m ON p.source_id = m.id
            ORDER BY p.started_at DESC
        """)
        active_encodes = []
        for row in cursor.fetchall():
            filename = pathlib.Path(row["source_path"]).name if row["source_path"] else "Unknown"
            show_name = extract_show_name(row["source_path"]) if row["source_path"] else None
            eta_display = None
            if row["eta_seconds"] and row["eta_seconds"] > 0:
                eta_display = format_duration(row["eta_seconds"])
            active_encodes.append({
                "filename": filename,
                "path": row["source_path"],
                "show_name": show_name,
                "machine": row["machine"],
                "tier": row["tier"],
                "started_at": row["started_at"][:16] if row["started_at"] else None,
                "percent_complete": row["percent_complete"] or 0,
                "speed": f"{row['speed']:.2f}x" if row["speed"] else "0x",
                "eta": eta_display,
                "phase": row["phase"] or "encoding",
            })

        # Recent completions (last 10)
        cursor = conn.execute("""
            SELECT m.path, m.size_bytes, e.output_size_bytes, e.completed_at,
                   m.detected_tier
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE e.output_size_bytes > 0
            ORDER BY e.completed_at DESC
            LIMIT 10
        """)
        recent_completions = []
        for row in cursor.fetchall():
            reduction = 0
            if row["size_bytes"] and row["output_size_bytes"]:
                reduction = int((1 - row["output_size_bytes"] / row["size_bytes"]) * 100)
            recent_completions.append({
                "path": row["path"],
                "filename": pathlib.Path(row["path"]).name,
                "source_size": format_size(row["size_bytes"]),
                "output_size": format_size(row["output_size_bytes"]),
                "reduction": reduction,
                "tier": row["detected_tier"],
                "completed_at": row["completed_at"][:16] if row["completed_at"] else "?",
            })

        # Get tier counts for pending
        cursor = conn.execute("""
            SELECT detected_tier, COUNT(*) as cnt
            FROM media_inventory
            WHERE status = 'pending'
            GROUP BY detected_tier
        """)
        tier_counts = {row["detected_tier"]: row["cnt"] for row in cursor.fetchall()}

        lib_status = get_library_status()
        workers = get_worker_status(library_root)

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

    try:
        with get_db_connection(library_root) as conn:
            return await queue_shows_view(
                request,
                conn,
                page,
                per_page,
                library_root,
                libs,
                show,
                tier,
                _parse_size_param(size_min),
                _parse_size_param(size_max),
            )
    except FileNotFoundError as exc:
        return templates.TemplateResponse("queue.html", {
            "request": request,
            "title": "Queue",
            "active": "queue",
            "view_mode": "shows",
            "shows": [],
            "total": 0,
            "total_files": 0,
            "total_savings": "0",
            "page": 1,
            "per_page": per_page,
            "total_pages": 1,
            "page_range": [1],
            "breadcrumbs": [],
            "pagination_url": build_pagination_url(request),
            "library_root": library_root,
            "libraries": libs,
            "lib_status": get_library_status(),
            "workers": get_worker_status(library_root),
            "watch_status": _watch_status_snapshot(),
            "nav_status": _nav_status(),
            "error": str(exc),
        })


async def queue_shows_view(
    request: Request,
    conn,
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

    sort_map = {
        "name": "show_name COLLATE NOCASE",
        "files": "file_count",
        "size": "total_size_bytes",
        "savings": "total_savings_bytes",
        "priority": "max_priority",
        "date": "latest_scan",
        "reduction": "reduction_ratio",
    }
    sort_expr = sort_map.get(sort, "max_priority")

    like_pattern = f"{library_root}/%"

    # Dynamic filters
    filters = ["status = 'pending'", "path LIKE ?"]
    params: list = [like_pattern]
    if show_filter:
        filters.append("LOWER(path) LIKE ?")
        params.append(f"%{show_filter.lower()}%")
    if tier_filter:
        filters.append("detected_tier = ?")
        params.append(tier_filter)
    if size_min_bytes is not None:
        filters.append("size_bytes >= ?")
        params.append(size_min_bytes)
    if size_max_bytes is not None:
        filters.append("size_bytes <= ?")
        params.append(size_max_bytes)

    where_clause = " AND ".join(filters)

    # Base aggregation CTE
    agg_sql = f"""
    WITH rel AS (
        SELECT
            CASE
                WHEN instr(substr(path, {len(library_root)+2}), '/') > 0
                    THEN substr(substr(path, {len(library_root)+2}), 1, instr(substr(path, {len(library_root)+2}), '/') - 1)
                ELSE substr(path, {len(library_root)+2})
            END AS show_name,
            size_bytes,
            potential_savings_bytes,
            priority_score,
            detected_tier,
            scanned_at,
            mtime,
            updated_at
        FROM media_inventory
        WHERE {where_clause}
    ), agg AS (
        SELECT
            show_name,
            COUNT(*) AS file_count,
            SUM(size_bytes) AS total_size_bytes,
            SUM(potential_savings_bytes) AS total_savings_bytes,
            AVG(priority_score) AS avg_priority,
            MAX(priority_score) AS max_priority,
            MAX(COALESCE(scanned_at, updated_at, datetime(mtime, 'unixepoch'))) AS latest_scan,
            CASE
                WHEN SUM(size_bytes) > 0 AND SUM(potential_savings_bytes) IS NOT NULL THEN
                    SUM(potential_savings_bytes) * 1.0 / SUM(size_bytes)
                ELSE 0
            END AS reduction_ratio
        FROM rel
        GROUP BY show_name
    )
    SELECT * FROM agg
    ORDER BY {sort_expr} {direction}
    LIMIT ? OFFSET ?
    """

    cache_key = f"totals:{library_root}:{sort}:{direction}:{like_pattern}:{show_filter}:{tier_filter}:{size_min_bytes}:{size_max_bytes}"
    total_row = _queue_totals_get(cache_key)
    if not total_row:
        total_row = conn.execute(
            f"""
            WITH rel AS (
                SELECT
                    CASE
                        WHEN instr(substr(path, {len(library_root)+2}), '/') > 0
                            THEN substr(substr(path, {len(library_root)+2}), 1, instr(substr(path, {len(library_root)+2}), '/') - 1)
                        ELSE substr(path, {len(library_root)+2})
                    END AS show_name,
                    size_bytes,
                    potential_savings_bytes
                FROM media_inventory
                WHERE {where_clause}
            ), agg AS (
                SELECT show_name,
                       COUNT(*) AS file_count,
                       SUM(size_bytes) AS total_size_bytes,
                       SUM(potential_savings_bytes) AS total_savings_bytes
                FROM rel
                GROUP BY show_name
            )
            SELECT COUNT(*) as total_shows,
                   SUM(file_count) as total_files,
                   SUM(total_savings_bytes) as total_savings
            FROM agg
            """,
            tuple(params),
        ).fetchone()
        _queue_totals_set(cache_key, total_row)

    total = (total_row[0] if total_row else 0) or 0
    total_files = (total_row[1] if total_row else 0) or 0
    total_savings = (total_row[2] if total_row else 0) or 0

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = conn.execute(agg_sql, tuple(params + [per_page, offset])).fetchall()

    shows = []
    for row in rows:
        shows.append({
            "show_name": row["show_name"],
            "file_count": row["file_count"],
            "total_size": format_size(row["total_size_bytes"]),
            "total_size_bytes": row["total_size_bytes"] or 0,
            "total_savings": format_size(row["total_savings_bytes"]) if row["total_savings_bytes"] else "?",
            "total_savings_bytes": row["total_savings_bytes"] or 0,
            "avg_priority": row["avg_priority"] or 0,
            "max_priority": row["max_priority"] or 0,
            "latest_scan": row["latest_scan"],
            "reduction_pct": (row["reduction_ratio"] or 0) * 100 if row.get("reduction_ratio") is not None else 0,
        })

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


async def queue_seasons_view(request: Request, conn, show: str, page: int, per_page: int, library_root: str):
    """View seasons for a specific show."""
    sort = request.query_params.get("sort", "priority")
    direction = request.query_params.get("order", "desc").lower()
    direction = "desc" if direction not in ["asc", "desc"] else direction

    sort_map = {
        "season": "season_name COLLATE NOCASE",
        "files": "file_count",
        "size": "total_size_bytes",
        "savings": "total_savings_bytes",
        "priority": "max_priority",
    }
    sort_expr = sort_map.get(sort, "max_priority")

    like_pattern = f"{library_root}/{show}/%"

    total_row = conn.execute(
        """
        WITH rel AS (
            SELECT
                CASE
                    WHEN instr(substr(path, ?), '/') > 0
                        THEN substr(substr(path, ?), 1, instr(substr(path, ?), '/') - 1)
                    ELSE 'Files'
                END AS season_name,
                size_bytes,
                potential_savings_bytes,
                priority_score
            FROM media_inventory
            WHERE status='pending' AND path LIKE ?
        ), agg AS (
            SELECT
                season_name,
                COUNT(*) AS file_count,
                SUM(size_bytes) AS total_size_bytes,
                SUM(potential_savings_bytes) AS total_savings_bytes,
                AVG(priority_score) AS avg_priority,
                MAX(priority_score) AS max_priority
            FROM rel
            GROUP BY season_name
        )
        SELECT COUNT(*) as total_seasons,
               SUM(file_count) as total_files,
               SUM(total_savings_bytes) as total_savings
        FROM agg
        """,
        (len(library_root)+len(show)+3,)*4 + (like_pattern,),
    ).fetchone()

    total = total_row["total_seasons"] or 0
    total_files = total_row["total_files"] or 0
    total_savings = total_row["total_savings"] or 0

    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    rows = conn.execute(
        f"""
        WITH rel AS (
            SELECT
                CASE
                    WHEN instr(substr(path, ?), '/') > 0
                        THEN substr(substr(path, ?), 1, instr(substr(path, ?), '/') - 1)
                    ELSE 'Files'
                END AS season_name,
                size_bytes,
                potential_savings_bytes,
                priority_score
            FROM media_inventory
            WHERE status='pending' AND path LIKE ?
        ), agg AS (
            SELECT
                season_name,
                COUNT(*) AS file_count,
                SUM(size_bytes) AS total_size_bytes,
                SUM(potential_savings_bytes) AS total_savings_bytes,
                AVG(priority_score) AS avg_priority,
                MAX(priority_score) AS max_priority
            FROM rel
            GROUP BY season_name
        )
        SELECT * FROM agg
        ORDER BY {sort_expr} {direction}
        LIMIT ? OFFSET ?
        """,
        (len(library_root)+len(show)+3,)*4 + (like_pattern, per_page, offset),
    ).fetchall()

    seasons = []
    for row in rows:
        seasons.append({
            "season_name": row["season_name"],
            "file_count": row["file_count"],
            "total_size": format_size(row["total_size_bytes"]),
            "total_savings": format_size(row["total_savings_bytes"]) if row["total_savings_bytes"] else "?",
            "total_savings_bytes": row["total_savings_bytes"] or 0,
            "avg_priority": row["avg_priority"] or 0,
            "max_priority": row["max_priority"] or 0,
        })

    # Pagination
    total = len(seasons)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    seasons = seasons[offset:offset + per_page]

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
        "sort": sort,
        "order": direction,
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
        SELECT COUNT(*) as total, SUM(potential_savings_bytes) as total_savings
        FROM media_inventory
        WHERE status = 'pending' AND path LIKE ?
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
        SELECT id, path, size_bytes, detected_tier, priority_score, bitrate_kbps,
               duration_sec, is_interlaced, potential_savings_bytes,
               video_codec, video_profile, resolution, width, height,
               bit_depth, frame_rate, is_hdr, hdr_format, audio_tracks, subtitle_tracks,
               tier_reasoning
        FROM media_inventory
        WHERE status = 'pending' AND path LIKE ?
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
    params: list = []
    if q:
        filters.append("LOWER(m.path) LIKE ?")
        params.append(f"%{q.lower()}%")
    if status:
        filters.append("m.status = ?")
        params.append(status)
    if tier:
        filters.append("m.detected_tier = ?")
        params.append(tier)

    where_clause = " AND ".join(filters) if filters else "1=1"

    with get_db_connection(library_root) as conn:
        cursor = conn.execute(
            f"""
            SELECT m.id, m.path, m.status, m.detected_tier, m.size_bytes, m.updated_at,
                   COALESCE(e.output_size_bytes, 0) as output_size_bytes,
                   e.completed_at
            FROM media_inventory m
            LEFT JOIN encode_results e ON e.source_id = m.id
            WHERE {where_clause}
            ORDER BY m.updated_at DESC
            LIMIT 200
            """,
            tuple(params),
        )

        rows = cursor.fetchall()

    results = []
    for row in rows:
        reduction = 0
        if row["output_size_bytes"] and row["size_bytes"]:
            reduction = int((1 - row["output_size_bytes"] / row["size_bytes"]) * 100)
        results.append({
            "path": row["path"],
            "filename": pathlib.Path(row["path"]).name,
            "status": row["status"],
            "tier": row["detected_tier"],
            "size": format_size(row["size_bytes"]),
            "reduction": reduction,
            "updated_at": row["updated_at"][:16] if row["updated_at"] else None,
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
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT e.id, m.path, m.size_bytes, m.detected_tier,
                   e.output_size_bytes, e.vmaf, e.promoted_at
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE m.status = 'completed'
              AND e.output_size_bytes > 0
            ORDER BY e.promoted_at DESC
            LIMIT 100
        """)

        encodes = []
        for row in cursor.fetchall():
            reduction = 0
            if row["size_bytes"] and row["output_size_bytes"]:
                reduction = int((1 - row["output_size_bytes"] / row["size_bytes"]) * 100)
            encodes.append({
                "id": row["id"],
                "source_path": row["path"],
                "filename": pathlib.Path(row["path"]).name,
                "source_size": format_size(row["size_bytes"]),
                "output_size": format_size(row["output_size_bytes"]),
                "reduction": reduction,
                "tier": row["detected_tier"],
                "vmaf": f"{row['vmaf']:.1f}" if row["vmaf"] else None,
                "promoted_at": row["promoted_at"][:16] if row["promoted_at"] else None,
            })

    return templates.TemplateResponse("completed.html", {
        "request": request,
        "title": "Completed",
        "active": "completed",
        "encodes": encodes,
        "nav_status": _nav_status(),
    })


@app.get("/review", response_class=HTMLResponse)
async def review(request: Request):
    """Review encodes pending promotion."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT e.id, m.path, m.size_bytes, m.detected_tier, e.output_path,
                   e.output_size_bytes, e.vmaf, e.is_outlier
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE m.status = 'encoded'
              AND e.output_path IS NOT NULL
              AND e.output_size_bytes > 0
            ORDER BY e.completed_at DESC
        """)

        encodes = []
        for row in cursor.fetchall():
            reduction_pct = 0
            size_increase_pct = 0
            if row["size_bytes"] and row["output_size_bytes"]:
                reduction_pct = (1 - row["output_size_bytes"] / row["size_bytes"]) * 100
                if reduction_pct < 0:
                    size_increase_pct = abs(reduction_pct)

            show_name = extract_show_name(row["path"])

            encodes.append({
                "id": row["id"],
                "source_path": row["path"],
                "filename": pathlib.Path(row["path"]).name,
                "show_name": show_name,
                "source_size": format_size(row["size_bytes"]),
                "output_size": format_size(row["output_size_bytes"]),
                "reduction": f"{reduction_pct:.0f}",
                "reduction_pct": reduction_pct,
                "size_increase_pct": f"{size_increase_pct:.0f}",
                "tier": row["detected_tier"],
                "vmaf": f"{row['vmaf']:.1f}" if row["vmaf"] else None,
                "is_outlier": bool(row["is_outlier"]),
            })

    return templates.TemplateResponse("review.html", {
        "request": request,
        "title": "Review",
        "active": "review",
        "encodes": encodes,
        "nav_status": _nav_status(),
    })


@app.get("/compare/{encode_id}", response_class=HTMLResponse)
async def compare(request: Request, encode_id: int):
    """Side-by-side video comparison."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT e.*, m.path as source_path, m.size_bytes as source_size,
                   m.video_codec, m.detected_tier, m.is_interlaced
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE e.id = ?
        """, (encode_id,))

        row = cursor.fetchone()

    if not row:
        return HTMLResponse("Encode not found", status_code=404)

    reduction = 0
    if row["source_size"] and row["output_size_bytes"]:
        reduction = int((1 - row["output_size_bytes"] / row["source_size"]) * 100)

    return templates.TemplateResponse("compare.html", {
        "request": request,
        "title": "Compare",
        "active": "review",
        "encode_id": encode_id,
        "filename": pathlib.Path(row["source_path"]).name,
        "source_codec": row["video_codec"] or "?",
        "source_size": format_size(row["source_size"]),
        "output_size": format_size(row["output_size_bytes"]),
        "reduction": reduction,
        "tier": row["detected_tier"],
        "crf": row["crf"],
        "preset": row["preset"],
        "vmaf": f"{row['vmaf']:.1f}" if row["vmaf"] else None,
        "ssim": f"{row['ssim']:.4f}" if row["ssim"] else None,
        "deinterlaced": row["is_interlaced"],
        "nav_status": _nav_status(),
    })


@app.get("/shows", response_class=HTMLResponse)
async def shows(request: Request):
    """Show/Series management page."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT path, status, detected_tier
            FROM media_inventory
            WHERE path LIKE '%/Season %'
        """)

        # Aggregate by show name
        show_data: dict[str, dict[str, Any]] = {}
        for row in cursor.fetchall():
            show_name = extract_show_name(row["path"])
            if not show_name:
                continue

            if show_name not in show_data:
                show_data[show_name] = {
                    "name": show_name,
                    "total": 0,
                    "pending": 0,
                    "encoded": 0,
                    "completed": 0,
                    "tiers": {},
                }

            show_data[show_name]["total"] += 1
            status = row["status"]
            if status in ["pending", "encoded", "completed"]:
                show_data[show_name][status] += 1

            tier = row["detected_tier"]
            if tier:
                show_data[show_name]["tiers"][tier] = show_data[show_name]["tiers"].get(tier, 0) + 1

        # Determine detected tier for each show
        for show in show_data.values():
            if show["tiers"]:
                show["detected_tier"] = max(show["tiers"].items(), key=lambda x: x[1])[0]
            else:
                show["detected_tier"] = None
            del show["tiers"]

        # Get show overrides
        cursor = conn.execute("SELECT show_name, default_tier FROM show_overrides")
        overrides = {row["show_name"]: row["default_tier"] for row in cursor.fetchall()}

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


# =============================================================================
# Video Streaming
# =============================================================================


@app.get("/video/{video_type}/{encode_id}")
async def serve_video(video_type: str, encode_id: int):
    """Serve video files for comparison."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT e.output_path, m.path as source_path
            FROM encode_results e
            JOIN media_inventory m ON e.source_id = m.id
            WHERE e.id = ?
        """, (encode_id,))

        row = cursor.fetchone()

    if not row:
        return HTMLResponse("Not found", status_code=404)

    if video_type == "source":
        video_path = pathlib.Path(row["source_path"])
    elif video_type == "encoded":
        video_path = pathlib.Path(row["output_path"])
    else:
        return HTMLResponse("Invalid video type", status_code=400)

    if not video_path.exists():
        return HTMLResponse("Video file not found", status_code=404)

    return FileResponse(video_path)


# =============================================================================
# API Endpoints
# =============================================================================


@app.post("/api/promote/{encode_id}")
async def api_promote(encode_id: int):
    """Promote an encode (replace original with encoded version)."""
    with get_db_connection() as shim:
        session = shim.session
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

        if not output_path.exists():
            return {"success": False, "error": "Encoded file not found"}

        try:
            new_name = source_path.stem + ".mp4"
            new_path = source_path.parent / new_name

            shutil.move(str(output_path), str(new_path))

            if source_path != new_path and source_path.exists():
                source_path.unlink()

            item.status = "completed"
            item.path = str(new_path)
            item.updated_at = now_iso()
            encode.promoted_at = now_iso()
            session.add(item)
            session.add(encode)
            session.commit()
            return {"success": True}
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}


@app.post("/api/reject/{encode_id}")
async def api_reject(encode_id: int, data: RejectRequest):
    """Reject an encode."""
    with get_db_connection() as shim:
        session = shim.session
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

        try:
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
            session.commit()

            return {"success": True, "show_name": show_name}
        except Exception as e:
            session.rollback()
            return {"success": False, "error": str(e)}


@app.post("/api/bump")
async def api_bump(data: BumpRequest):
    """Bump an item to the front of the queue by lowering manual_priority."""
    if data.id is None and (not data.path):
        return {"success": False, "error": "id or path required"}
    with get_db_connection() as shim:
        session = shim.session
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
        session.commit()

    return {"success": True, "manual_priority": new_priority}


class SendToWorkerRequest(BaseModel):
    id: int
    worker: str


@app.post("/api/send-to-worker")
async def api_send_to_worker(data: SendToWorkerRequest):
    """Hint a specific worker to take a pending item by bumping it and setting claimed_by."""
    if not data.worker:
        return {"success": False, "error": "worker required"}

    with get_db_connection() as shim:
        session = shim.session
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
        session.commit()

    return {"success": True, "manual_priority": new_priority}


@app.post("/api/bulk-promote")
async def api_bulk_promote(data: BulkPromoteRequest):
    """Bulk promote multiple encodes."""
    if not data.ids:
        return {"success": False, "error": "No IDs provided"}

    promoted = 0
    failed = 0

    with get_db_connection() as conn:
        for encode_id in data.ids:
            try:
                cursor = conn.execute("""
                    SELECT e.output_path, m.path as source_path, m.id as source_id
                    FROM encode_results e
                    JOIN media_inventory m ON e.source_id = m.id
                    WHERE e.id = ?
                """, (encode_id,))

                row = cursor.fetchone()
                if not row:
                    failed += 1
                    continue

                output_path = pathlib.Path(row["output_path"])
                source_path = pathlib.Path(row["source_path"])

                if not output_path.exists():
                    failed += 1
                    continue

                new_name = source_path.stem + ".mp4"
                new_path = source_path.parent / new_name

                shutil.move(str(output_path), str(new_path))

                if source_path != new_path and source_path.exists():
                    source_path.unlink()

                conn.execute("""
                    UPDATE media_inventory
                    SET status = 'completed', path = ?, updated_at = ?
                    WHERE id = ?
                """, (str(new_path), datetime.now().isoformat(), row["source_id"]))

                conn.execute("""
                    UPDATE encode_results
                    SET promoted_at = ?
                    WHERE id = ?
                """, (datetime.now().isoformat(), encode_id))

                promoted += 1
            except Exception:
                failed += 1

        conn.commit()

    return {"success": True, "promoted": promoted, "failed": failed}


@app.post("/api/bulk-reject")
async def api_bulk_reject(data: BulkRejectRequest):
    """Bulk reject multiple encodes."""
    if not data.ids:
        return {"success": False, "error": "No IDs provided"}

    rejected = 0
    failed = 0

    with get_db_connection() as conn:
        for encode_id in data.ids:
            try:
                cursor = conn.execute("""
                    SELECT e.output_path, m.id as source_id
                    FROM encode_results e
                    JOIN media_inventory m ON e.source_id = m.id
                    WHERE e.id = ?
                """, (encode_id,))

                row = cursor.fetchone()
                if not row:
                    failed += 1
                    continue

                output_path = pathlib.Path(row["output_path"])

                if output_path.exists():
                    output_path.unlink()

                try:
                    parent = output_path.parent
                    while parent.name and not any(parent.iterdir()):
                        parent.rmdir()
                        parent = parent.parent
                except (OSError, StopIteration):
                    pass

                if data.new_tier:
                    conn.execute("""
                        UPDATE media_inventory
                        SET status = 'pending', detected_tier = ?, updated_at = ?
                        WHERE id = ?
                    """, (data.new_tier, datetime.now().isoformat(), row["source_id"]))
                else:
                    conn.execute("""
                        UPDATE media_inventory
                        SET status = 'pending', updated_at = ?
                        WHERE id = ?
                    """, (datetime.now().isoformat(), row["source_id"]))

                conn.execute("DELETE FROM encode_results WHERE id = ?", (encode_id,))
                rejected += 1
            except Exception:
                failed += 1

        conn.commit()

    return {"success": True, "rejected": rejected, "failed": failed}


@app.post("/api/show-override")
async def api_show_override(data: ShowOverrideRequest):
    """Set or clear a tier override for a show."""
    if not data.show_name:
        return {"success": False, "error": "show_name required"}

    with get_db_connection() as conn:
        try:
            if data.tier:
                conn.execute("""
                    INSERT INTO show_overrides (show_name, default_tier, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(show_name) DO UPDATE SET
                        default_tier = excluded.default_tier,
                        updated_at = datetime('now')
                """, (data.show_name, data.tier))
            else:
                conn.execute("DELETE FROM show_overrides WHERE show_name = ?", (data.show_name,))

            conn.commit()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}


@app.post("/api/apply-tier-to-show")
async def api_apply_tier_to_show(data: ApplyTierRequest):
    """Apply a tier to all pending episodes of a show."""
    if not data.show_name or not data.tier:
        return {"success": False, "error": "show_name and tier required"}

    with get_db_connection() as conn:
        try:
            cursor = conn.execute("""
                UPDATE media_inventory
                SET detected_tier = ?, updated_at = datetime('now')
                WHERE status = 'pending'
                  AND path LIKE ?
            """, (data.tier, f"%/{data.show_name}/Season %"))

            updated = cursor.rowcount

            if data.set_override:
                conn.execute("""
                    INSERT INTO show_overrides (show_name, default_tier, updated_at)
                    VALUES (?, ?, datetime('now'))
                    ON CONFLICT(show_name) DO UPDATE SET
                        default_tier = excluded.default_tier,
                        updated_at = datetime('now')
                """, (data.show_name, data.tier))

            conn.commit()
            return {"success": True, "updated": updated}
        except Exception as e:
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

    with get_db_connection(library_root) as conn:
        cursor = conn.execute("""
            SELECT p.*, m.size_bytes as source_size_bytes, m.video_codec
            FROM encode_progress p
            LEFT JOIN media_inventory m ON p.source_id = m.id
            ORDER BY p.started_at DESC
        """)
        encodes = []
        for row in cursor.fetchall():
            eta_display = None
            if row["eta_seconds"] and row["eta_seconds"] > 0:
                eta_display = format_duration(row["eta_seconds"])
            encodes.append({
                "filename": pathlib.Path(row["source_path"]).name if row["source_path"] else "Unknown",
                "path": row["source_path"],
                "show_name": extract_show_name(row["source_path"]) if row["source_path"] else None,
                "machine": row["machine"],
                "tier": row["tier"],
                "started_at": row["started_at"][:16] if row["started_at"] else None,
                "percent_complete": row["percent_complete"] or 0,
                "speed": row["speed"] or 0,
                "eta": eta_display,
                "phase": row["phase"] or "encoding",
                "frame": row["frame"] or 0,
                "total_frames": row["total_frames"] or 0,
                "fps": row["fps"] or 0,
            })

    return {"success": True, "encodes": encodes}


@app.get("/api/queue/seasons/{show_name}")
async def api_queue_seasons(show_name: str, request: Request):
    """Get seasons for a specific show."""
    library_root = request.query_params.get('library') or _resolve_library(request)
    with get_db_connection(library_root) as conn:
        like_pattern = f"{library_root}/{show_name}/%"
        rows = conn.execute(
            "SELECT path, size_bytes, potential_savings_bytes, priority_score FROM media_inventory WHERE status='pending' AND path LIKE ?",
            (like_pattern,),
        ).fetchall()

        season_map: dict[str, dict] = {}
        for row in rows:
            parsed = parse_media_path(row["path"], library_root)
            season_name = parsed["season"] or "Files"
            ent = season_map.setdefault(season_name, {
                "season_name": season_name,
                "file_count": 0,
                "total_size_bytes": 0,
                "total_savings_bytes": 0,
                "max_priority": 0.0,
            })

            ent["file_count"] += 1
            ent["total_size_bytes"] += row["size_bytes"] or 0
            ent["total_savings_bytes"] += row["potential_savings_bytes"] or 0
            ent["max_priority"] = max(ent["max_priority"], row["priority_score"] or 0)

        seasons = [{
            "season_name": ent["season_name"],
            "file_count": ent["file_count"],
            "total_size": format_size(ent["total_size_bytes"]),
            "total_savings": format_size(ent["total_savings_bytes"]) if ent["total_savings_bytes"] else "?",
            "total_savings_bytes": ent["total_savings_bytes"],
            "max_priority": ent["max_priority"],
        } for ent in season_map.values()]

        seasons = sorted(seasons, key=lambda x: -x["max_priority"])

    return {"seasons": seasons}


@app.get("/api/queue/episodes/{show_name}/{season_name}")
async def api_queue_episodes(show_name: str, season_name: str, request: Request):
    """Get episodes for a specific show/season."""
    library_root = request.query_params.get('library') or _resolve_library(request)
    with get_db_connection(library_root) as conn:
        if season_name == "Files":
            like_pattern = f"{library_root}/{show_name}/%"
        else:
            like_pattern = f"{library_root}/{show_name}/{season_name}/%"
        cursor = conn.execute("""
            SELECT id, path, size_bytes, detected_tier, priority_score, bitrate_kbps,
                   duration_sec, is_interlaced, potential_savings_bytes,
                   video_codec, video_profile, resolution, width, height,
                   bit_depth, frame_rate, is_hdr, hdr_format, audio_tracks, subtitle_tracks,
                   tier_reasoning
            FROM media_inventory
            WHERE status = 'pending' AND path LIKE ?
            ORDER BY priority_score DESC
        """, (like_pattern,))

        episodes = []
        for row in cursor.fetchall():
            savings = row["potential_savings_bytes"] or 0

            # Parse audio tracks
            audio_info = "?"
            if row["audio_tracks"]:
                try:
                    tracks = json.loads(row["audio_tracks"])
                    audio_info = ", ".join(
                        f"{t.get('codec', '?')} {t.get('channels', '?')}ch"
                        for t in tracks[:3]
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

            episodes.append({
                "id": row["id"],
                "path": row["path"],
                "filename": pathlib.Path(row["path"]).name,
                "size_bytes": row["size_bytes"],
                "size": format_size(row["size_bytes"]),
                "detected_tier": row["detected_tier"],
                "priority_score": row["priority_score"],
                "bitrate_kbps": row["bitrate_kbps"],
                "bitrate": f"{row['bitrate_kbps']}k" if row["bitrate_kbps"] else "?",
                "duration": format_duration(row["duration_sec"]),
                "duration_sec": row["duration_sec"],
                "is_interlaced": row["is_interlaced"],
                "savings": format_size(savings) if savings else "?",
                "savings_bytes": savings,
                "video_codec": row["video_codec"] or "?",
                "video_profile": row["video_profile"] or "",
                "resolution": row["resolution"] or f"{row['width']}x{row['height']}" if row["width"] else "?",
                "width": row["width"],
                "height": row["height"],
                "bit_depth": row["bit_depth"],
                "frame_rate": row["frame_rate"] or "?",
                "is_hdr": row["is_hdr"],
                "hdr_format": row["hdr_format"],
                "audio_info": audio_info,
                "subtitle_count": sub_count,
                "tier_reasoning": row["tier_reasoning"] or "",
            })

    return {"episodes": episodes}


@app.get("/api/stats")
async def api_stats():
    """Get current stats as JSON."""
    with get_db_connection() as conn:
        cursor = conn.execute("""
            SELECT status, COUNT(*) as cnt
            FROM media_inventory
            GROUP BY status
        """)
        status_counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}

    return status_counts




# =============================================================================
# Main
# =============================================================================


def main():
    """Entry point for av1-web command."""
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
