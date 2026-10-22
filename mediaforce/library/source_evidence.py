"""A published file's source measurements, kept apart from measurements of its output.

``library_items`` holds the measurements of whatever file is in the library now, so after a
publish the evidence worker re-measures the output there. Grouping shows by what their
sources look like needs the source's measurements, so a publish records them here first.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_item_evidence_state, library_item_source_evidence, library_items, \
    staged_artifacts
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.cadence import CADENCE_EVIDENCE_KIND
from mediaforce.encoding.fingerprint import MEDIA_FINGERPRINT_EVIDENCE_KIND
from mediaforce.library.evidence_state import EVIDENCE_KINDS, EvidenceKind, evidence_summary_column, \
    evidence_summary_sha256, library_item_source_fingerprint, parse_evidence_summary

SourceEvidenceOrigin = Literal["publish", "run_plan", "catalog"]

SOURCE_EVIDENCE_ORIGIN_PUBLISH: SourceEvidenceOrigin = "publish"
SOURCE_EVIDENCE_ORIGIN_RUN_PLAN: SourceEvidenceOrigin = "run_plan"
SOURCE_EVIDENCE_ORIGIN_CATALOG: SourceEvidenceOrigin = "catalog"

# The run-plan item keys that carry each kind's measurements, as `planner.build_manifest_item` writes them.
_RUN_PLAN_SUMMARY_KEYS: dict[EvidenceKind, str] = {
    CADENCE_EVIDENCE_KIND: "cadence_summary",
    MEDIA_FINGERPRINT_EVIDENCE_KIND: "media_fingerprint",
}


@dataclass(frozen=True, slots=True)
class _SourceSummary:
    summary_json: str
    source_fingerprint: str | None
    origin: SourceEvidenceOrigin


@dataclass(slots=True)
class SourceEvidenceRestoreStats:
    published_item_count: int = 0
    restored_from_run_plan: dict[str, int] = field(default_factory=dict)
    restored_from_catalog: dict[str, int] = field(default_factory=dict)
    already_recorded: dict[str, int] = field(default_factory=dict)
    unrecoverable: dict[str, int] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "published_item_count": self.published_item_count,
            "restored_from_run_plan": dict(sorted(self.restored_from_run_plan.items())),
            "restored_from_catalog": dict(sorted(self.restored_from_catalog.items())),
            "already_recorded": dict(sorted(self.already_recorded.items())),
            "unrecoverable": dict(sorted(self.unrecoverable.items())),
        }


def canonical_summary_json(summary: Mapping[str, Any]) -> str:
    """The text form the evidence worker stores, so a restored summary hashes like a measured one."""
    return json.dumps(summary, separators=(",", ":"), sort_keys=True)


def record_source_evidence_before_publish(
        connection: DBClient,
        library_item_id: int,
        *,
        published_fingerprint: str | None,
        recorded_at: str,
) -> None:
    """Keep the measurements of the file a publish is about to replace.

    Runs in the publish transaction before the catalog row moves to the output. Only a summary
    whose evidence state binds it to this exact file is kept; anything else describes another
    file. Publishing over our own earlier output keeps the record of the original source.
    """
    item = connection.execute(
        select(library_items).where(library_items.c.id == library_item_id)
    ).mappings().fetchone()
    if item is None:
        return
    source_fingerprint = library_item_source_fingerprint(item)
    states = _evidence_states_by_item(connection, [library_item_id]).get(library_item_id, {})
    recorded = _recorded_kinds(connection, [library_item_id]).get(library_item_id, {})
    for evidence_kind in EVIDENCE_KINDS:
        previous = recorded.get(evidence_kind)
        if previous is not None and source_fingerprint and previous["published_fingerprint"] == source_fingerprint:
            continue
        summary_json = _summary_measured_on(item, states.get(evidence_kind), evidence_kind, source_fingerprint)
        if summary_json is None:
            _forget(connection, library_item_id, evidence_kind)
            continue
        _upsert(
            connection,
            library_item_id,
            evidence_kind,
            _SourceSummary(summary_json, source_fingerprint, SOURCE_EVIDENCE_ORIGIN_PUBLISH),
            published_fingerprint=published_fingerprint,
            recorded_at=recorded_at,
        )


def forget_source_evidence(connection: DBClient, library_item_id: int) -> None:
    """A new original replaced the published file, so the old source's measurements no longer apply."""
    connection.execute(
        delete(library_item_source_evidence)
        .where(library_item_source_evidence.c.library_item_id == library_item_id)
    )


