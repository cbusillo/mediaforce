"""How the queue's motion-pattern gate splits files, read the same way by queueing and by the pages.

`cadence_safety_partition` is the gate itself; `queue_motion_holds` reads it without changing
anything, for pages, library cards and the movie library to count and offer only what queueing takes.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND, cadence_awaits_as_is_decision
from mediaforce.library.candidate_selection import CandidateDecision, MotionHolds
from mediaforce.library.evidence_state import EVIDENCE_STATE_ANALYSIS_REQUIRED, \
    EVIDENCE_STATE_CLASSIFICATION_REQUIRED, EVIDENCE_STATE_CURRENT, project_library_item_evidence_state, \
    sync_library_item_evidence_state
from mediaforce.library.item_presence import library_item_is_missing


@dataclass(frozen=True, slots=True)
class CadenceSafetyPartition:
    cleared_item_ids: frozenset[int]
    blocked_item_ids: frozenset[int]
    evidence_required_item_ids: frozenset[int]
    # The evidence-required files a check can be queued for; a folder queue holds these back for it.
    check_needed_item_ids: frozenset[int] = frozenset()

    @property
    def excluded_item_ids(self) -> frozenset[int]:
        return self.blocked_item_ids | self.evidence_required_item_ids

    @property
    def queueable_item_ids(self) -> frozenset[int]:
        """What a folder queue takes now: the old folder gate passed files no check can be queued for."""
        return self.cleared_item_ids | (self.evidence_required_item_ids - self.check_needed_item_ids)


# Evidence states a motion-pattern check can be queued for.
_CHECKABLE_STATES = (EVIDENCE_STATE_ANALYSIS_REQUIRED, EVIDENCE_STATE_CLASSIFICATION_REQUIRED)


def cadence_safety_partition(
        connection: DBClient,
        *,
        library_item_ids: Sequence[int],
        synchronize: bool,
) -> CadenceSafetyPartition:
    normalized_ids = sorted({int(item_id) for item_id in library_item_ids if int(item_id) > 0})
    if not normalized_ids:
        return CadenceSafetyPartition(frozenset(), frozenset(), frozenset())
    items = connection.execute(
        select(library_items).where(library_items.c.id.in_(normalized_ids))
    ).mappings().fetchall()
    if synchronize:
        for item in items:
            sync_library_item_evidence_state(
                connection,
                item,
                CADENCE_EVIDENCE_KIND,
            )
    # Whole stored rows: their summary, source and policy identity decide whether retained
    # measurements still belong to the item's current source and policy.
    rows = connection.execute(
        select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id.in_(normalized_ids),
            library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
        )
    ).mappings().fetchall()
    state_by_item_id: dict[int, Any] = {
        int(row["library_item_id"]): row
        for row in rows
    }
    if not synchronize:
        # The same projection syncing would store, so a page reads what the synchronized queue decides.
        for item in items:
            item_id = int(item["id"])
            projection = project_library_item_evidence_state(
                item,
                CADENCE_EVIDENCE_KIND,
                previous_state=state_by_item_id.get(item_id),
            )
            state_by_item_id[item_id] = {
                "state": projection.state,
                "decision_status": projection.decision_status,
            }
    present_item_ids = {int(item["id"]) for item in items if not library_item_is_missing(item)}
    cleared_item_ids: set[int] = set()
    blocked_item_ids: set[int] = set()
    evidence_required_item_ids: set[int] = set()
    check_needed_item_ids: set[int] = set()
    for item_id in normalized_ids:
        row = state_by_item_id.get(item_id)
        state = str(row.get("state") or "") if row is not None else ""
        decision_status = str(row.get("decision_status") or "") if row is not None else ""
        if state == EVIDENCE_STATE_CURRENT and decision_status == "resolved":
            cleared_item_ids.add(item_id)
        elif state == EVIDENCE_STATE_CURRENT:
            blocked_item_ids.add(item_id)
        else:
            evidence_required_item_ids.add(item_id)
            if state in _CHECKABLE_STATES and item_id in present_item_ids:
                check_needed_item_ids.add(item_id)
    return CadenceSafetyPartition(
        cleared_item_ids=frozenset(cleared_item_ids),
        blocked_item_ids=frozenset(blocked_item_ids),
        evidence_required_item_ids=frozenset(evidence_required_item_ids),
        check_needed_item_ids=frozenset(check_needed_item_ids),
    )


def queue_motion_holds(connection: DBClient, decisions: Sequence[CandidateDecision]) -> MotionHolds:
    """How the queue's motion-pattern gate splits these encode candidates, read without changing anything.

    Pages and library cards use this so they count and offer only what queueing takes; it is the same
    split `cadence_queue_partition` makes, without syncing evidence rows or queueing checks. Like that
    gate it covers every media domain and every candidate, including files a lifecycle hold protects,
    since an override lifts the hold but never the motion-pattern gate.
    """
    partition = cadence_safety_partition(
        connection,
        library_item_ids=[decision.item_id for decision in decisions if decision.workflow_lane == "encode"],
        synchronize=False,
    )
    as_is: set[int] = set()
    if partition.blocked_item_ids:
        rows = connection.execute(
            select(library_items.c.id, library_items.c.cadence_summary_json).where(
                library_items.c.id.in_(sorted(partition.blocked_item_ids)),
            )
        ).fetchall()
        as_is = {int(item_id) for item_id, summary_json in rows if cadence_awaits_as_is_decision(_summary(summary_json))}
    return MotionHolds(
        held=partition.blocked_item_ids,
        check_needed=partition.check_needed_item_ids,
        unchecked=partition.evidence_required_item_ids - partition.check_needed_item_ids,
        as_is=frozenset(as_is),
    )


def _summary(summary_json: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(summary_json or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}
