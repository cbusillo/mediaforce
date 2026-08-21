from __future__ import annotations

import math
import shutil
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
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


@dataclass(frozen=True, slots=True)
class FreeSpaceReservePolicy:
    operating_headroom_bytes: int
    staged_output_overhead_percent: float
    large_job_bytes: int


@dataclass(frozen=True, slots=True)
class VolumeCapacity:
    key: str
    path: Path
    free_bytes: int


VolumeProbe = Callable[[Path], VolumeCapacity]
CapacityCache = dict[tuple[str, Path], VolumeCapacity]


@dataclass(frozen=True, slots=True)
class SpaceRequirement:
    path: Path
    bytes_required: int
    reason: str
    capacity: VolumeCapacity | None = None


@dataclass(frozen=True, slots=True)
class ReservePreflight:
    allowed: bool
    waiting_reason: str | None
    required_by_volume: dict[str, int]
    measurable: bool = True


class ReserveInputError(ValueError):
    pass


def free_space_reserve_policy(config: MediaforceConfig) -> FreeSpaceReservePolicy:
    payload = object_dict(config.free_space_reserve)
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
    nearest_parent_script = """path=$1
while [ ! -e "$path" ]; do
    parent=$(dirname "$path")
    if [ "$parent" = "$path" ]; then
        echo "no existing parent for $1" >&2
        exit 1
    fi
    path=$parent
done
exec df -Pk "$path"
"""
    result = run_remote_command(
        host,
        ["sh", "-lc", nearest_parent_script, "mediaforce-df", str(path)],
        timeout=REMOTE_DISK_USAGE_TIMEOUT_SECONDS,
    )
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
    return VolumeCapacity(
        key=f"remote:{_remote_host_capacity_key(host)}:{fields[0]}",
        path=path,
        free_bytes=free_bytes,
    )


