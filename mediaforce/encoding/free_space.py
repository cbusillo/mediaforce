from __future__ import annotations

import math
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.encoding.helpers import resolve_item_source_path, resolve_item_staging_path
from mediaforce.hosts.config import execution_mode_for_host, host_media_access_for_host
from mediaforce.remote import run_remote_command

GIBIBYTE = 1024 ** 3
REMOTE_DISK_USAGE_TIMEOUT_SECONDS = 10
DEFAULT_OPERATING_HEADROOM_GIB = 16
DEFAULT_STAGED_OUTPUT_OVERHEAD_PERCENT = 10
DEFAULT_LARGE_JOB_GIB = 16


@dataclass(frozen=True, slots=True)
class FreeSpaceReservePolicy:
    operating_headroom_bytes: int
    staged_output_overhead_percent: float
    large_job_bytes: int


@dataclass(frozen=True, slots=True)
class SpaceRequirement:
    path: Path
    bytes_required: int
    reason: str


@dataclass(frozen=True, slots=True)
class VolumeCapacity:
    key: str
    path: Path
    free_bytes: int


@dataclass(frozen=True, slots=True)
class ReservePreflight:
    allowed: bool
    waiting_reason: str | None
    required_by_volume: dict[str, int]


VolumeProbe = Callable[[Path], VolumeCapacity]


def reserve_inputs_available(items: Iterable[dict[str, Any]]) -> bool:
    materialized = list(items)
    return bool(materialized) and all(
        str(item.get("source_path") or "").strip()
        and str(item.get("staging_path") or "").strip()
        and str(item.get("rel_path") or "").strip()
        for item in materialized
    )


def free_space_reserve_policy(config: MediaforceConfig) -> FreeSpaceReservePolicy:
    payload = {
        "operating_headroom_gib": DEFAULT_OPERATING_HEADROOM_GIB,
        "staged_output_overhead_percent": DEFAULT_STAGED_OUTPUT_OVERHEAD_PERCENT,
        "large_job_gib": DEFAULT_LARGE_JOB_GIB,
        **object_dict(config.media.get("free_space_reserve")),
    }
    return FreeSpaceReservePolicy(
        operating_headroom_bytes=int(math.ceil(float(payload["operating_headroom_gib"]) * GIBIBYTE)),
        staged_output_overhead_percent=float(payload["staged_output_overhead_percent"]),
        large_job_bytes=int(math.ceil(float(payload["large_job_gib"]) * GIBIBYTE)),
    )


def local_volume_capacity(path: Path) -> VolumeCapacity:
    measurement_path = _nearest_existing_path(path)
    usage = shutil.disk_usage(measurement_path)
    return VolumeCapacity(
        key=f"local:{measurement_path.stat().st_dev}",
        path=measurement_path,
        free_bytes=int(usage.free),
    )


def remote_volume_capacity(host: dict[str, Any], path: Path) -> VolumeCapacity:
    result = run_remote_command(host, ["df", "-Pk", str(path)], timeout=REMOTE_DISK_USAGE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "remote df failed"
        raise OSError(detail)
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[-1]) < 6:
        raise OSError(f"remote df returned an unreadable result for {path}")
    fields = lines[-1]
    try:
        free_bytes = int(fields[3]) * 1024
    except ValueError as exc:
        raise OSError(f"remote df returned an invalid free-space value for {path}") from exc
    return VolumeCapacity(key=f"remote:{fields[0]}", path=path, free_bytes=free_bytes)


def encode_reserve_preflight(
        config: MediaforceConfig,
        items: Iterable[dict[str, Any]],
        *,
        host: dict[str, Any] | None = None,
        volume_probe: VolumeProbe | None = None,
) -> ReservePreflight:
    policy = free_space_reserve_policy(config)
    probe = volume_probe or _volume_probe_for_host(host)
    try:
        requirements = list(_encode_space_requirements(config, items, host=host, policy=policy, volume_probe=probe))
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return _unmeasurable_preflight(exc)
    return check_free_space_reserve(requirements, policy=policy, volume_probe=probe)


