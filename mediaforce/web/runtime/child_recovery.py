"""Fail-closed preview/apply support for terminal folder children.

This module deliberately contains no cleanup, media probing/hashing, or transport
work.  The caller supplies the approval and candidate-policy gates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import (
    encode_jobs,
    item_events,
    library_items,
    staged_artifacts,
)
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.encode_queue import list_child_encode_jobs, save_encode_job
from mediaforce.encoding.staging import partial_output_path


ACTIVE_PARENT_STATUSES = frozenset({"queued", "retry_backoff", "running"})
ELIGIBLE_CHILD_STATUSES = frozenset({"needs_attention", "failed"})
MAX_CHILDREN = 100
MAX_RECEIPTS = 8

ApprovalContractFn = Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any] | None]
CandidateEligibilityFn = Callable[[DBClient, dict[str, Any], list[dict[str, Any]]], Mapping[str, Any]]
SyncParentFn = Callable[[DBClient, dict[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class RecoveryPreview:
    parent_job_id: str
    manifest_path: str
    manifest_sha256: str
    child_ids: tuple[str, ...]
    manifest_indexes: tuple[int, ...]
    token: str
    items: tuple[dict[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "parent_job_id": self.parent_job_id,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "child_ids": list(self.child_ids),
            "manifest_indexes": list(self.manifest_indexes),
            "token": self.token,
            "items": [dict(item) for item in self.items],
        }


def preview_child_recovery(
    connection: DBClient,
    config: MediaforceConfig,
    parent_job_id: str,
    *,
    child_ids: Sequence[str],
    approval_contract: ApprovalContractFn,
    candidate_eligibility: CandidateEligibilityFn,
) -> dict[str, Any]:
    preview = _build_preview(
        connection,
        config,
        parent_job_id,
        child_ids=child_ids,
        approval_contract=approval_contract,
        candidate_eligibility=candidate_eligibility,
    )
    return preview.to_payload()


def apply_child_recovery(
    connection: DBClient,
    config: MediaforceConfig,
    parent_job_id: str,
    *,
    child_ids: Sequence[str],
    expected_token: str,
    approval_contract: ApprovalContractFn,
    candidate_eligibility: CandidateEligibilityFn,
    sync_parent: SyncParentFn,
    now_iso: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Apply using a fresh connection; the caller owns guarded commit on context exit."""
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        preview = _build_preview(
            connection,
            config,
            parent_job_id,
            child_ids=child_ids,
            approval_contract=approval_contract,
            candidate_eligibility=candidate_eligibility,
        )
        if str(expected_token or "") != preview.token:
            raise HTTPException(
                status_code=409,
                detail="The recovery preview is stale. Preview it again.",
            )
        now = (now_iso or _now_iso)()
        children = {str(job["job_id"]): job for job in list_child_encode_jobs(connection, parent_job_id)}
        selected = [children[child_id] for child_id in preview.child_ids]
        for child in selected:
            updated = dict(child)
            progress = dict(object_dict(updated.get("progress")))
            receipts = object_list(progress.get("recovery_receipts"))
            receipts = [object_dict(item) for item in receipts][-MAX_RECEIPTS + 1 :]
            receipts.append(
                {
                    "kind": "targeted_child_recovery",
                    "recorded_at": now,
                    "parent_job_id": parent_job_id,
                    "manifest_indexes": list(object_list(child.get("manifest_indexes"))),
                    "previous_status": str(child.get("status") or ""),
                    "previous_attempt_count": int_value(child.get("attempt_count")),
                    "previous_failure_kind": str(child.get("last_failure_kind") or ""),
                    "previous_error": str(child.get("error") or "")[:2000],
                }
            )
            progress["recovery_receipts"] = receipts
            updated.update(
                {
                    "status": "queued",
                    "host": {},
                    "last_host": object_dict(child.get("last_host")),
                    "process_pid": None,
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "schedule_close_deadline_at": None,
                    "retry_not_before": None,
                    "waiting_reason": None,
                    "terminal_reason": None,
                    "host_cooldown_until": child.get("host_cooldown_until"),
                    "finished_at": None,
                    "progress": progress,
                    "updated_at": now,
                }
            )
            save_encode_job(connection, updated)
            receipt = {
                **receipts[-1],
                "job_id": child["job_id"],
                "preview_token": preview.token,
            }
            for item in preview.items:
                if set(item["manifest_indexes"]) & set(child["manifest_indexes"]):
                    connection.execute(
                        item_events.insert().values(
                            library_item_id=item["library_item_id"],
                            created_at=now,
                            event_type="targeted_child_recovery",
                            details_json=json.dumps(receipt, sort_keys=True),
                        )
                    )
        sync_parent(connection, selected[0])
        return {
            "ok": True,
            "action": "targeted_child_recovery_applied",
            "parent_job_id": parent_job_id,
            "child_ids": list(preview.child_ids),
            "manifest_indexes": list(preview.manifest_indexes),
            "token": preview.token,
        }
    except Exception:
        if connection.in_transaction():
            connection.rollback()
        raise


