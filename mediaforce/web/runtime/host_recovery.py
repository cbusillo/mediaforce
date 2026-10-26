"""Queue-independent host checks and bounded no-dialog storage recovery."""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, update_runtime_settings
from mediaforce.hosts.config import host_targets_current_machine
from mediaforce.hosts.controller_mount import controller_mount_lock
from mediaforce.remote import (
    probe_host_status, recover_remote_host_mounts_no_ui,
    remote_mount_recovery_supported,
)
from mediaforce.web.runtime.host_status import publish_host_status

_INITIAL_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 300
DESKTOP_ABSENCE_GRACE = timedelta(hours=12)
_UNREADABLE_STATE = "Computer recovery state is unreadable; review it before reconnecting storage."


def host_recovery_path(config: MediaforceConfig) -> Path:
    return config.paths.runtime_settings_path.with_name("host-recovery.json")


def host_recovery_snapshot(config: MediaforceConfig) -> dict[str, Any]:
    try:
        return _validated_snapshot(load_runtime_settings(host_recovery_path(config)))
    except (OSError, ValueError):
        return {"schema_version": 1, "hosts": {}, "state_error": _UNREADABLE_STATE}


def _validated_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
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


def process_host_recovery_once(
        config: MediaforceConfig,
        stop_event: threading.Event | None = None,
) -> None:
    # One elected background owner runs these passes; Retry connection runs one more for a single
    # computer. Each computer's record is only written under that computer's connection lock.
    state_error = host_recovery_snapshot(config).get("state_error")
    _update_snapshot(config, lambda _hosts: None)
    for host in config.remote_hosts:
        if stop_event is not None and stop_event.is_set():
            break
        key = str(host.get("host") or "")
        if not key or host_targets_current_machine(host):
            continue
        with controller_mount_lock(config.paths.runtime_settings_path, host_key=key) as acquired:
            if not acquired:
                continue
            previous = host_recovery_snapshot(config)["hosts"].get(key, {})
            retry_at = _parse_time(previous.get("next_retry_at"))
            if retry_at and timedelta(0) < retry_at - datetime.now(timezone.utc) <= timedelta(seconds=_MAX_RETRY_SECONDS):
                continue
            _recovery_pass(config, host, key, previous, state_error=state_error, requested=False)
    if state_error:
        remote_keys = {
            str(host.get("host")) for host in config.remote_hosts
            if host.get("host") and not host_targets_current_machine(host)
        }
        hosts = host_recovery_snapshot(config)["hosts"]
        if all(hosts.get(key, {}).get("status") == "ready" for key in remote_keys):
            _update_snapshot(config, lambda _hosts: None, clear_state_error=True)


def retry_host_connection(config: MediaforceConfig, host_key: str) -> tuple[bool, str]:
    """Run one recovery pass for one computer now: the same no-dialog connection the background
    worker uses, without waiting out its retry delay. Refuses while another check or connection
    for that computer is running."""
    host = next((
        host for host in config.remote_hosts
        if host_key and str(host.get("host") or "") == host_key and not host_targets_current_machine(host)
    ), None)
    if host is None:
        return False, "That computer is no longer set up for Mediaforce."
    label = str(host.get("label") or host_key).strip() or host_key
    state_error = host_recovery_snapshot(config).get("state_error")
    with controller_mount_lock(config.paths.runtime_settings_path, host_key=host_key) as acquired:
        if not acquired:
            return False, f"{label} is already being checked or connected. Try again when that finishes."
        previous = host_recovery_snapshot(config)["hosts"].get(host_key, {})
        state = _recovery_pass(config, host, host_key, previous, state_error=state_error, requested=True)
    if state["status"] == "ready":
        return True, f"{state['label']} is connected."
    reason = (state["reason"] or "").strip()
    if reason and not reason.endswith((".", "!", "?")):
        reason += "."
    return False, f"{state['label']} is still unavailable. {reason}".strip()


