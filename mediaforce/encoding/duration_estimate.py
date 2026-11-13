import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import encode_jobs, library_items, staged_artifacts
from mediaforce.core.schedule_deadline import parse_schedule_close_deadline
from mediaforce.core.type_defs import float_value, object_dict


MAX_HISTORY_ROWS = 1_000
MAX_SAMPLES_PER_HOST_RESOLUTION = 30
DEFAULT_SOURCE_DURATION_SECONDS = 3_600.0
DEFAULT_ENCODE_REALTIME_FACTOR = 3.0
DEFAULT_QUALITY_OVERHEAD_SECONDS = 1_800.0
MINIMUM_TYPICAL_SAMPLES = 3
MAX_SAMPLE_COUNT = 12
RESOLUTION_CLASS_HEIGHTS = ((600, "sd"), (800, "720p"), (1_500, "1080p"))
DURATION_SAMPLE_CACHE_SECONDS = 60.0
MAX_CACHED_DATABASES = 8
_DURATION_TEXT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.IGNORECASE)
_DURATION_SAMPLE_CACHE: OrderedDict[str, tuple[float, tuple["EncodeDurationSample", ...]]] = OrderedDict()
_DURATION_SAMPLE_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class EncodeDurationSample:
    host_key: str
    host_label: str
    encode_realtime_factor: float
    quality_overhead_seconds: float | None
    resolution_class: str | None = None


@dataclass(frozen=True, slots=True)
class EncodeDurationEstimate:
    total_seconds: int
    encode_seconds: int
    quality_search_seconds: int
    source_duration_seconds: float
    host_sample_size: int
    confidence: str


def load_encode_duration_samples(connection: DBClient) -> tuple[EncodeDurationSample, ...]:
    cache_key = str(connection.engine.url.database or id(connection.engine))
    current_time = time.monotonic()
    with _DURATION_SAMPLE_CACHE_LOCK:
        cached = _DURATION_SAMPLE_CACHE.get(cache_key)
        if cached is not None and current_time - cached[0] < DURATION_SAMPLE_CACHE_SECONDS:
            _DURATION_SAMPLE_CACHE.move_to_end(cache_key)
            return cached[1]
    samples = _query_encode_duration_samples(connection)
    with _DURATION_SAMPLE_CACHE_LOCK:
        _DURATION_SAMPLE_CACHE[cache_key] = (current_time, samples)
        _DURATION_SAMPLE_CACHE.move_to_end(cache_key)
        while len(_DURATION_SAMPLE_CACHE) > MAX_CACHED_DATABASES:
            _DURATION_SAMPLE_CACHE.popitem(last=False)
    return samples


