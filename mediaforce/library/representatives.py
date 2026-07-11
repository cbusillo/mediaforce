import copy
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median, quantiles
from typing import Any

from sqlalchemy import or_, select

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items
from mediaforce.core.evidence import build_evidence_envelope, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import mapping_dict, object_dict, object_list
from mediaforce.library.planner import build_manifest_item

REPRESENTATIVE_SELECTION_TOOL = "mediaforce.representative_selection"
REPRESENTATIVE_SELECTION_TOOL_VERSION = "1"
REPRESENTATIVE_SELECTION_POLICY_VERSION = 2
MEANINGFUL_CLUSTER_FRACTION = 0.20

_PREFERRED_SAMPLE_STATUSES = frozenset({"discovered", "planned", "validated", "encoded"})
_PROFILE_DIMENSIONS = ("video_codec", "resolution", "cadence", "audio_layout", "runtime")
_FINGERPRINT_DIMENSIONS = (
    "luma",
    "gradient",
    "motion",
    "texture",
    "noise",
    "duplicate_cadence",
    "animation",
    "audio_complexity",
)
_COVERAGE_DIMENSIONS = (*_PROFILE_DIMENSIONS, *_FINGERPRINT_DIMENSIONS)
_UNKNOWN_PROFILE_VALUE = "unknown"
_PUBLIC_ITEM_FIELDS = (
    "library_item_id",
    "rel_path",
    "source_size_bytes",
    "video_codec",
    "video_bitrate",
    "width",
    "height",
    "cadence_class",
    "duration_seconds",
    "container",
    "status",
    "recommendation",
    "priority_score",
    "recommendation_reason",
    "audio_summary",
    "subtitle_summary",
    "attachment_summary",
    "resolved_policy",
    "media_fingerprint_decision",
)
_OPTIONAL_TECHNICAL_FIELDS = (
    "cadence_class",
    "cadence",
    "frame_cadence_class",
    "frame_rate_class",
)


@dataclass(slots=True)
class RepresentativeSelection:
    selected_items: tuple[dict[str, Any], ...]
    payload: dict[str, Any]

    def primary_item(self) -> dict[str, Any]:
        item = copy.deepcopy(self.selected_items[0])
        item["representative_source_id"] = self.payload["primary_source_id"]
        item["representative_selection"] = copy.deepcopy(self.payload)
        return item

    def public_payload(self) -> dict[str, Any]:
        return copy.deepcopy(self.payload)


@dataclass(slots=True)
class _Candidate:
    item: dict[str, Any]
    source_id: str
    rel_path: str
    fingerprint: str | None
    duration_seconds: float | None
    size_bytes: int | None
    profile: dict[str, str]

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return self.rel_path.casefold(), self.rel_path, self.source_id


