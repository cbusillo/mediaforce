"""Persistent, incident-deduplicated operator warnings; no external destination assumed."""
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.web.runtime.controller_storage_recovery import controller_storage_recovery_snapshot
from mediaforce.web.runtime.host_recovery import host_recovery_snapshot

LOGGER = logging.getLogger(__name__)
_HOST_OUTAGE_GRACE = timedelta(minutes=5)


def recovery_alerts(config: MediaforceConfig) -> list[dict[str, str]]:
    storage = controller_storage_recovery_snapshot(config)
    hosts = host_recovery_snapshot(config)
    alerts: list[dict[str, str]] = []
    for source, payload in (("storage", storage), ("hosts", hosts)):
        if payload.get("state_error"):
            alerts.append({"id": f"{source}:state", "title": "Storage recovery needs attention", "detail": payload["state_error"]})
    for mount in storage["mounts"]:
        if mount["status"] == "ready":
            continue
        retry = (
            "Mediaforce will keep checking. Reconnect storage with Finder or Prepare."
            if mount["status"] == "action_required"
            else "Mediaforce is retrying automatically."
        )
        alerts.append({
            "id": f"storage:{mount['mount_point']}:{mount['first_failure_at']}",
            "title": f"Controller storage unavailable: {mount['mount_point']}",
            "detail": f"{mount['reason'] or 'Storage is not ready.'} {retry}",
        })
    now = datetime.now(timezone.utc)
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
        alerts.append({
            "id": f"host:{key}:{host['first_failure_at']}",
            "title": f"{host['label']} is unavailable",
            "detail": f"{host['reason']} {retry}",
        })
    return alerts


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
            LOGGER.warning("%s. %s", alert["title"], alert["detail"])
    for alert in previous.get("alerts", []):
        if alert["id"] not in current_ids:
            LOGGER.info("Recovered: %s", alert["title"])
    if alerts != previous.get("alerts"):
        save_runtime_settings(path, {"alerts": alerts})
