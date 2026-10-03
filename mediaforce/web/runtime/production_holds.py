"""Files a production run held back, remembered so they can join it once their evidence clears.

A queue action records a hold for each file it leaves out for motion-pattern evidence, together with
the run's mode and approval. A background sweep queues held files whose evidence has since cleared,
under that same approval, without touching the run's other jobs or waiting for them to finish.
When the approval has changed, nothing is queued and the hold says so.
"""

from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import production_holds
from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict
from mediaforce.web.runtime.decision_evidence import cadence_safety_partition
from mediaforce.web.runtime.left_out_files import LeftOutFile

# Left-out codes whose file can join production once its motion-pattern evidence clears.
HOLDABLE_CODES = frozenset({
    "cadence_unresolved",
    "cadence_analysis_required",
    "cadence_analysis_failed",
    "cadence_analysis_unavailable",
})
HOLD_WAITING = "waiting"
HOLD_APPROVAL_CHANGED = "approval_changed"
HOLD_REFUSED = "refused"

MODE_FOLDER = "folder"
MODE_SEASON_OVERRIDE = "season_override"
MODE_OLDER_SEASONS = "older_seasons"


def queue_mode(*, override_policy_holds: bool, override_older_seasons: bool) -> str:
    if override_older_seasons:
        return MODE_OLDER_SEASONS
    if override_policy_holds:
        return MODE_SEASON_OVERRIDE
    return MODE_FOLDER


def approval_identity(calibration: dict[str, Any], contract: dict[str, Any] | None) -> str:
    """A stable name for the approval a run was queued under; any re-approval changes it."""
    if contract is not None:
        return f"contract:{stable_json_hash(contract)}"
    return "calibration:" + stable_json_hash({
        "accepted_at": calibration.get("accepted_at"),
        "accepted_policy_hash": calibration.get("accepted_policy_hash"),
        "policy": object_dict(calibration.get("policy")),
    })


def record_holds(
        connection: DBClient,
        *,
        prefix: str,
        mode: str,
        approval: str,
        files: Iterable[LeftOutFile],
        now_iso: str,
) -> None:
    for file in files:
        if file.library_item_id <= 0 or file.code not in HOLDABLE_CODES:
            continue
        values = {
            "prefix": prefix,
            "mode": mode,
            "approval_identity": approval,
            "reason_code": file.code,
            "status": HOLD_WAITING,
            "updated_at": now_iso,
        }
        connection.execute(
            sqlite_insert(production_holds)
            .values(library_item_id=file.library_item_id, held_at=now_iso, **values)
            .on_conflict_do_update(index_elements=[production_holds.c.library_item_id], set_=values)
        )


def release_holds(connection: DBClient, library_item_ids: Collection[int]) -> None:
    ids = sorted({int(item_id) for item_id in library_item_ids if int(item_id) > 0})
    if ids:
        connection.execute(delete(production_holds).where(production_holds.c.library_item_id.in_(ids)))


@dataclass(frozen=True, slots=True)
class ClearedHoldGroup:
    prefix: str
    mode: str
    approval: str
    library_item_ids: tuple[int, ...]


def cleared_hold_groups(connection: DBClient) -> list[ClearedHoldGroup]:
    """Waiting holds whose evidence has cleared, whether or not their scope's run is still going."""
    rows = connection.execute(
        select(production_holds).where(production_holds.c.status == HOLD_WAITING)
    ).mappings().fetchall()
    grouped: dict[tuple[str, str, str], list[int]] = {}
    for row in rows:
        key = (str(row["prefix"]), str(row["mode"]), str(row["approval_identity"]))
        grouped.setdefault(key, []).append(int(row["library_item_id"]))
    groups: list[ClearedHoldGroup] = []
    for (prefix, mode, approval), item_ids in sorted(grouped.items()):
        cleared = cadence_safety_partition(connection, library_item_ids=item_ids, synchronize=True).cleared_item_ids
        if not cleared:
            continue
        groups.append(ClearedHoldGroup(prefix, mode, approval, tuple(sorted(cleared))))
    return groups


def mark_holds(connection: DBClient, library_item_ids: Collection[int], *, status: str, now_iso: str) -> None:
    connection.execute(
        update(production_holds)
        .where(production_holds.c.library_item_id.in_(sorted(library_item_ids)))
        .values(status=status, updated_at=now_iso)
    )


def join_cleared_held_files(
        open_connection: Callable[[], Any],
        *,
        current_approval: Callable[[str], str | None],
        queue_held_files: Callable[[ClearedHoldGroup], dict[str, Any]],
        now_iso: Callable[[], str],
) -> list[dict[str, Any]]:
    """Queue each group of cleared held files under the approval they were held under.

    A group whose approval changed, or whose queue attempt was refused outright, stops being retried
    and keeps its hold with that status so the files stay listed.
    """
    with open_connection() as connection:
        groups = cleared_hold_groups(connection)
    results: list[dict[str, Any]] = []
    for group in groups:
        if current_approval(group.prefix) != group.approval:
            result: dict[str, Any] = {"ok": False, "code": HOLD_APPROVAL_CHANGED, "prefix": group.prefix}
        else:
            result = queue_held_files(group)
        if result.get("code") in {HOLD_APPROVAL_CHANGED, HOLD_REFUSED}:
            with open_connection() as connection:
                mark_holds(connection, group.library_item_ids, status=str(result["code"]), now_iso=now_iso())
        results.append(result)
    return results
