"""One owner decision per show for files whose motion pattern was fully measured but stays ambiguous.

Measuring such a file again gives the same answer, so it waits for a judgment instead of analysis. The
owner answers once per show: yes encodes each eligible file as-is, with no deinterlacing or pulldown
removal, and the usual quality check still applies. Files with more than a trace of interlaced-looking
frames are not covered by that yes; they stay listed on their own.

An accepted file joins production on its own: one a production run already held keeps that hold, and one
it never held is held under the show's latest run that covers it, so the held-files sweep queues it
without the owner queueing the show again.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_items, production_holds
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, accept_cadence_as_is, cadence_as_is_eligible, \
    cadence_measurement_complete, reclassify_cadence_summary
from mediaforce.library.evidence_state import sync_library_item_evidence_state
from mediaforce.encoding.encode_queue import list_encode_runs_for_prefix
from mediaforce.library.media_scopes import path_matches_scope, resolve_media_scope, scope_rel_path_filter
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.web.runtime.left_out_files import LeftOutFile
from mediaforce.web.runtime.production_holds import HOLD_WAITING, MODE_FOLDER, MODE_OLDER_SEASONS, MODE_SEASON_OVERRIDE, \
    record_holds

ACCEPTED_AS_IS_EVENT = "cadence_accepted_as_is"


@dataclass(frozen=True, slots=True)
class AmbiguousMotionFiles:
    eligible: tuple[Mapping[str, Any], ...]
    partly_interlaced: tuple[Mapping[str, Any], ...]

    def to_payload(self) -> dict[str, Any] | None:
        if not self.eligible and not self.partly_interlaced:
            return None
        return {
            "eligible_count": len(self.eligible),
            "eligible_files": [str(row["rel_path"]) for row in self.eligible],
            "partly_interlaced_count": len(self.partly_interlaced),
            "partly_interlaced_files": [str(row["rel_path"]) for row in self.partly_interlaced],
        }


def ambiguous_motion_files(
        connection: DBClient,
        prefix: str,
        *,
        library_types: Mapping[str, str],
) -> AmbiguousMotionFiles:
    """Files the current classifier leaves ambiguous after full measurement, read from their stored summaries.

    Stored evidence-state rows are refreshed only when something re-projects them, so the decision is
    offered from the measurements themselves rather than waiting for that refresh.
    """
    scope = resolve_media_scope(connection, prefix, library_types=library_types)
    rows = connection.execute(
        select(library_items)
        .where(
            library_items.c.cadence_summary_json.is_not(None),
            library_items.c.status != "missing",
            scope_rel_path_filter(library_items.c.rel_path, scope),
        )
        .order_by(library_items.c.rel_path)
    ).mappings().fetchall()
    eligible: list[Mapping[str, Any]] = []
    partly_interlaced: list[Mapping[str, Any]] = []
    for row in rows:
        summary = _summary(row)
        decision = object_dict(reclassify_cadence_summary(summary).get("decision"))
        if decision.get("classification") != "unknown" or decision.get("status") == "resolved":
            continue
        if cadence_as_is_eligible(summary):
            eligible.append(row)
        elif cadence_measurement_complete(object_dict(summary.get("analysis"))):
            partly_interlaced.append(row)
    return AmbiguousMotionFiles(tuple(eligible), tuple(partly_interlaced))


def accept_ambiguous_motion_action(
        config: MediaforceConfig,
        prefix: str,
        *,
        now_iso: str,
        current_approval: Callable[[str], str | None],
) -> dict[str, Any]:
    """Accept every eligible ambiguous file in one show as-is, and list the files the yes does not cover.

    ``current_approval`` names the production approval a queue scope has now, or None when it has none.
    """
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
        if scope.kind != "tv_series":
            raise HTTPException(status_code=400, detail="Decide ambiguous motion once for a whole show.")
        files = ambiguous_motion_files(connection, prefix, library_types=config.library_type_map)
        for row in files.eligible:
            accepted = accept_cadence_as_is(
                _summary(row),
                source_fingerprint=str(row["fingerprint"] or ""),
                accepted_at=now_iso,
            )
            summary_json = json.dumps(accepted, separators=(",", ":"), sort_keys=True)
            connection.execute(
                update(library_items)
                .where(library_items.c.id == int(row["id"]))
                .values(cadence_summary_json=summary_json, updated_at=now_iso)
            )
            sync_library_item_evidence_state(
                connection,
                {**row, "cadence_summary_json": summary_json},
                CADENCE_EVIDENCE_KIND,
                updated_at=now_iso,
            )
            connection.execute(
                item_events.insert().values(
                    library_item_id=int(row["id"]),
                    created_at=now_iso,
                    event_type=ACCEPTED_AS_IS_EVENT,
                    details_json=json.dumps({"show_prefix": prefix, "source_fingerprint": row["fingerprint"]}),
                )
            )
        next_queue_count = _hold_for_production(
            connection, prefix, files.eligible, current_approval=current_approval, now_iso=now_iso,
        )
    accepted_count = len(files.eligible)
    left_count = len(files.partly_interlaced)
    joining_count = accepted_count - next_queue_count
    message = (
        f"Encoding {accepted_count} {'episode' if accepted_count == 1 else 'episodes'} with an unclear motion "
        "pattern as-is."
        if accepted_count
        else "No files were waiting for this decision."
    )
    if joining_count:
        message += f" {_count_phrase(joining_count, accepted_count)} production once nothing else is encoding in the show."
    if next_queue_count:
        message += (
            f" {_count_phrase(next_queue_count, accepted_count)} production the next time you queue the show, "
            f"because no approved run covers {'it' if next_queue_count == 1 else 'them'} yet."
        )
    if left_count:
        message += (
            f" {left_count} {'episode looks' if left_count == 1 else 'episodes look'} partly interlaced, "
            f"so {'it stays' if left_count == 1 else 'they stay'} original and listed on the show page."
        )
    return {
        "ok": True,
        "accepted_count": accepted_count,
        "partly_interlaced_files": [str(row["rel_path"]) for row in files.partly_interlaced],
        "message": message,
    }


def _hold_for_production(
        connection: DBClient,
        show_prefix: str,
        rows: tuple[Mapping[str, Any], ...],
        *,
        current_approval: Callable[[str], str | None],
        now_iso: str,
) -> int:
    """Hold each accepted file no run holds yet under the newest approved run covering it; return how many were not.

    A run covers a file when the file is inside its scope and, for an older-seasons run, in a season that run
    included. The hold carries the run's mode and the approval its scope has now, so the sweep only queues
    the file the same way under that approval.
    """
    held_ids = set(
        connection.execute(
            select(production_holds.c.library_item_id).where(production_holds.c.status == HOLD_WAITING)
        ).scalars()
    )
    unheld = [row for row in rows if int(row["id"]) not in held_ids]
    if not unheld:
        return 0
    runs = [
        run
        for run in (_ProductionRun.from_job(job, current_approval) for job in list_encode_runs_for_prefix(
            connection, show_prefix,
        ))
        if run is not None
    ]
    not_covered = 0
    for row in unheld:
        run = next((run for run in runs if run.covers(str(row["rel_path"]))), None)
        if run is None:
            not_covered += 1
            continue
        record_holds(
            connection,
            prefix=run.prefix,
            mode=run.mode,
            approval=run.approval,
            files=[LeftOutFile(int(row["id"]), str(row["rel_path"]), "cadence_unresolved", "")],
            now_iso=now_iso,
        )
    return not_covered


@dataclass(frozen=True, slots=True)
class _ProductionRun:
    prefix: str
    mode: str
    approval: str
    included_seasons: tuple[str, ...] | None

    @classmethod
    def from_job(cls, job: Mapping[str, Any], current_approval: Callable[[str], str | None]) -> "_ProductionRun | None":
        prefix = str(job.get("prefix") or "").strip()
        approval = current_approval(prefix) if prefix else None
        if approval is None:
            return None
        manifest = _manifest(job)
        selection = object_dict(manifest.get("selection"))
        older_seasons = object_dict(selection.get("lifecycle_override"))
        mode = str(selection.get("queue_mode") or _legacy_queue_mode(manifest, older_seasons=bool(older_seasons)))
        included = tuple(str(value) for value in object_list(older_seasons.get("included_season_prefixes")))
        return cls(prefix, mode, approval, included if older_seasons else None)

    def covers(self, rel_path: str) -> bool:
        if not path_matches_scope(rel_path, self.prefix):
            return False
        return self.included_seasons is None or any(path_matches_scope(rel_path, season) for season in self.included_seasons)


def _legacy_queue_mode(manifest: Mapping[str, Any], *, older_seasons: bool) -> str:
    """The mode of a run queued before its mode was recorded, read from what it selected."""
    if older_seasons:
        return MODE_OLDER_SEASONS
    season_override = any(
        provenance.get("override_applied") is True and provenance.get("manual_override") is True
        for provenance in (
            object_dict(object_dict(item).get("selection_provenance")) for item in object_list(manifest.get("items"))
        )
    )
    return MODE_SEASON_OVERRIDE if season_override else MODE_FOLDER


def _manifest(job: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return object_dict(json.loads(Path(str(job.get("manifest_path") or "")).read_text()))
    except (OSError, json.JSONDecodeError):
        return {}


def _count_phrase(count: int, total: int) -> str:
    if count == total:
        return "It joins" if count == 1 else "They join"
    return f"{count} {'joins' if count == 1 else 'join'}"


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return object_dict(json.loads(str(row.get("cadence_summary_json") or "{}")))
    except json.JSONDecodeError:
        return {}
