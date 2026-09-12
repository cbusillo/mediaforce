from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
import sys
import threading
from typing import Any, Literal, cast
import uuid

from mediaforce.core.config import MediaforceConfig, load_runtime_settings, save_runtime_settings
from mediaforce.hosts.config import host_media_access_for_host
from mediaforce.hosts.controller_mount import (
    ControllerMountResult,
    controller_mount_lock,
    mount_controller_smb_no_ui,
    probe_controller_mount,
    probe_controller_volume,
)
from mediaforce.hosts.mount_runtime import (
    ControllerSmbMount,
    configured_controller_smb_mounts,
    finder_mount_roots_for_paths,
)


CONTROLLER_STORAGE_RECOVERY_FILE_NAME = "controller-storage-recovery.json"
_SCHEMA_VERSION = 1
_INITIAL_RETRY_SECONDS = 30
_MAX_RETRY_SECONDS = 300
_READY_FRESHNESS_SECONDS = 90
_PROCESS_GENERATION = uuid.uuid4().hex
_ACTION_REQUIRED_FAILURES = frozenset({
    "invalid_mapping",
    "mount_identity_mismatch",
    "mount_path_occupied",
    "mount_result_unknown",
    "mount_timeout",
    "unexpected_mount_path",
})

RecoveryStatus = Literal["checking", "retrying", "ready", "action_required"]
PathAccess = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class ControllerStorageMountState:
    mount_point: str
    requirement_signature: str | None
    verification_generation: str | None
    status: RecoveryStatus
    reason: str | None
    detail: str | None
    last_failure_kind: str | None
    first_failure_at: str | None
    last_check_at: str | None
    last_attempt_at: str | None
    next_retry_at: str | None
    failure_count: int


@dataclass(frozen=True, slots=True)
class ControllerStorageRecoverySnapshot:
    schema_version: int
    updated_at: str | None
    mounts: tuple[ControllerStorageMountState, ...]
    state_error: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "mounts": [asdict(mount) for mount in self.mounts],
            "state_error": self.state_error,
        }


def controller_storage_recovery_path(config: MediaforceConfig) -> Path:
    return config.paths.runtime_settings_path.with_name(CONTROLLER_STORAGE_RECOVERY_FILE_NAME)


