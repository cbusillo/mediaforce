"""One owner decision per show for files whose motion pattern was fully measured but stays ambiguous.

Measuring such a file again gives the same answer, so it waits for a judgment instead of analysis. The
owner answers once per show: yes encodes each eligible file as-is, with no deinterlacing or pulldown
removal, and the usual quality check still applies. Files with more than a trace of interlaced-looking
frames are not covered by that yes; they stay listed on their own.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, open_db
from mediaforce.core.db_tables import item_events, library_item_evidence_state, library_items
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, accept_cadence_as_is, cadence_as_is_eligible, \
    cadence_measurement_complete
from mediaforce.library.evidence_state import EVIDENCE_STATE_CURRENT, sync_library_item_evidence_state
from mediaforce.library.media_scopes import resolve_media_scope, scope_rel_path_filter
from mediaforce.core.type_defs import object_dict

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
    scope = resolve_media_scope(connection, prefix, library_types=library_types)
    rows = connection.execute(
        select(library_items)
        .select_from(
            library_items.join(
                library_item_evidence_state,
                library_item_evidence_state.c.library_item_id == library_items.c.id,
            )
        )
        .where(
            library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
            library_item_evidence_state.c.state == EVIDENCE_STATE_CURRENT,
            library_item_evidence_state.c.decision_status == "blocked",
            library_items.c.status != "missing",
            scope_rel_path_filter(library_items.c.rel_path, scope),
        )
        .order_by(library_items.c.rel_path)
    ).mappings().fetchall()
    eligible: list[Mapping[str, Any]] = []
    partly_interlaced: list[Mapping[str, Any]] = []
    for row in rows:
        summary = _summary(row)
        if str(object_dict(summary.get("decision")).get("classification") or "") != "unknown":
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
) -> dict[str, Any]:
    """Accept every eligible ambiguous file in one show as-is, and list the files the yes does not cover."""
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
    accepted_count = len(files.eligible)
    left_count = len(files.partly_interlaced)
    message = (
        f"Encoding {accepted_count} {'episode' if accepted_count == 1 else 'episodes'} with an unclear motion "
        f"pattern as-is. {'It joins' if accepted_count == 1 else 'They join'} production once nothing else is "
        "encoding in the show."
        if accepted_count
        else "No files were waiting for this decision."
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


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return object_dict(json.loads(str(row.get("cadence_summary_json") or "{}")))
    except json.JSONDecodeError:
        return {}
