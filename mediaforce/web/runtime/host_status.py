import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from mediaforce.core.config import MediaforceConfig
from mediaforce.remote import HostStatus, collect_host_statuses
from mediaforce.web.settings_runtime import normalize_host_capabilities

_HOST_STATUS_CACHE_LOCK = threading.Lock()
_HOST_STATUS_CACHE_KEY: tuple[str, int, int] | None = None
_HOST_STATUS_CACHE_FETCHED_AT = 0.0
_HOST_STATUS_CACHE_VALUE: list[HostStatus] = []
_HOST_STATUS_CACHE_REFRESH_FUTURE: Future[tuple[tuple[str, int, int], list[HostStatus]]] | None = None
_HOST_STATUS_CACHE_TTL_SECONDS = 15.0
_HOST_STATUS_CACHE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="host-status")


def collect_host_statuses_with_fallback(config: MediaforceConfig) -> list[HostStatus]:
    try:
        return collect_host_statuses(config)
    except Exception as exc:
        return [
            HostStatus(
                key="host-status-error",
                label="Host status unavailable",
                mode="ssh",
                priority=0,
                capabilities=[],
                available=False,
                message="Host checks could not load with the current runtime settings.",
                missing_paths=[],
                issues=["Review the runtime settings values and save the page again."],
                detail=str(exc),
            )
        ]


def refresh_host_status_cache(config: MediaforceConfig) -> list[HostStatus]:
    global _HOST_STATUS_CACHE_KEY
    global _HOST_STATUS_CACHE_FETCHED_AT
    global _HOST_STATUS_CACHE_VALUE
    global _HOST_STATUS_CACHE_REFRESH_FUTURE

    cache_key, statuses = _host_status_refresh_payload(config)
    with _HOST_STATUS_CACHE_LOCK:
        _HOST_STATUS_CACHE_KEY = cache_key
        _HOST_STATUS_CACHE_FETCHED_AT = time.monotonic()
        _HOST_STATUS_CACHE_VALUE = list(statuses)
        _HOST_STATUS_CACHE_REFRESH_FUTURE = None
    return list(statuses)


def safe_collect_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    _complete_host_status_refresh_if_ready()
    cache_key = _host_status_cache_key(config)
    with _HOST_STATUS_CACHE_LOCK:
        cached_matches = _HOST_STATUS_CACHE_KEY == cache_key and bool(_HOST_STATUS_CACHE_VALUE)
        cache_fresh = cached_matches and (
                time.monotonic() - _HOST_STATUS_CACHE_FETCHED_AT
        ) < _HOST_STATUS_CACHE_TTL_SECONDS
        cached_statuses = list(_HOST_STATUS_CACHE_VALUE) if cached_matches else []

    if cache_fresh:
        return cached_statuses
    if cached_statuses:
        _schedule_host_status_refresh(config)
        return cached_statuses
    if _host_status_config_is_malformed(config):
        return collect_host_statuses_with_fallback(config)
    _schedule_host_status_refresh(config)
    return _fallback_host_statuses(config)


def _host_status_cache_key(config: MediaforceConfig) -> tuple[str, int, int]:
    return (
        str(config.paths.config_path),
        _path_mtime_ns(config.paths.config_path),
        _path_mtime_ns(config.paths.runtime_settings_path),
    )


def _path_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


def _host_status_refresh_payload(config: MediaforceConfig) -> tuple[tuple[str, int, int], list[HostStatus]]:
    return _host_status_cache_key(config), collect_host_statuses_with_fallback(config)


def _fallback_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    statuses: list[HostStatus] = []
    for index, raw_host in enumerate(config.remote_hosts):
        host = raw_host if isinstance(raw_host, dict) else {}
        label = str(host.get("label") or host.get("host") or f"remote-{index + 1}").strip() or f"remote-{index + 1}"
        key = str(host.get("host") or label).strip() or label
        try:
            priority = int(str(host.get("priority") or "0").strip() or "0")
        except (TypeError, ValueError):
            priority = 0
        capabilities = normalize_host_capabilities(host.get("capabilities"))
        statuses.append(
            HostStatus(
                key=key,
                label=label,
                mode="ssh",
                priority=priority,
                capabilities=capabilities,
                available=False,
                message="Checking host status...",
                missing_paths=[],
                repo_path=str(host.get("repo_path") or "").strip() or None,
                issues=[],
            )
        )
    return statuses


def _host_status_config_is_malformed(config: MediaforceConfig) -> bool:
    return any(not isinstance(host, dict) for host in config.remote_hosts)


def _complete_host_status_refresh_if_ready() -> None:
    global _HOST_STATUS_CACHE_KEY
    global _HOST_STATUS_CACHE_FETCHED_AT
    global _HOST_STATUS_CACHE_VALUE
    global _HOST_STATUS_CACHE_REFRESH_FUTURE

    with _HOST_STATUS_CACHE_LOCK:
        future = _HOST_STATUS_CACHE_REFRESH_FUTURE
        if future is None or not future.done():
            return
        _HOST_STATUS_CACHE_REFRESH_FUTURE = None

    try:
        cache_key, statuses = future.result()
    except Exception:
        return

    with _HOST_STATUS_CACHE_LOCK:
        _HOST_STATUS_CACHE_KEY = cache_key
        _HOST_STATUS_CACHE_FETCHED_AT = time.monotonic()
        _HOST_STATUS_CACHE_VALUE = list(statuses)


def _schedule_host_status_refresh(config: MediaforceConfig) -> None:
    global _HOST_STATUS_CACHE_REFRESH_FUTURE

    _complete_host_status_refresh_if_ready()
    with _HOST_STATUS_CACHE_LOCK:
        if _HOST_STATUS_CACHE_REFRESH_FUTURE is not None:
            return
        _HOST_STATUS_CACHE_REFRESH_FUTURE = _HOST_STATUS_CACHE_EXECUTOR.submit(_host_status_refresh_payload, config)