def process_controller_storage_recovery_once(
        config: MediaforceConfig,
        stop_event: threading.Event | None = None,
) -> None:
    state_path = controller_storage_recovery_path(config)
    with controller_mount_lock(config.paths.runtime_settings_path) as acquired:
        if not acquired:
            return
        now = _utc_now()
        requirements = _required_mounts(config)
        previous = _load_snapshot(state_path)
        prior_indeterminate = previous.state_error is not None
        previous_by_mount = {mount.mount_point: mount for mount in previous.mounts}
        states_by_mount = {
            mount.mount_point: mount
            for mount in previous.mounts
            if mount.mount_point in {str(root) for root in requirements}
        }
        remaining_roots = set(requirements)
        attempted = False

        for root, (mount, required_paths) in sorted(requirements.items(), key=lambda item: str(item[0])):
            if stop_event is not None and stop_event.is_set():
                break
            prior = previous_by_mount.get(str(root))
            signature = _requirement_signature(root, mount, required_paths)
            if (
                prior is not None
                and prior.requirement_signature != signature
                and prior.status != "action_required"
            ):
                prior = None
            if sys.platform != "darwin":
                states_by_mount[str(root)] = _failed_state(
                    mount_point=root,
                    requirement_signature=signature,
                    previous=prior,
                    now=now,
                    status="action_required",
                    reason="Automatic controller storage recovery requires macOS.",
                    failure_kind="unsupported_platform",
                    attempted=False,
                )
                continue
            probe = (
                probe_controller_mount(mount, required_paths)
                if mount is not None
                else probe_controller_volume(root, required_paths)
            )
            remaining_roots.discard(root)
            checked_at = _utc_now()
            if probe.mounted and probe.accessible:
                states_by_mount[str(root)] = _ready_state(
                    root, signature, checked_at, previous=prior
                )
                continue

            failure_kind = probe.failure_kind or "mount_absent"
            if failure_kind in {"probe_failed", "probe_timeout"}:
                states_by_mount[str(root)] = _failed_state(
                    mount_point=root,
                    requirement_signature=signature,
                    previous=prior,
                    now=checked_at,
                    status=(
                        "action_required"
                        if prior_indeterminate or (prior and prior.status == "action_required")
                        else "retrying"
                    ),
                    reason=(
                        previous.state_error
                        or (prior.reason if prior and prior.status == "action_required" else None)
                        or _stable_reason(failure_kind)
                    ),
                    failure_kind=failure_kind,
                    attempted=False,
                )
                continue
            if probe.mounted and not probe.accessible:
                states_by_mount[str(root)] = _failed_state(
                    mount_point=root,
                    requirement_signature=signature,
                    previous=prior,
                    now=checked_at,
                    status="action_required",
                    reason=_stable_reason(failure_kind),
                    failure_kind=failure_kind,
                    attempted=False,
                )
                continue
            if (
                mount is None
                or prior_indeterminate
                or failure_kind in _ACTION_REQUIRED_FAILURES
                or (prior and prior.status == "action_required")
            ):
                states_by_mount[str(root)] = _failed_state(
                    mount_point=root,
                    requirement_signature=signature,
                    previous=prior,
                    now=now,
                    status="action_required",
                    reason=(
                        previous.state_error
                        if prior_indeterminate
                        else (prior.reason if prior and prior.status == "action_required" else None)
                        or _stable_reason(failure_kind)
                    ),
                    failure_kind=failure_kind,
                    attempted=False,
                )
                continue
            if attempted or not _retry_due(prior, now):
                states_by_mount[str(root)] = _failed_state(
                    mount_point=root,
                    requirement_signature=signature,
                    previous=prior,
                    now=now,
                    status="retrying",
                    reason=_stable_reason(failure_kind),
                    failure_kind=failure_kind,
                    attempted=False,
                )
                continue

            attempted = True
            states_by_mount[str(root)] = _attempting_state(
                root, signature, prior, now
            )
            _save_snapshot(state_path, list(states_by_mount.values()), now)
            assert mount is not None
            result = mount_controller_smb_no_ui(mount, required_paths)
            attempted_at = _utc_now()
            states_by_mount[str(root)] = _state_from_mount_result(
                mount, signature, prior, result, attempted_at
            )

        _save_snapshot(
            state_path,
            list(states_by_mount.values()),
            now,
            state_error=previous.state_error if prior_indeterminate and remaining_roots else None,
        )


def controller_storage_recovery_snapshot(config: MediaforceConfig) -> dict[str, Any]:
    return _load_snapshot(controller_storage_recovery_path(config)).as_payload()


def controller_storage_admission_issue(
        config: MediaforceConfig,
        host: dict[str, Any] | None,
) -> str | None:
    required_roots = _required_roots_for_host(config, host)
    if not required_roots:
        return None
    snapshot = _load_snapshot(controller_storage_recovery_path(config))
    if snapshot.state_error:
        return (
            f"Controller storage: {snapshot.state_error} "
            "Reconnect storage with Finder or Prepare; readiness checks continue."
        )
    by_mount = {Path(state.mount_point): state for state in snapshot.mounts}
    now = _utc_now()
    for root in sorted(required_roots, key=str):
        state = by_mount.get(root)
        if state is None:
            return f"Controller storage at {root} has not been checked."
        if state.status != "ready":
            if state.status == "checking":
                return "Controller storage: readiness is being checked."
            message = state.reason or f"Storage at {root} is unavailable."
            if state.status == "action_required":
                return (
                    f"Controller storage: {message} Reconnect storage with Finder or Prepare; "
                    "readiness checks continue."
                )
            retry = f" Retry at {state.next_retry_at}." if state.next_retry_at else ""
            return f"Controller storage: {message}{retry}"
        if state.verification_generation != _PROCESS_GENERATION:
            return f"Controller storage at {root} needs a check by the current service process."
        checked = _parse_utc(state.last_check_at)
        if (
            checked is None
            or checked > now
            or now - checked > timedelta(seconds=_READY_FRESHNESS_SECONDS)
        ):
            return f"Controller storage at {root} needs a fresh availability check."
        expected_signature = _expected_signature_for_root(config, root)
        if expected_signature is not None and state.requirement_signature != expected_signature:
            return f"Controller storage requirements at {root} have changed and need a fresh check."
    return None