def _build_preview(
    connection: DBClient,
    config: MediaforceConfig,
    parent_job_id: str,
    *,
    child_ids: Sequence[str],
    approval_contract: ApprovalContractFn,
    candidate_eligibility: CandidateEligibilityFn,
) -> RecoveryPreview:
    parent = (
        connection.execute(select(*encode_jobs.c).where(encode_jobs.c.job_id == str(parent_job_id or "")))
        .mappings()
        .fetchone()
    )
    if parent is None:
        raise HTTPException(status_code=400, detail="The folder recovery parent does not exist.")
    parent_job = _hydrate_minimal_job(parent)
    if parent_job["job_kind"] != "folder" or parent_job["status"] not in ACTIVE_PARENT_STATUSES:
        raise HTTPException(status_code=409, detail="The folder recovery parent is no longer active.")
    children = list_child_encode_jobs(connection, parent_job_id)
    if (
        not isinstance(child_ids, (list, tuple))
        or not child_ids
        or any(not isinstance(value, str) or not value.strip() for value in child_ids)
        or len(set(child_ids)) != len(child_ids)
    ):
        raise HTTPException(
            status_code=400,
            detail="Recovery child IDs must be explicit, nonempty and unique.",
        )
    requested_ids = set(child_ids)
    if len(requested_ids) > MAX_CHILDREN:
        raise HTTPException(status_code=400, detail="Too many recovery child IDs.")
    if not requested_ids.issubset({str(child.get("job_id")) for child in children}):
        raise HTTPException(
            status_code=400,
            detail="A requested recovery child does not belong to the parent.",
        )
    eligible = [
        child
        for child in children
        if str(child.get("job_id")) in requested_ids
        if str(child.get("status") or "") in ELIGIBLE_CHILD_STATUSES
        and str(child.get("last_failure_kind") or "") == "host_configuration"
    ]
    if not eligible:
        raise HTTPException(
            status_code=409,
            detail="No host-configuration child is eligible for recovery.",
        )
    if {str(child.get("job_id")) for child in eligible} != requested_ids:
        raise HTTPException(
            status_code=409,
            detail="One or more selected children are not eligible for recovery.",
        )
    ownership_fields = (
        "process_pid",
        "worker_id",
        "leased_at",
        "lease_expires_at",
        "heartbeat_at",
    )
    if any(
        child.get("job_kind") != "shard" or any(child.get(key) is not None for key in ownership_fields)
        for child in eligible
    ):
        raise HTTPException(
            status_code=409,
            detail="Selected children must be inactive, unleased shards.",
        )
    manifest_path = str(parent_job.get("manifest_path") or "").strip()
    if any(str(child.get("manifest_path") or "").strip() != manifest_path for child in eligible):
        raise HTTPException(
            status_code=409,
            detail="Child manifest paths do not match the folder parent.",
        )
    if any(str(child.get("prefix") or "") != str(parent_job.get("prefix") or "") for child in eligible):
        raise HTTPException(status_code=409, detail="Child prefixes do not match the folder parent.")
    try:
        manifest_bytes = Path(manifest_path).read_bytes()
        manifest = object_dict(json.loads(manifest_bytes))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=409, detail="The recovery manifest is unreadable.") from None
    items = [object_dict(item) for item in object_list(manifest.get("items"))]
    manifest_ids = [item.get("library_item_id") for item in items]
    if (
        not manifest_ids
        or any(type(value) is not int or value <= 0 for value in manifest_ids)
        or len(set(manifest_ids)) != len(manifest_ids)
    ):
        raise HTTPException(status_code=409, detail="The manifest must identify unique source items.")
    indexes_by_child: dict[str, list[int]] = {}
    all_indexes: list[int] = []
    for child in eligible:
        child_id = str(child["job_id"])
        raw = child.get("manifest_indexes")
        if not isinstance(raw, list) or not raw:
            raise HTTPException(
                status_code=409,
                detail="A recoverable child has no explicit manifest indexes.",
            )
        indexes: list[int] = []
        for value in raw:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= len(items):
                raise HTTPException(
                    status_code=409,
                    detail="A recoverable child has invalid manifest indexes.",
                )
            indexes.append(value)
        if len(set(indexes)) != len(indexes):
            raise HTTPException(
                status_code=409,
                detail="A recoverable child has duplicate manifest indexes.",
            )
        indexes_by_child[child_id] = indexes
        all_indexes.extend(indexes)
    active_or_completed = [
        child
        for child in children
        if str(child.get("status") or "") in {"queued", "retry_backoff", "running", "completed"}
    ]
    for child in active_or_completed:
        sibling_raw = child.get("manifest_indexes")
        if (
            not isinstance(sibling_raw, list)
            or not sibling_raw
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= len(items)
                for value in sibling_raw
            )
            or len(set(sibling_raw)) != len(sibling_raw)
        ):
            raise HTTPException(
                status_code=409,
                detail="An active or completed sibling has invalid manifest indexes.",
            )
        if any(index in all_indexes for index in _strict_indexes(child)):
            raise HTTPException(
                status_code=409,
                detail="A manifest item is already owned by an active or completed child.",
            )
    if len(set(all_indexes)) != len(all_indexes):
        raise HTTPException(
            status_code=409,
            detail="Manifest indexes are claimed by multiple recoverable children.",
        )
    _validate_approval(
        approval_contract(parent_job, manifest),
        object_dict(object_dict(manifest).get("selection")).get("production_approval_contract"),
    )
    item_rows = _validate_sources(connection, config, items, sorted(all_indexes))
    eligible_result = candidate_eligibility(connection, parent_job, [items[index] for index in sorted(all_indexes)])
    if not isinstance(eligible_result, Mapping) or not eligible_result:
        raise HTTPException(status_code=409, detail="Current candidate policy blocks recovery.")
    topology = {
        "candidate_evidence": dict(eligible_result),
        "config": config.raw,
        "parent": {
            "job_id": parent_job_id,
            "prefix": parent_job["prefix"],
            "kind": parent_job["job_kind"],
            "status": parent_job["status"],
            "manifest_path": manifest_path,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        },
        "children": [
            (
                child
                if str(child["job_id"]) in indexes_by_child
                else {
                    "job_id": child["job_id"],
                    "prefix": child.get("prefix"),
                    "manifest_path": child.get("manifest_path"),
                    "indexes": _strict_indexes(child),
                }
            )
            for child in children
        ],
        "sources": [
            {
                "id": int(row["id"]),
                "path": str(row["source_path"]),
                "size": int(row["size_bytes"]),
                "fingerprint": str(row["fingerprint"]),
                "stat": row.get("_stat"),
            }
            for row in item_rows
        ],
    }
    try:
        snapshot = json.dumps(topology, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail="Recovery state cannot be represented reliably.") from None
    token = hashlib.sha256(snapshot.encode()).hexdigest()
    public_items = tuple(
        {
            "library_item_id": int(row["id"]),
            "rel_path": str(row["rel_path"]),
            "manifest_indexes": [
                index for index in all_indexes if int(items[index].get("library_item_id") or 0) == int(row["id"])
            ],
        }
        for row in item_rows
    )
    return RecoveryPreview(
        parent_job_id,
        manifest_path,
        hashlib.sha256(manifest_bytes).hexdigest(),
        tuple(str(c["job_id"]) for c in eligible),
        tuple(sorted(all_indexes)),
        token,
        public_items,
    )