def evidence_sources(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    sources = [
        {
            "source_id": str(item.get("source_id") or stable_source_id(item)),
            "fingerprint": _source_fingerprint(item),
        }
        for item in items
    ]
    return sorted(sources, key=lambda source: (str(source["source_id"]), str(source.get("fingerprint") or "")))


def public_representative_item(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        field: copy.deepcopy(item[field])
        for field in _PUBLIC_ITEM_FIELDS
        if field in item
    }
    payload["source_id"] = str(item.get("representative_source_id") or stable_source_id(item))
    return payload


def load_representative_selection(
        connection: DBClient,
        config: MediaforceConfig,
        prefix: str,
) -> RepresentativeSelection | None:
    normalized_prefix = prefix.strip().strip("/")
    rows = connection.execute(
        select(library_items)
        .where(
            or_(
                library_items.c.rel_path == normalized_prefix,
                library_items.c.rel_path.like(_prefix_descendant_pattern(normalized_prefix), escape="\\"),
            )
        )
        .order_by(library_items.c.rel_path.asc())
    ).mappings().fetchall()
    if not rows:
        return None

    preferred_rows = [row for row in rows if str(row.get("status") or "") in _PREFERRED_SAMPLE_STATUSES]
    candidate_rows = preferred_rows or list(rows)
    items: list[dict[str, Any]] = []
    for row in candidate_rows:
        row_payload = mapping_dict(row)
        item = build_manifest_item(row_payload, config)
        for field in _OPTIONAL_TECHNICAL_FIELDS:
            if row_payload.get(field) not in (None, ""):
                item[field] = row_payload[field]
        items.append(item)
    return select_representatives(
        items,
        prefix=normalized_prefix,
        policy=config.resolve_policy(normalized_prefix),
    )


def select_representatives(
        items: Sequence[Mapping[str, Any]],
        *,
        prefix: str = "",
        policy: Mapping[str, Any] | None = None,
        tool_version: str = REPRESENTATIVE_SELECTION_TOOL_VERSION,
) -> RepresentativeSelection:
    if not items:
        raise ValueError("At least one representative candidate is required")

    item_payloads = [copy.deepcopy(dict(item)) for item in items]
    median_runtime = _median_positive(item.get("duration_seconds") for item in item_payloads)
    median_size = _median_positive(item.get("source_size_bytes", item.get("size_bytes")) for item in item_payloads)
    candidates = sorted(
        (_candidate(item, median_runtime) for item in item_payloads),
        key=lambda candidate: candidate.sort_key,
    )
    outlier_reasons = _outlier_reasons(candidates)
    profile_counts = _profile_counts(candidates)
    dominant_profile = {
        dimension: _dominant_value(profile_counts[dimension])
        for dimension in _COVERAGE_DIMENSIONS
    }
    required_targets, target_counts = _required_coverage_targets(candidates, outlier_reasons)

    primary = min(
        candidates,
        key=lambda candidate: _selection_sort_key(
            candidate,
            dominant_profile=dominant_profile,
            median_runtime=median_runtime,
            median_size=median_size,
            outlier_reasons=outlier_reasons,
        ),
    )
    selected = [primary]
    newly_covered: dict[str, set[tuple[str, str]]] = {
        primary.source_id: required_targets & _profile_targets(primary),
    }
    uncovered_targets = required_targets - _profile_targets(primary)
    while uncovered_targets:
        covering_candidates = [
            candidate
            for candidate in candidates
            if candidate not in selected and uncovered_targets & _profile_targets(candidate)
        ]
        if not covering_candidates:
            break
        next_candidate = min(
            covering_candidates,
            key=lambda candidate: _coverage_sort_key(
                candidate,
                uncovered_targets=uncovered_targets,
                target_counts=target_counts,
                dominant_profile=dominant_profile,
                median_runtime=median_runtime,
                median_size=median_size,
                outlier_reasons=outlier_reasons,
            ),
        )
        covered = uncovered_targets & _profile_targets(next_candidate)
        selected.append(next_candidate)
        newly_covered[next_candidate.source_id] = covered
        uncovered_targets -= covered

    assignments = _representation_assignments(candidates, selected)
    coverage = _coverage_payload(
        candidates,
        selected,
        assignments,
        profile_counts,
        required_targets,
        uncovered_targets,
        outlier_reasons,
    )
    confidence = _confidence_payload(coverage)
    rationale = [
        _selection_rationale(
            candidate,
            role="primary" if index == 0 else "coverage",
            covers=newly_covered[candidate.source_id],
            target_counts=target_counts,
            total_items=len(candidates),
            assignment=assignments[candidate.source_id],
            dominant_profile=dominant_profile,
            outlier_reasons=outlier_reasons,
        )
        for index, candidate in enumerate(selected)
    ]
    outliers = [
        {
            "source_id": candidate.source_id,
            "rel_path": candidate.rel_path,
            "reasons": outlier_reasons[candidate.source_id],
            "selected": candidate in selected,
        }
        for candidate in candidates
        if candidate.source_id in outlier_reasons
    ]
    selected_items = [
        _selected_item_payload(candidate, rationale[index])
        for index, candidate in enumerate(selected)
    ]
    selection_policy = {
        "version": REPRESENTATIVE_SELECTION_POLICY_VERSION,
        "meaningful_cluster_fraction": MEANINGFUL_CLUSTER_FRACTION,
        "profile_dimensions": list(_PROFILE_DIMENSIONS),
        "fingerprint_dimensions": list(_FINGERPRINT_DIMENSIONS),
        "numeric_outliers_required_for_coverage": False,
    }
    policy_snapshot = {
        "selection": selection_policy,
        "folder": copy.deepcopy(object_dict(policy)),
        "items": [
            {
                "source_id": candidate.source_id,
                "resolved_policy": copy.deepcopy(object_dict(candidate.item.get("resolved_policy"))),
            }
            for candidate in candidates
        ],
    }
    policy_hash = stable_policy_hash(policy_snapshot)
    evidence_result = {
        "primary_source_id": primary.source_id,
        "selected_source_ids": [candidate.source_id for candidate in selected],
        "rationale": rationale,
        "coverage": coverage,
        "outliers": outliers,
        "confidence": confidence,
        "selection_policy": selection_policy,
    }
    evidence = build_evidence_envelope(
        kind="representative_selection",
        sources=evidence_sources([candidate.item for candidate in candidates]),
        policy_hash=policy_hash,
        tool_name=REPRESENTATIVE_SELECTION_TOOL,
        tool_version=tool_version,
        subject={"prefix": prefix.strip().strip("/")},
        measurement={
            "unit": "items",
            "sample_job_id": None,
            "media_ranges": [
                {
                    "source_id": candidate.source_id,
                    "start_seconds": 0.0,
                    "end_seconds": candidate.duration_seconds,
                }
                for candidate in selected
                if candidate.duration_seconds is not None
            ],
        },
        result=evidence_result,
    )
    payload = {
        "schema_version": REPRESENTATIVE_SELECTION_POLICY_VERSION,
        "selection_id": evidence["evidence_id"],
        "primary_source_id": primary.source_id,
        "selected_items": selected_items,
        "coverage": coverage,
        "outliers": outliers,
        "confidence": confidence,
        "evidence": evidence,
    }
    return RepresentativeSelection(
        selected_items=tuple(candidate.item for candidate in selected),
        payload=payload,
    )


def _candidate(item: dict[str, Any], median_runtime: float | None) -> _Candidate:
    duration_seconds = _positive_float(item.get("duration_seconds"))
    size_bytes = _positive_int(item.get("source_size_bytes", item.get("size_bytes")))
    return _Candidate(
        item=item,
        source_id=stable_source_id(item),
        rel_path=_normalized_rel_path(item.get("rel_path")),
        fingerprint=_source_fingerprint(item),
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
        profile={
            "video_codec": _video_codec_class(item.get("video_codec")),
            "resolution": _resolution_class(item.get("width"), item.get("height")),
            "cadence": _cadence_class(item),
            "audio_layout": _audio_layout_class(item),
            "runtime": _runtime_class(duration_seconds, median_runtime),
            **_fingerprint_profile(item),
        },
    )


def _selection_sort_key(
        candidate: _Candidate,
        *,
        dominant_profile: Mapping[str, str],
        median_runtime: float | None,
        median_size: float | None,
        outlier_reasons: Mapping[str, list[str]],
) -> tuple[Any, ...]:
    mismatch_count = sum(
        candidate.profile[dimension] != dominant_profile[dimension]
        for dimension in _COVERAGE_DIMENSIONS
    )
    return (
        len(outlier_reasons.get(candidate.source_id, [])),
        mismatch_count,
        _numeric_distance(candidate.duration_seconds, median_runtime),
        _numeric_distance(candidate.size_bytes, median_size),
        candidate.sort_key,
    )


def _coverage_sort_key(
        candidate: _Candidate,
        *,
        uncovered_targets: set[tuple[str, str]],
        target_counts: Mapping[tuple[str, str], int],
        dominant_profile: Mapping[str, str],
        median_runtime: float | None,
        median_size: float | None,
        outlier_reasons: Mapping[str, list[str]],
) -> tuple[Any, ...]:
    covered = uncovered_targets & _profile_targets(candidate)
    covered_items = sum(target_counts[target] for target in covered)
    return (
        -covered_items,
        -len(covered),
        *_selection_sort_key(
            candidate,
            dominant_profile=dominant_profile,
            median_runtime=median_runtime,
            median_size=median_size,
            outlier_reasons=outlier_reasons,
        ),
    )


def _representation_assignments(
        candidates: Sequence[_Candidate],
        selected: Sequence[_Candidate],
) -> dict[str, dict[str, Any]]:
    assignments = {
        candidate.source_id: {
            "represented_item_count": 0,
            "represented_runtime_seconds": 0.0,
        }
        for candidate in selected
    }
    for candidate in candidates:
        representative = min(
            selected,
            key=lambda selected_candidate: _representation_distance(candidate, selected_candidate),
        )
        assignment = assignments[representative.source_id]
        assignment["represented_item_count"] += 1
        if candidate.duration_seconds is not None:
            assignment["represented_runtime_seconds"] += candidate.duration_seconds
    for assignment in assignments.values():
        assignment["represented_runtime_seconds"] = round(
            float(assignment["represented_runtime_seconds"]),
            3,
        )
    return assignments


def _representation_distance(candidate: _Candidate, representative: _Candidate) -> tuple[Any, ...]:
    profile_mismatches = sum(
        candidate.profile[dimension] != representative.profile[dimension]
        for dimension in _COVERAGE_DIMENSIONS
    )
    return (
        profile_mismatches,
        _numeric_distance(candidate.duration_seconds, representative.duration_seconds),
        _numeric_distance(candidate.size_bytes, representative.size_bytes),
        representative.sort_key,
    )


def _coverage_payload(
        candidates: Sequence[_Candidate],
        selected: Sequence[_Candidate],
        assignments: Mapping[str, Mapping[str, Any]],
        profile_counts: Mapping[str, Counter[str]],
        required_targets: set[tuple[str, str]],
        uncovered_targets: set[tuple[str, str]],
        outlier_reasons: Mapping[str, list[str]],
) -> dict[str, Any]:
    selected_profiles = {
        tuple(candidate.profile[dimension] for dimension in _COVERAGE_DIMENSIONS)
        for candidate in selected
    }
    exact_candidates = [
        candidate
        for candidate in candidates
        if tuple(candidate.profile[dimension] for dimension in _COVERAGE_DIMENSIONS) in selected_profiles
    ]
    total_runtime = sum(candidate.duration_seconds or 0.0 for candidate in candidates)
    exact_runtime = sum(candidate.duration_seconds or 0.0 for candidate in exact_candidates)
    meaningful_covered = len(required_targets - uncovered_targets)
    dimensions: dict[str, Any] = {}
    for dimension in _COVERAGE_DIMENSIONS:
        covered_values = {candidate.profile[dimension] for candidate in selected}
        covered_item_count = sum(
            count
            for value, count in profile_counts[dimension].items()
            if value in covered_values
        )
        dimensions[dimension] = {
            "covered_item_count": covered_item_count,
            "total_item_count": len(candidates),
            "item_fraction": _fraction(covered_item_count, len(candidates)),
            "covered_values": sorted(covered_values),
            "uncovered_values": [
                {
                    "value": value,
                    "item_count": count,
                    "item_fraction": _fraction(count, len(candidates)),
                    "meaningful": (dimension, value) in required_targets,
                }
                for value, count in sorted(profile_counts[dimension].items())
                if value not in covered_values
            ],
        }

    known_facts = sum(
        candidate.profile[dimension] != _UNKNOWN_PROFILE_VALUE
        for candidate in candidates
        for dimension in _COVERAGE_DIMENSIONS
    )
    total_facts = len(candidates) * len(_COVERAGE_DIMENSIONS)
    return {
        "candidate_item_count": len(candidates),
        "candidate_runtime_seconds": round(total_runtime, 3),
        "selected_item_count": len(selected),
        "represented_item_count": sum(
            int(assignment["represented_item_count"])
            for assignment in assignments.values()
        ),
        "represented_runtime_seconds": round(
            sum(float(assignment["represented_runtime_seconds"]) for assignment in assignments.values()),
            3,
        ),
        "exact_profile_item_count": len(exact_candidates),
        "exact_profile_runtime_seconds": round(exact_runtime, 3),
        "exact_profile_item_fraction": _fraction(len(exact_candidates), len(candidates)),
        "meaningful_cluster_count": len(required_targets),
        "covered_meaningful_cluster_count": meaningful_covered,
        "meaningful_cluster_fraction": _fraction(meaningful_covered, len(required_targets)),
        "known_fact_fraction": _fraction(known_facts, total_facts),
        "outlier_item_count": len(outlier_reasons),
        "dimensions": dimensions,
    }


def _confidence_payload(coverage: Mapping[str, Any]) -> dict[str, Any]:
    exact_fraction = float(coverage["exact_profile_item_fraction"])
    meaningful_fraction = float(coverage["meaningful_cluster_fraction"])
    known_fraction = float(coverage["known_fact_fraction"])
    candidate_count = int(coverage["candidate_item_count"])
    outlier_fraction = _fraction(int(coverage["outlier_item_count"]), candidate_count)
    if (
            meaningful_fraction == 1.0
            and exact_fraction >= 0.80
            and known_fraction >= 0.75
            and outlier_fraction <= 0.10
    ):
        level = "high"
    elif meaningful_fraction == 1.0 and exact_fraction >= 0.60 and known_fraction >= 0.50:
        level = "medium"
    else:
        level = "low"
    return {
        "level": level,
        "known_fact_fraction": known_fraction,
        "exact_profile_item_fraction": exact_fraction,
        "outlier_item_fraction": outlier_fraction,
    }


def _selection_rationale(
        candidate: _Candidate,
        *,
        role: str,
        covers: set[tuple[str, str]],
        target_counts: Mapping[tuple[str, str], int],
        total_items: int,
        assignment: Mapping[str, Any],
        dominant_profile: Mapping[str, str],
        outlier_reasons: Mapping[str, list[str]],
) -> dict[str, Any]:
    covered_clusters = [
        {
            "dimension": dimension,
            "value": value,
            "item_count": target_counts[(dimension, value)],
            "item_fraction": _fraction(target_counts[(dimension, value)], total_items),
        }
        for dimension, value in sorted(covers)
    ]
    dominant_matches = [
        dimension
        for dimension in _COVERAGE_DIMENSIONS
        if candidate.profile[dimension] == dominant_profile[dimension]
    ]
    if role == "primary":
        summary = "Closest non-outlier match to the dominant technical profile and median runtime/source size."
    else:
        labels = ", ".join(f"{cluster['dimension']}={cluster['value']}" for cluster in covered_clusters)
        summary = f"Adds meaningful folder coverage for {labels}."
    if candidate.source_id in outlier_reasons:
        summary = f"{summary} Selected despite a numeric outlier because its technical coverage is required."
    return {
        "source_id": candidate.source_id,
        "role": role,
        "summary": summary,
        "dominant_matches": dominant_matches,
        "covers": covered_clusters,
        "represented_item_count": int(assignment["represented_item_count"]),
        "represented_runtime_seconds": float(assignment["represented_runtime_seconds"]),
    }


def _selected_item_payload(candidate: _Candidate, rationale: Mapping[str, Any]) -> dict[str, Any]:
    public_item = public_representative_item(candidate.item)
    public_item.update(
        {
            "source_id": candidate.source_id,
            "technical_profile": dict(candidate.profile),
            "rationale": copy.deepcopy(dict(rationale)),
        }
    )
    return public_item


def _required_coverage_targets(
        candidates: Sequence[_Candidate],
        outlier_reasons: Mapping[str, list[str]],
) -> tuple[set[tuple[str, str]], dict[tuple[str, str], int]]:
    threshold = max(1, math.ceil(len(candidates) * MEANINGFUL_CLUSTER_FRACTION))
    targets: set[tuple[str, str]] = set()
    target_counts: dict[tuple[str, str], int] = {}
    for dimension in _COVERAGE_DIMENSIONS:
        eligible_candidates = [
            candidate
            for candidate in candidates
            if not (
                dimension == "runtime"
                and any(reason.startswith("runtime_") for reason in outlier_reasons.get(candidate.source_id, []))
            )
        ]
        counts = Counter(candidate.profile[dimension] for candidate in eligible_candidates)
        for value, count in counts.items():
            if value == _UNKNOWN_PROFILE_VALUE or count < threshold:
                continue
            target = (dimension, value)
            targets.add(target)
            target_counts[target] = count
    return targets, target_counts


def _profile_targets(candidate: _Candidate) -> set[tuple[str, str]]:
    return {
        (dimension, candidate.profile[dimension])
        for dimension in _COVERAGE_DIMENSIONS
        if candidate.profile[dimension] != _UNKNOWN_PROFILE_VALUE
    }


def _profile_counts(candidates: Sequence[_Candidate]) -> dict[str, Counter[str]]:
    return {
        dimension: Counter(candidate.profile[dimension] for candidate in candidates)
        for dimension in _COVERAGE_DIMENSIONS
    }


def _dominant_value(counts: Counter[str]) -> str:
    known_counts = {
        value: count
        for value, count in counts.items()
        if value != _UNKNOWN_PROFILE_VALUE
    }
    candidates = known_counts or dict(counts)
    return min(candidates, key=lambda value: (-candidates[value], value))


def _outlier_reasons(candidates: Sequence[_Candidate]) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = {}
    for source_id in _numeric_outlier_ids(candidates, "duration_seconds"):
        reasons.setdefault(source_id, []).append("runtime_outlier")
    for source_id in _numeric_outlier_ids(candidates, "size_bytes"):
        reasons.setdefault(source_id, []).append("source_size_outlier")
    return reasons


def _numeric_outlier_ids(candidates: Sequence[_Candidate], attribute: str) -> set[str]:
    values = [
        float(value)
        for candidate in candidates
        if (value := getattr(candidate, attribute)) is not None
    ]
    if len(values) < 4:
        return set()
    first_quartile, _, third_quartile = quantiles(values, n=4, method="inclusive")
    interquartile_range = third_quartile - first_quartile
    if interquartile_range > 0:
        lower_bound = first_quartile - 1.5 * interquartile_range
        upper_bound = third_quartile + 1.5 * interquartile_range
    else:
        middle = float(median(values))
        lower_bound = middle * 0.5
        upper_bound = middle * 2.0
    return {
        candidate.source_id
        for candidate in candidates
        if (value := getattr(candidate, attribute)) is not None
        and (float(value) < lower_bound or float(value) > upper_bound)
    }


def _median_positive(values: Iterable[object]) -> float | None:
    positive_values = [value for raw_value in values if (value := _positive_float(raw_value)) is not None]
    if not positive_values:
        return None
    return float(median(positive_values))


def _numeric_distance(value: int | float | None, reference: int | float | None) -> float:
    if value is None or reference is None or value <= 0 or reference <= 0:
        return math.inf
    return abs(math.log(float(value) / float(reference)))


def _video_codec_class(value: object) -> str:
    codec = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "avc": "h264",
        "avc1": "h264",
        "h.264": "h264",
        "h265": "hevc",
        "h.265": "hevc",
        "x265": "hevc",
    }
    return aliases.get(codec, codec or _UNKNOWN_PROFILE_VALUE)


