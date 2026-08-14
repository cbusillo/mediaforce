from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope, scope_rel_path_filter

IntegrityDisposition = Literal[
    "promotable",
    "tracked",
    "unvalidated",
    "validation_failed",
    "missing",
    "drifted",
    "orphaned",
    "partial_or_temporary",
    "remote_only_or_unreachable",
    "not_started",
]

MAX_INTEGRITY_RECORDS = 500
MAX_DISCOVERY_ENTRIES = 500
MAX_DETAIL_PAGE_SIZE = 100

_BLOCKING_DISPOSITIONS = frozenset({
    "unvalidated",
    "validation_failed",
    "missing",
    "drifted",
    "orphaned",
    "partial_or_temporary",
    "remote_only_or_unreachable",
    "not_started",
})
_TEMPORARY_SUFFIXES = (".part", ".partial", ".tmp", ".temp", ".working")
_TEMPORARY_MARKERS = (".partial.", ".part.", ".tmp.", ".temp.", ".working.")
_IGNORED_DISCOVERY_NAMES = frozenset({".ds_store"})


@dataclass(frozen=True, slots=True)
class StagedIntegrityRecord:
    disposition: IntegrityDisposition
    item_id: int | None
    rel_path: str | None
    staging_path: str | None
    code: str
    next_action: str
    detail: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition,
            "item_id": self.item_id,
            "rel_path": self.rel_path,
            "staging_path": self.staging_path,
            "code": self.code,
            "next_action": self.next_action,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class StagedIntegrityReport:
    scope: MediaScope
    records: tuple[StagedIntegrityRecord, ...]
    counts: dict[str, int]
    database_truncated: bool
    discovery_requested: bool
    discovery_truncated: bool
    discovery_entries_scanned: int

    @property
    def blocker_count(self) -> int:
        return sum(self.counts.get(disposition, 0) for disposition in _BLOCKING_DISPOSITIONS)

    def summary_payload(self) -> dict[str, Any]:
        blockers = [
            {
                "code": _disposition_code(disposition),
                "count": self.counts[disposition],
                "next_action": _next_action(disposition),
            }
            for disposition in sorted(_BLOCKING_DISPOSITIONS)
            if self.counts.get(disposition, 0)
        ]
        return {
            "scope": self.scope.to_payload(),
            "counts": dict(sorted(self.counts.items())),
            "blocker_count": self.blocker_count,
            "blockers": blockers,
            "database_truncated": self.database_truncated,
            "discovery": {
                "requested": self.discovery_requested,
                "truncated": self.discovery_truncated,
                "entries_scanned": self.discovery_entries_scanned,
            },
        }

    def detail_payload(self, *, offset: int, limit: int) -> dict[str, Any]:
        normalized_offset = max(0, offset)
        normalized_limit = min(max(1, limit), MAX_DETAIL_PAGE_SIZE)
        page = self.records[normalized_offset:normalized_offset + normalized_limit]
        return {
            **self.summary_payload(),
            "offset": normalized_offset,
            "limit": normalized_limit,
            "records": [record.to_payload() for record in page],
            "next_offset": (
                normalized_offset + len(page)
                if normalized_offset + len(page) < len(self.records)
                else None
            ),
        }


def staged_integrity_report(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        discover: bool,
        record_limit: int = MAX_INTEGRITY_RECORDS,
) -> StagedIntegrityReport:
    scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
    return staged_integrity_report_for_scope(
        connection,
        config,
        scope,
        discover=discover,
        record_limit=record_limit,
    )


def staged_integrity_report_for_scope(
        connection: DBClient,
        config: MediaforceConfig,
        scope: MediaScope,
        *,
        discover: bool,
        record_limit: int = MAX_INTEGRITY_RECORDS,
) -> StagedIntegrityReport:
    bounded_limit = min(max(1, record_limit), MAX_INTEGRITY_RECORDS)
    rows = _load_scope_rows(connection, scope, limit=bounded_limit + 1)
    database_truncated = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    staging_roots = _configured_staging_roots(config)
    records = [_classify_row(row, staging_roots) for row in rows]
    discovery_truncated = bool(discover and database_truncated)
    entries_scanned = 0
    if discover and not database_truncated:
        discovered_records, discovery_truncated, entries_scanned = _discover_untracked_outputs(
            scope,
            staging_roots,
            output_suffix=f".{str(config.media.get('output_container') or 'mkv').lstrip('.')}",
            tracked_paths={_normalized_path(record.staging_path) for record in records if record.staging_path},
        )
        records.extend(discovered_records)
    records.sort(key=_record_sort_key)
    counts = Counter(record.disposition for record in records)
    return StagedIntegrityReport(
        scope=scope,
        records=tuple(records),
        counts=dict(counts),
        database_truncated=database_truncated,
        discovery_requested=discover,
        discovery_truncated=discovery_truncated,
        discovery_entries_scanned=entries_scanned,
    )


