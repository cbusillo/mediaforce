"""The same per-file encode outcome drives automatic admission and the item's page."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import literal_column, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs
from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.encode_queue import ACTIVE_ENCODE_JOB_STATUSES, RECENT_ENCODE_JOB_STATUSES, encode_run_manifest_files, encode_run_rel_paths
from mediaforce.library.media_scopes import path_matches_scope
from mediaforce.web.runtime.encode_runtime import encode_writer_cleanup_pending, retained_encode_writer_filter, unfinished_child_reason


@dataclass(frozen=True, slots=True)
class FileEncodeOutcome:
    job: dict[str, Any]
    hold_jobs: tuple[dict[str, Any], ...]

    @property
    def holds_file(self) -> bool:
        return bool(self.hold_jobs)


def file_encode_outcomes(
        connection: DBClient, files: Mapping[int, str], *, config: MediaforceConfig | None = None,
        display_progress_fallback: bool = False,
) -> dict[int, FileEncodeOutcome]:
    if not files:
        return {}
    remaining = set(files)
    current: dict[int, tuple[dict[str, Any], bool]] = {}
    holds: dict[int, list[dict[str, Any]]] = {}
    parents = set(connection.execute(select(encode_jobs.c.parent_job_id).where(
        encode_jobs.c.parent_job_id.is_not(None),
    ).distinct()).scalars())
    rows = connection.execute(select(encode_jobs, retained_encode_writer_filter().label("retained_writer")).where(
        encode_jobs.c.status.in_((*ACTIVE_ENCODE_JOB_STATUSES, *RECENT_ENCODE_JOB_STATUSES)),
    ).order_by(encode_jobs.c.created_at.desc(), literal_column("rowid").desc())).mappings()
    for row in rows:
        if row["job_kind"] == "folder" and row["job_id"] in parents:
            continue
        job = {**row, "manifest_indexes": json.loads(row["manifest_indexes_json"]) if row["manifest_indexes_json"] else None}
        references = encode_run_manifest_files(job, strict_indexes=True)
        if references:
            referenced_ids = {item.library_item_id for item in references if item.library_item_id > 0}
            legacy_paths = {item.rel_path for item in references if item.library_item_id <= 0}
            affected = {item_id for item_id, path in files.items() if item_id in referenced_ids or path in legacy_paths}
            if not affected:
                continue
        else:
            if job["status"] == "completed":
                continue
            affected = {item_id for item_id, path in files.items() if path_matches_scope(path, str(job["prefix"] or ""))}
            if not affected and not display_progress_fallback:
                continue
        # Most historical runs cannot match this page. Decode their state only after membership can match.
        job.update(progress=json.loads(row["progress_json"] or "{}"), host=object_dict(json.loads(row["host_json"] or "{}")))
        custody_hold = bool(row["retained_writer"]) and encode_writer_cleanup_pending(config, job)
        reason, _label, needs_owner = (unfinished_child_reason(job) if job["status"] not in {"completed", "running"}
                                      else ("", "", False))
        held = custody_hold or needs_owner or reason == "size_exception_declined"
        display_paths = (encode_run_rel_paths(job) if display_progress_fallback and not references and not held
                         and job["status"] != "completed" else [])
        if not references and not held and not display_paths:
            continue
        admission_affected = affected if references or held else set()
        for item_id in admission_affected:
            if (item_id in remaining and held) or custody_hold:
                holds.setdefault(item_id, []).append(job)
            remaining.discard(item_id)
        if display_paths:
            # Progress can identify visible activity, but cannot narrow a held scope.
            affected = {item_id for item_id, path in files.items() if path in display_paths}
        specific = bool(references or display_paths)
        for item_id in affected:
            previous = current.get(item_id)
            if (previous is None
                    or (not previous[1] and specific and (job["status"] in ACTIVE_ENCODE_JOB_STATUSES or held))
                    or (custody_hold and previous[0]["status"] == "completed")):
                current[item_id] = (job, specific)
    return {item_id: FileEncodeOutcome(job, tuple(holds.get(item_id, ())))
            for item_id, (job, _specific) in current.items()}
