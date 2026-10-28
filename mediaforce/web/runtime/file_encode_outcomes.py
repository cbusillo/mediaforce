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
    holds_file: bool
    writer_cleanup_pending: bool


def file_encode_outcomes(
        connection: DBClient, files: Mapping[int, str], *, config: MediaforceConfig | None = None,
        display_progress_fallback: bool = False,
) -> dict[int, FileEncodeOutcome]:
    if not files:
        return {}
    remaining = dict(files)
    outcomes: dict[int, FileEncodeOutcome] = {}
    parents = set(connection.execute(select(encode_jobs.c.parent_job_id).where(
        encode_jobs.c.parent_job_id.is_not(None),
    )).scalars())
    rows = connection.execute(select(encode_jobs, retained_encode_writer_filter().label("retained_writer")).where(
        encode_jobs.c.status.in_((*ACTIVE_ENCODE_JOB_STATUSES, *RECENT_ENCODE_JOB_STATUSES)),
    ).order_by(encode_jobs.c.created_at.desc(), literal_column("rowid").desc())).mappings()
    for row in rows:
        if row["job_kind"] == "folder" and row["job_id"] in parents:
            continue
        if not remaining and not row["retained_writer"]:
            continue
        job = {**row, "progress": json.loads(row["progress_json"] or "{}"),
               "manifest_indexes": json.loads(row["manifest_indexes_json"]) if row["manifest_indexes_json"] else None,
               "host": object_dict(json.loads(row["host_json"] or "{}"))}
        custody_hold = bool(row["retained_writer"]) and encode_writer_cleanup_pending(config, job)
        reason, _label, needs_owner = unfinished_child_reason(job) if job["status"] != "completed" else ("", "", False)
        held = custody_hold or needs_owner or reason == "size_exception_declined"
        references = encode_run_manifest_files(job, strict_indexes=True)
        display_paths = (encode_run_rel_paths(job) if display_progress_fallback and not references
                         and job["status"] != "completed" else [])
        if not references and not held and not display_paths:
            continue
        candidates = files if custody_hold else remaining
        if references:
            referenced_ids = {item.library_item_id for item in references if item.library_item_id > 0}
            legacy_paths = {item.rel_path for item in references if item.library_item_id <= 0}
            affected = {item_id for item_id, path in candidates.items() if item_id in referenced_ids or path in legacy_paths}
        elif display_paths:
            # A page may show the file named by an unfinished attempt's progress. That hint cannot
            # narrow a safety hold when automatic admission cannot prove its saved membership.
            affected = {item_id for item_id, path in candidates.items() if path in display_paths}
        else:
            affected = {item_id for item_id, path in candidates.items() if path_matches_scope(path, str(job["prefix"] or ""))}
        for item_id in affected:
            remaining.pop(item_id, None)
            # Retained custody survives a later completion; among those holds keep the newest one.
            if item_id not in outcomes or not outcomes[item_id].writer_cleanup_pending:
                outcomes[item_id] = FileEncodeOutcome(job, held, custody_hold)
    return outcomes