def integrity_disposition_blocks_promotion(disposition: IntegrityDisposition) -> bool:
    return disposition in _BLOCKING_DISPOSITIONS


def _load_scope_rows(connection: DBClient, scope: MediaScope, *, limit: int) -> list[DBRow]:
    query = (
        select(
            library_items.c.id.label("item_id"),
            library_items.c.rel_path,
            library_items.c.status,
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.staging_path,
            staged_artifacts.c.staging_size_bytes,
            staged_artifacts.c.staging_mtime_ns,
            staged_artifacts.c.validation_json,
            staged_artifacts.c.validated_at,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.promoted_path,
            staged_artifacts.c.encode_host_key,
            staged_artifacts.c.encode_host_label,
            staged_artifacts.c.encode_media_access,
        )
        .select_from(
            library_items.outerjoin(
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .order_by(library_items.c.rel_path.asc())
        .limit(limit)
    )
    return connection.execute(query).mappings().fetchall()


def _classify_row(row: DBRow, staging_roots: tuple[_StagingRoot, ...]) -> StagedIntegrityRecord:
    item_id = int(row["item_id"])
    rel_path = str(row["rel_path"])
    status = str(row["status"] or "")
    staging_path = str(row["staging_path"] or "").strip() or None
    if status == "promoted" or row["promoted_at"] is not None:
        return _record("tracked", item_id, rel_path, staging_path, "Already promoted and tracked in the library.")
    if staging_path is None:
        disposition: IntegrityDisposition = (
            "not_started"
            if status in {"discovered", "planned", "approved"}
            else "missing"
        )
        detail = "No staged artifact is recorded for this item."
        return _record(disposition, item_id, rel_path, None, detail)
    path = Path(staging_path).expanduser()
    root = _staging_root_for_path(path, staging_roots)
    if _is_temporary_path(path):
        return _record(
            "partial_or_temporary",
            item_id,
            rel_path,
            staging_path,
            "The recorded staged path is a partial or temporary artifact.",
        )
    if root is None:
        return _record(
            "remote_only_or_unreachable",
            item_id,
            rel_path,
            staging_path,
            "The staged path is outside configured staging roots and cannot be inspected safely.",
        )
    try:
        stat_result = path.stat()
    except OSError:
        if root.remote or _row_has_remote_origin(row) or (
                str(row["encode_host_key"] or "").strip()
                and not root.path.is_dir()
        ):
            return _record(
                "remote_only_or_unreachable",
                item_id,
                rel_path,
                staging_path,
                "The staged output is configured on a remote host but is not reachable from this host.",
            )
        return _record("missing", item_id, rel_path, staging_path, "The recorded staged output is missing.")
    if not path.is_file():
        return _record("missing", item_id, rel_path, staging_path, "The recorded staged output is not a regular file.")
    expected_size = _optional_int(row["staging_size_bytes"])
    expected_mtime = _optional_int(row["staging_mtime_ns"])
    if (
            (expected_size is not None and stat_result.st_size != expected_size)
            or (expected_mtime is not None and stat_result.st_mtime_ns != expected_mtime)
    ):
        return _record(
            "drifted",
            item_id,
            rel_path,
            staging_path,
            "The local staged output no longer matches the recorded size or modification time.",
        )
    validation_passed = _validation_passed(row["validation_json"])
    if validation_passed is False:
        return _record("validation_failed", item_id, rel_path, staging_path, "Machine validation recorded a failure.")
    if validation_passed is not True or row["validated_at"] is None:
        return _record("unvalidated", item_id, rel_path, staging_path, "The staged output has not passed machine validation.")
    if status != "validated":
        return _record(
            "unvalidated",
            item_id,
            rel_path,
            staging_path,
            "Validation evidence does not agree with the library item's workflow state.",
        )
    return _record("promotable", item_id, rel_path, staging_path, "Validated staged output is ready for promotion.")


@dataclass(frozen=True, slots=True)
class _StagingRoot:
    path: Path
    remote: bool


def _configured_staging_roots(config: MediaforceConfig) -> tuple[_StagingRoot, ...]:
    configured_root = str(config.media.get("staging_root") or "").strip()
    roots = [_StagingRoot(_normalized_path(configured_root), remote=False)] if configured_root else []
    for host in config.remote_hosts:
        configured_host_root = str(host.get("staging_root") or configured_root).strip()
        if not configured_host_root:
            continue
        host_root = _normalized_path(configured_host_root)
        if any(root.path == host_root for root in roots):
            continue
        roots.append(_StagingRoot(host_root, remote=True))
    return tuple(roots)


def _staging_root_for_path(path: Path, roots: tuple[_StagingRoot, ...]) -> _StagingRoot | None:
    normalized_path = _normalized_path(path)
    return next((root for root in roots if _is_relative_to(normalized_path, root.path)), None)


def _discover_untracked_outputs(
        scope: MediaScope,
        staging_roots: tuple[_StagingRoot, ...],
        *,
        output_suffix: str,
        tracked_paths: set[Path],
) -> tuple[list[StagedIntegrityRecord], bool, int]:
    records: list[StagedIntegrityRecord] = []
    entries_scanned = 0
    for root in staging_roots:
        scope_root = _normalized_path(root.path / Path(scope.prefix))
        if not _is_relative_to(scope_root, root.path):
            return records, True, entries_scanned
        candidates, root_truncated = _bounded_files(
            scope_root,
            limit=max(0, MAX_DISCOVERY_ENTRIES - entries_scanned),
            output_suffix=output_suffix,
        )
        for candidate in candidates:
            entries_scanned += 1
            normalized_candidate = _normalized_path(candidate)
            if normalized_candidate in tracked_paths:
                continue
            if candidate.name.lower() in _IGNORED_DISCOVERY_NAMES:
                continue
            rel_path = str(candidate.relative_to(root.path))
            if _is_temporary_path(candidate):
                records.append(_record(
                    "partial_or_temporary",
                    None,
                    rel_path,
                    str(candidate),
                    "Discovered a partial or temporary artifact under a configured staging root.",
                ))
                continue
            records.append(_record(
                "orphaned",
                None,
                rel_path,
                str(candidate),
                "Discovered an untracked staged output under a configured staging root.",
            ))
        if root_truncated or entries_scanned >= MAX_DISCOVERY_ENTRIES:
            return records, True, entries_scanned
    return records, False, entries_scanned


def _bounded_files(root: Path, *, limit: int, output_suffix: str) -> tuple[list[Path], bool]:
    if not root.is_dir():
        return [], False
    files: list[Path] = []
    pending = [root]
    incomplete = False
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.startswith("."):
                                continue
                            pending.append(path)
                        elif entry.is_file(follow_symlinks=False) or entry.is_symlink():
                            if path.suffix.lower() != output_suffix.lower() and not _is_temporary_path(path):
                                continue
                            if len(files) >= limit:
                                return files, True
                            files.append(path)
                    except OSError:
                        incomplete = True
                        continue
        except OSError:
            incomplete = True
            continue
    return files, incomplete


def _record(
        disposition: IntegrityDisposition,
        item_id: int | None,
        rel_path: str | None,
        staging_path: str | None,
        detail: str,
) -> StagedIntegrityRecord:
    return StagedIntegrityRecord(
        disposition=disposition,
        item_id=item_id,
        rel_path=rel_path,
        staging_path=staging_path,
        code=_disposition_code(disposition),
        next_action=_next_action(disposition),
        detail=detail,
    )


def _disposition_code(disposition: IntegrityDisposition) -> str:
    return f"staged_integrity_{disposition}"


def _next_action(disposition: IntegrityDisposition) -> str:
    return {
        "promotable": "promote_output",
        "tracked": "none",
        "unvalidated": "validate_output",
        "validation_failed": "inspect_validation_failure",
        "missing": "recreate_staged_output",
        "drifted": "revalidate_or_recreate_output",
        "orphaned": "inspect_untracked_output",
        "partial_or_temporary": "wait_or_inspect_temporary_output",
        "remote_only_or_unreachable": "restore_staging_access",
        "not_started": "queue_encode",
    }[disposition]


def _validation_passed(raw_validation: Any) -> bool | None:
    if not isinstance(raw_validation, str) or not raw_validation.strip():
        return None
    try:
        payload = json.loads(raw_validation)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return None
    return bool(payload["passed"])


def _row_has_remote_origin(row: DBRow) -> bool:
    return str(row["encode_media_access"] or "").strip() == "stream"


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_temporary_path(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(".")
        or name.endswith(_TEMPORARY_SUFFIXES)
        or any(marker in name for marker in _TEMPORARY_MARKERS)
    )


def _normalized_path(path: Path | str | None) -> Path:
    return Path(path or "").expanduser().resolve(strict=False)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _record_sort_key(record: StagedIntegrityRecord) -> tuple[str, str, int]:
    return record.rel_path or "", record.staging_path or "", record.item_id or -1
