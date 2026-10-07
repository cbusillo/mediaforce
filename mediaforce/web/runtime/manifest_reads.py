"""Manifest readers for recovery checks; callers choose whether to reuse a read."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeAlias

from mediaforce.core.type_defs import object_dict

ManifestReader: TypeAlias = Callable[[Path], dict[str, Any] | None]


def read_manifest(path: Path) -> dict[str, Any] | None:
    """Return None for an unreadable record, distinct from a readable empty object."""
    try:
        return object_dict(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return None
