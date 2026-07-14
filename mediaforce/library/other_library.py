from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, outerjoin, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, staged_artifacts
from mediaforce.core.type_defs import mapping_dict, object_dict
from mediaforce.library.candidate_selection import CandidateDecision, project_candidates, workflow_eligibility
from mediaforce.library.media_scopes import MediaScope, resolve_media_scope, scope_rel_path_filter
from mediaforce.library.other_profiles import (
    OTHER_FOLDER_SCOPE_MAX_ITEMS,
    OTHER_LIBRARY_CATALOG_MAX_ITEMS,
    OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS,
    other_group_scope_for_rel_path,
    other_item_profile_blocker,
    other_profile_readiness,
    other_scope_boundary_blocker,
)
from mediaforce.library.workflow_state import build_folder_workflow_states, derive_item_workflow_state

OtherMetrics = Mapping[str, Mapping[str, Any]]


def load_other_library_payload(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        include_details: bool,
        metrics_by_prefix: OtherMetrics | None = None,
        candidate_decisions: list[CandidateDecision] | None = None,
) -> dict[str, Any]:
    libraries = _other_libraries(config)
    library_by_root = {str(library["key"]): library for library in libraries}
    if not library_by_root:
        return {
            "schema_version": 1,
            "libraries": [],
            "work_units": [],
            "catalog_empty": True,
            "catalog_truncated": False,
            "catalog_item_limit": OTHER_LIBRARY_CATALOG_MAX_ITEMS,
            "catalog_work_unit_limit": OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS,
            "details_loading": not include_details,
        }
    catalog_rows = [
        mapping_dict(row)
        for row in connection.execute(
            select(library_items)
            .where(library_items.c.media_root.in_(tuple(library_by_root)))
            .where(library_items.c.status != "missing")
            .order_by(library_items.c.rel_path.asc())
            .limit(OTHER_LIBRARY_CATALOG_MAX_ITEMS + 1)
        ).mappings().fetchall()
    ]
    rows = catalog_rows[:OTHER_LIBRARY_CATALOG_MAX_ITEMS]
    item_limit_reached = len(catalog_rows) > OTHER_LIBRARY_CATALOG_MAX_ITEMS
    grouped_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scopes: dict[str, MediaScope] = {}
    for row in rows:
        scope = other_group_scope_for_rel_path(str(row["rel_path"]), config)
        if scope is None:
            continue
        scopes[scope.prefix] = scope
        grouped_rows[scope.prefix].append(row)
    ordered_prefixes = sorted(grouped_rows)
    visible_prefixes = ordered_prefixes[:OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS]
    work_unit_limit_reached = len(ordered_prefixes) > OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS
    incomplete_prefix = ""
    if item_limit_reached and rows:
        last_scope = other_group_scope_for_rel_path(str(rows[-1]["rel_path"]), config)
        next_scope = other_group_scope_for_rel_path(
            str(catalog_rows[OTHER_LIBRARY_CATALOG_MAX_ITEMS]["rel_path"]),
            config,
        )
        if last_scope is not None and next_scope is not None and last_scope.prefix == next_scope.prefix:
            incomplete_prefix = last_scope.prefix
    visible_rows = {prefix: grouped_rows[prefix] for prefix in visible_prefixes}
    decisions = (
        candidate_decisions
        if include_details and candidate_decisions is not None
        else project_candidates(
            connection,
            config,
            prefixes=[prefix for prefix in visible_prefixes if prefix != incomplete_prefix],
        )
        if include_details and visible_prefixes
        else []
    )
    decisions_by_item = {decision.item_id: decision for decision in decisions}
    workflows = (
        build_folder_workflow_states(
            connection,
            visible_prefixes,
            candidate_eligibility=workflow_eligibility(decisions),
            library_types=config.library_type_map,
        )
        if include_details and visible_prefixes
        else {}
    )
    metrics = metrics_by_prefix or {}
    work_units = []
    for prefix in visible_prefixes:
        grouped = visible_rows[prefix]
        member_count = len(grouped)
        total_size_bytes = sum(int(row.get("size_bytes") or 0) for row in grouped)
        membership_complete = prefix != incomplete_prefix
        if not membership_complete:
            member_count, total_size_bytes = connection.execute(
                select(func.count(), func.coalesce(func.sum(library_items.c.size_bytes), 0))
                .select_from(library_items)
                .where(library_items.c.status != "missing")
                .where(scope_rel_path_filter(library_items.c.rel_path, scopes[prefix]))
            ).one()
        work_units.append(_work_unit_payload(
            scope=scopes[prefix],
            rows=grouped,
            library=library_by_root[scopes[prefix].root],
            decisions_by_item=decisions_by_item,
            workflow=workflows.get(prefix),
            metrics=object_dict(metrics.get(prefix)),
            metrics_available=prefix in metrics,
            include_details=include_details,
            member_count=int(member_count),
            total_size_bytes=int(total_size_bytes or 0),
            membership_complete=membership_complete,
        ))
    work_units.sort(key=lambda unit: (str(unit["title"]).casefold(), str(unit["prefix"])))
    return {
        "schema_version": 1,
        "libraries": libraries,
        "work_units": work_units,
        "catalog_empty": not rows,
        "catalog_truncated": item_limit_reached or work_unit_limit_reached,
        "catalog_item_limit": OTHER_LIBRARY_CATALOG_MAX_ITEMS,
        "catalog_work_unit_limit": OTHER_LIBRARY_CATALOG_MAX_WORK_UNITS,
        "details_loading": not include_details,
    }