def _validate_sources(
    connection: DBClient,
    config: MediaforceConfig,
    items: list[dict[str, Any]],
    indexes: list[int],
) -> list[dict[str, Any]]:
    ids = [int(items[index].get("library_item_id") or 0) for index in indexes]
    if any(item_id <= 0 for item_id in ids) or len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=409,
            detail="The recovery manifest does not identify unique source items.",
        )
    rows = [
        dict(row)
        for row in connection.execute(select(library_items).where(library_items.c.id.in_(ids))).mappings().fetchall()
    ]
    by_id = {int(row["id"]): row for row in rows}
    for index, item_id in zip(indexes, ids):
        row = by_id.get(item_id)
        item = items[index]
        if (
            row is None
            or str(item.get("source_path") or "") != str(row["source_path"])
            or str(item.get("source_rel_path") or item.get("rel_path") or "") != str(row["rel_path"])
        ):
            raise HTTPException(
                status_code=409,
                detail="The recovery source no longer matches the manifest.",
            )
        if int_value(item.get("source_size_bytes")) != int(row["size_bytes"]) or str(
            item.get("source_fingerprint") or ""
        ) != str(row["fingerprint"]):
            raise HTTPException(status_code=409, detail="The recovery source identity changed.")
        path = Path(str(row["source_path"])).expanduser()
        root = config.source_root_map.get(str(row.get("media_root") or ""))
        if root is None or not _under_root(path, root) or not path.is_file():
            raise HTTPException(status_code=409, detail="The recovery source is inaccessible.")
        try:
            stat = path.stat()
            if stat.st_size != int(row["size_bytes"]) or stat.st_mtime_ns != int(row["mtime_ns"]):
                raise HTTPException(
                    status_code=409,
                    detail="The recovery source size or modification time changed.",
                )
            row["_stat"] = {
                "dev": int(stat.st_dev),
                "ino": int(stat.st_ino),
                "size": int(stat.st_size),
                "mtime_ns": int(stat.st_mtime_ns),
            }
        except OSError:
            raise HTTPException(status_code=409, detail="The recovery source is inaccessible.") from None
        output_value = str(item.get("staging_path") or "").strip()
        if not output_value:
            raise HTTPException(status_code=409, detail="Recovery output path is missing.")
        output_path = Path(output_value).expanduser()
        try:
            if not config.staging_root.is_dir() or not _under_root(output_path, config.staging_root):
                raise HTTPException(
                    status_code=409,
                    detail="Recovery output storage is inaccessible or changed.",
                )
            for candidate in (output_path, partial_output_path(output_path)):
                try:
                    candidate.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise HTTPException(
                        status_code=409,
                        detail="A final or partial output already exists for a recovery item.",
                    )
        except OSError:
            raise HTTPException(status_code=409, detail="Recovery output storage is inaccessible.") from None
        stage = (
            connection.execute(select(staged_artifacts).where(staged_artifacts.c.library_item_id == item_id))
            .mappings()
            .fetchone()
        )
        if stage is not None:
            raise HTTPException(
                status_code=409,
                detail="A staged artifact already exists for a recovery item.",
            )
    return [by_id[item_id] for item_id in ids]


def _under_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(Path(root).expanduser().resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_approval(current: Mapping[str, Any] | None, saved: Mapping[str, Any] | None) -> None:
    if (
        not isinstance(current, Mapping)
        or not isinstance(saved, Mapping)
        or not current
        or not saved
        or dict(current) != dict(saved)
    ):
        raise HTTPException(status_code=409, detail="The current production approval contract changed.")


def _strict_indexes(job: Mapping[str, Any]) -> list[int]:
    raw = job.get("manifest_indexes")
    return (
        [value for value in raw]
        if isinstance(raw, list) and all(isinstance(value, int) and not isinstance(value, bool) for value in raw)
        else []
    )


def _hydrate_minimal_job(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(row["job_id"]),
        "prefix": str(row["prefix"]),
        "job_kind": str(row["job_kind"] or "single"),
        "status": str(row["status"]),
        "manifest_path": str(row["manifest_path"]),
        "updated_at": row["updated_at"],
    }


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")
