"""Kind approvals: one approval covering every TV season of a kind of content (#1047).

The Director decided on 2026-10-22 (#517) that one approval may cover every show that measures the same,
starting with darker and bright live action. Cartoons and 4K stay one-time choices made show by show.

A kind approval is a copy of a current folder approval the Director chose to extend, kept in the web state
directory's `kinds` folder, one file per kind. It governs a season only when no folder on the season's path
holds any saved sample (see production_approval), and the season's kind comes from its originals' traits
(library/content_kinds). Every file it covers still meets the per-file quality floor and size hold.
"""

import json
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import open_readonly_db
from mediaforce.core.type_defs import object_dict
from mediaforce.library.candidate_selection import season_identity
from mediaforce.library.content_kinds import season_grouping
from mediaforce.web.runtime.production_approval import EffectiveApproval, LoadCalibrationState, holds_approval, \
    production_approval_contract

APPROVABLE_KINDS = ("darker_live_action", "bright_live_action")
KIND_IS_ONE_TIME_CODE = "kind_is_one_time"
APPROVAL_NOT_CURRENT_CODE = "approval_not_current"
SIZE_GOAL_NOT_SCALABLE_CODE = "size_goal_not_scalable"
# A season's kind changes only when its originals' measurements do, so a page or queue pass can reuse it a while.
SEASON_KIND_SECONDS = 600.0

KindOf = Callable[[MediaforceConfig, str], str | None]


def kind_approval_file(config: MediaforceConfig, kind: str) -> Path:
    return config.paths.web_state_dir / "kinds" / f"{kind}.json"


def load_kind_approval(config: MediaforceConfig, kind: str) -> dict[str, Any] | None:
    path = kind_approval_file(config, kind)
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and payload.get("kind") == kind else None


def approved_kinds(config: MediaforceConfig) -> list[str]:
    return [kind for kind in APPROVABLE_KINDS if holds_approval(load_kind_approval(config, kind))]


def approve_kind(
        config: MediaforceConfig,
        kind: str,
        from_prefix: str,
        calibration: Mapping[str, Any] | None,
        *,
        approved_at: str,
) -> dict[str, Any]:
    """Extend a folder's current approval to every season of this kind; refusals say why with a code."""
    if kind not in APPROVABLE_KINDS:
        return {"ok": False, "code": KIND_IS_ONE_TIME_CODE}
    payload = object_dict(calibration)
    approval = EffectiveApproval(from_prefix, payload, production_approval_contract(payload))
    if not holds_approval(payload) or not approval.current or approval.contract is None:
        return {"ok": False, "code": APPROVAL_NOT_CURRENT_CODE}
    size_goal = object_dict(object_dict(approval.contract.get("operator_intent")).get("size_goal"))
    # A size per episode fits only that show's runtime; a size per 45 minutes carries to other shows.
    if size_goal.get("mode") != "normalized":
        return {"ok": False, "code": SIZE_GOAL_NOT_SCALABLE_CODE}
    saved = {**payload, "kind": kind, "from_prefix": from_prefix, "kind_approved_at": approved_at}
    path = kind_approval_file(config, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
        json.dump(saved, handle, indent=2, sort_keys=True)
    os.replace(handle.name, path)
    return {"ok": True, "kind": kind, "from_prefix": from_prefix}


def stop_kind_approval(config: MediaforceConfig, kind: str) -> bool:
    try:
        kind_approval_file(config, kind).unlink()
    except FileNotFoundError:
        return False
    return True


def season_prefix_of(config: MediaforceConfig, rel_path: str) -> str | None:
    """The TV season a file or season folder belongs to; None for a show, a movie or a file in a show folder."""
    path = PurePosixPath(str(rel_path or "").strip().strip("/"))
    if len(path.parts) < 3 or (len(path.parts) == 3 and path.suffix):
        return None
    identity = season_identity({"rel_path": path.as_posix()}, library_types=config.library_type_map)
    return identity.season_prefix if identity is not None else None


class SeasonKinds:
    """Each season's kind from the live database, kept for a while because pages ask for many at once."""

    def __init__(self, seconds: float = SEASON_KIND_SECONDS, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._kinds: dict[str, tuple[float, str]] = {}

    def __call__(self, config: MediaforceConfig, season_prefix: str) -> str | None:
        now = self._clock()
        with self._lock:
            cached = self._kinds.get(season_prefix)
        if cached is not None and now - cached[0] < self._seconds:
            return cached[1]
        if not config.paths.db_path.exists():
            return None
        with open_readonly_db(config.paths.db_path) as connection:
            kind = season_grouping(connection, season_prefix, library_types=config.library_type_map).kind
        with self._lock:
            self._kinds[season_prefix] = (now, kind)
        return kind


class KindAwareLoader:
    """A calibration loader that reads folders exactly as the one it wraps, and also finds kind approvals.

    Only the approval resolver asks for kind approvals, so nothing that saves a folder's own sample ever
    reads a kind's copy as that folder's.
    """

    def __init__(self, load_calibration_state: LoadCalibrationState, kind_of: KindOf) -> None:
        self._load = load_calibration_state
        self._kind_of = kind_of

    def __call__(self, config: MediaforceConfig, prefix: str) -> dict[str, Any] | None:
        return self._load(config, prefix)

    def kind_approval(self, config: MediaforceConfig, rel_path: str) -> EffectiveApproval | None:
        # No season's kind is read until the Director has approved a kind.
        approved = approved_kinds(config)
        season = season_prefix_of(config, rel_path) if approved else None
        if season is None:
            return None
        kind = self._kind_of(config, season)
        if kind not in approved:
            return None
        calibration = load_kind_approval(config, str(kind))
        if calibration is None:
            return None
        return EffectiveApproval(season, calibration, production_approval_contract(calibration), kind=kind)