def load_other_scope_payload(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        candidate_decisions: list[CandidateDecision] | None = None,
) -> dict[str, Any] | None:
    scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
    if scope.domain != "other":
        return None
    library = config.library_definition_map.get(scope.root)
    if library is None or str(library.get("type") or "") != "other":
        return None
    if other_scope_boundary_blocker(scope, library) is not None:
        return None
    member_count, total_size_bytes = connection.execute(
            select(func.count(), func.coalesce(func.sum(library_items.c.size_bytes), 0))
            .select_from(library_items)
            .where(library_items.c.status != "missing")
            .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        ).one()
    member_count = int(member_count)
    rows = [
        mapping_dict(row)
        for row in connection.execute(
            _other_member_query(scope).limit(OTHER_FOLDER_SCOPE_MAX_ITEMS)
        ).mappings().fetchall()
    ]
    membership_complete = member_count <= OTHER_FOLDER_SCOPE_MAX_ITEMS
    decisions = (
        candidate_decisions or project_candidates(connection, config, prefixes=[scope.prefix])
        if membership_complete
        else []
    )
    decisions_by_item = {decision.item_id: decision for decision in decisions}
    eligible_item_count = sum(decision.eligible for decision in decisions)
    blocked_item_count = (
        sum(
            decision.profile_blocker is not None or decision.target_size_blocker is not None
            for decision in decisions
        )
        if membership_complete
        else member_count
    )
    blockers = [
        blocker
        for row in rows
        if (blocker := other_item_profile_blocker(row, library)) is not None
    ]
    membership_token = _membership_token(rows) if membership_complete else ""
    readiness = other_profile_readiness(
        library,
        item_count=member_count,
        blockers=blockers,
        membership_complete=membership_complete,
    )
    members = [
        _member_payload(row, decisions_by_item.get(int(row["item_id"])), library=library)
        for row in rows
    ]
    return {
        "schema_version": 1,
        "prefix": scope.prefix,
        "root": scope.root,
        "library_label": str(library.get("label") or scope.root),
        "availability": str(library.get("availability") or "browse_only"),
        "default_profile": str(library.get("default_profile") or ""),
        "policy": object_dict(library.get("policy")),
        "media_scope": scope.to_payload(),
        "item_count": member_count,
        "eligible_item_count": eligible_item_count,
        "blocked_item_count": blocked_item_count,
        "total_size_bytes": int(total_size_bytes or 0),
        "membership_complete": membership_complete,
        "membership_limit": OTHER_FOLDER_SCOPE_MAX_ITEMS,
        "membership_requires_confirmation": scope.match == "descendants" and member_count > 0,
        "membership_token": membership_token,
        "profile_readiness": readiness,
        "members": members,
    }


def other_scope_action_blocker(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        membership_token: str,
) -> dict[str, Any] | None:
    scope = resolve_media_scope(connection, prefix, library_types=config.library_type_map)
    if scope.domain != "other":
        return None
    library = config.library_definition_map.get(scope.root)
    if library is None or str(library.get("type") or "") != "other":
        return None
    boundary_blocker = other_scope_boundary_blocker(scope, library)
    if boundary_blocker is not None:
        return {
            "ok": False,
            "code": "other_scope_not_bounded",
            "message": boundary_blocker,
        }
    context = load_other_scope_payload(connection, config, prefix)
    if context is None:
        return None
    readiness = object_dict(context.get("profile_readiness"))
    if readiness.get("state") != "ready":
        return {
            "ok": False,
            "code": "other_profile_not_ready",
            "message": str(readiness.get("detail") or "This Other scope is not ready for processing."),
        }
    if (
            bool(context.get("membership_requires_confirmation"))
            and membership_token != str(context.get("membership_token") or "")
    ):
        return {
            "ok": False,
            "code": "other_scope_confirmation_required",
            "message": (
                f"Review and confirm all {context['item_count']} files in this folder scope before sampling "
                "or queueing work."
            ),
        }
    return None


def _membership_token(rows: list[dict[str, Any]]) -> str:
    membership = "\n".join(
        (
            f"{int(row['item_id'])}:{row.get('rel_path') or ''}:{row.get('fingerprint') or ''}:"
            f"{int(row.get('size_bytes') or 0)}:{int(row.get('mtime_ns') or 0)}"
        )
        for row in rows
    )
    return hashlib.sha256(membership.encode("utf-8")).hexdigest()