def promotion_reserve_preflight(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        source_path: Path,
        staging_path: Path,
        destination_path: Path,
        archive_path: Path,
        volume_probe: VolumeProbe = local_volume_capacity,
) -> ReservePreflight:
    policy = free_space_reserve_policy(config)
    try:
        requirements = list(
            _promotion_space_requirements(
                item,
                source_path=source_path,
                staging_path=staging_path,
                destination_path=destination_path,
                archive_path=archive_path,
                volume_probe=volume_probe,
                staged_output_bytes=_path_size_bytes(staging_path),
            )
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return _unmeasurable_preflight(exc)
    return check_free_space_reserve(requirements, policy=policy, volume_probe=volume_probe)


def check_free_space_reserve(
        requirements: Iterable[SpaceRequirement],
        *,
        policy: FreeSpaceReservePolicy,
        volume_probe: VolumeProbe,
) -> ReservePreflight:
    totals: dict[str, int] = defaultdict(int)
    capacities: dict[str, VolumeCapacity] = {}
    reasons: dict[str, list[str]] = defaultdict(list)
    for requirement in requirements:
        try:
            capacity = volume_probe(requirement.path)
        except (OSError, RuntimeError, ValueError) as exc:
            return ReservePreflight(
                allowed=False,
                waiting_reason=(
                    f"Waiting for a measurable free-space reserve: cannot measure {requirement.path}. "
                    f"Mount or repair that storage, then retry. ({exc})"
                ),
                required_by_volume=dict(totals),
            )
        totals[capacity.key] += requirement.bytes_required
        capacities[capacity.key] = capacity
        reasons[capacity.key].append(requirement.reason)
    for key, required_bytes in totals.items():
        capacity = capacities[key]
        minimum_free = required_bytes + policy.operating_headroom_bytes
        if capacity.free_bytes >= minimum_free:
            continue
        reason_summary = ", ".join(sorted(set(reasons[key])))
        return ReservePreflight(
            allowed=False,
            waiting_reason=(
                f"Waiting for free-space reserve on {capacity.path}: needs {_format_bytes(minimum_free)} free "
                f"for {reason_summary} plus operating headroom, but only {_format_bytes(capacity.free_bytes)} is available."
            ),
            required_by_volume=dict(totals),
        )
    return ReservePreflight(allowed=True, waiting_reason=None, required_by_volume=dict(totals))


def large_job_requires_serialization(config: MediaforceConfig, items: Iterable[dict[str, Any]]) -> bool:
    threshold = free_space_reserve_policy(config).large_job_bytes
    return sum(_source_size_bytes(item) for item in items) >= threshold


def _encode_space_requirements(
        config: MediaforceConfig,
        items: Iterable[dict[str, Any]],
        *,
        host: dict[str, Any] | None,
        policy: FreeSpaceReservePolicy,
        volume_probe: VolumeProbe,
) -> Iterable[SpaceRequirement]:
    for item in items:
        source_value = str(item.get("source_path") or "").strip()
        source_path = (
            Path(source_value)
            if source_value
            else resolve_item_source_path(
                config,
                item,
                host=host,
                host_media_access_for_host=host_media_access_for_host,
            )
        )
        staging_value = str(item.get("staging_path") or "").strip()
        staging_path = (
            Path(staging_value)
            if staging_value
            else resolve_item_staging_path(
                config,
                item,
                host=host,
                host_media_access_for_host=host_media_access_for_host,
            )
        )
        archive_root = config.archive_root_for_host(host)
        archive_path = archive_root / Path(str(item["rel_path"]))
        output_bytes = _planned_output_bytes(item, policy)
        destination_suffix = staging_path.suffix or source_path.suffix
        yield SpaceRequirement(staging_path.parent, output_bytes, "staged output")
        yield from _promotion_space_requirements(
            item,
            source_path=source_path,
            staging_path=staging_path,
            destination_path=source_path.with_suffix(destination_suffix),
            archive_path=archive_path,
            staged_output_bytes=output_bytes,
            volume_probe=volume_probe,
        )


def _promotion_space_requirements(
        item: dict[str, Any],
        *,
        source_path: Path,
        staging_path: Path,
        destination_path: Path,
        archive_path: Path,
        volume_probe: VolumeProbe,
        staged_output_bytes: int | None = None,
) -> Iterable[SpaceRequirement]:
    source_bytes = _source_size_bytes(item, source_path=source_path)
    output_bytes = staged_output_bytes if staged_output_bytes is not None else _path_size_bytes(staging_path)
    source_volume = volume_probe(source_path)
    staging_volume = volume_probe(staging_path.parent)
    archive_volume = volume_probe(archive_path.parent)
    destination_volume = volume_probe(destination_path.parent)
    if source_volume.key != archive_volume.key:
        yield SpaceRequirement(archive_path.parent, source_bytes, "source archive copy")
    if staging_volume.key != destination_volume.key:
        yield SpaceRequirement(destination_path.parent, output_bytes, "staged output promotion")


def _volume_probe_for_host(host: dict[str, Any] | None) -> VolumeProbe:
    if host_media_access_for_host(host) == "stream" or execution_mode_for_host(host) != "ssh":
        return local_volume_capacity
    return lambda path: remote_volume_capacity(dict(host or {}), path)


def _nearest_existing_path(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise OSError(f"no existing parent for {path}")
        current = parent
    return current


def _source_size_bytes(item: dict[str, Any], *, source_path: Path | None = None) -> int:
    if source_path is not None:
        return _path_size_bytes(source_path)
    configured_size = int_value(item.get("size_bytes"))
    if configured_size > 0:
        return configured_size
    raise OSError("source size is unavailable")


def _planned_output_bytes(item: dict[str, Any], policy: FreeSpaceReservePolicy) -> int:
    stream_budget = object_dict(item.get("stream_budget_ledger"))
    target_bytes = int_value(stream_budget.get("total_target_bytes"))
    if target_bytes <= 0:
        operator_intent = object_dict(item.get("resolved_operator_intent"))
        size_goal = object_dict(operator_intent.get("size_goal"))
        target_bytes = int_value(size_goal.get("target_size_bytes"))
    if target_bytes <= 0:
        resolved_policy = object_dict(item.get("resolved_policy"))
        video_policy = object_dict(resolved_policy.get("video"))
        source_bytes = _source_size_bytes(item)
        max_encoded_percent = float_value(video_policy.get("max_encoded_percent"))
        target_bytes = (
            int(math.ceil(source_bytes * max_encoded_percent / 100.0))
            if max_encoded_percent > 0
            else source_bytes
        )
    overhead_multiplier = 1.0 + policy.staged_output_overhead_percent / 100.0
    return int(math.ceil(target_bytes * overhead_multiplier))


def _path_size_bytes(path: Path) -> int:
    size = path.stat().st_size
    if size < 0:
        raise OSError(f"invalid size for {path}")
    return int(size)


def _format_bytes(value: int) -> str:
    return f"{value / GIBIBYTE:.1f} GiB"


def _unmeasurable_preflight(exc: Exception) -> ReservePreflight:
    return ReservePreflight(
        allowed=False,
        waiting_reason=(
            "Waiting for a measurable free-space reserve. Mount or repair the required storage, "
            f"then retry. ({exc})"
        ),
        required_by_volume={},
    )