def _resolution_class(width: object, height: object) -> str:
    width_value = _positive_int(width)
    height_value = _positive_int(height)
    if width_value is None or height_value is None:
        return _UNKNOWN_PROFILE_VALUE
    largest_dimension = max(width_value, height_value)
    if largest_dimension >= 3800:
        return "2160p"
    if largest_dimension >= 2500:
        return "1440p"
    if largest_dimension >= 1900:
        return "1080p"
    if largest_dimension >= 1200:
        return "720p"
    return f"{width_value}x{height_value}"


def _cadence_class(item: Mapping[str, Any]) -> str:
    cadence = item.get("cadence_class") or item.get("frame_cadence_class") or item.get("frame_rate_class")
    cadence_payload = object_dict(item.get("cadence"))
    cadence = cadence or cadence_payload.get("classification") or cadence_payload.get("class") or cadence_payload.get("kind")
    normalized = str(cadence or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or _UNKNOWN_PROFILE_VALUE


def _audio_layout_class(item: Mapping[str, Any]) -> str:
    explicit_layout = str(item.get("audio_layout") or "").strip().lower()
    if explicit_layout:
        return explicit_layout
    tracks = item.get("audio_summary")
    if isinstance(tracks, str):
        try:
            tracks = json.loads(tracks)
        except json.JSONDecodeError:
            tracks = []
    channels = sorted(
        {
            channel_count
            for track in object_list(tracks)
            if isinstance(track, Mapping)
            and (channel_count := _positive_int(track.get("channels"))) is not None
        }
    )
    if not channels:
        return _UNKNOWN_PROFILE_VALUE
    labels = {
        1: "mono",
        2: "stereo",
        6: "5.1",
        8: "7.1",
    }
    return "+".join(labels.get(channel_count, f"{channel_count}ch") for channel_count in channels)


def _fingerprint_profile(item: Mapping[str, Any]) -> dict[str, str]:
    decision = object_dict(item.get("media_fingerprint_decision"))
    if not decision or str(decision.get("status") or "") != "measured":
        return {dimension: _UNKNOWN_PROFILE_VALUE for dimension in _FINGERPRINT_DIMENSIONS}
    traits = {str(value) for value in object_list(decision.get("traits"))}
    finding_ids = {
        str(finding.get("id"))
        for finding in (object_dict(value) for value in object_list(decision.get("findings")))
    }
    values = traits | finding_ids
    return {
        "luma": "dark" if "dark_luma" in values else "typical",
        "gradient": "gradient_risk" if "dark_gradient_banding_risk" in values else "typical",
        "motion": "high_motion" if "high_motion" in values else "typical",
        "texture": "high_texture" if "high_texture" in values else "typical",
        "noise": _noise_profile(values),
        "duplicate_cadence": "duplicate_cadence" if "duplicate_cadence" in values else "typical",
        "animation": "animation_cues" if "animation_cues" in values else "typical",
        "audio_complexity": "complex" if "audio_complexity" in values else "typical",
    }


def _noise_profile(values: set[str]) -> str:
    for value in (
            "likely_film_grain",
            "likely_analog_noise",
            "compression_noise_advisory",
            "uncertain_noise_mix",
    ):
        if value in values:
            return value
    return "typical"


def _runtime_class(duration_seconds: float | None, median_runtime: float | None) -> str:
    if duration_seconds is None or median_runtime is None or median_runtime <= 0:
        return _UNKNOWN_PROFILE_VALUE
    ratio = duration_seconds / median_runtime
    if ratio < 0.75:
        return "short"
    if ratio > 1.25:
        return "long"
    return "typical"


def _source_fingerprint(item: Mapping[str, Any]) -> str | None:
    value = item.get("source_fingerprint", item.get("fingerprint"))
    fingerprint = str(value or "").strip()
    return fingerprint or None


def _normalized_rel_path(value: object) -> str:
    return str(value or "").strip().strip("/").replace("\\", "/")


def _positive_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _positive_int(value: object) -> int | None:
    parsed = _positive_float(value)
    return int(parsed) if parsed is not None else None


def _fraction(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 1.0
    return round(float(numerator) / float(denominator), 4)


def _prefix_descendant_pattern(prefix: str) -> str:
    escaped_prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped_prefix}/%"