def _recovery_pass(
        config: MediaforceConfig,
        host: dict[str, Any],
        key: str,
        previous: dict[str, Any],
        *,
        state_error: str | None,
        requested: bool,
) -> dict[str, Any]:
    """Check one computer and, when its storage is missing, try one no-dialog connection.

    The caller holds that computer's connection lock. A requested pass (Retry connection) also
    tries again after a pause for someone at that computer: the request says they have done it.
    """
    now = datetime.now(timezone.utc)
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
    state["computer_model"] = status.computer_model or previous.get("computer_model")
    state["network_absent"] = status.check_failure == "unreachable" or (
        status.check_failure == "not_found" and bool(state["computer_model"])
    )
    state["absent_since"] = (
        previous.get("absent_since") or now.isoformat() if state["network_absent"] else None
    )
    if previous.get("status") == "action_required" and not status.available and not state["network_absent"]:
        state.update(status="action_required", reason=previous.get("reason") or status.message)
    if status.available:
        state["mount_uncertain"] = False
    elif state["network_absent"]:
        _store_host_state(config, key, state)
        return state
    elif state_error:
        state.update(status="action_required", reason=state_error)
    elif remote_mount_recovery_supported(config, host, status) and previous.get("retry_paused") and not requested:
        # Only someone at that computer can fix this; keep asking, and keep checking often so
        # the warning clears soon after Retry connection, Set up computer or a manual connection.
        state.update(
            status="action_required", reason=previous.get("reason"), retry_paused=True,
            next_retry_at=(now + timedelta(seconds=_INITIAL_RETRY_SECONDS)).isoformat(),
        )
    elif remote_mount_recovery_supported(config, host, status):
        # Persist before SSH: transport loss can leave a remote mount request running.
        state["mount_uncertain"] = True
        state["last_attempt_at"] = now.isoformat()
        _store_host_state(config, key, state)
        result = recover_remote_host_mounts_no_ui(config, host, status)
        state["mount_uncertain"] = result.uncertain
        if result.uncertain:
            state["status"] = "action_required" if previous.get("mount_uncertain") else "retrying"
            state["reason"] = result.action or "This computer did not confirm its storage connection. Check its shared storage; Mediaforce keeps trying."
        elif result.action:
            state.update(status="action_required", reason=result.action, retry_paused=not result.retry)
            if not result.retry:
                state["next_retry_at"] = (now + timedelta(seconds=_INITIAL_RETRY_SECONDS)).isoformat()
        else:
            refreshed = probe_host_status(config, host)
            publish_host_status(config, refreshed)
            state.update(status="retrying", reason=refreshed.message)
            if refreshed.available:
                state.update(status="ready", reason=None, first_failure_at=None, failure_count=0)
    if status.check_failure and not state_error and (
        state["mount_uncertain"] or previous.get("status") == "action_required"
        or (count >= 2 and status.check_failure in {"check_failed", "unreadable_reply"})
    ):
        state.update(status="action_required", reason=status.message)
    _store_host_state(config, key, state)
    return state


def _store_host_state(config: MediaforceConfig, key: str, state: dict[str, Any]) -> None:
    def store(hosts: dict[str, Any]) -> None:
        hosts[key] = state

    _update_snapshot(config, store)


def _update_snapshot(
        config: MediaforceConfig,
        change: Callable[[dict[str, Any]], None],
        *,
        clear_state_error: bool = False,
) -> None:
    """Change the saved snapshot in place under the file lock, so passes for different computers
    never write back each other's stale records. Unconfigured computers are dropped, and an
    unreadable file is replaced by one that keeps saying it was unreadable."""
    configured_keys = {str(host.get("host") or "") for host in config.remote_hosts}

    def update(current: dict[str, Any]) -> dict[str, Any]:
        try:
            snapshot = _validated_snapshot(current)
        except ValueError:
            snapshot = {"schema_version": 1, "hosts": {}, "state_error": _UNREADABLE_STATE}
        snapshot["hosts"] = {key: value for key, value in snapshot["hosts"].items() if key in configured_keys}
        change(snapshot["hosts"])
        if clear_state_error:
            snapshot.pop("state_error", None)
        return snapshot

    update_runtime_settings(host_recovery_path(config), update)


def host_absence_state(record: dict[str, Any], now: datetime | None = None) -> str | None:
    if not record.get("network_absent"):
        return None
    if "macbook" in str(record.get("computer_model") or "").lower():
        return "away"
    since = _parse_time(record.get("absent_since"))
    if since is not None and (now or datetime.now(timezone.utc)) - since >= DESKTOP_ABSENCE_GRACE:
        return "unreachable_long"
    return "reconnecting"


def _parse_time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo else None
