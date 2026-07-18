import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import or_, outerjoin, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, plex_item_metadata, series_metadata, staged_artifacts
from mediaforce.core.type_defs import object_dict
from mediaforce.library.library_settings import normalize_library_policy, normalize_library_type
from mediaforce.library.media_scopes import (
    MediaScope,
    normalize_scope_prefix,
    path_matches_scope,
    resolve_media_scopes,
    scope_rel_path_filter,
)
from mediaforce.library.movie_workflow import classify_movie_path, movie_item_included
from mediaforce.library.other_profiles import (
    OTHER_FOLDER_SCOPE_MAX_ITEMS,
    other_group_scope_for_rel_path,
    other_item_profile_blocker,
)
from mediaforce.library.planner import build_manifest_item
from mediaforce.library.workflow_state import ENCODE_CANDIDATE_STATUSES, EncodeEligibility, derive_item_workflow_state
from mediaforce.tuning.stream_budget import (
    StreamBudgetLedger,
    StreamBudgetProjectionBlocker,
    stream_budget_projection_blocker,
)

LifecycleMode = Literal["auto", "on", "off"]
ProviderState = Literal["active", "ended", "unknown", "stale"]
SEASON_PATTERN = re.compile(r"^Season\s+0*(\d+)$", re.IGNORECASE)
OVERRIDEABLE_HOLD_CODES = frozenset({"recent_acquisition", "current_season"})


@dataclass(frozen=True, slots=True)
class SeasonIdentity:
    series_prefix: str
    season_prefix: str
    season_number: int | None
    label: str
    is_special: bool
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class AgeEvidence:
    timestamp: datetime | None
    source: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(timespec="seconds") if self.timestamp is not None else None,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class HoldReason:
    code: str
    label: str
    detail: str
    release_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "detail": self.detail,
            "release_at": self.release_at.isoformat(timespec="seconds") if self.release_at is not None else None,
        }


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    row: dict[str, Any]
    workflow_lane: str
    season: SeasonIdentity | None
    policy_mode: LifecycleMode
    provider_state: ProviderState
    provider_status: str | None
    provider_observed_at: datetime | None
    is_current_season: bool
    age: AgeEvidence
    season_rank_age: AgeEvidence
    season_activity_at: datetime | None
    hold_reasons: tuple[HoldReason, ...]
    manual_override: bool
    media_role: str | None
    production_included: bool
    production_blocker: str | None
    profile_blocker: str | None
    target_size_blocker: StreamBudgetProjectionBlocker | None
    ranking_mode: str
    media_domain: str

    @property
    def item_id(self) -> int:
        return int(self.row["item_id"])

    @property
    def eligible(self) -> bool:
        return (
            self.workflow_lane == "encode"
            and self.production_included
            and self.target_size_blocker is None
            and (not self.hold_reasons or self.override_applied)
        )

    @property
    def override_applied(self) -> bool:
        return self.manual_override and all(reason.code in OVERRIDEABLE_HOLD_CODES for reason in self.hold_reasons)

    def provenance(self, *, rank_position: int | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "eligible": self.eligible,
            "workflow_lane": self.workflow_lane,
            "policy_mode": self.policy_mode,
            "provider_state": self.provider_state,
            "provider_status": self.provider_status,
            "provider_observed_at": (
                self.provider_observed_at.isoformat(timespec="seconds")
                if self.provider_observed_at is not None
                else None
            ),
            "series_prefix": self.season.series_prefix if self.season is not None else None,
            "season_prefix": self.season.season_prefix if self.season is not None else None,
            "season_number": self.season.season_number if self.season is not None else None,
            "is_current_season": self.is_current_season,
            "hold_reasons": [reason.to_payload() for reason in self.hold_reasons],
            "manual_override": self.manual_override,
            "override_applied": self.override_applied,
            "media_role": self.media_role,
            "production_included": self.production_included,
            "production_blocker": self.production_blocker,
            "profile_blocker": self.profile_blocker,
            "target_size_blocker": (
                self.target_size_blocker.to_payload()
                if self.target_size_blocker is not None
                else None
            ),
            "ranking_mode": self.ranking_mode,
            "media_domain": self.media_domain,
            "media_age": self.age.to_payload(),
            "season_rank_age": self.season_rank_age.to_payload(),
            "season_activity_at": (
                self.season_activity_at.isoformat(timespec="seconds")
                if self.season_activity_at is not None
                else None
            ),
            "rank_position": rank_position,
        }