def _query_encode_duration_samples(connection: DBClient) -> tuple[EncodeDurationSample, ...]:
    rows = connection.execute(
        select(
            staged_artifacts.c.encode_job_id,
            staged_artifacts.c.encode_host_key,
            staged_artifacts.c.encode_host_label,
            staged_artifacts.c.source_duration_seconds,
            staged_artifacts.c.encode_duration_seconds,
            staged_artifacts.c.encode_started_at,
            encode_jobs.c.started_at.label("job_started_at"),
            library_items.c.height,
        )
        .select_from(
            staged_artifacts.outerjoin(
                encode_jobs,
                encode_jobs.c.job_id == staged_artifacts.c.encode_job_id,
            ).outerjoin(
                library_items,
                library_items.c.id == staged_artifacts.c.library_item_id,
            )
        )
        .where(staged_artifacts.c.source_duration_seconds > 0)
        .where(staged_artifacts.c.encode_duration_seconds > 0)
        .order_by(
            func.coalesce(
                staged_artifacts.c.encode_completed_at,
                staged_artifacts.c.staged_at,
                staged_artifacts.c.updated_at,
            ).desc()
        )
        .limit(MAX_HISTORY_ROWS)
    ).mappings().fetchall()
    first_encode_started_by_job: dict[str, datetime] = {}
    for row in rows:
        job_id = str(row["encode_job_id"] or "").strip()
        encode_started = _parse_timestamp(row["encode_started_at"])
        if not job_id or encode_started is None:
            continue
        earliest = first_encode_started_by_job.get(job_id)
        if earliest is None or encode_started < earliest:
            first_encode_started_by_job[job_id] = encode_started
    samples: list[EncodeDurationSample] = []
    samples_per_host: dict[tuple[str, str | None], int] = {}
    for row in rows:
        host_key = str(row["encode_host_key"] or "").strip()
        host_label = str(row["encode_host_label"] or "").strip()
        identity = (host_key or host_label).casefold()
        sample_resolution = resolution_class(row["height"])
        cap_key = (identity, sample_resolution)
        if not identity or samples_per_host.get(cap_key, 0) >= MAX_SAMPLES_PER_HOST_RESOLUTION:
            continue
        source_duration_seconds = float_value(row["source_duration_seconds"])
        encode_duration_seconds = float_value(row["encode_duration_seconds"])
        encode_realtime_factor = (
            encode_duration_seconds / source_duration_seconds
            if source_duration_seconds > 0 and encode_duration_seconds > 0
            else 0.0
        )
        if not math.isfinite(encode_realtime_factor) or not 0.02 <= encode_realtime_factor <= 20.0:
            continue
        job_id = str(row["encode_job_id"] or "").strip()
        encode_started = _parse_timestamp(row["encode_started_at"])
        quality_overhead_seconds = (
            _quality_overhead_seconds(row["job_started_at"], row["encode_started_at"])
            if job_id
            and encode_started is not None
            and encode_started == first_encode_started_by_job.get(job_id)
            else None
        )
        samples.append(
            EncodeDurationSample(
                host_key=host_key,
                host_label=host_label,
                encode_realtime_factor=encode_realtime_factor,
                quality_overhead_seconds=quality_overhead_seconds,
                resolution_class=sample_resolution,
            )
        )
        samples_per_host[cap_key] = samples_per_host.get(cap_key, 0) + 1
    return tuple(samples)


def estimate_encode_duration(
        item: dict[str, Any],
        host: dict[str, Any],
        samples: tuple[EncodeDurationSample, ...],
) -> EncodeDurationEstimate:
    source_duration_seconds = float_value(item.get("duration_seconds")) or DEFAULT_SOURCE_DURATION_SECONDS
    host_tokens = host_identity_tokens(host)
    host_samples = [sample for sample in samples if _sample_matches_host(sample, host_tokens)]
    comparable = _closest_sample_groups(host_samples, samples, resolution_class(item.get("height")))
    speeds = [[sample.encode_realtime_factor for sample in group] for group in comparable]
    encode_realtime_factor = _typical_value(speeds, default=DEFAULT_ENCODE_REALTIME_FACTOR)
    encode_seconds = source_duration_seconds * encode_realtime_factor
    quality_search_seconds = _typical_value(
        [[sample.quality_overhead_seconds for sample in group if sample.quality_overhead_seconds is not None]
         for group in comparable],
        default=_modeled_quality_search_seconds(
            item,
            source_duration_seconds=source_duration_seconds,
            encode_realtime_factor=encode_realtime_factor,
        ),
    )
    return EncodeDurationEstimate(
        total_seconds=math.ceil(encode_seconds + quality_search_seconds),
        encode_seconds=math.ceil(encode_seconds),
        quality_search_seconds=math.ceil(quality_search_seconds),
        source_duration_seconds=source_duration_seconds,
        host_sample_size=len(host_samples),
        confidence=_estimate_confidence(len(host_samples)),
    )


def resolution_class(height: Any) -> str | None:
    pixels_high = float_value(height)
    if pixels_high <= 0:
        return None
    return next((label for limit, label in RESOLUTION_CLASS_HEIGHTS if pixels_high <= limit), "2160p")


def _closest_sample_groups(
        host_samples: list[EncodeDurationSample],
        all_samples: tuple[EncodeDurationSample, ...],
        item_resolution: str | None,
) -> list[list[EncodeDurationSample]]:
    def same_resolution(group: list[EncodeDurationSample]) -> list[EncodeDurationSample]:
        return [sample for sample in group if item_resolution and sample.resolution_class == item_resolution]

    return [same_resolution(host_samples), host_samples, same_resolution(list(all_samples)), list(all_samples)]


def _typical_value(groups: list[list[float]], *, default: float) -> float:
    values = _first_usable_group(groups)
    return _percentile(values, 0.50) if values else default