def load_source_evidence_summaries(
        connection: DBClient,
        library_item_ids: Iterable[int],
        evidence_kind: EvidenceKind,
) -> dict[int, dict[str, Any]]:
    """Each item's source measurements: the recorded ones once published, else the current file's.

    An unpublished file's measurements count only while its evidence state binds them to that file.
    A published item without a record is left out rather than answered with its output's measurements.
    """
    item_ids = sorted({int(item_id) for item_id in library_item_ids})
    if not item_ids:
        return {}
    recorded = {
        int(row["library_item_id"]): parse_evidence_summary(row["summary_json"])
        for row in connection.execute(
            select(library_item_source_evidence.c.library_item_id, library_item_source_evidence.c.summary_json)
            .where(library_item_source_evidence.c.library_item_id.in_(item_ids))
            .where(library_item_source_evidence.c.evidence_kind == evidence_kind)
        ).mappings()
    }
    states = _evidence_states_by_item(connection, item_ids)
    items = connection.execute(
        select(library_items, staged_artifacts.c.promoted_at)
        .select_from(library_items.outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id))
        .where(library_items.c.id.in_(item_ids))
    ).mappings()
    summaries: dict[int, dict[str, Any]] = {}
    for item in items:
        item_id = int(item["id"])
        if item_id in recorded:
            summary = recorded[item_id]
        elif item["promoted_at"] is None:
            summary = parse_evidence_summary(_summary_measured_on(
                item,
                states.get(item_id, {}).get(evidence_kind),
                evidence_kind,
                library_item_source_fingerprint(item),
            ))
        else:
            summary = None
        if summary is not None:
            summaries[item_id] = summary
    return summaries


def restore_published_source_evidence(
        connection: DBClient,
        *,
        recorded_at: str,
        apply: bool = True,
        batch_size: int = 200,
) -> SourceEvidenceRestoreStats:
    """Give already-published items back their source measurements where they survive.

    The item's run plan wins: it holds what the publish was planned from. Otherwise the catalog
    row still holds them when its evidence state binds them to an earlier file than the output.
    Existing records are left alone, so running this again changes nothing.
    """
    stats = SourceEvidenceRestoreStats()
    plans: dict[str, list[Any] | None] = {}
    rows = connection.execute(
        select(
            library_items,
            staged_artifacts.c.manifest_path,
            staged_artifacts.c.item_index,
            staged_artifacts.c.source_fingerprint.label("staged_source_fingerprint"),
        )
        .join(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)
        .where(staged_artifacts.c.promoted_at.is_not(None))
        .order_by(library_items.c.id)
    ).mappings().fetchall()
    for start in range(0, len(rows), max(1, batch_size)):
        batch = rows[start:start + max(1, batch_size)]
        item_ids = [int(row["id"]) for row in batch]
        states = _evidence_states_by_item(connection, item_ids)
        recorded = _recorded_kinds(connection, item_ids)
        for row in batch:
            stats.published_item_count += 1
            item_id = int(row["id"])
            plan_item = _run_plan_item(row, plans)
            for evidence_kind in EVIDENCE_KINDS:
                if evidence_kind in recorded.get(item_id, {}):
                    _count(stats.already_recorded, evidence_kind)
                    continue
                restored = _run_plan_summary(plan_item, evidence_kind) or _catalog_summary_before_publish(
                    row, states.get(item_id, {}).get(evidence_kind), evidence_kind
                )
                if restored is None:
                    _count(stats.unrecoverable, evidence_kind)
                    continue
                _count(
                    stats.restored_from_run_plan
                    if restored.origin == SOURCE_EVIDENCE_ORIGIN_RUN_PLAN
                    else stats.restored_from_catalog,
                    evidence_kind,
                )
                if apply:
                    _upsert(
                        connection,
                        item_id,
                        evidence_kind,
                        restored,
                        published_fingerprint=library_item_source_fingerprint(row),
                        recorded_at=recorded_at,
                    )
    return stats


def _summary_measured_on(
        item: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        evidence_kind: EvidenceKind,
        source_fingerprint: str | None,
) -> str | None:
    summary_json = _stored_summary(item, state, evidence_kind)
    if summary_json is None or not source_fingerprint or state is None:
        return None
    return summary_json if state.get("source_fingerprint") == source_fingerprint else None


def _catalog_summary_before_publish(
        item: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        evidence_kind: EvidenceKind,
) -> _SourceSummary | None:
    """The catalog row's summary when its evidence was bound to a file before the published output."""
    summary_json = _stored_summary(item, state, evidence_kind)
    bound_fingerprint = str((state or {}).get("source_fingerprint") or "") or None
    if summary_json is None or bound_fingerprint is None or bound_fingerprint == library_item_source_fingerprint(item):
        return None
    return _SourceSummary(summary_json, bound_fingerprint, SOURCE_EVIDENCE_ORIGIN_CATALOG)