def project_candidates(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefixes: list[str] | None = None,
        statuses: set[str] | frozenset[str] | None = None,
        now: datetime | None = None,
        manual_override_prefix: str | None = None,
) -> list[CandidateDecision]:
    current_time = (now or datetime.now(tz=UTC)).astimezone(UTC)
    library_types = config.library_type_map
    library_definitions = config.library_definition_map
    resolved_scopes = resolve_media_scopes(
        connection,
        prefixes or [],
        library_types=library_types,
    )
    query_scopes = (
        resolved_scopes
        if resolved_scopes and all(scope.domain == "other" for scope in resolved_scopes)
        else []
    )
    rows = _load_rows(connection, scopes=query_scopes)
    season_by_item: dict[int, SeasonIdentity | None] = {}
    season_rows: dict[str, list[dict[str, Any]]] = {}
    numbered_by_series: dict[str, set[int]] = {}
    for row in rows:
        item_id = int(row["item_id"])
        season = season_identity(row, library_types=library_types)
        season_by_item[item_id] = season
        if season is None:
            continue
        season_rows.setdefault(season.season_prefix, []).append(row)
        if season.season_number is not None:
            numbered_by_series.setdefault(season.series_prefix, set()).add(season.season_number)

    metadata_by_series = (
        {
            str(row["series_prefix"]): object_dict(row)
            for row in connection.execute(select(series_metadata)).mappings().fetchall()
        }
        if season_rows
        else {}
    )

    season_activity = {
        season_prefix: _latest_timestamp(_content_activity_at(row) for row in grouped_rows)
        for season_prefix, grouped_rows in season_rows.items()
    }
    season_rank_age = {
        season_prefix: _newest_age_evidence(_media_age(row) for row in grouped_rows)
        for season_prefix, grouped_rows in season_rows.items()
    }
    explicit_scope_keys = {(scope.prefix, scope.match) for scope in resolved_scopes}
    other_rows_by_prefix: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        media_root = str(row.get("media_root") or "")
        library = library_definitions.get(media_root, {})
        if normalize_library_type(library.get("type"), key=media_root) != "other":
            continue
        group_scope = other_group_scope_for_rel_path(str(row.get("rel_path") or ""), config)
        if group_scope is not None:
            other_rows_by_prefix[group_scope.prefix].append(row)
    other_group_blockers: dict[str, str | None] = {}
    for group_prefix, grouped_rows in other_rows_by_prefix.items():
        library = library_definitions.get(str(grouped_rows[0].get("media_root") or ""), {})
        profile_blockers = [
            blocker
            for row in grouped_rows
            if (blocker := other_item_profile_blocker(row, library)) is not None
        ]
        if len(grouped_rows) > OTHER_FOLDER_SCOPE_MAX_ITEMS:
            other_group_blockers[group_prefix] = (
                f"This Other folder contains more than {OTHER_FOLDER_SCOPE_MAX_ITEMS} files. "
                "Split it or use exact-file grouping before processing."
            )
        else:
            other_group_blockers[group_prefix] = profile_blockers[0] if profile_blockers else None
    normalized_override = normalize_scope_prefix(manual_override_prefix or "")
    decisions: list[CandidateDecision] = []
    production_roots = set(config.source_root_map)
    scan_roots = set(config.scan_source_root_map)
    library_context_by_root: dict[str, tuple[dict[str, Any], str, dict[str, Any]]] = {}
    policy_by_prefix: dict[str, dict[str, Any]] = {}

    for row in rows:
        media_root = str(row["media_root"] or "")
        if media_root not in scan_roots:
            continue
        if statuses is not None and str(row["status"] or "") not in statuses:
            continue
        rel_path = str(row["rel_path"] or "")
        if resolved_scopes and not any(scope.includes(rel_path) for scope in resolved_scopes):
            continue
        workflow_lane = derive_item_workflow_state(row).lane
        season = season_by_item[int(row["item_id"])]
        library_context = library_context_by_root.get(media_root)
        if library_context is None:
            library = library_definitions.get(media_root, {})
            library_type = normalize_library_type(library.get("type"), key=media_root)
            library_policy = normalize_library_policy(library_type, library.get("policy"))
            library_context = library, library_type, library_policy
            library_context_by_root[media_root] = library_context
        library, library_type, library_policy = library_context
        membership = (
            classify_movie_path(rel_path, root=media_root)
            if library_type == "movie"
            else None
        )
        explicit_exact = any(
            scope.match == "exact_item" and scope.prefix == rel_path
            for scope in resolved_scopes
        )
        member_included, member_blocker = (
            movie_item_included(membership, library_policy, explicit_exact=explicit_exact)
            if membership is not None
            else (True, None)
        )
        profile_blocker = None
        if library_type == "other":
            group_scope = other_group_scope_for_rel_path(rel_path, config)
            if group_scope is None or (group_scope.prefix, group_scope.match) not in explicit_scope_keys:
                profile_blocker = (
                    "Open this item through its bounded Other Library work unit before processing."
                )
            else:
                profile_blocker = other_group_blockers.get(group_scope.prefix)
        production_enabled = media_root in production_roots
        production_included = production_enabled and member_included and profile_blocker is None
        production_blocker = profile_blocker or member_blocker
        if not production_enabled:
            label = str(library.get("label") or media_root or "This library")
            production_blocker = f"{label} is browse only; set its availability to Production before processing."
        target_size_blocker = (
            _target_size_projection_blocker(row, config)
            if library_type == "movie" and workflow_lane == "encode" and production_included
            else None
        )
        ranking_mode = (
            str(library_policy.get("ranking") or "oldest_added_first")
            if library_type == "movie"
            else "oldest_added_first"
        )
        policy_prefix = season.series_prefix if season is not None else rel_path
        policy = policy_by_prefix.get(policy_prefix)
        if policy is None:
            policy = _resolved_policy(config, policy_prefix)
            policy_by_prefix[policy_prefix] = policy
        lifecycle_keys = (
            "series_lifecycle_mode",
            "season_acquisition_hold_days",
            "current_season_inactive_days",
            "series_metadata_stale_days",
        )
        policy_enabled = season is not None and (
            all(
                isinstance(config.raw.get(section), dict)
                for section in ("video", "audio", "subtitle", "planning")
            )
            or any(key in policy for key in lifecycle_keys)
        )
        mode = _lifecycle_mode(policy.get("series_lifecycle_mode")) if season is not None else "off"
        acquisition_days = _duration_days(policy.get("season_acquisition_hold_days"), 30)
        inactive_days = _duration_days(policy.get("current_season_inactive_days"), 365)
        stale_days = _positive_int(policy.get("series_metadata_stale_days"), 7)
        series_metadata_row = metadata_by_series.get(season.series_prefix, {}) if season is not None else {}
        provider_state, provider_status, provider_observed_at = _provider_state(
            series_metadata_row,
            now=current_time,
            stale_after=timedelta(days=stale_days),
        )
        highest_number = (
            max(numbered_by_series.get(season.series_prefix, set()), default=None)
            if season is not None
            else None
        )
        is_current = (
            season is not None
            and season.season_number is not None
            and season.season_number == highest_number
        )
        activity_at = season_activity.get(season.season_prefix) if season is not None else None
        rank_age = season_rank_age.get(season.season_prefix, _media_age(row)) if season is not None else _media_age(row)
        manual_override = bool(
            normalized_override
            and season is not None
            and season.season_prefix == normalized_override
        )
        hold_reasons = _hold_reasons(
            season=season,
            policy_enabled=policy_enabled,
            mode=mode,
            provider_state=provider_state,
            is_current=is_current,
            activity_at=activity_at,
            now=current_time,
            acquisition_days=acquisition_days,
            inactive_days=inactive_days,
        )
        decisions.append(
            CandidateDecision(
                row=row,
                workflow_lane=workflow_lane,
                season=season,
                policy_mode=mode,
                provider_state=provider_state,
                provider_status=provider_status,
                provider_observed_at=provider_observed_at,
                is_current_season=is_current,
                age=_media_age(row),
                season_rank_age=rank_age,
                season_activity_at=activity_at,
                hold_reasons=hold_reasons,
                manual_override=manual_override,
                media_role=membership.role if membership is not None else None,
                production_included=production_included,
                production_blocker=production_blocker,
                profile_blocker=profile_blocker,
                target_size_blocker=target_size_blocker,
                ranking_mode=ranking_mode,
                media_domain=library_type,
            )
        )
    return decisions


