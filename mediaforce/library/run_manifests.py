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
from mediaforce.library.workflow_state import ENCODE_CANDIDATE_STATUSES
from mediaforce.library.workflow_state import derive_item_workflow_state
from mediaforce.core.type_defs import object_dict
from mediaforce.library.candidate_selection import candidate_rank_key, project_candidates
from mediaforce.library.media_scopes import resolve_media_scope, resolve_media_scopes, scope_rel_path_filter
from mediaforce.library.planner import build_manifest_item, recommend_item
from mediaforce.library.scanner import scan_library


def _selection_recommendation(
        row: dict[str, Any],
        config: MediaforceConfig,
) -> tuple[str, float, str]:
    complete_policy = all(
        isinstance(config.raw.get(section), dict)
        for section in ("video", "audio", "subtitle", "planning")
    )
    if complete_policy:
        recommendation = recommend_item(row, config)
        return recommendation.bucket, recommendation.score, recommendation.reason
    return (
        str(row.get("recommendation") or ""),
        float(row.get("priority_score") or 0.0),
        str(row.get("recommendation_reason") or ""),
    )


def select_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        statuses: list[str],
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
        require_encode_lane: bool = False,
        manual_override_prefix: str | None = None,
) -> list[dict[str, Any]]:
    if require_encode_lane:
        projected = project_candidates(
            connection,
            config,
            prefixes=prefixes,
            statuses=set(statuses),
            manual_override_prefix=manual_override_prefix,
        )
        ranked: list[tuple[Any, dict[str, Any]]] = []
        for decision in projected:
            if not decision.eligible:
                continue
            recommendation_bucket, recommendation_score, recommendation_reason = _selection_recommendation(
                decision.row,
                config,
            )
            if buckets and recommendation_bucket not in buckets:
                continue
            ranked.append(
                (
                    candidate_rank_key(
                        decision,
                        recommendation_score=recommendation_score,
                        size_bytes=int(decision.row["size_bytes"] or 0),
                    ),
                    {
                        **decision.row,
                        "recommendation": recommendation_bucket,
                        "priority_score": recommendation_score,
                        "recommendation_reason": recommendation_reason,
                        "_selection_decision": decision,
                    },
                )
            )
        ranked.sort(key=lambda value: value[0])
        rows = [row for _, row in ranked]
        if limit is not None:
            rows = rows[:limit]
        for position, row in enumerate(rows, start=1):
            decision = row.pop("_selection_decision")
            row["selection_provenance"] = decision.provenance(rank_position=position)
        return rows

    joined_tables = outerjoin(
        library_items,
        staged_artifacts,
        staged_artifacts.c.library_item_id == library_items.c.id,
    )
    query = (
        select(
            library_items,
            library_items.c.id.label("item_id"),
            staged_artifacts.c.staging_size_bytes,
            staged_artifacts.c.staging_path,
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.promoted_at,
            staged_artifacts.c.validated_at,
            staged_artifacts.c.quality_metric,
            staged_artifacts.c.quality_score,
            staged_artifacts.c.validation_json,
        )
        .select_from(joined_tables)
        .where(library_items.c.status.in_(statuses))
    )
    if prefixes:
        scopes = resolve_media_scopes(connection, prefixes)
        query = query.where(or_(*(scope_rel_path_filter(library_items.c.rel_path, scope) for scope in scopes)))
    query = query.order_by(library_items.c.priority_score.desc(), library_items.c.size_bytes.desc())
    if limit is not None:
        query = query.limit(limit)
    rows = [object_dict(row) for row in connection.execute(query).mappings().fetchall()]
    if buckets:
        rows = [row for row in rows if recommend_item(row, config).bucket in buckets]
    return rows


def select_encode_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefixes: list[str],
        limit: int | None,
        buckets: list[str] | None = None,
        manual_override_prefix: str | None = None,
) -> list[dict[str, Any]]:
    return select_candidates(
        connection,
        config,
        statuses=sorted(ENCODE_CANDIDATE_STATUSES),
        prefixes=prefixes,
        limit=limit,
        buckets=buckets,
        require_encode_lane=True,
        manual_override_prefix=manual_override_prefix,
    )


def build_run_manifest(rows: list[dict[str, Any]], config: MediaforceConfig) -> dict[str, Any]:
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    run_id = uuid.uuid4().hex[:12]
    items: list[dict[str, Any]] = []
    for row in rows:
        item = build_manifest_item(row, config)
        provenance = row.get("selection_provenance")
        if isinstance(provenance, dict):
            item["selection_provenance"] = provenance
        items.append(item)
    selection = {
        "schema_version": 1,
        "selector": (
            "library_lifecycle_v1"
            if items and all(isinstance(item.get("selection_provenance"), dict) for item in items)
            else "candidate_selection_v1"
        ),
        "item_count": len(items),
        "items": [
            {
                "library_item_id": item.get("library_item_id"),
                "source_path": item.get("source_path"),
                "selection_provenance": item.get("selection_provenance"),
            }
            for item in items
        ],
    }
    return {
        "run_id": run_id,
        "created_at": now,
        "config_path": str(config.paths.config_path),
        "db_path": str(config.paths.db_path),
        "staging_root": str(config.staging_root),
        "output_container": config.output_container,
        "selection": selection,
        "items": items,
    }


def write_manifest(
        connection: DBClient,
        config: MediaforceConfig,
        manifest: dict[str, Any],
        output_path: Path | None = None,
) -> Path:
    config.paths.run_manifest_dir.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        manifest_path = config.paths.run_manifest_dir / f"run-{manifest['run_id']}.json"
    else:
        manifest_path = output_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    selection = object_dict(manifest.get("selection")) or {
        "schema_version": 1,
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
        manual_override_prefix: str | None = None,
) -> tuple[dict[str, Any], Path]:
    if scan_first:
        scan_library(connection, config, prefixes=[prefix])
    scope = resolve_media_scope(connection, prefix)
    rows = select_encode_candidates(
        connection,
        config,
        prefixes=[prefix],
        limit=limit,
        manual_override_prefix=manual_override_prefix,
    )
    manifest = build_run_manifest(rows, config)
    manifest["selection"]["media_scope"] = scope.to_payload()
    manifest_path = write_manifest(connection, config, manifest)
    return manifest, manifest_path
