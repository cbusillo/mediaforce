import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from pathlib import Path

from mediaforce.core.config import MediaforceConfig
from mediaforce.hosts.config import host_targets_current_machine
from mediaforce.hosts.mount_runtime import controller_smb_mounts_path
from mediaforce.remote import HostStatus, collect_host_statuses, probe_host_status
from mediaforce.web.runtime.controller_storage_recovery import controller_storage_recovery_snapshot
from mediaforce.web.settings_runtime import normalize_host_capabilities

_HOST_STATUS_CACHE_LOCK = threading.Lock()
_HOST_STATUS_CACHE_KEY: tuple[str, int, str, int, int] | None = None
_HOST_STATUS_CACHE_FETCHED_AT = 0.0
_HOST_STATUS_CACHE_VALUE: list[HostStatus] = []
_HOST_STATUS_CACHE_REFRESH_FUTURE: Future[tuple[tuple[str, int, str, int, int], list[HostStatus]]] | None = None
_HOST_STATUS_CACHE_TTL_SECONDS = 15.0
_HOST_STATUS_CACHE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="host-status")
_HOST_STATUS_PENDING_MESSAGE = "Checking this computer..."
_LOCAL_HOST_STORAGE_SIGNATURE: object = None


def collect_host_statuses_with_fallback(config: MediaforceConfig) -> list[HostStatus]:
    try:
        return collect_host_statuses(config)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        return _host_status_error(str(exc))


def refresh_host_status_cache(config: MediaforceConfig) -> list[HostStatus]:
    global _HOST_STATUS_CACHE_KEY
    global _HOST_STATUS_CACHE_FETCHED_AT
    global _HOST_STATUS_CACHE_VALUE
    global _HOST_STATUS_CACHE_REFRESH_FUTURE

    cache_key, statuses = _host_status_refresh_payload(config)
    if _host_status_cache_key(config) != cache_key:
        cache_key, statuses = _host_status_refresh_payload(config)
    if _host_status_cache_key(config) != cache_key:
        return list(statuses)
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


def publish_host_status(config: MediaforceConfig, status: HostStatus) -> None:
    """Show a background check's result on the host cards without checking again.

    Web reads only serve this cache (ddd0c6c1), so the background checks keep it current
    even with no queued work (#922). One computer's result does not make the rest fresh.
    """
    global _HOST_STATUS_CACHE_KEY
    global _HOST_STATUS_CACHE_FETCHED_AT
    global _HOST_STATUS_CACHE_VALUE

    if _host_status_config_is_malformed(config):
        return
    _complete_host_status_refresh_if_ready()
    cache_key = _host_status_cache_key(config)
    with _HOST_STATUS_CACHE_LOCK:
        if _HOST_STATUS_CACHE_KEY != cache_key or not _HOST_STATUS_CACHE_VALUE:
            # Settings changed since the last full check: the other computers show as being checked,
            # as a read would show them anyway, and the next full check is still due.
            _HOST_STATUS_CACHE_KEY = cache_key
            _HOST_STATUS_CACHE_FETCHED_AT = float("-inf")
            _HOST_STATUS_CACHE_VALUE = _fallback_host_statuses(config)
        _HOST_STATUS_CACHE_VALUE = [
            status if cached.key == status.key else cached for cached in _HOST_STATUS_CACHE_VALUE
        ]


def refresh_current_machine_host_statuses(config: MediaforceConfig) -> None:
    """Check this computer's own rows again after its storage changed, so a reboot's first check does not stick."""
    global _LOCAL_HOST_STORAGE_SIGNATURE

    hosts = [host for host in config.remote_hosts if isinstance(host, dict) and host_targets_current_machine(host)]
    if not hosts:
        return
    storage = controller_storage_recovery_snapshot(config)
    signature = (
        storage.get("state_error"),
        tuple((mount["mount_point"], mount["status"]) for mount in storage["mounts"]),
    )
    cached = {status.key: status for status in _cached_statuses_for_current_key(config)}
    if signature == _LOCAL_HOST_STORAGE_SIGNATURE and all(
        (status := cached.get(str(host.get("host") or ""))) is not None
        and status.message != _HOST_STATUS_PENDING_MESSAGE
        for host in hosts
    ):
        return
    for host in hosts:
        publish_host_status(config, probe_host_status(config, host))
    _LOCAL_HOST_STORAGE_SIGNATURE = signature


def _cached_statuses_for_current_key(config: MediaforceConfig) -> list[HostStatus]:
    cache_key = _host_status_cache_key(config)
    with _HOST_STATUS_CACHE_LOCK:
        return list(_HOST_STATUS_CACHE_VALUE) if _HOST_STATUS_CACHE_KEY == cache_key else []


def checked_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    """The computers' last check results as the cards show them, without checking anything.

    Computers that have not been checked since settings changed or the service started are left out.
    """
    return [
        status for status in _cached_statuses_for_current_key(config)
        if status.message != _HOST_STATUS_PENDING_MESSAGE
    ]


def is_awaiting_first_check(message: str) -> bool:
    return message == _HOST_STATUS_PENDING_MESSAGE


def cached_host_statuses(config: MediaforceConfig) -> list[HostStatus]:
    _complete_host_status_refresh_if_ready()
    cache_key = _host_status_cache_key(config)
    with _HOST_STATUS_CACHE_LOCK:
        if _HOST_STATUS_CACHE_KEY == cache_key and _HOST_STATUS_CACHE_VALUE:
            return list(_HOST_STATUS_CACHE_VALUE)
    if _host_status_config_is_malformed(config):
        return _host_status_error(None)
    return _fallback_host_statuses(config)


def _host_status_cache_key(config: MediaforceConfig) -> tuple[str, int, str, int, int]:
    return (
        str(config.paths.config_path),
        _path_mtime_ns(config.paths.config_path),
        str(config.paths.runtime_settings_path),
        _path_mtime_ns(config.paths.runtime_settings_path),
        _path_mtime_ns(controller_smb_mounts_path(config.paths.runtime_settings_path)),
    )


def _path_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1


def _host_status_refresh_payload(config: MediaforceConfig) -> tuple[tuple[str, int, str, int, int], list[HostStatus]]:
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
                message=_HOST_STATUS_PENDING_MESSAGE,
                missing_paths=[],
                probe_available=False,
                repo_path=str(host.get("repo_path") or "").strip() or None,
                issues=[],
            )
        )
    return statuses


def _host_status_config_is_malformed(config: MediaforceConfig) -> bool:
    return any(not isinstance(host, dict) for host in config.remote_hosts)


def _host_status_error(detail: str | None) -> list[HostStatus]:
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
            probe_available=False,
            issues=["Review the runtime settings values and save the page again."],
            detail=detail,
        )
    ]


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
    except (AttributeError, CancelledError, OSError, RuntimeError, TypeError, ValueError):
        return

    with _HOST_STATUS_CACHE_LOCK:
        current_key = _host_status_cache_key_from_path(cache_key)
        if current_key == cache_key:
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


def _host_status_cache_key_from_path(
        cache_key: tuple[str, int, str, int, int],
) -> tuple[str, int, str, int, int]:
    runtime_settings_path = Path(cache_key[2])
    return (
        cache_key[0],
        _path_mtime_ns(Path(cache_key[0])),
        cache_key[2],
        _path_mtime_ns(runtime_settings_path),
        _path_mtime_ns(controller_smb_mounts_path(runtime_settings_path)),
    )