def encode_candidate_decisions(
        connection: DBClient,
        config: MediaforceConfig,
        *,
        prefixes: list[str],
        now: datetime | None = None,
        manual_override_prefix: str | None = None,
) -> list[CandidateDecision]:
    return project_candidates(
        connection,
        config,
        prefixes=prefixes,
        statuses=ENCODE_CANDIDATE_STATUSES,
        now=now,
        manual_override_prefix=manual_override_prefix,
    )


def scope_target_size_blocker(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> StreamBudgetProjectionBlocker | None:
    blocked = [
        decision
        for decision in encode_candidate_decisions(connection, config, prefixes=[prefix])
        if decision.target_size_blocker is not None
    ]
    if not blocked:
        return None
    blocked.sort(key=lambda decision: str(decision.row.get("rel_path") or ""))
    return blocked[0].target_size_blocker


def workflow_eligibility(decisions: list[CandidateDecision]) -> dict[int, EncodeEligibility]:
    return {
        decision.item_id: EncodeEligibility(
            eligible=decision.eligible if decision.workflow_lane == "encode" else decision.production_included,
            blocker=(
                decision.production_blocker
                or (decision.target_size_blocker.message if decision.target_size_blocker is not None else None)
                or (decision.hold_reasons[0].detail if decision.hold_reasons else None)
            ),
            blocked=decision.profile_blocker is not None or decision.target_size_blocker is not None,
        )
        for decision in decisions
    }


def _target_size_projection_blocker(
        row: dict[str, Any],
        config: MediaforceConfig,
) -> StreamBudgetProjectionBlocker | None:
    item = build_manifest_item(row, config)
    ledger = StreamBudgetLedger.from_payload(object_dict(item.get("stream_budget_ledger")))
    return stream_budget_projection_blocker(ledger)


def scope_lifecycle_payload(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
        *,
        now: datetime | None = None,
) -> dict[str, Any]:
    normalized_prefix = prefix.strip().strip("/")
    decisions = encode_candidate_decisions(connection, config, prefixes=[normalized_prefix], now=now)
    return scope_lifecycle_payload_from_decisions(normalized_prefix, decisions)


def scope_lifecycle_payload_from_decisions(
        prefix: str,
        decisions: list[CandidateDecision],
) -> dict[str, Any]:
    normalized_prefix = prefix.strip().strip("/")
    decisions = [
        decision
        for decision in decisions
        if path_matches_scope(str(decision.row["rel_path"] or ""), normalized_prefix)
    ]
    if not decisions:
        return {
            "schema_version": 1,
            "prefix": normalized_prefix,
            "candidate_count": 0,
            "eligible_candidate_count": 0,
            "held_candidate_count": 0,
            "seasons": [],
        }
    held = [decision for decision in decisions if decision.workflow_lane == "encode" and not decision.eligible]
    eligible = [decision for decision in decisions if decision.eligible]
    season_groups: dict[str, list[CandidateDecision]] = {}
    for decision in decisions:
        if decision.season is not None:
            season_groups.setdefault(decision.season.season_prefix, []).append(decision)
    seasons = [_season_payload(group) for group in season_groups.values()]
    seasons.sort(key=lambda value: (value.get("season_number") is None, value.get("season_number") or 0, value["prefix"]))
    representative = next((decision for decision in decisions if decision.season is not None), decisions[0])
    reason_counts = Counter(reason.code for decision in held for reason in decision.hold_reasons)
    rank_age = min(
        (decision.season_rank_age.timestamp for decision in eligible if decision.season_rank_age.timestamp is not None),
        default=None,
    )
    return {
        "schema_version": 1,
        "prefix": normalized_prefix,
        "series_prefix": representative.season.series_prefix if representative.season is not None else None,
        "policy_mode": representative.policy_mode,
        "provider_state": representative.provider_state,
        "provider_status": representative.provider_status,
        "provider_observed_at": (
            representative.provider_observed_at.isoformat(timespec="seconds")
            if representative.provider_observed_at is not None
            else None
        ),
        "current_season_number": next(
            (decision.season.season_number for decision in decisions if decision.is_current_season and decision.season),
            None,
        ),
        "candidate_count": sum(decision.workflow_lane == "encode" for decision in decisions),
        "eligible_candidate_count": len(eligible),
        "held_candidate_count": len(held),
        "hold_reason_counts": dict(reason_counts),
        "ranking_added_at": rank_age.isoformat(timespec="seconds") if rank_age is not None else None,
        "can_override_holds": bool(
            held
            and any(
                decision.season is not None and decision.season.season_prefix == normalized_prefix
                for decision in decisions
            )
            and all(all(reason.code in OVERRIDEABLE_HOLD_CODES for reason in decision.hold_reasons) for decision in held)
        ),
        "seasons": seasons,
    }


def season_identity(
        row: dict[str, Any],
        *,
        library_types: Mapping[str, str] | None = None,
) -> SeasonIdentity | None:
    rel_path = str(row.get("rel_path") or "")
    parts = Path(rel_path).parts
    if len(parts) < 3:
        return None
    root = parts[0]
    if library_types is None:
        is_tv = root.lower() == "tv"
    else:
        is_tv = normalize_library_type(library_types.get(root), key=root) == "tv"
    if not is_tv:
        return None
    label = parts[2]
    match = SEASON_PATTERN.fullmatch(label)
    path_number = int(match.group(1)) if match is not None else None
    plex_number = _positive_or_zero_int(row.get("plex_season_index"))
    is_special = label.lower() == "specials" or path_number == 0 or plex_number == 0
    if is_special:
        number = None
        ambiguous = False
    else:
        positive_path = path_number if path_number is not None and path_number > 0 else None
        positive_plex = plex_number if plex_number is not None and plex_number > 0 else None
        ambiguous = positive_path is not None and positive_plex is not None and positive_path != positive_plex
        number = None if ambiguous else positive_plex or positive_path
    return SeasonIdentity(
        series_prefix="/".join(parts[:2]),
        season_prefix="/".join(parts[:3]),
        season_number=number,
        label=label,
        is_special=is_special,
        ambiguous=ambiguous,
    )


def candidate_rank_key(decision: CandidateDecision, *, recommendation_score: float, size_bytes: int) -> tuple[Any, ...]:
    timestamp = decision.season_rank_age.timestamp
    if decision.ranking_mode == "largest_first":
        return (
            -size_bytes,
            timestamp is None,
            timestamp or datetime.max.replace(tzinfo=UTC),
            -recommendation_score,
            str(decision.row["rel_path"]),
            decision.item_id,
        )
    return (
        timestamp is None,
        timestamp or datetime.max.replace(tzinfo=UTC),
        -recommendation_score,
        -size_bytes,
        str(decision.season.season_prefix if decision.season is not None else decision.row["rel_path"]),
        str(decision.row["rel_path"]),
        decision.item_id,
    )


def _load_rows(
        connection: DBClient,
        *,
        scopes: list[MediaScope] | None = None,
) -> list[dict[str, Any]]:
    joined = outerjoin(
        outerjoin(
            library_items,
            staged_artifacts,
            staged_artifacts.c.library_item_id == library_items.c.id,
        ),
        plex_item_metadata,
        plex_item_metadata.c.library_item_id == library_items.c.id,
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
            plex_item_metadata.c.plex_item_rating_key,
            plex_item_metadata.c.plex_show_rating_key,
            plex_item_metadata.c.plex_season_index,
            plex_item_metadata.c.plex_added_at,
            plex_item_metadata.c.observed_at.label("plex_observed_at"),
        )
        .select_from(joined)
        .where(library_items.c.status != "missing")
    )
    if scopes:
        query = query.where(
            or_(*(scope_rel_path_filter(library_items.c.rel_path, scope) for scope in scopes))
        )
    return [object_dict(row) for row in connection.execute(query).mappings().fetchall()]


