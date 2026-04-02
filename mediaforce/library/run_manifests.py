import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam
from sqlalchemy import or_
from sqlalchemy import outerjoin
from sqlalchemy import select
from sqlalchemy import update

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.db_tables import run_manifests
from mediaforce.core.db_tables import staged_artifacts
from mediaforce.library.planner import build_manifest_item, recommend_item
from mediaforce.library.scanner import scan_library


def select_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        statuses: list[str],
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
) -> list[dict[str, Any]]:
    joined_tables = outerjoin(
        library_items,
        staged_artifacts,
        staged_artifacts.c.library_item_id == library_items.c.id,
    )
    query = (
        select(
            library_items,
            staged_artifacts.c.staging_size_bytes,
            staged_artifacts.c.staging_path,
            staged_artifacts.c.quality_metric,
            staged_artifacts.c.quality_score,
            staged_artifacts.c.validation_json,
        )
        .select_from(joined_tables)
        .where(library_items.c.status.in_(statuses))
    )
    if prefixes:
        query = query.where(or_(*(library_items.c.rel_path.like(f"{prefix}%") for prefix in prefixes)))
    query = query.order_by(library_items.c.priority_score.desc(), library_items.c.size_bytes.desc())
    if limit is not None:
        query = query.limit(limit)
    rows = [dict(row) for row in connection.execute(query).mappings().fetchall()]
    if buckets:
        rows = [row for row in rows if recommend_item(row, config).bucket in buckets]
    return rows


def build_run_manifest(rows: list[dict[str, Any]], config: MediaforceConfig) -> dict[str, Any]:
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    run_id = uuid.uuid4().hex[:12]
    items = [build_manifest_item(row, config) for row in rows]
    return {
        "run_id": run_id,
        "created_at": now,
        "config_path": str(config.paths.config_path),
        "db_path": str(config.paths.db_path),
        "staging_root": str(config.staging_root),
        "output_container": config.output_container,
        "items": items,
    }


def write_manifest(
        connection: DBClient,
        config: MediaforceConfig,
        manifest: dict[str, Any],
        output_path: Path | None = None,
) -> Path:
    config.paths.run_manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_path or config.paths.run_manifest_dir / f"run-{manifest['run_id']}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    selection = {
        "item_count": len(manifest["items"]),
        "sources": [item["source_path"] for item in manifest["items"]],
    }
    connection.execute(
        run_manifests.insert().values(
            run_id=manifest["run_id"],
            created_at=manifest["created_at"],
            output_path=str(manifest_path),
            selection_json=json.dumps(selection, separators=(",", ":")),
            item_count=len(manifest["items"]),
        )
    )
    if manifest["items"]:
        connection.execute(
            update(library_items)
            .where(library_items.c.source_path == bindparam("source_path_param"))
            .values(status="planned", updated_at=bindparam("updated_at_param")),
            [
                {
                    "updated_at_param": manifest["created_at"],
                    "source_path_param": item["source_path"],
                }
                for item in manifest["items"]
            ],
        )
    return manifest_path


def create_folder_manifest(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefix: str,
        limit: int | None = None,
        scan_first: bool = False,
) -> tuple[dict[str, Any], Path]:
    if scan_first:
        scan_library(connection, config, prefixes=[prefix])
    rows = select_candidates(connection, config, statuses=["discovered", "planned", "validated"], prefixes=[prefix],
                             limit=limit)
    manifest = build_run_manifest(rows, config)
    manifest_path = write_manifest(connection, config, manifest)
    return manifest, manifest_path
