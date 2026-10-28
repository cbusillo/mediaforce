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
from mediaforce.core.db_tables import encode_jobs, staged_artifacts
from mediaforce.core.schedule_deadline import parse_schedule_close_deadline
from mediaforce.core.type_defs import float_value, object_dict


MAX_HISTORY_ROWS = 1_000
MAX_SAMPLES_PER_HOST = 30
DEFAULT_SOURCE_DURATION_SECONDS = 3_600.0
DEFAULT_ENCODE_REALTIME_FACTOR = 3.0
DEFAULT_QUALITY_OVERHEAD_SECONDS = 1_800.0
FIXED_ADMISSION_SLACK_SECONDS = 300.0
MINIMUM_ADMISSION_SECONDS = 900
MAX_SAMPLE_COUNT = 12
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
        )
        .select_from(
            staged_artifacts.outerjoin(
                encode_jobs,
                encode_jobs.c.job_id == staged_artifacts.c.encode_job_id,
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
    samples_per_host: dict[str, int] = {}
    for row in rows:
        host_key = str(row["encode_host_key"] or "").strip()
        host_label = str(row["encode_host_label"] or "").strip()
        identity = (host_key or host_label).casefold()
        if not identity or samples_per_host.get(identity, 0) >= MAX_SAMPLES_PER_HOST:
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
            )
        )
        samples_per_host[identity] = samples_per_host.get(identity, 0) + 1
    return tuple(samples)


def estimate_encode_duration(
        item: dict[str, Any],
        host: dict[str, Any],
        samples: tuple[EncodeDurationSample, ...],
) -> EncodeDurationEstimate:
    source_duration_seconds = float_value(item.get("duration_seconds")) or DEFAULT_SOURCE_DURATION_SECONDS
    host_tokens = _host_identity_tokens(host)
    host_samples = [sample for sample in samples if _sample_matches_host(sample, host_tokens)]
    host_ratios = [sample.encode_realtime_factor for sample in host_samples]
    global_ratios = [sample.encode_realtime_factor for sample in samples]
    encode_realtime_factor = _conservative_history_value(
        host_ratios,
        global_ratios,
        default=DEFAULT_ENCODE_REALTIME_FACTOR,
    )
    uncertainty_multiplier = _uncertainty_multiplier(len(host_ratios))
    encode_seconds = source_duration_seconds * encode_realtime_factor * uncertainty_multiplier

    host_overheads = [
        sample.quality_overhead_seconds
        for sample in host_samples
        if sample.quality_overhead_seconds is not None
    ]
    global_overheads = [
        sample.quality_overhead_seconds
        for sample in samples
        if sample.quality_overhead_seconds is not None
    ]
    modeled_quality_seconds = _modeled_quality_search_seconds(
        item,
        source_duration_seconds=source_duration_seconds,
        encode_realtime_factor=encode_realtime_factor,
    )
    if host_overheads:
        quality_overhead = _conservative_history_value(
            host_overheads,
            global_overheads,
            default=modeled_quality_seconds,
        )
    elif global_overheads:
        quality_overhead = max(_percentile(global_overheads, 0.80), modeled_quality_seconds)
    else:
        quality_overhead = max(DEFAULT_QUALITY_OVERHEAD_SECONDS, modeled_quality_seconds)
    quality_search_seconds = quality_overhead * uncertainty_multiplier
    total_seconds = max(
        MINIMUM_ADMISSION_SECONDS,
        math.ceil(encode_seconds + quality_search_seconds + FIXED_ADMISSION_SLACK_SECONDS),
    )
    return EncodeDurationEstimate(
        total_seconds=total_seconds,
        encode_seconds=math.ceil(encode_seconds),
        quality_search_seconds=math.ceil(quality_search_seconds),
        source_duration_seconds=source_duration_seconds,
        host_sample_size=len(host_ratios),
        confidence=_estimate_confidence(len(host_ratios)),
    )


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


def _conservative_history_value(
        host_values: list[float],
        global_values: list[float],
        *,
        default: float,
) -> float:
    if len(host_values) >= 8:
        return _percentile(host_values, 0.80)
    if len(host_values) >= 3:
        global_floor = _percentile(global_values, 0.60) if global_values else default
        return max(_percentile(host_values, 0.80), global_floor)
    if host_values:
        global_floor = _percentile(global_values, 0.80) if global_values else default
        return max(max(host_values), global_floor)
    if global_values:
        return _percentile(global_values, 0.80)
    return default


def _uncertainty_multiplier(sample_size: int) -> float:
    if sample_size >= 10:
        return 1.10
    if sample_size >= 4:
        return 1.20
    if sample_size > 0:
        return 1.35
    return 1.50


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


def _host_identity_tokens(host: dict[str, Any]) -> set[str]:
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