def encode_reserve_preflight(
        config: MediaforceConfig,
        items: Iterable[dict[str, Any]],
        *,
        host: dict[str, Any] | None = None,
        volume_probe: VolumeProbe | None = None,
        volume_probe_key: str | None = None,
        capacity_cache: CapacityCache | None = None,
        reserved_by_volume: Mapping[str, int] | None = None,
) -> ReservePreflight:
    policy = free_space_reserve_policy(config)
    if volume_probe is not None and capacity_cache is not None and not volume_probe_key:
        return _unmeasurable_preflight(
            ReserveInputError("a shared custom volume probe requires a stable volume_probe_key")
        )
    encode_probe = volume_probe or _volume_probe_for_host(host)
    controller_probe = volume_probe or local_volume_capacity
    shared_capacity_cache = capacity_cache if capacity_cache is not None else {}
    custom_probe_key = (volume_probe_key or "custom") if volume_probe is not None else None
    encode_probe_key = custom_probe_key or _volume_probe_key_for_host(host)
    controller_probe_key = custom_probe_key or "controller"
    try:
        requirements = list(
            _encode_space_requirements(
                config,
                items,
                host=host,
                policy=policy,
                encode_volume_probe=encode_probe,
                encode_volume_probe_key=encode_probe_key,
                controller_volume_probe=controller_probe,
                controller_volume_probe_key=controller_probe_key,
                capacity_cache=shared_capacity_cache,
            )
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return _unmeasurable_preflight(exc)
    return check_free_space_reserve(
        requirements,
        policy=policy,
        volume_probe=controller_probe,
        volume_probe_key=controller_probe_key,
        capacity_cache=shared_capacity_cache,
        reserved_by_volume=reserved_by_volume,
    )


def promotion_reserve_preflight(
        config: MediaforceConfig,
        *,
        source_path: Path,
        staging_path: Path,
        destination_path: Path,
        archive_path: Path,
        volume_probe: VolumeProbe = local_volume_capacity,
        volume_probe_key: str | None = None,
) -> ReservePreflight:
    policy = free_space_reserve_policy(config)
    capacity_cache: CapacityCache = {}
    probe_key = volume_probe_key or "promotion"
    try:
        requirements = list(
            _promotion_space_requirements(
                source_path=source_path,
                staging_path=staging_path,
                destination_path=destination_path,
                archive_path=archive_path,
                volume_probe=volume_probe,
                volume_probe_key=probe_key,
                capacity_cache=capacity_cache,
                source_bytes=_path_size_bytes(source_path) if source_path.exists() else None,
                staged_output_bytes=_path_size_bytes(staging_path),
            )
        )
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        return _unmeasurable_preflight(exc)
    return check_free_space_reserve(
        requirements,
        policy=policy,
        volume_probe=volume_probe,
        volume_probe_key=probe_key,
        capacity_cache=capacity_cache,
    )


def check_free_space_reserve(
        requirements: Iterable[SpaceRequirement],
        *,
        policy: FreeSpaceReservePolicy,
        volume_probe: VolumeProbe,
        volume_probe_key: str = "default",
        capacity_cache: CapacityCache | None = None,
        reserved_by_volume: Mapping[str, int] | None = None,
) -> ReservePreflight:
    totals: dict[str, int] = defaultdict(int)
    capacities: dict[str, VolumeCapacity] = {}
    reasons: dict[str, list[str]] = defaultdict(list)
    shared_capacity_cache = capacity_cache if capacity_cache is not None else {}
    for requirement in requirements:
        try:
            capacity = requirement.capacity or _cached_volume_capacity(
                requirement.path,
                volume_probe,
                volume_probe_key,
                shared_capacity_cache,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return ReservePreflight(
                allowed=False,
                waiting_reason=(
                    f"Waiting for a measurable free-space reserve: cannot measure {requirement.path}. "
                    f"Mount or repair that storage, then retry. ({exc})"
                ),
                required_by_volume=dict(totals),
                measurable=False,
            )
        totals[capacity.key] += requirement.bytes_required
        capacities[capacity.key] = capacity
        reasons[capacity.key].append(requirement.reason)
    for key, required_bytes in totals.items():
        capacity = capacities[key]
        active_reserved_bytes = max(int((reserved_by_volume or {}).get(key, 0)), 0)
        minimum_free = required_bytes + active_reserved_bytes + policy.operating_headroom_bytes
        if capacity.free_bytes >= minimum_free:
            continue
        reason_summary = ", ".join(sorted(set(reasons[key])))
        active_reserve_detail = (
            f", including {_format_bytes(active_reserved_bytes)} already reserved by active work"
            if active_reserved_bytes > 0
            else ""
        )
        return ReservePreflight(
            allowed=False,
            waiting_reason=(
                f"Waiting for free-space reserve on {capacity.path}: needs {_format_bytes(minimum_free)} free "
                f"for {reason_summary} plus operating headroom{active_reserve_detail}, "
                f"but only {_format_bytes(capacity.free_bytes)} is available."
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
        encode_volume_probe: VolumeProbe,
        encode_volume_probe_key: str,
        controller_volume_probe: VolumeProbe,
        controller_volume_probe_key: str,
        capacity_cache: CapacityCache,
) -> Iterable[SpaceRequirement]:
    materialized = list(items)
    if not materialized:
        raise ReserveInputError("no encode items were available")
    for item in materialized:
        source_path = _controller_source_path(config, item)
        staging_path = _encode_staging_path(config, item, host=host)
        rel_path = str(item.get("rel_path") or "").strip().strip("/")
        if not rel_path:
            raise ReserveInputError("relative media path is unavailable")
        archive_path = config.archive_root / Path(rel_path)
        source_bytes = _source_size_bytes(item)
        output_bytes = _planned_output_bytes(item, policy)
        destination_suffix = staging_path.suffix or source_path.suffix
        staging_capacity = _cached_volume_capacity(
            staging_path.parent,
            encode_volume_probe,
            encode_volume_probe_key,
            capacity_cache,
        )
        yield SpaceRequirement(
            staging_path.parent,
            output_bytes,
            "staged output",
            capacity=staging_capacity,
        )
        if encode_volume_probe_key != controller_volume_probe_key:
            controller_staging_root = config.staging_root_for_host(host).expanduser()
            if not controller_staging_root.is_dir():
                raise OSError(
                    f"controller-visible staging root is unavailable: {controller_staging_root}"
                )
            controller_staging_capacity = _cached_volume_capacity(
                staging_path.parent,
                controller_volume_probe,
                controller_volume_probe_key,
                capacity_cache,
            )
            yield SpaceRequirement(
                staging_path.parent,
                output_bytes,
                "controller-visible staged output",
                capacity=controller_staging_capacity,
            )
        yield from _promotion_space_requirements(
            source_path=source_path,
            staging_path=staging_path,
            destination_path=source_path.with_suffix(destination_suffix),
            archive_path=archive_path,
            source_bytes=source_bytes,
            staged_output_bytes=output_bytes,
            volume_probe=controller_volume_probe,
            volume_probe_key=controller_volume_probe_key,
            capacity_cache=capacity_cache,
        )


def _promotion_space_requirements(
        *,
        source_path: Path,
        staging_path: Path,
        destination_path: Path,
        archive_path: Path,
        source_bytes: int | None,
        volume_probe: VolumeProbe,
        volume_probe_key: str,
        capacity_cache: CapacityCache,
        staged_output_bytes: int | None = None,
) -> Iterable[SpaceRequirement]:
    output_bytes = staged_output_bytes if staged_output_bytes is not None else _path_size_bytes(staging_path)
    staging_volume = _cached_volume_capacity(staging_path.parent, volume_probe, volume_probe_key, capacity_cache)
    destination_volume = _cached_volume_capacity(
        destination_path.parent,
        volume_probe,
        volume_probe_key,
        capacity_cache,
    )
    if source_bytes is not None:
        source_volume = _cached_volume_capacity(source_path, volume_probe, volume_probe_key, capacity_cache)
        archive_volume = _cached_volume_capacity(
            archive_path.parent,
            volume_probe,
            volume_probe_key,
            capacity_cache,
        )
        if source_volume.key != archive_volume.key:
            yield SpaceRequirement(
                archive_path.parent,
                source_bytes,
                "source archive copy",
                capacity=archive_volume,
            )
    if staging_volume.key != destination_volume.key:
        yield SpaceRequirement(
            destination_path.parent,
            output_bytes,
            "staged output promotion",
            capacity=destination_volume,
        )


def _volume_probe_for_host(host: dict[str, Any] | None) -> VolumeProbe:
    if host_media_access_for_host(host) == "stream" or execution_mode_for_host(host) != "ssh":
        return local_volume_capacity
    return lambda path: remote_volume_capacity(dict(host or {}), path)


def _volume_probe_key_for_host(host: dict[str, Any] | None) -> str:
    if host_media_access_for_host(host) == "stream" or execution_mode_for_host(host) != "ssh":
        return "controller"
    return f"remote:{_remote_host_capacity_key(dict(host or {}))}"


def _remote_host_capacity_key(host: Mapping[str, Any]) -> str:
    return str(host.get("key") or host.get("host") or host.get("label") or "unknown").strip() or "unknown"


def _controller_source_path(config: MediaforceConfig, item: dict[str, Any]) -> Path:
    source_value = str(item.get("source_path") or "").strip()
    if source_value:
        return Path(source_value)
    try:
        return resolve_item_source_path(
            config,
            item,
            host=None,
            host_media_access_for_host=host_media_access_for_host,
        )
    except KeyError as exc:
        raise ReserveInputError("controller source path is unavailable") from exc


def _encode_staging_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None,
) -> Path:
    staging_value = str(item.get("staging_path") or "").strip()
    if execution_mode_for_host(host) != "ssh" or host_media_access_for_host(host) == "stream":
        if staging_value:
            return Path(staging_value)
    try:
        return resolve_item_staging_path(
            config,
            item,
            host=host,
            host_media_access_for_host=host_media_access_for_host,
        )
    except KeyError as exc:
        raise ReserveInputError("encode staging path is unavailable") from exc


def _nearest_existing_path(path: Path) -> Path:
    current = path.expanduser()
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise OSError(f"no existing parent for {path}")
        current = parent
    return current


def _source_size_bytes(item: dict[str, Any]) -> int:
    configured_size = int_value(item.get("source_size_bytes") or item.get("size_bytes"))
    if configured_size > 0:
        return configured_size
    raise ReserveInputError("source size evidence is unavailable")


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


def _cached_volume_capacity(
        path: Path,
        volume_probe: VolumeProbe,
        volume_probe_key: str,
        cache: CapacityCache,
) -> VolumeCapacity:
    cache_key = (volume_probe_key, path)
    capacity = cache.get(cache_key)
    if capacity is None:
        capacity = volume_probe(path)
        cache[cache_key] = capacity
    return capacity


def _format_bytes(value: int) -> str:
    return f"{value / GIBIBYTE:.1f} GiB"


def _unmeasurable_preflight(exc: Exception) -> ReservePreflight:
    if isinstance(exc, (KeyError, ReserveInputError)):
        return ReservePreflight(
            allowed=False,
            waiting_reason=(
                "Waiting for complete free-space reserve inputs. Rebuild the production plan or rescan "
                f"the library, then retry. ({exc})"
            ),
            required_by_volume={},
            measurable=False,
        )
    return ReservePreflight(
        allowed=False,
        waiting_reason=(
            "Waiting for a measurable free-space reserve. Mount or repair the required storage, "
            f"then retry. ({exc})"
        ),
        required_by_volume={},
        measurable=False,
    )