def _stored_summary(
        item: Mapping[str, Any],
        state: Mapping[str, Any] | None,
        evidence_kind: EvidenceKind,
) -> str | None:
    raw = item.get(evidence_summary_column(evidence_kind))
    if not isinstance(raw, str) or parse_evidence_summary(raw) is None or state is None:
        return None
    return raw if state.get("summary_sha256") == evidence_summary_sha256(raw) else None


def _run_plan_item(row: Mapping[str, Any], plans: dict[str, list[Any] | None]) -> dict[str, Any] | None:
    manifest_path = str(row.get("manifest_path") or "").strip()
    if not manifest_path:
        return None
    if manifest_path not in plans:
        plans[manifest_path] = _read_run_plan_items(Path(manifest_path))
    items = plans[manifest_path]
    if not items:
        return None
    item_id = int(row["id"])
    index = row.get("item_index")
    candidates = [items[index]] if isinstance(index, int) and 0 <= index < len(items) else []
    candidates.extend(items)
    for candidate in candidates:
        plan_item = object_dict(candidate)
        if plan_item.get("library_item_id") != item_id:
            continue
        staged_source = row.get("staged_source_fingerprint")
        if staged_source and plan_item.get("source_fingerprint") != staged_source:
            return None
        return plan_item
    return None


def _read_run_plan_items(path: Path) -> list[Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return object_list(object_dict(payload).get("items"))


def _run_plan_summary(plan_item: Mapping[str, Any] | None, evidence_kind: EvidenceKind) -> _SourceSummary | None:
    if plan_item is None:
        return None
    summary = plan_item.get(_RUN_PLAN_SUMMARY_KEYS[evidence_kind])
    if not isinstance(summary, dict) or not summary:
        return None
    source_fingerprint = library_item_source_fingerprint(
        {
            "content_version_fingerprint": plan_item.get("content_version_fingerprint"),
            "fingerprint": plan_item.get("source_fingerprint"),
        }
    )
    return _SourceSummary(canonical_summary_json(summary), source_fingerprint, SOURCE_EVIDENCE_ORIGIN_RUN_PLAN)


def _evidence_states_by_item(
        connection: DBClient,
        item_ids: Sequence[int],
) -> dict[int, dict[str, dict[str, Any]]]:
    states: dict[int, dict[str, dict[str, Any]]] = {}
    for row in connection.execute(
            select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id.in_(item_ids))
    ).mappings():
        states.setdefault(int(row["library_item_id"]), {})[str(row["evidence_kind"])] = dict(row)
    return states


def _recorded_kinds(connection: DBClient, item_ids: Sequence[int]) -> dict[int, dict[str, dict[str, Any]]]:
    recorded: dict[int, dict[str, dict[str, Any]]] = {}
    for row in connection.execute(
            select(
                library_item_source_evidence.c.library_item_id,
                library_item_source_evidence.c.evidence_kind,
                library_item_source_evidence.c.published_fingerprint,
            ).where(library_item_source_evidence.c.library_item_id.in_(item_ids))
    ).mappings():
        recorded.setdefault(int(row["library_item_id"]), {})[str(row["evidence_kind"])] = dict(row)
    return recorded


def _upsert(
        connection: DBClient,
        library_item_id: int,
        evidence_kind: EvidenceKind,
        summary: _SourceSummary,
        *,
        published_fingerprint: str | None,
        recorded_at: str,
) -> None:
    values = {
        "summary_json": summary.summary_json,
        "source_fingerprint": summary.source_fingerprint,
        "published_fingerprint": published_fingerprint,
        "origin": summary.origin,
        "recorded_at": recorded_at,
    }
    statement = sqlite_insert(library_item_source_evidence).values(
        library_item_id=library_item_id,
        evidence_kind=evidence_kind,
        **values,
    )
    connection.execute(
        statement.on_conflict_do_update(
            index_elements=[library_item_source_evidence.c.library_item_id, library_item_source_evidence.c.evidence_kind],
            set_=values,
        )
    )


def _forget(connection: DBClient, library_item_id: int, evidence_kind: EvidenceKind) -> None:
    connection.execute(
        delete(library_item_source_evidence)
        .where(library_item_source_evidence.c.library_item_id == library_item_id)
        .where(library_item_source_evidence.c.evidence_kind == evidence_kind)
    )


def _count(counts: dict[str, int], evidence_kind: EvidenceKind) -> None:
    counts[evidence_kind] = counts.get(evidence_kind, 0) + 1