def _hold_reasons(
        *,
        season: SeasonIdentity | None,
        policy_enabled: bool,
        mode: LifecycleMode,
        provider_state: ProviderState,
        is_current: bool,
        activity_at: datetime | None,
        now: datetime,
        acquisition_days: int,
        inactive_days: int,
) -> tuple[HoldReason, ...]:
    if season is None or not policy_enabled:
        return ()
    reasons: list[HoldReason] = []
    if season.ambiguous:
        reasons.append(
            HoldReason(
                code="season_identity_conflict",
                label="Season mismatch",
                detail="The folder name and Plex metadata identify different season numbers.",
            )
        )
    if activity_at is None:
        reasons.append(
            HoldReason(
                code="content_age_unknown",
                label="Age unknown",
                detail="Mediaforce cannot verify when this season was added or replaced.",
            )
        )
        return tuple(reasons)
    acquisition_release = activity_at + timedelta(days=acquisition_days)
    if now < acquisition_release:
        reasons.append(
            HoldReason(
                code="recent_acquisition",
                label="Recent acquisition",
                detail=f"This whole season is held for {acquisition_days} days after its newest addition or replacement.",
                release_at=acquisition_release,
            )
        )
    inactivity_release = activity_at + timedelta(days=inactive_days)
    protection_enabled = mode == "on" or (mode == "auto" and provider_state in {"active", "unknown", "stale"})
    if is_current and protection_enabled and now < inactivity_release:
        reasons.append(
            HoldReason(
                code="current_season",
                label="Current season",
                detail=(
                    "This is the highest numbered season. It releases when a newer season appears "
                    f"or after {inactive_days} days without new or replaced episodes."
                ),
                release_at=inactivity_release,
            )
        )
    return tuple(reasons)


