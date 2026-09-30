"""Fail-closed preview/apply support for terminal folder children.

Each selected child is judged on its own: an ineligible child is listed in the preview's
``skipped`` entries with its reason and the rest are still recovered.

This module deliberately contains no cleanup, media probing/hashing, or transport
work.  The caller supplies the approval and candidate-policy gates.  A header-only
output that the encoder itself failed to probe is named in the preview and handed
to the queue's existing retry cleanup by requeueing the child as retry_backoff.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat as stat_module
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, is_database_busy_failure
from mediaforce.core.db_tables import (
    encode_jobs,
    item_events,
    library_items,
    staged_artifacts,
)
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.encode_queue import list_child_encode_jobs, save_encode_job
from mediaforce.encoding.quality import RemoteQualityTimeoutError
from mediaforce.encoding.staging import HEADER_ONLY_OUTPUT_MAX_BYTES, partial_output_path
from mediaforce.hosts.types import is_storage_io_failure, is_vmaf_model_load_failure


ACTIVE_PARENT_STATUSES = frozenset({"queued", "retry_backoff", "running"})
# A folder parent aggregates to needs_attention once its last active child ends, which is
# exactly when the remaining terminal children most need targeted recovery.
RECOVERABLE_PARENT_STATUSES = ACTIVE_PARENT_STATUSES | frozenset({"needs_attention"})
ELIGIBLE_CHILD_STATUSES = frozenset({"needs_attention", "failed", "stopped"})
DATABASE_IDENTITY_ERROR = "Mediaforce database identity changed during connection"
# The raw timeout an SSH quality run raised before it had its own failure kind: the whole ssh argv,
# with the ab-av1 script inside it, then Python's "timed out after N seconds".
LEGACY_REMOTE_QUALITY_TIMEOUT_RE = re.compile(
    r"^Command '\['ssh', .*ab-av1 (?:crf-search|sample-encode)\b.*\]' timed out after \d+(?:\.\d+)? seconds",
    re.DOTALL,
)
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
    requested_child_ids: tuple[str, ...] = ()
    skipped: tuple[dict[str, str], ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "parent_job_id": self.parent_job_id,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "child_ids": list(self.child_ids),
            "requested_child_ids": list(self.requested_child_ids),
            "manifest_indexes": list(self.manifest_indexes),
            "token": self.token,
            "items": [dict(item) for item in self.items],
            "skipped": [dict(item) for item in self.skipped],
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
                    "recoverable_failure_class": _recoverable_failure_class(child),
                    "previous_error": str(child.get("error") or "")[:2000],
                }
            )
            progress["recovery_receipts"] = receipts
            needs_retry_cleanup = any(
                item.get("header_only_output")
                for item in preview.items
                if set(item["manifest_indexes"]) & set(child["manifest_indexes"])
            )
            updated.update(
                {
                    "status": "retry_backoff" if needs_retry_cleanup else "queued",
                    "host": {},
                    "last_host": object_dict(child.get("last_host")),
                    "process_pid": None,
                    "leased_at": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "worker_id": None,
                    "schedule_close_deadline_at": None,
                    "retry_not_before": now if needs_retry_cleanup else None,
                    "waiting_reason": (
                        "waiting to clean header-only output before retry" if needs_retry_cleanup else None
                    ),
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
            "skipped": [dict(item) for item in preview.skipped],
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
    if parent_job["job_kind"] != "folder" or parent_job["status"] not in RECOVERABLE_PARENT_STATUSES:
        raise HTTPException(status_code=409, detail="The folder recovery parent is no longer recoverable.")
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
    selected = [child for child in children if str(child.get("job_id")) in requested_ids]
    manifest_path = str(parent_job.get("manifest_path") or "").strip()
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
    owned_indexes: set[int] = set()
    for sibling in children:
        if str(sibling.get("status") or "") not in {"queued", "retry_backoff", "running", "completed"}:
            continue
        if _valid_indexes(sibling.get("manifest_indexes"), len(items)) is None:
            raise HTTPException(
                status_code=409,
                detail="An active or completed sibling has invalid manifest indexes.",
            )
        owned_indexes.update(_strict_indexes(sibling))
    current_approval = approval_contract(parent_job, manifest)
    if not isinstance(current_approval, Mapping) or not current_approval:
        raise HTTPException(status_code=409, detail="The current production approval contract changed.")
    saved_approval = object_dict(manifest.get("selection")).get("production_approval_contract")

    # Each selected child is judged on its own: an ineligible child is skipped with its reason
    # and never holds back the others.
    skipped: dict[str, str] = {}
    indexes_by_child: dict[str, list[int]] = {}
    for child in selected:
        try:
            indexes_by_child[str(child["job_id"])] = _child_indexes(
                child, parent_job, items, owned_indexes, current_approval, saved_approval,
            )
        except HTTPException as exc:
            skipped[str(child["job_id"])] = str(exc.detail)
    claims: dict[int, list[str]] = {}
    for child_id, indexes in indexes_by_child.items():
        for index in indexes:
            claims.setdefault(index, []).append(child_id)
    for claimants in claims.values():
        if len(claimants) > 1:
            for child_id in claimants:
                skipped.setdefault(child_id, "Another selected child claims the same file.")
    rows_by_child: dict[str, list[dict[str, Any]]] = {}
    for child in selected:
        child_id = str(child["job_id"])
        if child_id in skipped:
            continue
        indexes = indexes_by_child[child_id]
        stub_tolerant = indexes if _recoverable_failure_class(child) == "unreadable_staged_output" else ()
        try:
            rows_by_child[child_id] = _validate_sources(
                connection, config, items, indexes, stub_tolerant_indexes=stub_tolerant,
            )
        except HTTPException as exc:
            skipped[child_id] = str(exc.detail)
    candidate_indexes = sorted(index for child_id in rows_by_child for index in indexes_by_child[child_id])
    eligible_result: Mapping[str, Any] = {}
    if candidate_indexes:
        eligible_result = candidate_eligibility(connection, parent_job, [items[index] for index in candidate_indexes])
        if not isinstance(eligible_result, Mapping) or not eligible_result:
            raise HTTPException(status_code=409, detail="Current candidate policy blocks recovery.")
        item_evidence = object_dict(eligible_result.get("items"))
        for child_id in list(rows_by_child):
            reason = next(
                (
                    str(blocked)
                    for index in indexes_by_child[child_id]
                    if (blocked := object_dict(item_evidence.get(str(items[index]["library_item_id"]))).get("blocked_reason"))
                ),
                None,
            )
            if reason:
                skipped[child_id] = reason
                del rows_by_child[child_id]
    eligible = [child for child in selected if str(child["job_id"]) in rows_by_child]
    skipped_payload = tuple(
        {"job_id": str(child["job_id"]), "reason": skipped[str(child["job_id"])]}
        for child in selected
        if str(child["job_id"]) in skipped
    )
    if not eligible:
        raise HTTPException(
            status_code=409,
            detail="No selected child can be retried. "
            + " ".join(f"{skip['job_id']}: {skip['reason']}" for skip in skipped_payload),
        )
    all_indexes = sorted(index for child in eligible for index in indexes_by_child[str(child["job_id"])])
    item_rows = [row for child in eligible for row in rows_by_child[str(child["job_id"])]]
    topology = {
        "approval": dict(current_approval),
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
                if str(child["job_id"]) in rows_by_child
                else {
                    "job_id": child["job_id"],
                    "prefix": child.get("prefix"),
                    "manifest_path": child.get("manifest_path"),
                    "indexes": _strict_indexes(child),
                }
            )
            for child in children
        ],
        "skipped": list(skipped_payload),
        "sources": [
            {
                "id": int(row["id"]),
                "path": str(row["source_path"]),
                "size": int(row["size_bytes"]),
                "fingerprint": str(row["fingerprint"]),
                "stat": row.get("_stat"),
                "header_only_output": row.get("_header_only_output"),
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
            "header_only_output": row.get("_header_only_output"),
        }
        for row in item_rows
    )
    return RecoveryPreview(
        parent_job_id,
        manifest_path,
        hashlib.sha256(manifest_bytes).hexdigest(),
        tuple(str(c["job_id"]) for c in eligible),
        tuple(all_indexes),
        token,
        public_items,
        tuple(str(value) for value in child_ids),
        skipped_payload,
    )


def _child_indexes(
    child: Mapping[str, Any],
    parent_job: Mapping[str, Any],
    items: list[dict[str, Any]],
    owned_indexes: Collection[int],
    current_approval: Mapping[str, Any],
    saved_approval: Any,
) -> list[int]:
    """Return the manifest indexes of one recoverable child, or raise a 409 naming why it is not."""
    if str(child.get("status") or "") not in ELIGIBLE_CHILD_STATUSES or _recoverable_failure_class(child) is None:
        raise HTTPException(status_code=409, detail="Its failure is not one that recovery can retry.")
    ownership_fields = ("process_pid", "worker_id", "leased_at", "lease_expires_at", "heartbeat_at")
    if child.get("job_kind") != "shard" or any(child.get(key) is not None for key in ownership_fields):
        raise HTTPException(status_code=409, detail="It is still held by a worker or is not part of a folder batch.")
    if str(child.get("manifest_path") or "").strip() != str(parent_job.get("manifest_path") or "").strip():
        raise HTTPException(status_code=409, detail="Child manifest paths do not match the folder parent.")
    if str(child.get("prefix") or "") != str(parent_job.get("prefix") or ""):
        raise HTTPException(status_code=409, detail="Child prefixes do not match the folder parent.")
    indexes = _valid_indexes(child.get("manifest_indexes"), len(items))
    if indexes is None:
        raise HTTPException(status_code=409, detail="A recoverable child has invalid manifest indexes.")
    if _recoverable_failure_class(child) == "unreadable_staged_output" and not any(
        str(items[index].get("staging_path") or "").strip() in str(child.get("error") or "")
        for index in indexes
        if str(items[index].get("staging_path") or "").strip()
    ):
        raise HTTPException(status_code=409, detail="A probe failure does not name the child's own staged output.")
    if any(index in owned_indexes for index in indexes):
        raise HTTPException(status_code=409, detail="A manifest item is already owned by an active or completed child.")
    if not _approval_covers(current_approval, saved_approval, [items[index] for index in indexes]):
        raise HTTPException(status_code=409, detail="The current approval no longer covers its settings.")
    return indexes


def _valid_indexes(raw: Any, item_count: int) -> list[int] | None:
    if (
        not isinstance(raw, list)
        or not raw
        or any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < item_count for value in raw)
        or len(set(raw)) != len(raw)
    ):
        return None
    return list(raw)


def _approval_covers(current: Mapping[str, Any], saved: Any, child_items: Sequence[Mapping[str, Any]]) -> bool:
    """Whether the current approval still covers the settings this child's files were queued with.

    A newer sample approval with the same policy and operator intent covers them; a changed
    policy or a change to the intent a file was resolved under does not.
    """
    if not isinstance(saved, Mapping) or not saved:
        return False
    if dict(current) == dict(saved):
        return True
    per_item_fields = {"sample_job_id", "operator_intent", "operator_intent_hash"}
    if any(current.get(key) != saved.get(key) for key in (set(current) | set(saved)) - per_item_fields):
        return False
    current_intent = object_dict(current.get("operator_intent"))
    if not current_intent:
        return False
    return all(
        (object_dict(object_dict(item.get("resolved_operator_intent")).get("request")) or object_dict(saved.get("operator_intent")))
        == current_intent
        for item in child_items
    )


def _recoverable_failure_class(child: Mapping[str, Any]) -> str | None:
    """Name the narrow failure classes that did not judge the source, policy or encode result."""
    failure_kind = str(child.get("last_failure_kind") or "")
    if str(child.get("status") or "") == "stopped":
        # An operator stop ends the child without judging it; its output was already cleaned up.
        return "operator_stopped"
    if failure_kind == "host_configuration":
        return "host_configuration"
    if failure_kind == "unreadable_output":
        # The encode removed its own header-only output and used up its automatic retries.
        return "unreadable_output"
    if failure_kind in {"storage_io", "stale_lease", "worker_restart", "controller_database_busy"}:
        # The share or the controller failed and the automatic retries ran out; nothing judged the item.
        return failure_kind
    if failure_kind == "unknown":
        # An unrecognised error used up its automatic retries; nothing showed it was certain to fail again.
        return failure_kind
    if failure_kind == RemoteQualityTimeoutError.failure_kind:
        # A quality run on another computer ran out of time; it measured nothing about the item.
        return failure_kind
    if failure_kind != "deterministic":
        return None
    error = str(child.get("error") or "")
    if is_storage_io_failure(error):
        # Recorded before storage errors had their own failure kind.
        return "storage_io"
    if is_database_busy_failure(error):
        # Recorded before controller lock contention had its own failure kind.
        return "controller_database_busy"
    if is_vmaf_model_load_failure(error):
        # Recorded before this failure was classified as the host's: the computer could not load
        # a VMAF model, which judged nothing about the item.
        return "host_configuration"
    if error == DATABASE_IDENTITY_ERROR:
        return "database_identity"
    if error.startswith("Command '[") and "ffprobe" in error and "returned non-zero exit status" in error:
        return "unreadable_staged_output"
    if LEGACY_REMOTE_QUALITY_TIMEOUT_RE.search(error):
        # Recorded before a remote quality run that ran out of time had its own failure kind.
        return RemoteQualityTimeoutError.failure_kind
    return None


def _validate_sources(
    connection: DBClient,
    config: MediaforceConfig,
    items: list[dict[str, Any]],
    indexes: list[int],
    *,
    stub_tolerant_indexes: Collection[int] = (),
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
                    candidate_stat = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    candidate == output_path
                    and index in stub_tolerant_indexes
                    and stat_module.S_ISREG(candidate_stat.st_mode)
                    and candidate_stat.st_size <= HEADER_ONLY_OUTPUT_MAX_BYTES
                ):
                    row["_header_only_output"] = {
                        "path": str(candidate),
                        "size": int(candidate_stat.st_size),
                        "mtime_ns": int(candidate_stat.st_mtime_ns),
                    }
                    continue
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
