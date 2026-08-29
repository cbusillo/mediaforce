from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, outerjoin, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, plex_item_metadata, staged_artifacts
from mediaforce.core.type_defs import int_value, mapping_dict, object_dict
from mediaforce.core.utils import filesystem_collision_key
from mediaforce.library.candidate_selection import CandidateDecision, project_candidates, workflow_eligibility
from mediaforce.library.media_scopes import resolve_media_scope, resolve_media_scopes, scope_rel_path_filter
from mediaforce.library.movie_workflow import MovieMembership, classify_movie_path, movie_item_included
from mediaforce.library.planner import build_manifest_item
from mediaforce.library.workflow_state import EncodeEligibility, build_folder_workflow_states, derive_item_workflow_state
from mediaforce.tuning.calibration_jobs import load_completed_sample_jobs_for_prefixes

MovieMetrics = Mapping[str, Mapping[str, Any]]


def _sampled_calibration_estimates(
        connection: DBClient,
        config: MediaforceConfig,
        grouped_rows: Mapping[str, list[tuple[dict[str, Any], MovieMembership]]],
) -> dict[str, dict[str, Any]]:
    members_by_title, candidates_by_member = _current_movie_members(grouped_rows, config)
    job_prefixes = {
        prefix
        for title_prefix, members in members_by_title.items()
        for prefix in (title_prefix, *(member["rel_path"] for member in members))
    }
    evidence_by_member: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for job in load_completed_sample_jobs_for_prefixes(connection, sorted(job_prefixes)):
        sample_item = object_dict(job.get("sample_item"))
        predicted_output_bytes = _positive_int(
            object_dict(object_dict(job.get("result")).get("sample_result")).get("predicted_total_size_bytes")
        )
        if predicted_output_bytes is None:
            continue
        for member in candidates_by_member.get(int_value(sample_item.get("library_item_id")), []):
            if not _sampled_evidence_matches_member(job, member):
                continue
            evidence_by_member[member["item_id"]].append(
                {
                    "output_bytes": predicted_output_bytes,
                    "finished_at": str(job.get("finished_at") or ""),
                    "created_at": str(job.get("created_at") or ""),
                }
            )

    estimates: dict[str, dict[str, Any]] = {}
    for title_prefix, included_members in members_by_title.items():
        if not included_members:
            continue
        selected_evidence = [
            _latest_sampled_evidence(evidence_by_member.get(member["item_id"], []))
            for member in included_members
        ]
        covered_member_count = sum(evidence is not None for evidence in selected_evidence)
        coverage = {
            "covered_member_count": covered_member_count,
            "required_member_count": len(included_members),
            "complete": covered_member_count == len(included_members),
        }
        if not coverage["complete"]:
            estimates[title_prefix] = coverage
            continue
        predicted_output_bytes = sum(int(evidence["output_bytes"]) for evidence in selected_evidence if evidence is not None)
        source_size_bytes = sum(int(member["source_size_bytes"]) for member in included_members)
        estimates[title_prefix] = {
            **coverage,
            "estimated_included_output_bytes": predicted_output_bytes,
            "projected_reclaim_bytes": source_size_bytes - predicted_output_bytes,
            "estimated_savings_bytes": source_size_bytes - predicted_output_bytes,
        }
    return estimates


