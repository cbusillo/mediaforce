from collections.abc import Sequence
from typing import Any

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_items
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND
from mediaforce.library.evidence_queue import EvidenceQueueConflict, queue_decision_evidence_work
from mediaforce.library.evidence_state import EVIDENCE_STATE_CURRENT, sync_library_item_evidence_state


def cadence_evidence_blocker(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        library_item_ids: Sequence[int],
        decision_label: str,
        work_reason: str,
) -> dict[str, Any] | None:
    normalized_ids = sorted({int(item_id) for item_id in library_item_ids if int(item_id) > 0})
    if not normalized_ids:
        return None
    items = connection.execute(
        select(library_items).where(library_items.c.id.in_(normalized_ids))
    ).mappings().fetchall()
    for item in items:
        sync_library_item_evidence_state(
            connection,
            item,
            CADENCE_EVIDENCE_KIND,
        )
    unresolved_count = len(
        connection.execute(
            select(library_item_evidence_state.c.library_item_id)
            .where(
                library_item_evidence_state.c.library_item_id.in_(normalized_ids),
                library_item_evidence_state.c.evidence_kind == CADENCE_EVIDENCE_KIND,
                library_item_evidence_state.c.state == EVIDENCE_STATE_CURRENT,
                library_item_evidence_state.c.decision_status != "resolved",
            )
        ).fetchall()
    )
    if unresolved_count:
        file_label = "file has" if unresolved_count == 1 else "files have"
        return {
            "ok": False,
            "code": "cadence_unresolved",
            "message": (
                f"{unresolved_count} selected {file_label} a measured motion pattern that Mediaforce cannot "
                f"convert safely on its own. Review that item in Activity before {decision_label}."
            ),
            "affected_item_count": unresolved_count,
            "next_route": "/ops",
            "next_action_label": "Open Activity",
        }
    scope_prefix = str(prefix or "").strip().strip("/")
    queue_groups: list[tuple[str, list[int]]]
    if scope_prefix:
        queue_groups = [(scope_prefix, normalized_ids)]
    else:
        item_ids_by_root: dict[str, list[int]] = {}
        for item in items:
            media_root = str(item.get("media_root") or "").strip()
            if media_root:
                item_ids_by_root.setdefault(media_root, []).append(int(item["id"]))
        queue_groups = sorted(item_ids_by_root.items())
    preparations: list[dict[str, Any]] = []
    try:
        for group_prefix, group_item_ids in queue_groups:
            preparations.append(queue_decision_evidence_work(
                connection,
                config,
                group_prefix,
                library_item_ids=group_item_ids,
                evidence_kind=CADENCE_EVIDENCE_KIND,
                work_reason=work_reason,
                manage_transaction=False,
            ))
    except EvidenceQueueConflict as exc:
        return {
            "ok": False,
            "code": "cadence_analysis_unavailable",
            "message": (
                f"Cadence safety evidence is required before {decision_label}, but {str(exc).rstrip('.').lower()}."
            ),
            "affected_item_count": len(normalized_ids),
            "next_route": "/ops",
            "next_action_label": "Open Activity",
        }
    required_count = sum(int(preparation.get("required_count") or 0) for preparation in preparations)
    if required_count == 0:
        return None
    terminal_failure_count = sum(
        int(preparation.get("terminal_failure_count") or 0)
        for preparation in preparations
    )
    work = next(
        (
            preparation.get("work")
            for preparation in reversed(preparations)
            if isinstance(preparation.get("work"), dict)
        ),
        {},
    )
    if terminal_failure_count:
        file_label = "file" if terminal_failure_count == 1 else "files"
        return {
            "ok": False,
            "code": "cadence_analysis_failed",
            "message": (
                f"Motion-pattern analysis previously failed for {terminal_failure_count} selected {file_label}. "
                f"Open Activity and use Prepare item to retry explicitly before {decision_label}."
            ),
            "affected_item_count": terminal_failure_count,
            "evidence_work": work,
            "next_route": "/ops",
            "next_action_label": "Open Activity",
        }
    work_status = str(work.get("status") or "")
    if bool(work.get("is_paused")):
        work_instruction = "The work is prepared in Activity. Select Start analysis, then try again."
    elif work_status == "running":
        work_instruction = "The analysis is already running in Activity. Try again after it finishes."
    else:
        work_instruction = "The work is queued in Activity. Try again after it finishes."
    file_label = "file" if required_count == 1 else "files"
    return {
        "ok": False,
        "code": "cadence_analysis_required",
        "message": (
            f"Motion-pattern safety evidence is needed for {required_count} selected {file_label}. "
            f"{work_instruction}"
        ),
        "affected_item_count": required_count,
        "evidence_work": work,
        "next_route": "/ops",
        "next_action_label": "Open Activity",
    }