def _first_usable_group(groups: list[list[float]]) -> list[float] | None:
    well_sampled = next((values for values in groups if len(values) >= MINIMUM_TYPICAL_SAMPLES), None)
    return well_sampled or next((values for values in groups if values), None)


def estimate_encode_job_duration(
        items: list[dict[str, Any]],
        host: dict[str, Any],
        samples: tuple[EncodeDurationSample, ...],
) -> EncodeDurationEstimate:
    item_estimates = [estimate_encode_duration(item, host, samples) for item in items] or [
        estimate_encode_duration({}, host, samples)
    ]
    confidence_order = {"limited": 0, "moderate": 1, "high": 2}
    confidence = min(item_estimates, key=lambda estimate: confidence_order[estimate.confidence]).confidence
    return EncodeDurationEstimate(
        total_seconds=sum(estimate.total_seconds for estimate in item_estimates),
        encode_seconds=sum(estimate.encode_seconds for estimate in item_estimates),
        quality_search_seconds=sum(estimate.quality_search_seconds for estimate in item_estimates),
        source_duration_seconds=sum(estimate.source_duration_seconds for estimate in item_estimates),
        host_sample_size=min(estimate.host_sample_size for estimate in item_estimates),
        confidence=confidence,
    )


def estimate_fits_before_schedule_close(
        estimate: EncodeDurationEstimate,
        host: dict[str, Any],
        *,
        now: datetime,
) -> bool:
    closes_at = parse_schedule_close_deadline(host.get("schedule_closes_at"))
    if closes_at is None:
        return True
    return estimate.total_seconds <= max((closes_at - now.astimezone(UTC)).total_seconds(), 0.0)


def _estimate_confidence(sample_size: int) -> str:
    if sample_size >= 10:
        return "high"
    if sample_size >= 4:
        return "moderate"
    return "limited"


def _modeled_quality_search_seconds(
        item: dict[str, Any],
        *,
        source_duration_seconds: float,
        encode_realtime_factor: float,
) -> float:
    video_policy = object_dict(object_dict(item.get("resolved_policy")).get("video"))
    sample_every_seconds = _duration_text_seconds(video_policy.get("sample_every"), default=480.0)
    sample_duration_seconds = _duration_text_seconds(video_policy.get("sample_duration"), default=20.0)
    sample_count = max(1, min(MAX_SAMPLE_COUNT, math.ceil(source_duration_seconds / sample_every_seconds)))
    search_passes = 8 if bool(video_policy.get("thorough")) else 6
    bounded_realtime_factor = min(max(encode_realtime_factor, 0.5), 6.0)
    return max(
        300.0,
        sample_count * sample_duration_seconds * bounded_realtime_factor * search_passes,
    )


def _duration_text_seconds(value: Any, *, default: float) -> float:
    match = _DURATION_TEXT_RE.fullmatch(str(value or ""))
    if match is None:
        return default
    magnitude = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    multiplier = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3_600.0}[unit]
    seconds = magnitude * multiplier
    return seconds if seconds > 0 else default


def _quality_overhead_seconds(job_started_at: Any, encode_started_at: Any) -> float | None:
    job_started = _parse_timestamp(job_started_at)
    encode_started = _parse_timestamp(encode_started_at)
    if job_started is None or encode_started is None or encode_started < job_started:
        return None
    overhead = (encode_started - job_started).total_seconds()
    return overhead if overhead <= 21_600 else None


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def host_identity_tokens(host: dict[str, Any]) -> set[str]:
    tokens = {
        str(host.get(key) or "").strip().casefold()
        for key in ("key", "host", "label")
        if str(host.get(key) or "").strip()
    }
    identity_tokens = host.get("identity_tokens")
    if isinstance(identity_tokens, list):
        tokens.update(str(token).strip().casefold() for token in identity_tokens if str(token).strip())
    return tokens


def _sample_matches_host(sample: EncodeDurationSample, host_tokens: set[str]) -> bool:
    sample_tokens = {
        token.casefold()
        for token in (sample.host_key, sample.host_label)
        if token
    }
    return bool(host_tokens & sample_tokens)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(value for value in values if math.isfinite(value) and value >= 0)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    weight = position - lower_index
    return ordered[lower_index] * (1.0 - weight) + ordered[upper_index] * weight
