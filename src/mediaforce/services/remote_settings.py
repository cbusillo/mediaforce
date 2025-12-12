from __future__ import annotations

import json
import urllib.request
from typing import Optional

from mediaforce.config.settings import AppSettings, LibrarySettings


def load_remote_settings(url: str) -> Optional[AppSettings]:
    """Fetch settings JSON from master API and convert to AppSettings.

    Note: This intentionally only maps fields currently used by the worker/CLI
    orchestration (library roots + global_max_height). Other AppSettings fields
    are left at defaults and may be overridden by CLI flags.
    """

    try:
        with urllib.request.urlopen(url) as resp:
            payload = json.loads(resp.read().decode())
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    settings_payload = payload.get("settings") if "settings" in payload else payload
    if not isinstance(settings_payload, dict):
        return None

    libs_raw = settings_payload.get("libraries", [])
    libraries: list[LibrarySettings] = []
    for raw in libs_raw:
        try:
            libraries.append(
                LibrarySettings(
                    id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    media_type=str(raw.get("media_type") or ""),
                    mac_path=str(raw.get("mac_path") or ""),
                    linux_path=str(raw.get("linux_path") or ""),
                    watch=bool(raw.get("watch", True)),
                    max_height=(int(raw.get("max_height")) if raw.get("max_height") else None),
                    weight=float(raw.get("weight", 1.0)),
                )
            )
        except Exception:
            continue

    global_max_height = settings_payload.get("global_max_height")
    try:
        global_max_height = int(global_max_height) if global_max_height is not None else None
    except Exception:
        global_max_height = None

    if not libraries:
        return None

    return AppSettings(libraries=libraries, global_max_height=global_max_height)