def _other_libraries(config: MediaforceConfig) -> list[dict[str, Any]]:
    return [
        {
            "key": str(definition["key"]),
            "label": str(definition.get("label") or definition["key"]),
            "availability": str(definition.get("availability") or "browse_only"),
            "default_profile": str(definition.get("default_profile") or ""),
            "policy": object_dict(definition.get("policy")),
        }
        for definition in config.library_definitions
        if str(definition.get("type") or "") == "other"
        and str(definition.get("availability") or "") != "disabled"
    ]


def _work_unit_payload(
        *,
        scope: MediaScope,
        rows: list[dict[str, Any]],
        library: Mapping[str, Any],
        decisions_by_item: Mapping[int, CandidateDecision],
        workflow: Any,
        metrics: dict[str, Any],
        metrics_available: bool,
        include_details: bool,
        member_count: int,
        total_size_bytes: int,
        membership_complete: bool,
) -> dict[str, Any]:
    blockers = [
        blocker
        for row in rows
        if (blocker := other_item_profile_blocker(row, library)) is not None
    ]
    membership_complete = membership_complete and member_count <= OTHER_FOLDER_SCOPE_MAX_ITEMS
    readiness = other_profile_readiness(
        library,
        item_count=member_count,
        blockers=blockers,
        membership_complete=membership_complete,
    )
    statuses = Counter(str(row.get("status") or "unknown") for row in rows)
    video_codecs = Counter(str(row.get("video_codec") or "unknown") for row in rows)
    eligible_count = sum(
        decision.eligible
        for row in rows
        if (decision := decisions_by_item.get(int(row["id"]))) is not None
    )
    return {
        "prefix": scope.prefix,
        "root": scope.root,
        "library_label": str(library.get("label") or scope.root),
        "availability": str(library.get("availability") or "browse_only"),
        "default_profile": str(library.get("default_profile") or ""),
        "policy": object_dict(library.get("policy")),
        "title": scope.title,
        "subtitle": scope.subtitle,
        "scope_label": scope.scope_label,
        "scope_mode": "exact_file" if scope.match == "exact_item" else "folder",
        "media_scope": scope.to_payload(),
        "item_count": member_count,
        "total_size_bytes": total_size_bytes,
        "eligible_item_count": eligible_count if include_details else None,
        "blocked_item_count": len(blockers) if membership_complete else member_count,
        "statuses": dict(statuses),
        "video_codecs": dict(video_codecs),
        "projected_reclaim_bytes": (
            int(metrics.get("projected_reclaim_bytes") or 0) if metrics_available else None
        ),
        "estimate_unavailable_count": (
            int(metrics.get("estimate_unavailable_count") or 0) if metrics_available else None
        ),
        "profile_readiness": readiness,
        "workflow_state": workflow.to_payload() if workflow is not None else None,
        "membership_requires_confirmation": scope.match == "descendants" and member_count > 0,
        "details_loading": not include_details,
    }


def _other_member_query(scope: MediaScope) -> Any:
    return (
        select(
            library_items,
            library_items.c.id.label("item_id"),
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.staging_path,
            staged_artifacts.c.promoted_at,
        )
        .select_from(
            outerjoin(
                library_items,
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(library_items.c.status != "missing")
        .where(scope_rel_path_filter(library_items.c.rel_path, scope))
        .order_by(library_items.c.rel_path.asc())
    )


def _member_payload(
        row: dict[str, Any],
        decision: CandidateDecision | None,
        *,
        library: Mapping[str, Any],
) -> dict[str, Any]:
    profile_blocker = other_item_profile_blocker(row, library)
    if decision is None:
        workflow_state = derive_item_workflow_state(
            row,
            encode_eligible=profile_blocker is None,
            policy_blocker=profile_blocker,
            encode_blocked=profile_blocker is not None,
        )
    else:
        workflow_state = derive_item_workflow_state(
            row,
            encode_eligible=(
                decision.eligible
                if decision.workflow_lane == "encode"
                else decision.production_included
            ),
            policy_blocker=(
                decision.production_blocker
                or (
                    decision.target_size_blocker.message
                    if decision.target_size_blocker is not None
                    else None
                )
            ),
            encode_blocked=(
                decision.profile_blocker is not None
                or decision.target_size_blocker is not None
            ),
        )
    rel_path = str(row.get("rel_path") or "")
    return {
        "item_id": int(row["item_id"]),
        "prefix": rel_path,
        "rel_path": rel_path,
        "label": PurePosixPath(rel_path).name,
        "status": str(row.get("status") or "unknown"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "duration_seconds": float(row.get("duration_seconds") or 0),
        "video_codec": str(row.get("video_codec") or "") or None,
        "width": int(row.get("width") or 0) or None,
        "height": int(row.get("height") or 0) or None,
        "profile_supported": profile_blocker is None,
        "profile_blocker": profile_blocker,
        "workflow_state": workflow_state.to_payload(),
    }