def _required_mounts(
        config: MediaforceConfig,
) -> dict[Path, tuple[ControllerSmbMount | None, dict[Path, PathAccess]]]:
    required_paths = _all_required_paths(config)
    configured = configured_controller_smb_mounts(config)
    configured_by_root = {mount.mount_point: mount for mount in configured}
    requirements: dict[Path, tuple[ControllerSmbMount | None, dict[Path, PathAccess]]] = {}
    for path, access in required_paths.items():
        root = _volume_root(path)
        if root is None:
            continue
        if root not in requirements:
            requirements[root] = (configured_by_root.get(root), {})
        requirements[root][1][path] = access
    return requirements


def _all_required_paths(config: MediaforceConfig) -> dict[Path, PathAccess]:
    paths: dict[Path, PathAccess] = {
        path.expanduser(): "read" for path in config.source_root_map.values()
    }
    paths[config.staging_root.expanduser()] = "write"
    for host in config.remote_hosts:
        if host_media_access_for_host(host) != "mounted":
            continue
        paths[config.staging_root_for_host(host).expanduser()] = "write"
    return paths


def _required_roots_for_host(
        config: MediaforceConfig,
        host: dict[str, Any] | None,
) -> set[Path]:
    staging_root = (
        config.staging_root
        if host is None or host_media_access_for_host(host) == "stream"
        else config.staging_root_for_host(host)
    )
    paths = [*config.source_root_map.values(), staging_root]
    return set(finder_mount_roots_for_paths(paths))


def _volume_root(path: Path) -> Path | None:
    roots = finder_mount_roots_for_paths([path])
    return roots[0] if roots else None


