"""Which encode computer measures a file, so evidence work never runs on the controller once one is set up."""

from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import object_list
from mediaforce.encoding.helpers import resolve_item_source_path
from mediaforce.hosts.config import _host_priority, configured_remote_host_execution_mode, host_media_access_for_host
from mediaforce.hosts.types import FFMPEG_MISSING_ISSUE
from mediaforce.remote import collect_host_statuses

EVIDENCE_HOST_WAIT_REASON = "Waiting for an encode computer to measure this file."
_ENCODE_CAPABILITY = "encode_queue"


def remote_evidence_hosts(config: MediaforceConfig) -> list[dict[str, Any]]:
    """Encode computers that read the library from their own mount and are reached over SSH."""
    return [
        host
        for host in config.remote_hosts
        if isinstance(host, dict)
        and host_media_access_for_host(host) == "mounted"
        and configured_remote_host_execution_mode(host) == "ssh"
    ]


def collect_evidence_host_rows(config: MediaforceConfig) -> list[dict[str, Any]]:
    return [asdict(status) for status in collect_host_statuses(config)]


def select_evidence_host(
        config: MediaforceConfig,
        host_rows: list[dict[str, Any]],
        *,
        media_root: str,
) -> dict[str, Any] | None:
    """The highest-priority encode computer that may take encode work now and can read this library."""
    rows_by_key = {
        key: row
        for row in host_rows
        for key in (str(row.get("key") or "").strip(), str(row.get("label") or "").strip())
        if key
    }
    candidates: list[dict[str, Any]] = []
    for host in remote_evidence_hosts(config):
        row = rows_by_key.get(str(host.get("host") or "").strip()) or rows_by_key.get(
            str(host.get("label") or "").strip()
        )
        if row is None or not _host_row_eligible(row):
            continue
        if not _host_allows_library(host, media_root):
            continue
        if media_root not in config.source_root_map_for_host(host):
            continue
        candidates.append(host)
    if not candidates:
        return None
    return sorted(candidates, key=lambda host: (-_host_priority(host), str(host.get("label") or "")))[0]


def evidence_source_path_on_host(
        config: MediaforceConfig,
        host: dict[str, Any],
        *,
        source_path: str,
        media_root: str,
        rel_path: str,
) -> Path:
    return resolve_item_source_path(
        config,
        {"source_path": source_path, "media_root": media_root, "rel_path": rel_path},
        host=host,
        host_media_access_for_host=host_media_access_for_host,
    )


def _host_row_eligible(row: Mapping[str, Any]) -> bool:
    capabilities = {str(capability).strip().lower() for capability in object_list(row.get("capabilities"))}
    issues = {str(issue) for issue in [*object_list(row.get("issues")), *object_list(row.get("probe_issues"))]}
    return (
        bool(row.get("available"))
        and _ENCODE_CAPABILITY in capabilities
        and FFMPEG_MISSING_ISSUE not in issues
        and bool(row.get("schedule_open", True))
    )


def _host_allows_library(host: Mapping[str, Any], media_root: str) -> bool:
    allowed_libraries = host.get("allowed_libraries")
    if not isinstance(allowed_libraries, list) or not allowed_libraries:
        return True
    allowed = {str(value or "").strip().lower() for value in allowed_libraries if str(value or "").strip()}
    return media_root.strip().lower() in allowed