def _provider_state(
        row: dict[str, Any],
        *,
        now: datetime,
        stale_after: timedelta,
) -> tuple[ProviderState, str | None, datetime | None]:
    status = str(row.get("tmdb_status") or "").strip() or None
    observed_at = _parse_timestamp(row.get("tmdb_observed_at"))
    if observed_at is None:
        return "unknown", status, None
    if now - observed_at > stale_after:
        return "stale", status, observed_at
    in_production = row.get("tmdb_in_production")
    if in_production in {1, True}:
        return "active", status, observed_at
    normalized = str(status or "").strip().lower()
    if normalized in {"returning series", "planned", "in production", "pilot"}:
        return "active", status, observed_at
    if normalized in {"ended", "canceled", "cancelled"} or in_production in {0, False}:
        return "ended", status, observed_at
    return "unknown", status, observed_at


def _media_age(row: dict[str, Any]) -> AgeEvidence:
    plex_added_at = _parse_timestamp(row.get("plex_added_at"))
    if plex_added_at is not None:
        return AgeEvidence(plex_added_at, "plex")
    discovered_at = _parse_timestamp(row.get("discovered_at"))
    if discovered_at is not None:
        return AgeEvidence(discovered_at, "mediaforce_discovered")
    mtime_ns = _non_negative_int(row.get("mtime_ns"))
    if mtime_ns is not None and mtime_ns > 0:
        try:
            return AgeEvidence(datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC), "filesystem_mtime")
        except (OSError, OverflowError, ValueError):
            pass
    return AgeEvidence(None, "unknown")