def _current_movie_members(
        grouped_rows: Mapping[str, list[tuple[dict[str, Any], MovieMembership]]],
        config: MediaforceConfig,
) -> tuple[dict[str, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    members_by_title: dict[str, list[dict[str, Any]]] = defaultdict(list)
    members_by_item_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for title_prefix, members in grouped_rows.items():
        for row, membership in members:
            current_member = _current_movie_member(row, membership, config)
            if current_member is not None:
                current_member = {
                    **current_member,
                    "title_prefix": title_prefix,
                }
                members_by_title[title_prefix].append(current_member)
                members_by_item_id[current_member["item_id"]].append(current_member)
    return members_by_title, members_by_item_id


def _current_movie_member(
        row: dict[str, Any],
        membership: MovieMembership,
        config: MediaforceConfig,
) -> dict[str, Any] | None:
    library = _movie_library_for_membership(config, membership)
    included, _blocker = _production_inclusion(library, membership)
    if not included:
        return None
    try:
        manifest_item = build_manifest_item(row, config)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "item_id": int(row["item_id"]),
        "media_root": str(row.get("media_root") or ""),
        "rel_path": str(row.get("rel_path") or ""),
        "source_path": str(row.get("source_path") or ""),
        "source_fingerprint": str(row.get("fingerprint") or ""),
        "content_version_fingerprint": row.get("content_version_fingerprint"),
        "source_size_bytes": max(0, int(row.get("size_bytes") or 0)),
        "output_container": str(manifest_item.get("output_container") or ""),
        "resolved_policy": object_dict(manifest_item.get("resolved_policy")),
        "title_prefix": membership.title_prefix,
    }


def _movie_library_for_membership(config: MediaforceConfig, membership: MovieMembership) -> dict[str, Any]:
    return next(
        library
        for library in _movie_libraries(config)
        if str(library["key"]) == membership.root
    )


def _sampled_evidence_matches_member(
        job: dict[str, Any],
        member: dict[str, Any],
) -> bool:
    sample_item = object_dict(job.get("sample_item"))
    if str(job.get("prefix") or "") not in {member["title_prefix"], member["rel_path"]}:
        return False
    if not _sample_item_matches_member(sample_item, member):
        return False
    return (
        sample_item.get("resolved_policy") == member["resolved_policy"]
        and object_dict(job.get("policy")) == member["resolved_policy"]
        and str(sample_item.get("output_container") or "") == member["output_container"]
    )


def _sample_item_matches_member(sample_item: Mapping[str, Any], member: Mapping[str, Any]) -> bool:
    return (
        _same_int(sample_item.get("library_item_id"), member["item_id"])
        and str(sample_item.get("media_root") or "") == member["media_root"]
        and str(sample_item.get("rel_path") or "") == member["rel_path"]
        and str(sample_item.get("source_path") or "") == member["source_path"]
        and str(sample_item.get("source_fingerprint") or "") == member["source_fingerprint"]
        and sample_item.get("content_version_fingerprint") == member["content_version_fingerprint"]
    )


def _same_int(value: object, expected: int) -> bool:
    return int_value(value) == expected


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _latest_sampled_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        evidence,
        key=lambda candidate: (
            _timestamp_value(candidate["finished_at"]),
            _timestamp_value(candidate["created_at"]),
        ),
        default=None,
    )


