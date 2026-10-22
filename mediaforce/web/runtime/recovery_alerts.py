"""Persistent, incident-deduplicated operator warnings; no external destination assumed."""
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.web.runtime.controller_storage_recovery import controller_storage_recovery_snapshot
from mediaforce.web.runtime.host_recovery import host_recovery_snapshot
from mediaforce.web.runtime.host_status import checked_host_statuses

LOGGER = logging.getLogger(__name__)
_HOST_OUTAGE_GRACE = timedelta(minutes=5)


def recovery_alerts(config: MediaforceConfig) -> list[dict[str, str]]:
    """Current warnings, read from saved state only; reading them never checks or connects anything."""
    storage = controller_storage_recovery_snapshot(config)
    hosts = host_recovery_snapshot(config)
    alerts: list[dict[str, str]] = []
    for source, payload in (("storage", storage), ("hosts", hosts)):
        if payload.get("state_error"):
            whose = "this Mac's" if source == "storage" else "a computer's"
            alerts.append({
                "id": f"{source}:state",
                "title": "Storage reconnecting is paused",
                "detail": (
                    f"Mediaforce lost track of an earlier try to reconnect {whose} storage, so it will not try again "
                    "on its own. Check the storage in Finder, then use Retry connection."
                ),
                "technical": payload["state_error"],
            })
    for mount in storage["mounts"]:
        if mount["status"] == "ready":
            continue
        retry = (
            "Mediaforce will keep checking. Reconnect storage in Finder or with Set up computer."
            if mount["status"] == "action_required"
            else "Mediaforce is retrying automatically."
        )
        # The share and its mount point are for whoever fixes it, so they sit behind Details.
        alerts.append({
            "id": f"storage:{mount['mount_point']}:{mount['first_failure_at']}",
            "title": "This Mac can't see the media library",
            "detail": f"Some shared storage is not connected. {retry}",
            "technical": ": ".join(part for part in (mount["mount_point"], mount["reason"]) if part),
        })
    now = datetime.now(timezone.utc)
    configured_keys = {str(host.get("host") or "") for host in config.remote_hosts} - {""}
    checked = {status.key: status.available for status in checked_host_statuses(config)}
    for key, host in hosts["hosts"].items():
        if host["status"] == "ready":
            continue
        try:
            first = datetime.fromisoformat(host["first_failure_at"])
            prolonged = first.tzinfo is not None and now - first >= _HOST_OUTAGE_GRACE
        except (TypeError, ValueError):
            prolonged = True
        if host["status"] != "action_required" and not prolonged:
            continue
        retry = "Mediaforce will keep checking." if host["status"] == "action_required" else "Mediaforce is retrying automatically. Check this computer's network and storage if it stays unavailable."
        others = _other_computers_copy(configured_keys - {key}, checked)
        alerts.append({
            "id": f"host:{key}:{host['first_failure_at']}",
            "title": f"{host['label']} is unavailable",
            "detail": " ".join(part for part in (_sentence(host["reason"]), retry, others) if part),
            "host_key": key,
        })
    return alerts


def _sentence(text: str | None) -> str | None:
    text = (text or "").strip()
    return text if not text or text.endswith((".", "!", "?")) else f"{text}."


def _other_computers_copy(other_keys: set[str], checked: dict[str, bool]) -> str | None:
    """Say whether work goes on elsewhere, from the last checks the computer cards show."""
    if not other_keys:
        return "No other computer is set up, so work waits for this one."
    working = sum(1 for key in other_keys if checked.get(key))
    if not working:
        # Until every other computer has been checked, "none" might not be true.
        return "No other computer is working, so work waits for this one." if other_keys <= checked.keys() else None
    return f"{working} other {'computer is' if working == 1 else 'computers are'} still working."


def record_recovery_alerts(config: MediaforceConfig) -> None:
    path = config.paths.runtime_settings_path.with_name("recovery-alerts.json")
    try:
        previous: dict[str, Any] = load_runtime_settings(path)
    except (OSError, ValueError):
        previous = {}
    alerts = recovery_alerts(config)
    previous_ids = {alert["id"] for alert in previous.get("alerts", [])}
    current_ids = {alert["id"] for alert in alerts}
    for alert in alerts:
        if alert["id"] not in previous_ids:
            LOGGER.warning("%s. %s %s", alert["title"], alert["detail"], alert.get("technical", ""))
    for alert in previous.get("alerts", []):
        if alert["id"] not in current_ids:
            LOGGER.info("Recovered: %s", alert["title"])
    if alerts != previous.get("alerts"):
        save_runtime_settings(path, {"alerts": alerts})
