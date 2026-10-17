"""Queue-independent host checks and bounded no-dialog storage recovery."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.hosts.config import host_targets_current_machine
from mediaforce.hosts.controller_mount import controller_mount_lock
from mediaforce.remote import (
    probe_host_status, recover_remote_host_mounts_no_ui,
    remote_mount_recovery_supported,
)
from mediaforce.web.runtime.host_status import publish_host_status

_INITIAL_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 300


def host_recovery_path(config: MediaforceConfig) -> Path:
    return config.paths.runtime_settings_path.with_name("host-recovery.json")


def host_recovery_snapshot(config: MediaforceConfig) -> dict[str, Any]:
    path = host_recovery_path(config)
    try:
        payload = load_runtime_settings(path)
        if payload:
            if payload.get("schema_version") != 1 or not isinstance(payload.get("hosts"), dict):
                raise ValueError("Invalid host recovery state")
            for state in payload["hosts"].values():
                if (
                    not isinstance(state, dict)
                    or state.get("status") not in {"ready", "retrying", "action_required"}
                    or not isinstance(state.get("failure_count"), int)
                    or state["failure_count"] < 0
                    or not isinstance(state.get("mount_uncertain"), bool)
                    or not isinstance(state.get("retry_paused", False), bool)
                    or not isinstance(state.get("label"), str)
                    or not isinstance(state.get("reason"), (str, type(None)))
                    or (state.get("status") != "ready" and _parse_time(state.get("first_failure_at")) is None)
                ):
                    raise ValueError("Invalid host recovery record")
        return payload or {"schema_version": 1, "hosts": {}}
    except (OSError, ValueError):
        return {"schema_version": 1, "hosts": {}, "state_error": "Computer recovery state is unreadable; review it before reconnecting storage."}


def process_host_recovery_once(
        config: MediaforceConfig,
        stop_event: threading.Event | None = None,
) -> None:
    # One elected background owner writes this snapshot. Connection locks are per computer.
    snapshot = host_recovery_snapshot(config)
    hosts = snapshot["hosts"]
    configured_keys = {str(host.get("host") or "") for host in config.remote_hosts}
    snapshot["hosts"] = hosts = {key: value for key, value in hosts.items() if key in configured_keys}
    for host in config.remote_hosts:
        if stop_event is not None and stop_event.is_set():
            break
        key = str(host.get("host") or "")
        if not key or host_targets_current_machine(host):
            continue
        with controller_mount_lock(config.paths.runtime_settings_path, host_key=key) as acquired:
            if not acquired:
                continue
            previous = hosts.get(key, {})
            now = datetime.now(timezone.utc)
            retry_at = _parse_time(previous.get("next_retry_at"))
            if retry_at and timedelta(0) < retry_at - now <= timedelta(seconds=_MAX_RETRY_SECONDS):
                continue
            status = probe_host_status(config, host)
            publish_host_status(config, status)
            count = min(int(previous.get("failure_count") or 0) + 1, 1000)
            state = {
                "label": status.label,
                "status": "ready" if status.available else "retrying",
                "reason": None if status.available else status.message,
                "first_failure_at": None if status.available else previous.get("first_failure_at") or now.isoformat(),
                "last_check_at": now.isoformat(),
                "last_attempt_at": previous.get("last_attempt_at"),
                "failure_count": 0 if status.available else count,
                "next_retry_at": (now + timedelta(seconds=min(
                    _INITIAL_RETRY_SECONDS * 2 ** min(count - 1, 4), _MAX_RETRY_SECONDS,
                ))).isoformat(),
                "mount_uncertain": previous.get("mount_uncertain", False),
                "retry_paused": False,
            }
            if status.available:
                state["mount_uncertain"] = False
            elif state["mount_uncertain"] or snapshot.get("state_error"):
                state["status"] = "action_required"
                state["reason"] = snapshot.get("state_error") or (
                    previous.get("reason") if previous.get("status") == "action_required" else None
                ) or "An earlier storage request has no confirmed result. Reconnect storage on this computer, then use Prepare."
            elif remote_mount_recovery_supported(config, host, status) and previous.get("retry_paused"):
                # Only someone at that computer can fix this; keep asking, and keep checking often so
                # the warning clears soon after Prepare or a manual connection.
                state.update(
                    status="action_required", reason=previous.get("reason"), retry_paused=True,
                    next_retry_at=(now + timedelta(seconds=_INITIAL_RETRY_SECONDS)).isoformat(),
                )
            elif remote_mount_recovery_supported(config, host, status):
                # Persist before SSH: transport loss can leave a remote mount request running.
                state["mount_uncertain"] = True
                state["last_attempt_at"] = now.isoformat()
                hosts[key] = state
                save_runtime_settings(host_recovery_path(config), snapshot)
                result = recover_remote_host_mounts_no_ui(config, host, status)
                state["mount_uncertain"] = result.uncertain
                if result.uncertain:
                    state["status"] = "action_required"
                    state["reason"] = "Storage could not be connected safely. Check this computer's mounted shares, then use Prepare."
                elif result.action:
                    state.update(status="action_required", reason=result.action, retry_paused=not result.retry)
                    if not result.retry:
                        state["next_retry_at"] = (now + timedelta(seconds=_INITIAL_RETRY_SECONDS)).isoformat()
                else:
                    refreshed = probe_host_status(config, host)
                    publish_host_status(config, refreshed)
                    if refreshed.available:
                        state.update(status="ready", reason=None, first_failure_at=None, failure_count=0)
            hosts[key] = state
            save_runtime_settings(host_recovery_path(config), snapshot)
    remote_keys = {
        str(host.get("host")) for host in config.remote_hosts
        if host.get("host") and not host_targets_current_machine(host)
    }
    if snapshot.get("state_error") and all(
        hosts.get(key, {}).get("status") == "ready" for key in remote_keys
    ):
        snapshot.pop("state_error")
        save_runtime_settings(host_recovery_path(config), snapshot)


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo else None
