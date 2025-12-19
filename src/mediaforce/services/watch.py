import asyncio
import pathlib
import time
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from mediaforce.config.logging import log_event
from mediaforce.config.paths import iter_libraries_for_current_host, normalize_path
from mediaforce.config.settings import ENGINE, AppSettings, LibrarySettings, load_app_settings
from mediaforce.db import MediaItem, now_iso
from mediaforce.services.classification import classify_source
from mediaforce.services.media_probe import probe_media
from mediaforce.services.queue import recalculate_priorities
from mediaforce.services.scanner import VIDEO_EXTENSIONS, scan_file_to_db, calculate_priority


try:  # Optional dependency
    from watchfiles import Change, awatch
except Exception:  # pragma: no cover
    Change = None  # type: ignore
    awatch = None  # type: ignore


async def _watch_single_library(lib: LibrarySettings, root: pathlib.Path) -> None:
    if awatch is None or Change is None:
        log_event(40, "watch_unavailable", error="watchfiles_not_installed")
        return

    if not root.exists():
        log_event(30, "watch_root_missing", library=lib.id, root=str(root))
        return

    log_event(20, "watch_start", library=lib.id, name=lib.name, root=str(root))

    async for changes in awatch(root, recursive=True):
        for change, path_str in changes:
            if change not in (Change.added, Change.modified):
                continue

            path = normalize_path(pathlib.Path(path_str))
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            if path.name.startswith("."):
                continue
            if not path.is_file():
                continue

            try:
                with Session(ENGINE) as session:
                    now = int(time.time())
                    oldest_mtime = session.exec(
                        select(func.min(func.coalesce(MediaItem.mtime, 0))).where(
                            MediaItem.status == "pending",
                            func.coalesce(MediaItem.mtime, 0) > 0,
                        )
                    ).first() or int(path.stat().st_mtime)
                    max_age = max(now - int(oldest_mtime), 1)

                    log_event(20, "watch_detected", library=lib.id, file=str(path))
                    result = scan_file_to_db(
                        session,
                        path,
                        max_savings=1,
                        max_age=max_age,
                        library_id=lib.id,
                        classify_source=classify_source,
                        probe_media=probe_media,
                        now_iso=now_iso,
                    )
                    session.commit()
                    recalculate_priorities(session, max_age=max_age, calculate_priority=calculate_priority)

                if result:
                    log_event(20, "watch_queued", library=lib.id, file=str(path), tier=result.get("tier"))
                else:
                    log_event(40, "watch_scan_failed", library=lib.id, file=str(path))
            except Exception as exc:
                log_event(40, "watch_handle_error", library=lib.id, file=str(path), error=str(exc))


async def watch_libraries(settings: Optional[AppSettings] = None) -> None:
    if settings is None:
        settings = load_app_settings()

    libraries = [(lib, root) for lib, root in iter_libraries_for_current_host(settings) if lib.watch]
    if not libraries:
        log_event(30, "watch_no_libraries")
        return

    await asyncio.gather(*[_watch_single_library(lib, root) for lib, root in libraries])