def _timestamp_value(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def load_movie_library_payload(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        include_details: bool,
        metrics_by_prefix: MovieMetrics | None = None,
        prefixes: list[str] | None = None,
        candidate_decisions: list[CandidateDecision] | None = None,
) -> dict[str, Any]:
    libraries = _movie_libraries(config)
    library_by_root = {str(library["key"]): library for library in libraries}
    if not library_by_root:
        return {
            "schema_version": 1,
            "libraries": [],
            "titles": [],
            "catalog_empty": True,
            "details_loading": not include_details,
        }

    query = (
        select(
            library_items,
            library_items.c.id.label("item_id"),
            plex_item_metadata.c.plex_added_at,
            staged_artifacts.c.library_item_id.label("staged_library_item_id"),
            staged_artifacts.c.staging_path,
            staged_artifacts.c.staging_size_bytes,
            staged_artifacts.c.bytes_saved,
            staged_artifacts.c.validated_at,
            staged_artifacts.c.promoted_at,
        )
        .select_from(
            outerjoin(
                outerjoin(
                    library_items,
                    plex_item_metadata,
                    plex_item_metadata.c.library_item_id == library_items.c.id,
                ),
                staged_artifacts,
                staged_artifacts.c.library_item_id == library_items.c.id,
            )
        )
        .where(library_items.c.media_root.in_(tuple(library_by_root)))
        .where(library_items.c.status != "missing")
    )
    if prefixes:
        scopes = resolve_media_scopes(
            connection,
            prefixes,
            library_types=config.library_type_map,
        )
        query = query.where(or_(*(scope_rel_path_filter(library_items.c.rel_path, scope) for scope in scopes)))
    rows = connection.execute(query.order_by(library_items.c.rel_path.asc())).mappings().fetchall()

    grouped_rows: dict[str, list[tuple[dict[str, Any], MovieMembership]]] = defaultdict(list)
    for db_row in rows:
        row = mapping_dict(db_row)
        membership = classify_movie_path(str(row["rel_path"]), root=str(row["media_root"]))
        if membership is not None:
            grouped_rows[membership.title_prefix].append((row, membership))

    decisions = (
        candidate_decisions
        if include_details and candidate_decisions is not None
        else project_candidates(connection, config, prefixes=prefixes or [])
        if include_details
        else []
    )
    decisions_by_item = {decision.item_id: decision for decision in decisions}
    eligibility = workflow_eligibility(decisions)
    for grouped in grouped_rows.values():
        for row, membership in grouped:
            item_id = int(row["item_id"])
            library = library_by_root[membership.root]
            included, blocker = _production_inclusion(library, membership)
            eligibility.setdefault(item_id, EncodeEligibility(eligible=included, blocker=blocker))

    workflows = (
        build_folder_workflow_states(
            connection,
            list(grouped_rows),
            candidate_eligibility=eligibility,
            library_types=config.library_type_map,
        )
        if include_details and grouped_rows
        else {}
    )
    metrics = metrics_by_prefix or {}
    sampled_estimates = _sampled_calibration_estimates(connection, config, grouped_rows) if include_details else {}
    titles = [
        _title_payload(
            prefix,
            grouped,
            library_by_root=library_by_root,
            decisions_by_item=decisions_by_item,
            eligibility=eligibility,
            workflow=workflows.get(prefix),
            metrics=object_dict(metrics.get(prefix)),
            metrics_available=prefix in metrics,
            sampled_estimate=sampled_estimates.get(prefix),
            config=config,
            include_details=include_details,
        )
        for prefix, grouped in grouped_rows.items()
    ]
    titles.sort(key=_title_sort_key)
    return {
        "schema_version": 1,
        "libraries": libraries,
        "titles": titles,
        "catalog_empty": not titles,
        "details_loading": not include_details,
    }


def load_movie_scope_payload(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        metrics_by_prefix: MovieMetrics | None = None,
        candidate_decisions: list[CandidateDecision] | None = None,
) -> dict[str, Any] | None:
    normalized = str(prefix or "").strip().strip("/")
    scope = resolve_media_scope(
        connection,
        normalized,
        library_types=config.library_type_map,
    )
    if scope.domain != "movie":
        return None
    parts = Path(normalized).parts
    title_prefix = normalized if scope.match == "exact_item" and len(parts) == 2 else "/".join(parts[:2])
    payload = load_movie_library_payload(
        connection,
        config,
        include_details=True,
        metrics_by_prefix=metrics_by_prefix,
        prefixes=[title_prefix],
        candidate_decisions=candidate_decisions,
    )
    for title in payload["titles"]:
        if str(title.get("prefix") or "") == normalized:
            return title
        for member in title.get("members", []):
            if str(member.get("prefix") or "") == normalized:
                return {
                    **title,
                    "active_member_prefix": normalized,
                    "active_member": member,
                }
    return None


def movie_promotion_conflicts(
        config: MediaforceConfig,
        rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [row for row in rows if row.get("staging_path") and row.get("promoted_at") is None]
    destinations: dict[str, tuple[Path, list[dict[str, Any]]]] = {}
    for row in candidates:
        source_path = Path(str(row["source_path"]))
        destination = source_path.with_suffix(f".{config.output_container.lstrip('.')}")
        destination_key = filesystem_collision_key(destination)
        if destination_key not in destinations:
            destinations[destination_key] = (destination, [])
        destinations[destination_key][1].append(row)

    conflicts: list[dict[str, Any]] = []
    for destination, destination_rows in destinations.values():
        if len(destination_rows) > 1:
            conflicts.append({
                "kind": "duplicate_destination",
                "destination_path": str(destination),
                "member_prefixes": [str(row["rel_path"]) for row in destination_rows],
                "detail": "Multiple movie files would promote to the same destination path.",
            })
        for row in destination_rows:
            source_path = Path(str(row["source_path"]))
            if destination != source_path and destination.exists():
                conflicts.append({
                    "kind": "destination_exists",
                    "destination_path": str(destination),
                    "member_prefixes": [str(row["rel_path"])],
                    "detail": "The promotion destination already exists in the movie library.",
                })
    return conflicts


def _movie_libraries(config: MediaforceConfig) -> list[dict[str, Any]]:
    production_roots = set(config.source_root_map)
    libraries: list[dict[str, Any]] = []
    for definition in config.library_definitions:
        if str(definition.get("type") or "") != "movie":
            continue
        root = str(definition["key"])
        configured_availability = str(definition.get("availability") or "browse_only")
        availability = "production" if root in production_roots else configured_availability
        if availability == "disabled":
            continue
        libraries.append({
            "key": root,
            "label": str(definition.get("label") or root),
            "availability": availability,
            "default_profile": str(definition.get("default_profile") or "movie_balanced"),
            "policy": object_dict(definition.get("policy")),
        })
    return libraries


def _title_payload(
        prefix: str,
        grouped: list[tuple[dict[str, Any], MovieMembership]],
        *,
        library_by_root: dict[str, dict[str, Any]],
        decisions_by_item: dict[int, Any],
        eligibility: dict[int, EncodeEligibility],
        workflow: Any,
        metrics: dict[str, Any],
        metrics_available: bool,
        sampled_estimate: dict[str, Any] | None,
        config: MediaforceConfig,
        include_details: bool,
) -> dict[str, Any]:
    first_membership = grouped[0][1]
    library = library_by_root[first_membership.root]
    conflicts = movie_promotion_conflicts(config, [row for row, _ in grouped]) if include_details else []
    workflow_items = {
        item.item_id: item.to_payload()
        for item in (workflow.items if workflow is not None else ())
    }
    members = [
        _member_payload(
            row,
            membership,
            library=library,
            decision=decisions_by_item.get(int(row["item_id"])),
            eligibility=eligibility[int(row["item_id"])],
            workflow_item=workflow_items.get(int(row["item_id"])),
            conflicts=conflicts,
            include_details=include_details,
        )
        for row, membership in grouped
    ]
    role_counts = Counter(str(member["role"]) for member in members)
    included_members = [member for member in members if member["included_by_default"]]
    feature_members = [member for member in members if member["role"] == "feature"]
    ages = [
        age
        for member in included_members
        if isinstance((age := member.get("age")), Mapping) and age.get("timestamp")
    ]
    title_age = min(ages, key=lambda age: str(age["timestamp"])) if ages else None
    estimate_unavailable_count = (
        int(metrics.get("estimate_unavailable_count") or 0)
        if metrics_available
        else 1
    )
    projected_reclaim = (
        None
        if estimate_unavailable_count > 0
        else int(metrics.get("projected_reclaim_bytes") or 0)
    )
    known_saved = int(metrics.get("known_saved_bytes") or 0)
    estimated_savings = (
        None
        if estimate_unavailable_count > 0
        else int(metrics.get("estimated_savings_bytes") or 0)
    )
    sampled_complete = bool(sampled_estimate and sampled_estimate.get("complete"))
    use_sampled_estimate = sampled_complete and estimate_unavailable_count > 0
    total_size_bytes = sum(int(member["size_bytes"]) for member in members)
    included_size_bytes = sum(int(member["size_bytes"]) for member in included_members)
    sampled_output = None
    if use_sampled_estimate and sampled_estimate is not None:
        sampled_output = (
            int(sampled_estimate["estimated_included_output_bytes"])
            + total_size_bytes
            - included_size_bytes
        )
        projected_reclaim = int(sampled_estimate["projected_reclaim_bytes"])
        estimated_savings = int(sampled_estimate["estimated_savings_bytes"])
    workflow_payload = workflow.to_payload() if workflow is not None else None
    workflow_payload = adapt_movie_workflow_payload(workflow_payload, library=library, members=members)
    return {
        "prefix": prefix,
        "root": first_membership.root,
        "library_label": library["label"],
        "availability": library["availability"],
        "policy": library["policy"],
        "title": first_membership.title,
        "scope_mode": first_membership.scope_mode,
        "item_count": len(members),
        "feature_count": role_counts["feature"],
        "edition_count": len(feature_members),
        "extra_count": role_counts["extra"],
        "uncertain_count": role_counts["uncertain"],
        "included_item_count": len(included_members),
        "total_size_bytes": total_size_bytes,
        "included_size_bytes": included_size_bytes,
        "projected_reclaim_bytes": projected_reclaim if include_details else None,
        "known_saved_bytes": known_saved if include_details else None,
        "estimated_savings_bytes": estimated_savings if include_details else None,
        "estimated_output_bytes": sampled_output if include_details else None,
        "savings_confidence": (
            "estimated" if use_sampled_estimate
            else "unavailable" if estimate_unavailable_count > 0
            else "measured" if known_saved > 0 and estimated_savings == 0
            else "estimated" if projected_reclaim and projected_reclaim > 0
            else "unavailable"
        ) if include_details else "pending",
        "estimate_provenance": (
            "sampled_calibration" if use_sampled_estimate
            else "measured" if known_saved > 0 and estimated_savings == 0
            else "projected" if estimate_unavailable_count == 0
            else "unavailable"
        ) if include_details else "pending",
        "estimate_coverage": {
            "covered_included_members": int(sampled_estimate.get("covered_member_count") or 0),
            "required_included_members": int(sampled_estimate.get("required_member_count") or len(included_members)),
            "complete": sampled_complete,
        } if include_details else None,
        "age": title_age if include_details else None,
        "workflow_state": workflow_payload if include_details else None,
        "review_badge": {
            "label": metrics.get("review_badge_label"),
            "tone": metrics.get("review_badge_tone"),
            "detail": metrics.get("review_badge_detail"),
        } if include_details else None,
        "promotion_conflicts": conflicts if include_details else [],
        "members": members,
        "details_loading": not include_details,
    }


def _member_payload(
        row: dict[str, Any],
        membership: MovieMembership,
        *,
        library: dict[str, Any],
        decision: Any,
        eligibility: EncodeEligibility,
        workflow_item: dict[str, Any] | None,
        conflicts: list[dict[str, Any]],
        include_details: bool,
) -> dict[str, Any]:
    included, blocker = _production_inclusion(library, membership)
    age = decision.age.to_payload() if decision is not None else _row_age(row)
    member_conflicts = [
        conflict
        for conflict in conflicts
        if membership.rel_path in conflict["member_prefixes"]
    ]
    if include_details and workflow_item is None:
        workflow_item = derive_item_workflow_state(
            row,
            encode_eligible=eligibility.eligible,
            policy_blocker=eligibility.blocker,
            encode_blocked=eligibility.blocked,
        ).to_payload()
    return {
        **membership.to_payload(),
        "item_id": int(row["item_id"]),
        "status": str(row.get("status") or "unknown"),
        "size_bytes": max(0, int(row.get("size_bytes") or 0)),
        "duration_seconds": row.get("duration_seconds"),
        "video_codec": row.get("video_codec"),
        "included_by_default": included,
        "selection_blocker": blocker,
        "exact_action_available": library["availability"] == "production",
        "age": age if include_details else None,
        "workflow_state": workflow_item if include_details else None,
        "promotion_conflicts": member_conflicts if include_details else [],
        "details_loading": not include_details,
    }


def _production_inclusion(
        library: Mapping[str, Any],
        membership: MovieMembership,
) -> tuple[bool, str | None]:
    if str(library.get("availability") or "") != "production":
        return False, "This movie library is browse only. Change its availability to Production before processing."
    return movie_item_included(membership, object_dict(library.get("policy")), explicit_exact=False)


def adapt_movie_workflow_payload(
        payload: dict[str, Any] | None,
        *,
        library: Mapping[str, Any],
        members: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if payload is None:
        return None
    if str(library.get("availability") or "") != "production":
        return {
            **payload,
            "state": "browse_only",
            "primary_lane": "none",
            "label": "Browse only",
            "tone": "idle",
            "detail": "This movie library is visible but production actions are disabled.",
            "next_action": {
                "kind": "none",
                "label": "Browse title",
                "enabled": False,
                "target_prefix": payload["prefix"],
            },
        }
    if payload.get("state") == "held":
        explicit_count = sum(member["role"] in {"extra", "uncertain"} for member in members)
        return {
            **payload,
            "state": "explicit_selection_required",
            "label": "Explicit selection required",
            "detail": f"{explicit_count} movie file(s) require an exact-file action.",
            "next_action": {
                "kind": "review_scope",
                "label": "Review title files",
                "enabled": True,
                "target_prefix": payload["prefix"],
            },
        }
    return payload


def _row_age(row: Mapping[str, Any]) -> dict[str, Any]:
    plex_added_at = _parse_timestamp(row.get("plex_added_at"))
    if plex_added_at is not None:
        return {"timestamp": plex_added_at.isoformat(timespec="seconds"), "source": "plex"}
    discovered_at = _parse_timestamp(row.get("discovered_at"))
    if discovered_at is not None:
        return {"timestamp": discovered_at.isoformat(timespec="seconds"), "source": "mediaforce_discovered"}
    mtime_ns = int(row.get("mtime_ns") or 0)
    if mtime_ns > 0:
        timestamp = datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC)
        return {"timestamp": timestamp.isoformat(timespec="seconds"), "source": "filesystem_mtime"}
    return {"timestamp": None, "source": "unknown"}


def _parse_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _title_sort_key(title: Mapping[str, Any]) -> tuple[Any, ...]:
    workflow = object_dict(title.get("workflow_state"))
    lane_order = {
        "attention": 0,
        "processing": 1,
        "validate": 2,
        "promote": 3,
        "encode": 4,
        "none": 5,
        "complete": 6,
    }
    return (
        lane_order.get(str(workflow.get("primary_lane") or "none"), 5),
        str(title.get("title") or "").casefold(),
        str(title.get("prefix") or ""),
    )
