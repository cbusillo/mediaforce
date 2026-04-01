import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.library.planner import build_manifest_item, recommend_item
from mediaforce.library.scanner import scan_library


def select_candidates(
        connection: sqlite3.Connection,
        config: MediaforceConfig,
        *,
        statuses: list[str],
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT
            library_items.*,
            staged_artifacts.staging_size_bytes,
            staged_artifacts.staging_path,
            staged_artifacts.quality_metric,
            staged_artifacts.quality_score,
            staged_artifacts.validation_json
        FROM library_items
        LEFT JOIN staged_artifacts ON staged_artifacts.library_item_id = library_items.id
        WHERE library_items.status IN ({statuses})
    """.format(
        statuses=",".join("?" for _ in statuses)
    )
    params: list[Any] = list(statuses)
    if prefixes:
        query += " AND (" + " OR ".join("rel_path LIKE ?" for _ in prefixes) + ")"
        params.extend([f"{prefix}%" for prefix in prefixes])
    query += " ORDER BY priority_score DESC, size_bytes DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    rows = [dict(row) for row in connection.execute(query, params).fetchall()]
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
        connection: sqlite3.Connection,
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
        "INSERT INTO run_manifests(run_id, created_at, output_path, selection_json, item_count) VALUES (?, ?, ?, ?, ?)",
        (
            manifest["run_id"],
            manifest["created_at"],
            str(manifest_path),
            json.dumps(selection, separators=(",", ":")),
            len(manifest["items"]),
        ),
    )
    if manifest["items"]:
        connection.executemany(
            "UPDATE library_items SET status = 'planned', updated_at = ? WHERE source_path = ?",
            [(manifest["created_at"], item["source_path"]) for item in manifest["items"]],
        )
    return manifest_path


def create_folder_manifest(
        connection: sqlite3.Connection,
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