def _requirement_signature(
        root: Path,
        mount: ControllerSmbMount | None,
        required_paths: Mapping[Path, PathAccess],
) -> str:
    parts = [mount.source if mount is not None else "local-volume", str(root)]
    parts.extend(
        f"{access}:{path}"
        for path, access in sorted(required_paths.items(), key=lambda item: str(item[0]))
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _expected_signature_for_root(config: MediaforceConfig, root: Path) -> str | None:
    required_paths = {
        path: access
        for path, access in _all_required_paths(config).items()
        if _volume_root(path) == root
    }
    if not required_paths:
        return None
    configured = configured_controller_smb_mounts(config)
    mount = next((candidate for candidate in configured if candidate.mount_point == root), None)
    return (
        _requirement_signature(root, mount, required_paths)
    )


def _state_from_mount_result(
        mount: ControllerSmbMount,
        requirement_signature: str,
        previous: ControllerStorageMountState | None,
        result: ControllerMountResult,
        now: datetime,
) -> ControllerStorageMountState:
    if result.ok and result.probe.mounted and result.probe.accessible:
        return _ready_state(
            mount.mount_point,
            requirement_signature,
            now,
            previous=previous,
            attempted=True,
        )
    failure_kind = result.failure_kind or result.probe.failure_kind or "mount_failed"
    action_required = result.action_required or failure_kind in _ACTION_REQUIRED_FAILURES
    return _failed_state(
        mount_point=mount.mount_point,
        requirement_signature=requirement_signature,
        previous=previous,
        now=now,
        status="action_required" if action_required else "retrying",
        reason=_stable_reason(failure_kind),
        detail=_safe_detail(failure_kind, result.detail),
        failure_kind=failure_kind,
        attempted=True,
    )


def _attempting_state(
        mount_point: Path,
        requirement_signature: str,
        previous: ControllerStorageMountState | None,
        now: datetime,
) -> ControllerStorageMountState:
    timestamp = _format_utc(now)
    return ControllerStorageMountState(
        mount_point=str(mount_point),
        requirement_signature=requirement_signature,
        verification_generation=None,
        status="action_required",
        reason="The SMB mount attempt is in progress or ended without a recorded result.",
        detail=None,
        last_failure_kind="mount_result_unknown",
        first_failure_at=(previous.first_failure_at if previous else None) or timestamp,
        last_check_at=timestamp,
        last_attempt_at=timestamp,
        next_retry_at=None,
        failure_count=max(1, previous.failure_count if previous else 0),
    )


def _ready_state(
        mount_point: Path,
        requirement_signature: str,
        now: datetime,
        *,
        previous: ControllerStorageMountState | None = None,
        attempted: bool = False,
) -> ControllerStorageMountState:
    timestamp = _format_utc(now)
    return ControllerStorageMountState(
        mount_point=str(mount_point),
        requirement_signature=requirement_signature,
        verification_generation=_PROCESS_GENERATION,
        status="ready",
        reason=None,
        detail=None,
        last_failure_kind=None,
        first_failure_at=None,
        last_check_at=timestamp,
        last_attempt_at=(
            timestamp if attempted else (previous.last_attempt_at if previous else None)
        ),
        next_retry_at=None,
        failure_count=0,
    )


def _failed_state(
        *,
        mount_point: Path,
        requirement_signature: str,
        previous: ControllerStorageMountState | None,
        now: datetime,
        status: Literal["retrying", "action_required"],
        reason: str,
        detail: str | None = None,
        failure_kind: str | None = None,
        attempted: bool,
) -> ControllerStorageMountState:
    count = max(1, (previous.failure_count if previous else 0) + (1 if attempted else 0))
    timestamp = _format_utc(now)
    delay = min(_INITIAL_RETRY_SECONDS * (2 ** max(0, count - 1)), _MAX_RETRY_SECONDS)
    if status == "retrying":
        if attempted:
            next_retry_at = _format_utc(now + timedelta(seconds=delay))
        elif previous and previous.next_retry_at:
            next_retry_at = previous.next_retry_at
        else:
            next_retry_at = timestamp
    else:
        next_retry_at = None
    return ControllerStorageMountState(
        mount_point=str(mount_point),
        requirement_signature=requirement_signature,
        verification_generation=None,
        status=status,
        reason=reason,
        detail=detail,
        last_failure_kind=failure_kind or (previous.last_failure_kind if previous else None),
        first_failure_at=(previous.first_failure_at if previous else None) or timestamp,
        last_check_at=timestamp,
        last_attempt_at=timestamp if attempted else (previous.last_attempt_at if previous else None),
        next_retry_at=next_retry_at,
        failure_count=count,
    )


def _retry_due(previous: ControllerStorageMountState | None, now: datetime) -> bool:
    if previous is None:
        return True
    retry_at = _parse_utc(previous.next_retry_at)
    return (
        retry_at is None
        or now >= retry_at
        or retry_at - now > timedelta(seconds=_MAX_RETRY_SECONDS)
    )


def _stable_reason(failure_kind: str) -> str:
    return {
        "invalid_mapping": "The learned SMB mapping is invalid and needs operator review.",
        "mount_absent": "The required SMB share is not mounted.",
        "mount_failed": "The required SMB share could not be mounted; Mediaforce will retry.",
        "mount_helper_failed": "The SMB mount helper is unavailable; Mediaforce will retry.",
        "mount_identity_mismatch": "A different SMB share is mounted at the required volume path.",
        "mount_path_occupied": "The required volume path is occupied by an unexpected filesystem.",
        "mount_timeout": "The SMB mount request did not finish and needs operator review.",
        "mount_result_unknown": "The SMB mount result was ambiguous and needs operator review.",
        "path_unavailable": "The required path on the SMB share is unavailable.",
        "probe_failed": "The mounted share identity could not be verified safely.",
        "probe_timeout": "The mounted share identity check timed out.",
        "unexpected_mount_path": "The SMB share appeared at an unexpected volume path.",
    }.get(failure_kind, "The required SMB share is unavailable; Mediaforce will retry.")


def _safe_detail(failure_kind: str, detail: str | None) -> str | None:
    if not detail:
        return None
    if failure_kind == "mount_failed":
        match = re.search(r"\bstatus (-?\d+)\b", detail)
        return f"NetFS status {match.group(1)}." if match else None
    if failure_kind == "unexpected_mount_path":
        match = re.search(r"(/Volumes/[A-Za-z0-9 ._()'\-]+)", detail)
        return f"Unexpected mount path: {match.group(1)}." if match else None
    return None


def _load_snapshot(path: Path) -> ControllerStorageRecoverySnapshot:
    try:
        exists = path.exists()
    except OSError:
        return _invalid_snapshot("Controller storage recovery state is unreadable.")
    if not exists:
        return ControllerStorageRecoverySnapshot(_SCHEMA_VERSION, None, ())
    try:
        payload = load_runtime_settings(path)
    except (OSError, ValueError):
        return _invalid_snapshot("Controller storage recovery state is unreadable.")
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return _invalid_snapshot("Controller storage recovery state has an unsupported schema.")
    raw_mounts = payload.get("mounts")
    if not isinstance(raw_mounts, list):
        return _invalid_snapshot("Controller storage recovery state is malformed.")
    mounts: list[ControllerStorageMountState] = []
    for raw in raw_mounts:
        if not isinstance(raw, dict):
            return _invalid_snapshot("Controller storage recovery state is malformed.")
        mount_point = str(raw.get("mount_point") or "").strip()
        status = str(raw.get("status") or "")
        if not mount_point or status not in {"checking", "retrying", "ready", "action_required"}:
            return _invalid_snapshot("Controller storage recovery state is malformed.")
        mounts.append(ControllerStorageMountState(
            mount_point=mount_point,
            requirement_signature=_optional_text(raw.get("requirement_signature")),
            verification_generation=_optional_text(raw.get("verification_generation")),
            status=cast(RecoveryStatus, status),
            reason=_optional_text(raw.get("reason")),
            detail=_optional_text(raw.get("detail")),
            last_failure_kind=_optional_text(raw.get("last_failure_kind")),
            first_failure_at=_optional_text(raw.get("first_failure_at")),
            last_check_at=_optional_text(raw.get("last_check_at")),
            last_attempt_at=_optional_text(raw.get("last_attempt_at")),
            next_retry_at=_optional_text(raw.get("next_retry_at")),
            failure_count=max(0, _integer(raw.get("failure_count"))),
        ))
    return ControllerStorageRecoverySnapshot(
        schema_version=_SCHEMA_VERSION,
        updated_at=_optional_text(payload.get("updated_at")),
        mounts=tuple(mounts),
    )


def _invalid_snapshot(reason: str) -> ControllerStorageRecoverySnapshot:
    return ControllerStorageRecoverySnapshot(
        schema_version=_SCHEMA_VERSION,
        updated_at=None,
        mounts=(),
        state_error=reason,
    )


def _save_snapshot(
        path: Path,
        states: list[ControllerStorageMountState],
        now: datetime,
        *,
        state_error: str | None = None,
) -> None:
    snapshot = ControllerStorageRecoverySnapshot(
        schema_version=_SCHEMA_VERSION,
        updated_at=_format_utc(now),
        mounts=tuple(sorted(states, key=lambda state: state.mount_point)),
        state_error=state_error,
    )
    save_runtime_settings(path, snapshot.as_payload())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _integer(value: object) -> int:
    try:
        return int(str(value or "0"))
    except ValueError:
        return 0


__all__ = [
    "ControllerStorageMountState",
    "ControllerStorageRecoverySnapshot",
    "controller_storage_admission_issue",
    "controller_storage_recovery_path",
    "controller_storage_recovery_snapshot",
    "process_controller_storage_recovery_once",
]
