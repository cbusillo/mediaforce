from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.web.settings_runtime import runtime_library_signature, runtime_source_roots


def catalog_signature_file(config: MediaforceConfig) -> Path:
    config.paths.web_state_dir.mkdir(parents=True, exist_ok=True)
    return config.paths.web_state_dir / "full-catalog.signature.json"


def current_catalog_signature(config: MediaforceConfig) -> dict[str, Any]:
    return {
        "source_roots": {key: str(path) for key, path in config.scan_source_root_map.items()},
        "libraries": runtime_library_signature(config.raw),
    }


def load_catalog_signature(config: MediaforceConfig) -> dict[str, Any] | None:
    path = catalog_signature_file(config)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    source_roots = payload.get("source_roots")
    if not isinstance(source_roots, dict):
        return None
    libraries = payload.get("libraries")
    return {
        "source_roots": runtime_source_roots({"media": {"source_roots": source_roots}}),
        "libraries": runtime_library_signature(
            {"media": {"libraries": libraries if isinstance(libraries, list) else []}}
        ),
    }


def save_catalog_signature(config: MediaforceConfig) -> None:
    catalog_signature_file(config).write_text(json.dumps(current_catalog_signature(config), indent=2) + "\n")
