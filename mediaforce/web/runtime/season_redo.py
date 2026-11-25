import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient, DBRow, open_db
from mediaforce.core.db_tables import encode_jobs, library_items, season_redos, staged_artifacts
from mediaforce.core.type_defs import object_dict
from mediaforce.core.utils import timestamp
from mediaforce.encoding.encode_queue import QUEUED_ENCODE_JOB_STATUSES
from mediaforce.library.media_scopes import scope_descendant_filter, scope_rel_path_filter
from mediaforce.library.planner import build_manifest_item
from mediaforce.web.runtime.left_out_files import LeftOutFile
from mediaforce.web.runtime.production_approval import EffectiveApproval, LoadCalibrationState, effective_production_approval, production_approval_contract


def cartoon_redo_approval(config: MediaforceConfig, prefix: str) -> EffectiveApproval | None:
    from mediaforce.web.runtime.kind_approvals import CARTOON, CARTOON_TARGET_MB, load_kind_approval

    calibration = load_kind_approval(config, CARTOON)
    if not calibration:
        return None
    contract = production_approval_contract(calibration)
    approval = EffectiveApproval(prefix, calibration, contract, kind=CARTOON)
    goal = object_dict(object_dict(object_dict(contract).get("operator_intent")).get("size_goal"))
    if (
        approval.current
        and goal.get("mode") == "normalized"
        and goal.get("value_mb") == CARTOON_TARGET_MB
        and goal.get("reference_runtime_minutes") == 45
    ):
        return approval
    return None


def run_approval(config: MediaforceConfig, item: Mapping[str, Any], load: LoadCalibrationState) -> EffectiveApproval | None:
    if prefix := item.get("redo_season"):
        return cartoon_redo_approval(config, prefix)
    return effective_production_approval(config, str(item.get("rel_path") or ""), load)


def stop_redo_season(config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        state = connection.execute(select(season_redos.c.state).where(season_redos.c.prefix == prefix)).scalar_one_or_none()
        if state not in {"marked", "stopped"}:
            return {"ok": False, "message": "This season has no pending redo to stop for."}
        now = timestamp()
        connection.execute(
            update(encode_jobs)
            .where(
                scope_rel_path_filter(encode_jobs.c.prefix, prefix),
                (
                    encode_jobs.c.status.in_(QUEUED_ENCODE_JOB_STATUSES)
                    | ((encode_jobs.c.job_kind == "folder") & (encode_jobs.c.status == "running"))
                ),
            )
            .values(status="stopped", terminal_reason="season_stopped", finished_at=now, updated_at=now)
        )
        connection.execute(update(season_redos).where(season_redos.c.prefix == prefix).values(state="stopped"))
    return {"ok": True}


def remaining_redo_files(connection: DBClient, prefix: str, redo: DBRow) -> dict[str, Any]:
    if redo["remaining_json"] is not None:
        return json.loads(redo["remaining_json"])
    rows = connection.execute(
        select(
            library_items.c.id,
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.archived_source_path,
            staged_artifacts.c.manifest_path,
            staged_artifacts.c.item_index,
        )
        .outerjoin(staged_artifacts, staged_artifacts.c.library_item_id == library_items.c.id)
        .where(
            scope_descendant_filter(library_items.c.rel_path, prefix),
            library_items.c.missing_since.is_(None),
        )
    ).mappings()
    return {
        str(row["id"]): (
            {"archive": row["archived_source_path"], "plan": row["manifest_path"], "index": row["item_index"]}
            if row["promoted_at"]
            else None
        )
        for row in rows
    }


def archived_redo_items(
    connection: DBClient, config: MediaforceConfig, prefix: str, remaining: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[LeftOutFile]]:
    items, left_out = [], []
    for key, original_stage in remaining.items():
        if original_stage is None:
            continue
        item_id = int(key)
        row = dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
        try:
            old = json.loads(Path(original_stage["plan"]).read_text(encoding="utf-8"))["items"][original_stage["index"]]
            backup = Path(original_stage["archive"] or "").resolve()
            relative_backup = backup.relative_to(config.archive_root.resolve())
            if (
                old["library_item_id"] != item_id
                or not old["rel_path"].startswith(prefix + "/")
                or not backup.is_file()
                or not old.get("content_version_fingerprint")
            ):
                raise ValueError("Original backup or its saved measurements are missing.")
            original = {
                **row,
                "size_bytes": old["source_size_bytes"],
                "duration_seconds": old["duration_seconds"],
                "fingerprint": old["source_fingerprint"],
                "video_codec": old["video_codec"],
                "video_bitrate": old.get("video_bitrate"),
                "width": old["width"],
                "height": old["height"],
                "container": old["container"],
                "audio_summary_json": json.dumps(old["audio_summary"]),
                "subtitle_summary_json": json.dumps(old["subtitle_summary"]),
                "attachment_summary_json": json.dumps(old.get("attachment_summary")),
                "cadence_summary_json": json.dumps(old.get("cadence_summary")),
                "media_fingerprint_json": json.dumps(old.get("media_fingerprint")),
            }
            item = build_manifest_item(original, config)
            item.update(
                source_fingerprint=row["fingerprint"],
                redo_backup=relative_backup.as_posix(),
                redo_library_size=row["size_bytes"],
                redo_original_path=old["source_path"],
                redo_original_fingerprint=old["content_version_fingerprint"],
                redo_cadence_fingerprint=old["source_fingerprint"],
            )
            items.append(item)
        except (OSError, KeyError, IndexError, ValueError, TypeError) as error:
            left_out.append(
                LeftOutFile(
                    item_id,
                    row["rel_path"],
                    "original_unavailable",
                    f"The original backup or its saved measurements could not be read: {error}",
                )
            )
    return items, left_out