def _content_activity_at(row: dict[str, Any]) -> datetime | None:
    return _latest_timestamp((
        _parse_timestamp(row.get("plex_added_at")),
        _parse_timestamp(row.get("content_version_changed_at")),
        _parse_timestamp(row.get("discovered_at")),
    ))


def _newest_age_evidence(values: Any) -> AgeEvidence:
    known = [value for value in values if value.timestamp is not None]
    if not known:
        return AgeEvidence(None, "unknown")
    return max(known, key=lambda value: value.timestamp or datetime.min.replace(tzinfo=UTC))


def _season_payload(decisions: list[CandidateDecision]) -> dict[str, Any]:
    representative = decisions[0]
    held = [decision for decision in decisions if decision.workflow_lane == "encode" and not decision.eligible]
    eligible = [decision for decision in decisions if decision.eligible]
    reasons = []
    seen_codes: set[str] = set()
    for decision in held:
        for reason in decision.hold_reasons:
            if reason.code in seen_codes:
                continue
            seen_codes.add(reason.code)
            reasons.append(reason.to_payload())
    return {
        "prefix": representative.season.season_prefix if representative.season is not None else "",
        "label": representative.season.label if representative.season is not None else "",
        "season_number": representative.season.season_number if representative.season is not None else None,
        "is_special": bool(representative.season and representative.season.is_special),
        "ambiguous": bool(representative.season and representative.season.ambiguous),
        "is_current_season": representative.is_current_season,
        "candidate_count": sum(decision.workflow_lane == "encode" for decision in decisions),
        "eligible_candidate_count": len(eligible),
        "held_candidate_count": len(held),
        "hold_reasons": reasons,
        "activity_at": (
            representative.season_activity_at.isoformat(timespec="seconds")
            if representative.season_activity_at is not None
            else None
        ),
        "ranking_added_at": (
            representative.season_rank_age.timestamp.isoformat(timespec="seconds")
            if representative.season_rank_age.timestamp is not None
            else None
        ),
        "ranking_added_source": representative.season_rank_age.source,
    }


def _resolved_policy(config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    try:
        policy = config.resolve_policy(prefix)
    except KeyError:
        planning = config.raw.get("planning")
        return dict(planning) if isinstance(planning, dict) else {}
    planning = policy.get("planning")
    return dict(planning) if isinstance(planning, dict) else {}


def _lifecycle_mode(value: object) -> LifecycleMode:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "on", "off"}:
        return normalized  # type: ignore[return-value]
    return "auto"


def _latest_timestamp(values: Any) -> datetime | None:
    known = [value for value in values if isinstance(value, datetime)]
    return max(known) if known else None


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive_int(value: object, default: int) -> int:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None and parsed > 0 else default


def _duration_days(value: object, default: int) -> int:
    parsed = _non_negative_int(value)
    return parsed if parsed is not None else default


def _positive_or_zero_int(value: object) -> int | None:
    return _non_negative_int(value)


def _non_negative_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
