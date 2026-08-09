"""Owner-only AV1 v4 revision-3 ordinal-window registry.

The registry is intentionally small and strict: one absolute 0700 directory,
one process-local lock, one flocked ``O_DIRECTORY|O_NOFOLLOW`` descriptor, and
append-only 0600 JSON records.  Published caller mappings are treated as hints;
state transitions custody-read and canonical-load the registry chain before
deriving the next record.
"""

from __future__ import annotations

import fcntl
import hmac
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4r3_execution_grant import (
    assert_av1_v4r3_execution_grant_active,
    assert_av1_v4r3_execution_grant_chain,
    deserialize_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight import (
    AV1V4R3ExecutionPreflightError,
    assert_av1_v4r3_ordinal_plan_preflight_pair,
    deserialize_av1_v4r3_execution_preflight,
    serialize_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_ORDINAL_COUNT,
    AV1_V4R3_OW_SETUP_SECONDS,
    AV1_V4R3_OW_TEARDOWN_SECONDS,
    AV1V4R3OrdinalWindowError,
    assert_av1_v4r3_ordinal_window_grant,
    assert_av1_v4r3_ordinal_window_plan,
    build_av1_v4r3_ordinal_window_claim,
    build_av1_v4r3_ordinal_window_grant,
    build_av1_v4r3_ordinal_window_outcome,
    build_av1_v4r3_ordinal_window_started,
    build_av1_v4r3_ordinal_window_terminal,
    deserialize_av1_v4r3_ordinal_window_claim,
    deserialize_av1_v4r3_ordinal_window_grant,
    deserialize_av1_v4r3_ordinal_window_outcome,
    deserialize_av1_v4r3_ordinal_window_plan,
    deserialize_av1_v4r3_ordinal_window_started,
    deserialize_av1_v4r3_ordinal_window_terminal,
    serialize_av1_v4r3_ordinal_window_claim,
    serialize_av1_v4r3_ordinal_window_grant,
    serialize_av1_v4r3_ordinal_window_outcome,
    serialize_av1_v4r3_ordinal_window_plan,
    serialize_av1_v4r3_ordinal_window_started,
    serialize_av1_v4r3_ordinal_window_terminal,
)
from mediaforce.tuning.av1_validation_v4r3_runner_admission import (
    assert_av1_v4r3_execution_claim_chain,
    assert_av1_v4r3_runner_admission,
    build_av1_v4r3_runner_admission,
    deserialize_av1_v4r3_execution_claim,
    deserialize_av1_v4r3_runner_admission,
    serialize_av1_v4r3_runner_admission,
)


class AV1V4R3OrdinalWindowRegistryError(AV1V4R3OrdinalWindowError):
    pass


Clock = Callable[[], datetime]

_PROCESS_LOCK = threading.RLock()
_PLAN_NAME = "plan.json"
_PREFLIGHT_NAME = "preflight.json"
_TERMINAL_NAME = "terminal.json"
_MAX_FILE_BYTES = 64 * 1024
_TEMP_SUFFIX = ".tmp"
_EXECUTION_CLAIM_FILENAME_RE = re.compile(
    r"av1v4r3execgrant_[0-9a-f]{32}\.claim\.json\Z"
)


@dataclass(frozen=True)
class AV1V4R3OrdinalWindowRegistryBinding:
    """Private registry binding kept out of public plan artifacts."""

    registry: Path
    run_registry_id: str


@dataclass(frozen=True, slots=True)
class AV1V4R3OrdinalWindowPairPublication:
    plan: Mapping[str, Any]
    preflight: Mapping[str, Any]
    plan_created: bool
    preflight_created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R3RunnerStartPublication:
    claim: Mapping[str, Any]
    admission: Mapping[str, Any]
    started: Mapping[str, Any]


def av1_v4r3_ordinal_window_registry_hmac_id(
    registry: Path,
    *,
    key: bytes,
) -> str:
    """Return the opaque public registry ID for a private absolute path."""

    if len(key) < 32:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry HMAC key must be at least 32 bytes"
        )
    path = _normalize_registry_path(registry)
    digest = hmac.digest(
        key,
        f"mediaforce:av1:v4:r3:ordinal-window:registry:{path}".encode(),
        "sha256",
    ).hex()
    return f"av1v4r3runreg_{digest}"


def assert_av1_v4r3_ordinal_window_registry(registry: Path) -> None:
    """Verify *registry* is an absolute owner-only real directory."""

    registry_path = _normalize_registry_path(registry)
    flags = _dir_open_flags()
    try:
        dir_fd = os.open(registry_path, flags)
    except OSError as exc:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry is unavailable"
        ) from exc
    try:
        _assert_registry_fd(dir_fd)
    finally:
        os.close(dir_fd)


def assert_av1_v4r3_ordinal_window_file_custody(
    registry: Path,
    filename: str,
) -> None:
    """Verify one registry file has owner-owned 0600 single-link custody."""

    with _locked_registry(registry) as ctx:
        ctx.assert_file_custody(filename)


def publish_av1_v4r3_ordinal_window_plan(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish the public plan into the private registry."""

    plan_payload = dict(plan)
    assert_av1_v4r3_ordinal_window_plan(plan_payload)
    with _locked_registry(binding.registry) as ctx:
        _assert_binding_matches_plan(binding, plan_payload)
        ctx.assert_no_terminal()
        if ctx.exists(_PLAN_NAME):
            existing = ctx.load_plan()
            _assert_same_record(existing, plan_payload, "plan")
            return existing
        ctx.write(_PLAN_NAME, serialize_av1_v4r3_ordinal_window_plan(plan_payload))
    return plan_payload


def publish_av1_v4r3_ordinal_window_grant(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    ordinal: int,
    clock: Clock,
    valid_until: str,
) -> dict[str, Any]:
    """Publish an authoritative grant stamped by *clock* under lock."""

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_no_terminal()
        plan_payload, _preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        if not isinstance(ordinal, int) or isinstance(ordinal, bool):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window grant ordinal must be an integer"
            )
        if not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window grant ordinal is out of range"
            )
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, ctx.now(clock))
        filename = _grant_name(ordinal)
        if ctx.exists(filename):
            grant = ctx.load_grant(ordinal)
            _assert_record_binds_plan(grant, plan_payload, "grant")
            return grant
        grant = build_av1_v4r3_ordinal_window_grant(
            plan=plan_payload,
            ordinal=ordinal,
            authorized_at=_format_ts(ctx.now(clock)),
            valid_until=valid_until,
        )
        ctx.write(filename, serialize_av1_v4r3_ordinal_window_grant(grant))
        return grant


def publish_av1_v4r3_ordinal_window_claim(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    """Publish a claim using authoritative registry plan/grant records."""

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_no_terminal()
        plan_payload, _preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        grant_payload = ctx.load_matching_grant(plan_payload, grant)
        ordinal = int(grant_payload["ordinal"])
        ctx.assert_next_ordinal_admissible(plan_payload, ordinal, ctx.now(clock))
        ctx.assert_no_record(_claim_name(ordinal), "claim", ordinal)
        try:
            ctx.assert_grant_open_for_admission(grant_payload, ctx.now(clock))
        except AV1V4R3OrdinalWindowRegistryError:
            if ctx.now(clock) > _admission_closes(grant_payload):
                ctx.publish_terminal(plan_payload)
            raise
        claim = build_av1_v4r3_ordinal_window_claim(
            plan=plan_payload,
            grant=grant_payload,
            claimed_at=_format_ts(ctx.now(clock)),
        )
        ctx.write(_claim_name(ordinal), serialize_av1_v4r3_ordinal_window_claim(claim))
        return claim


def publish_av1_v4r3_ordinal_window_started(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    """Publish a started record using authoritative registry predecessors."""

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_no_terminal()
        plan_payload, _preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        grant_payload = ctx.load_matching_grant(plan_payload, grant)
        ordinal = int(grant_payload["ordinal"])
        claim_payload = ctx.load_claim(ordinal)
        _assert_same_record(claim_payload, claim, "claim")
        _assert_record_binds_grant(claim_payload, grant_payload, "claim")
        ctx.assert_no_record(_started_name(ordinal), "started", ordinal)
        try:
            ctx.assert_grant_open_for_admission(grant_payload, ctx.now(clock))
        except AV1V4R3OrdinalWindowRegistryError:
            if ctx.now(clock) > _admission_closes(grant_payload):
                ctx.publish_terminal(plan_payload)
            raise
        claimed_at = _parse_ts(claim_payload["claimed_at"])
        if ctx.now(clock) < claimed_at:
            ctx.publish_terminal(plan_payload)
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window started_at cannot precede claim time"
            )
        started = build_av1_v4r3_ordinal_window_started(
            plan=plan_payload,
            grant=grant_payload,
            claim=claim_payload,
            started_at=_format_ts(ctx.now(clock)),
        )
        ctx.write(
            _started_name(ordinal),
            serialize_av1_v4r3_ordinal_window_started(started),
        )
        return started


def publish_av1_v4r3_runner_admission_and_started(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    execution_claim: Mapping[str, Any],
    source_path_hmac_id: str,
    instance_path_hmac_ids: Mapping[str, str],
    quality_temp_hmac_id: str,
    quality_temp_key_id: str,
    production_stream_plan: Mapping[str, Any],
    stream_budget_ledger: Mapping[str, Any],
    transform_plan_id: str,
    returned_stream_budget_ledger: Mapping[str, Any],
    clock: Clock,
) -> AV1V4R3RunnerStartPublication:
    """Atomically admit and start exactly one execution-authorized ordinal."""

    _assert_clock(clock)
    wrote_prefix = False
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_no_terminal()
        plan_payload, preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        sequencing_payload = ctx.load_matching_grant(plan_payload, sequencing_grant)
        ordinal = int(sequencing_payload["ordinal"])
        execution_grant_payload = ctx.load_execution_grant(ordinal)
        _assert_same_record(execution_grant_payload, execution_grant, "execution grant")
        execution_claim_payload = ctx.load_execution_claim(
            str(execution_grant_payload["grant_id"])
        )
        _assert_same_record(execution_claim_payload, execution_claim, "execution claim")
        try:
            assert_av1_v4r3_execution_grant_chain(
                plan=plan_payload,
                preflight=preflight_payload,
                sequencing_grant=sequencing_payload,
                execution_grant=execution_grant_payload,
            )
            assert_av1_v4r3_execution_claim_chain(
                execution_claim=execution_claim_payload,
                execution_grant=execution_grant_payload,
                sequencing_grant=sequencing_payload,
            )
        except ValueError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 runner execution authority chain is invalid"
            ) from exc
        ctx.assert_no_record(_claim_name(ordinal), "claim", ordinal)
        ctx.assert_no_record(
            _runner_admission_name(ordinal), "runner admission", ordinal
        )
        ctx.assert_no_record(_started_name(ordinal), "started", ordinal)
        ctx.assert_no_record(_outcome_name(ordinal), "outcome", ordinal)
        now = ctx.now(clock)
        ctx.assert_next_execution_ordinal_admissible(
            plan_payload,
            preflight_payload,
            ordinal,
            now,
        )
        ctx.assert_grant_open_for_admission(sequencing_payload, now)
        timestamp = _format_ts(now)
        claim = build_av1_v4r3_ordinal_window_claim(
            plan=plan_payload,
            grant=sequencing_payload,
            claimed_at=timestamp,
        )
        admission = build_av1_v4r3_runner_admission(
            plan=plan_payload,
            execution_claim=execution_claim_payload,
            execution_grant=execution_grant_payload,
            sequencing_grant=sequencing_payload,
            preflight=preflight_payload,
            admitted_at=timestamp,
            source_path_hmac_id=source_path_hmac_id,
            instance_path_hmac_ids=instance_path_hmac_ids,
            quality_temp_hmac_id=quality_temp_hmac_id,
            quality_temp_key_id=quality_temp_key_id,
            production_stream_plan=production_stream_plan,
            stream_budget_ledger=stream_budget_ledger,
            transform_plan_id=transform_plan_id,
            returned_stream_budget_ledger=returned_stream_budget_ledger,
        )
        started = build_av1_v4r3_ordinal_window_started(
            plan=plan_payload,
            grant=sequencing_payload,
            claim=claim,
            started_at=timestamp,
        )
        try:
            ctx.write(
                _claim_name(ordinal), serialize_av1_v4r3_ordinal_window_claim(claim)
            )
            wrote_prefix = True
            ctx.write(
                _runner_admission_name(ordinal),
                serialize_av1_v4r3_runner_admission(admission),
            )
            ctx.write(
                _started_name(ordinal),
                serialize_av1_v4r3_ordinal_window_started(started),
            )
            _assert_same_record(ctx.load_claim(ordinal), claim, "claim")
            _assert_same_record(
                ctx.load_runner_admission(ordinal), admission, "runner admission"
            )
            _assert_same_record(ctx.load_started(ordinal), started, "started")
        except BaseException:
            if wrote_prefix:
                ctx.publish_terminal(plan_payload)
            raise
        return AV1V4R3RunnerStartPublication(
            claim=claim,
            admission=admission,
            started=started,
        )


def publish_av1_v4r3_ordinal_window_outcome(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    started: Mapping[str, Any],
    clock: Clock,
    success: bool,
) -> dict[str, Any]:
    """Publish an outcome and atomically absorb terminal failures/success."""

    _assert_clock(clock)
    if not isinstance(success, bool):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window outcome success must be a boolean"
        )
    with _locked_registry(binding.registry) as ctx:
        ctx.assert_no_terminal()
        plan_payload, _preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        now = ctx.now(clock)
        started_payload = dict(started)
        ordinal = int(started_payload.get("ordinal") or 0)
        canonical_started = ctx.load_started(ordinal)
        _assert_same_record(canonical_started, started_payload, "started")
        grant_payload = ctx.load_grant(ordinal)
        claim_payload = ctx.load_claim(ordinal)
        _assert_record_binds_plan(canonical_started, plan_payload, "started")
        _assert_record_binds_grant(canonical_started, grant_payload, "started")
        if canonical_started.get("claim_id") != claim_payload.get(
            "claim_id"
        ) or canonical_started.get("claim_payload_sha256") != claim_payload.get(
            "payload_sha256"
        ):
            ctx.publish_terminal(plan_payload)
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window started record does not bind the registry claim"
            )
        ctx.assert_no_record(_outcome_name(ordinal), "outcome", ordinal)
        started_at = _parse_ts(canonical_started["started_at"])
        valid_until = _parse_ts(grant_payload["valid_until"])
        if now <= started_at or now > valid_until:
            ctx.publish_terminal(plan_payload)
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window outcome time is outside the grant window"
            )
        outcome = build_av1_v4r3_ordinal_window_outcome(
            plan=plan_payload,
            started=canonical_started,
            outcome_at=_format_ts(now),
            success=success,
        )
        ctx.write(
            _outcome_name(ordinal), serialize_av1_v4r3_ordinal_window_outcome(outcome)
        )
        if not success or ordinal == AV1_V4R3_OW_ORDINAL_COUNT:
            ctx.publish_terminal(plan_payload)
        return outcome


def publish_av1_v4r3_ordinal_window_terminal(
    *,
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    clock: Clock,
) -> dict[str, Any]:
    """Publish or return the absorbing terminal derived from registry outcomes."""

    _assert_clock(clock)
    with _locked_registry(binding.registry) as ctx:
        plan_payload, _preflight_payload = ctx.load_plan_preflight_pair(binding, plan)
        if ctx.exists(_TERMINAL_NAME):
            return ctx.load_terminal()
        ctx.now(clock)
        return ctx.publish_terminal(plan_payload)


def derive_av1_v4r3_ordinal_window_high_water(
    registry: Path,
    *,
    clock: Clock,
) -> int:
    """Derive contiguous successful high-water from custody-validated outcomes."""

    _assert_clock(clock)
    with _locked_registry(registry) as ctx:
        ctx.now(clock)
        plan_payload, _preflight_payload = ctx.load_registry_plan_preflight_pair()
        if ctx.exists(_TERMINAL_NAME):
            terminal = ctx.load_terminal()
            if terminal["success_all_ordinals"] is True:
                return AV1_V4R3_OW_ORDINAL_COUNT
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window terminal registry is non-comparable"
            )
        return ctx.derive_high_water_or_terminal(plan_payload)


def load_av1_v4r3_ordinal_window_registry_terminal(
    registry: Path,
) -> dict[str, Any] | None:
    """Return the custody-validated terminal record if it exists."""

    with _locked_registry(registry) as ctx:
        if not ctx.exists(_TERMINAL_NAME):
            return None
        return ctx.load_terminal()


@dataclass
class _RegistryContext:
    registry: Path
    dir_fd: int
    _now: datetime | None = None

    def now(self, clock: Clock) -> datetime:
        if self._now is None:
            self._now = self._read_clock(clock)
        return self._now

    def fresh_now(self, clock: Clock) -> datetime:
        current = self._read_clock(clock)
        if self._now is not None and current < self._now:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry clock moved backward"
            )
        self._now = current
        return current

    @staticmethod
    def _read_clock(clock: Clock) -> datetime:
        current = clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry clock must be timezone-aware"
            )
        return current.astimezone(UTC).replace(microsecond=0)

    def exists(self, filename: str) -> bool:
        _assert_registry_filename(filename)
        try:
            os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def assert_file_custody(
        self,
        filename: str,
        *,
        allowed_link_counts: tuple[int, ...] = (1,),
    ) -> os.stat_result:
        _assert_registry_filename(filename)
        try:
            metadata = os.stat(filename, dir_fd=self.dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal window file custody check failed: {filename}"
            ) from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink not in allowed_link_counts
            or metadata.st_uid != os.geteuid()
        ):
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal window file custody is invalid: {filename}"
            )
        return metadata

    def read(self, filename: str) -> bytes:
        metadata = self.assert_file_custody(filename)
        if metadata.st_size <= 0 or metadata.st_size > _MAX_FILE_BYTES:
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal window file size is invalid: {filename}"
            )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(filename, flags, dir_fd=self.dir_fd)
        try:
            opened = os.fstat(fd)
            if not _same_inode(metadata, opened):
                raise AV1V4R3OrdinalWindowRegistryError(
                    f"AV1 v4 r3 ordinal window file changed during read: {filename}"
                )
            data = b""
            while len(data) < metadata.st_size:
                chunk = os.read(fd, metadata.st_size - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) != metadata.st_size:
                raise AV1V4R3OrdinalWindowRegistryError(
                    f"AV1 v4 r3 ordinal window file read was incomplete: {filename}"
                )
            after = os.fstat(fd)
            if not _same_inode(metadata, after) or after.st_size != metadata.st_size:
                raise AV1V4R3OrdinalWindowRegistryError(
                    f"AV1 v4 r3 ordinal window file changed during read: {filename}"
                )
            return data
        finally:
            os.close(fd)

    def write(self, filename: str, data: bytes) -> None:
        _assert_registry_filename(filename)
        temp_name = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=self.dir_fd)
        except FileExistsError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window temporary artifact already exists"
            ) from exc
        try:
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                written = 0
                while written < len(view):
                    count = os.write(fd, view[written:])
                    if count <= 0:
                        raise OSError(
                            "registry temporary artifact write made no progress"
                        )
                    written += count
                os.fsync(fd)
            finally:
                os.close(fd)
        except Exception:
            os.unlink(temp_name, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
            raise
        try:
            os.link(
                temp_name,
                filename,
                src_dir_fd=self.dir_fd,
                dst_dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            os.unlink(temp_name, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal window artifact already exists: {filename}"
            ) from exc
        except Exception:
            os.unlink(temp_name, dir_fd=self.dir_fd)
            os.fsync(self.dir_fd)
            raise
        os.fsync(self.dir_fd)
        os.unlink(temp_name, dir_fd=self.dir_fd)
        os.fsync(self.dir_fd)
        self.assert_file_custody(filename)

    def write_burn(self, filename: str, data: bytes) -> None:
        """Create the final artifact exclusively and never remove it on failure."""

        _assert_registry_filename(filename)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(filename, flags, 0o600, dir_fd=self.dir_fd)
        except FileExistsError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal window artifact already exists: {filename}"
            ) from exc
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError("registry burn artifact write made no progress")
                written += count
            os.fsync(fd)
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_size != len(data)
            ):
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 ordinal window burn artifact custody is invalid"
                )
        finally:
            os.close(fd)
            os.fsync(self.dir_fd)
        self.assert_file_custody(filename)

    def cleanup_stale_temps(self) -> None:
        removed = False
        for name in os.listdir(self.dir_fd):
            if not name.startswith(".") or not name.endswith(_TEMP_SUFFIX):
                continue
            body = name[1 : -len(_TEMP_SUFFIX)]
            parts = body.rsplit(".", 2)
            if len(parts) != 3 or not parts[1].isdigit() or len(parts[2]) != 16:
                continue
            final_name = parts[0]
            if not _is_registry_artifact_filename(final_name):
                continue
            try:
                int(parts[2], 16)
            except ValueError:
                continue
            temp_metadata = os.stat(
                name,
                dir_fd=self.dir_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(temp_metadata.st_mode)
                or stat.S_IMODE(temp_metadata.st_mode) != 0o600
                or temp_metadata.st_uid != os.geteuid()
                or temp_metadata.st_nlink not in {1, 2}
            ):
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 ordinal window temporary artifact custody is invalid"
                )
            if temp_metadata.st_nlink == 2:
                if not self.exists(final_name):
                    raise AV1V4R3OrdinalWindowRegistryError(
                        "AV1 v4 r3 ordinal window temporary artifact final link is missing"
                    )
                final_metadata = self.assert_file_custody(
                    final_name,
                    allowed_link_counts=(2,),
                )
                if not _same_inode(temp_metadata, final_metadata):
                    raise AV1V4R3OrdinalWindowRegistryError(
                        "AV1 v4 r3 ordinal window temporary artifact conflicts with final data"
                    )
            os.unlink(name, dir_fd=self.dir_fd)
            removed = True
        if removed:
            os.fsync(self.dir_fd)

    def load_plan(self) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_plan(self.read(_PLAN_NAME))

    def load_preflight(self) -> dict[str, Any]:
        try:
            return deserialize_av1_v4r3_execution_preflight(self.read(_PREFLIGHT_NAME))
        except AV1V4R3ExecutionPreflightError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window preflight registry artifact is invalid"
            ) from exc

    def publish_plan_preflight_pair(
        self,
        binding: AV1V4R3OrdinalWindowRegistryBinding,
        plan: Mapping[str, Any],
        preflight: Mapping[str, Any],
    ) -> AV1V4R3OrdinalWindowPairPublication:
        plan_payload = dict(plan)
        preflight_payload = dict(preflight)
        try:
            assert_av1_v4r3_ordinal_plan_preflight_pair(
                plan=plan_payload,
                preflight=preflight_payload,
            )
        except AV1V4R3ExecutionPreflightError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window plan/preflight pair is invalid"
            ) from exc
        _assert_binding_matches_plan(binding, plan_payload)
        self.assert_no_terminal()
        plan_exists = self.exists(_PLAN_NAME)
        preflight_exists = self.exists(_PREFLIGHT_NAME)
        if preflight_exists and not plan_exists:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window preflight exists without its plan"
            )
        if plan_exists:
            canonical_plan = self.load_plan()
            _assert_same_record(canonical_plan, plan_payload, "plan")
            plan_created = False
        else:
            self.write(
                _PLAN_NAME,
                serialize_av1_v4r3_ordinal_window_plan(plan_payload),
            )
            canonical_plan = plan_payload
            plan_created = True
        if preflight_exists:
            canonical_preflight = self.load_preflight()
            _assert_same_record(canonical_preflight, preflight_payload, "preflight")
            preflight_created = False
        else:
            self.write(
                _PREFLIGHT_NAME,
                serialize_av1_v4r3_execution_preflight(preflight_payload),
            )
            canonical_preflight = preflight_payload
            preflight_created = True
        try:
            assert_av1_v4r3_ordinal_plan_preflight_pair(
                plan=canonical_plan,
                preflight=canonical_preflight,
            )
        except AV1V4R3ExecutionPreflightError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry pair is invalid"
            ) from exc
        return AV1V4R3OrdinalWindowPairPublication(
            plan=canonical_plan,
            preflight=canonical_preflight,
            plan_created=plan_created,
            preflight_created=preflight_created,
        )

    def load_plan_preflight_pair(
        self,
        binding: AV1V4R3OrdinalWindowRegistryBinding,
        plan: Mapping[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        plan_payload = dict(plan)
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
        _assert_binding_matches_plan(binding, plan_payload)
        if not self.exists(_PLAN_NAME) or not self.exists(_PREFLIGHT_NAME):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window plan/preflight pair is unavailable"
            )
        canonical_plan = self.load_plan()
        _assert_same_record(canonical_plan, plan_payload, "plan")
        canonical_preflight = self.load_preflight()
        try:
            assert_av1_v4r3_ordinal_plan_preflight_pair(
                plan=canonical_plan,
                preflight=canonical_preflight,
            )
        except AV1V4R3ExecutionPreflightError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry pair is invalid"
            ) from exc
        return canonical_plan, canonical_preflight

    def load_registry_plan_preflight_pair(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.exists(_PLAN_NAME) or not self.exists(_PREFLIGHT_NAME):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window plan/preflight pair is unavailable"
            )
        canonical_plan = self.load_plan()
        canonical_preflight = self.load_preflight()
        try:
            assert_av1_v4r3_ordinal_plan_preflight_pair(
                plan=canonical_plan,
                preflight=canonical_preflight,
            )
        except AV1V4R3ExecutionPreflightError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry pair is invalid"
            ) from exc
        return canonical_plan, canonical_preflight

    def load_grant(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_grant(
            self.read(_grant_name(ordinal))
        )

    def load_claim(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_claim(
            self.read(_claim_name(ordinal))
        )

    def load_execution_grant(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_execution_grant(
            self.read(_execution_grant_name(ordinal))
        )

    def load_execution_claim(self, grant_id: str) -> dict[str, Any]:
        return deserialize_av1_v4r3_execution_claim(
            self.read(_execution_claim_name(grant_id))
        )

    def load_runner_admission(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_runner_admission(
            self.read(_runner_admission_name(ordinal))
        )

    def load_started(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_started(
            self.read(_started_name(ordinal))
        )

    def load_outcome(self, ordinal: int) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_outcome(
            self.read(_outcome_name(ordinal))
        )

    def load_terminal(self) -> dict[str, Any]:
        return deserialize_av1_v4r3_ordinal_window_terminal(self.read(_TERMINAL_NAME))

    def load_or_publish_plan(
        self,
        binding: AV1V4R3OrdinalWindowRegistryBinding,
        plan: Mapping[str, Any],
    ) -> dict[str, Any]:
        plan_payload = dict(plan)
        assert_av1_v4r3_ordinal_window_plan(plan_payload)
        _assert_binding_matches_plan(binding, plan_payload)
        if not self.exists(_PLAN_NAME):
            self.write(_PLAN_NAME, serialize_av1_v4r3_ordinal_window_plan(plan_payload))
            return plan_payload
        canonical = self.load_plan()
        _assert_same_record(canonical, plan_payload, "plan")
        return canonical

    def load_matching_grant(
        self,
        plan: Mapping[str, Any],
        grant: Mapping[str, Any],
    ) -> dict[str, Any]:
        grant_hint = dict(grant)
        assert_av1_v4r3_ordinal_window_grant(grant_hint)
        _assert_record_binds_plan(grant_hint, plan, "grant")
        ordinal = int(grant_hint["ordinal"])
        canonical = self.load_grant(ordinal)
        _assert_same_record(canonical, grant_hint, "grant")
        _assert_record_binds_plan(canonical, plan, "grant")
        return canonical

    def assert_no_terminal(self) -> None:
        if self.exists(_TERMINAL_NAME):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry is sealed by a terminal record"
            )

    def assert_no_record(self, filename: str, kind: str, ordinal: int) -> None:
        if self.exists(filename):
            raise AV1V4R3OrdinalWindowRegistryError(
                f"AV1 v4 r3 ordinal {ordinal} {kind} already exists"
            )

    def assert_grant_open_for_admission(
        self,
        grant: Mapping[str, Any],
        now: datetime,
    ) -> None:
        opens = _admission_opens(grant)
        closes = _admission_closes(grant)
        if now < opens or now >= closes:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window admission is outside the grant interval"
            )

    def assert_next_ordinal_admissible(
        self,
        plan: Mapping[str, Any],
        ordinal: int,
        now: datetime,
    ) -> None:
        high_water = self.derive_prior_success_high_water(plan, ordinal)
        if self.exists(_TERMINAL_NAME):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry is sealed by a terminal record"
            )
        if ordinal != high_water + 1:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window authority is not for the next ordinal"
            )
        if ordinal > 1:
            prior = self.load_outcome(ordinal - 1)
            if now < _parse_ts(prior["outcome_at"]):
                self.publish_terminal(plan)
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 ordinal window clock moved before prior outcome"
                )

    def assert_next_execution_ordinal_admissible(
        self,
        plan: Mapping[str, Any],
        preflight: Mapping[str, Any],
        ordinal: int,
        now: datetime,
    ) -> None:
        self.assert_next_ordinal_admissible(plan, ordinal, now)
        for prior in range(1, ordinal):
            try:
                sequencing_grant = self.load_grant(prior)
                execution_grant = self.load_execution_grant(prior)
                execution_claim = self.load_execution_claim(
                    str(execution_grant["grant_id"])
                )
                admission = self.load_runner_admission(prior)
                assert_av1_v4r3_execution_grant_chain(
                    plan=plan,
                    preflight=preflight,
                    sequencing_grant=sequencing_grant,
                    execution_grant=execution_grant,
                )
                assert_av1_v4r3_execution_claim_chain(
                    execution_claim=execution_claim,
                    execution_grant=execution_grant,
                    sequencing_grant=sequencing_grant,
                )
                assert_av1_v4r3_runner_admission(admission)
                self._assert_prior_execution_admission(
                    prior=prior,
                    sequencing_grant=sequencing_grant,
                    execution_grant=execution_grant,
                    execution_claim=execution_claim,
                    admission=admission,
                    preflight=preflight,
                )
            except (KeyError, OSError, ValueError) as exc:
                self.publish_terminal(plan)
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 prior execution admission is incomplete"
                ) from exc

    def _assert_prior_execution_admission(
        self,
        *,
        prior: int,
        sequencing_grant: Mapping[str, Any],
        execution_grant: Mapping[str, Any],
        execution_claim: Mapping[str, Any],
        admission: Mapping[str, Any],
        preflight: Mapping[str, Any],
    ) -> None:
        started = self.load_started(prior)
        readiness = next(
            (
                object_dict(item)
                for item in object_list(preflight.get("ordinal_readiness"))
                if object_dict(item).get("ordinal") == prior
            ),
            {},
        )
        pairs = (
            (admission.get("ordinal"), prior),
            (admission.get("execution_grant_id"), execution_grant.get("grant_id")),
            (
                admission.get("execution_grant_payload_sha256"),
                execution_grant.get("payload_sha256"),
            ),
            (admission.get("execution_claim_id"), execution_claim.get("claim_id")),
            (
                admission.get("execution_claim_payload_sha256"),
                execution_claim.get("payload_sha256"),
            ),
            (admission.get("plan_id"), execution_grant.get("plan_id")),
            (
                admission.get("plan_payload_sha256"),
                execution_grant.get("plan_payload_sha256"),
            ),
            (admission.get("preflight_id"), execution_grant.get("preflight_id")),
            (
                admission.get("preflight_payload_sha256"),
                execution_grant.get("preflight_payload_sha256"),
            ),
            (
                admission.get("sequencing_grant_id"),
                sequencing_grant.get("grant_id"),
            ),
            (
                admission.get("sequencing_grant_payload_sha256"),
                sequencing_grant.get("payload_sha256"),
            ),
            (
                admission.get("owner_principal"),
                execution_grant.get("owner_principal"),
            ),
            (admission.get("asset_id"), execution_grant.get("asset_id")),
            (admission.get("closure_id"), execution_grant.get("closure_id")),
            (
                admission.get("invocation_sha256"),
                execution_grant.get("invocation_sha256"),
            ),
            (
                admission.get("path_privacy_key_id"),
                execution_grant.get("path_privacy_key_id"),
            ),
            (admission.get("asset_id"), readiness.get("asset_id")),
            (admission.get("closure_id"), readiness.get("closure_id")),
            (
                admission.get("invocation_sha256"),
                readiness.get("invocation_sha256"),
            ),
            (
                admission.get("transform_plan_id"),
                readiness.get("transform_plan_id"),
            ),
            (admission.get("size_goal_id"), readiness.get("size_goal_id")),
            (
                admission.get("ledger_closure_id"),
                readiness.get("ledger_closure_id"),
            ),
        )
        admitted_at = _parse_ts(admission["admitted_at"])
        if (
            any(left != right for left, right in pairs)
            or admitted_at < _parse_ts(execution_claim["claimed_at"])
            or admitted_at > _parse_ts(started["started_at"])
        ):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 prior execution admission chain is invalid"
            )
        self.assert_grant_open_for_admission(sequencing_grant, admitted_at)
        assert_av1_v4r3_execution_grant_active(
            execution_grant,
            as_of=str(admission["admitted_at"]),
        )

    def derive_prior_success_high_water(
        self,
        plan: Mapping[str, Any],
        ordinal: int,
    ) -> int:
        high_water = 0
        for prior in range(1, ordinal):
            if not all(
                self.exists(name)
                for name in (
                    _claim_name(prior),
                    _started_name(prior),
                    _outcome_name(prior),
                )
            ):
                self.publish_terminal(plan)
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 ordinal window prior ordinal is incomplete"
                )
            claim = self.load_claim(prior)
            started = self.load_started(prior)
            outcome = self.load_outcome(prior)
            self.assert_success_chain(plan, claim, started, outcome)
            high_water = prior
        return high_water

    def derive_high_water_or_terminal(self, plan: Mapping[str, Any]) -> int:
        high_water = 0
        for ordinal in range(1, AV1_V4R3_OW_ORDINAL_COUNT + 1):
            claim_exists = self.exists(_claim_name(ordinal))
            started_exists = self.exists(_started_name(ordinal))
            outcome_exists = self.exists(_outcome_name(ordinal))
            if not claim_exists and not started_exists and not outcome_exists:
                for later in range(ordinal + 1, AV1_V4R3_OW_ORDINAL_COUNT + 1):
                    if any(
                        self.exists(name)
                        for name in (
                            _claim_name(later),
                            _started_name(later),
                            _outcome_name(later),
                        )
                    ):
                        self.publish_terminal(plan)
                        raise AV1V4R3OrdinalWindowRegistryError(
                            "AV1 v4 r3 ordinal window registry has an ordinal gap"
                        )
                return high_water
            if not (claim_exists and started_exists and outcome_exists):
                self.publish_terminal(plan)
                raise AV1V4R3OrdinalWindowRegistryError(
                    "AV1 v4 r3 ordinal window registry has an incomplete started state"
                )
            claim = self.load_claim(ordinal)
            started = self.load_started(ordinal)
            outcome = self.load_outcome(ordinal)
            try:
                self.assert_success_chain(plan, claim, started, outcome)
            except AV1V4R3OrdinalWindowRegistryError:
                self.publish_terminal(plan)
                raise
            high_water = ordinal
        self.publish_terminal(plan)
        return high_water

    def assert_success_chain(
        self,
        plan: Mapping[str, Any],
        claim: Mapping[str, Any],
        started: Mapping[str, Any],
        outcome: Mapping[str, Any],
    ) -> None:
        _assert_record_binds_plan(claim, plan, "claim")
        _assert_record_binds_plan(started, plan, "started")
        _assert_record_binds_plan(outcome, plan, "outcome")
        if started.get("claim_id") != claim.get("claim_id") or started.get(
            "claim_payload_sha256"
        ) != claim.get("payload_sha256"):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window started chain is invalid"
            )
        if outcome.get("started_id") != started.get("started_id") or outcome.get(
            "started_payload_sha256"
        ) != started.get("payload_sha256"):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window outcome chain is invalid"
            )
        if _parse_ts(started["started_at"]) < _parse_ts(claim["claimed_at"]):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window started precedes claim"
            )
        if _parse_ts(outcome["outcome_at"]) <= _parse_ts(started["started_at"]):
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window outcome does not follow start"
            )
        if outcome.get("success") is not True:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window failure outcome is terminal"
            )

    def publish_terminal(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        if self.exists(_TERMINAL_NAME):
            return self.load_terminal()
        outcomes: list[Mapping[str, Any]] = []
        for ordinal in range(1, AV1_V4R3_OW_ORDINAL_COUNT + 1):
            if not self.exists(_outcome_name(ordinal)):
                break
            outcome = self.load_outcome(ordinal)
            _assert_record_binds_plan(outcome, plan, "outcome")
            outcomes.append(outcome)
            if outcome.get("success") is not True:
                break
        if self._now is None:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window terminal requires an authoritative clock"
            )
        terminal = build_av1_v4r3_ordinal_window_terminal(
            plan=plan,
            outcomes=outcomes,
            terminal_at=_format_ts(self._now),
        )
        self.write(_TERMINAL_NAME, serialize_av1_v4r3_ordinal_window_terminal(terminal))
        return terminal


@contextmanager
def _locked_registry(registry: Path) -> Iterator[_RegistryContext]:
    registry_path = _normalize_registry_path(registry)
    flags = _dir_open_flags()
    with _PROCESS_LOCK:
        try:
            dir_fd = os.open(registry_path, flags)
        except OSError as exc:
            raise AV1V4R3OrdinalWindowRegistryError(
                "AV1 v4 r3 ordinal window registry is unavailable"
            ) from exc
        try:
            _assert_registry_fd(dir_fd)
            fcntl.flock(dir_fd, fcntl.LOCK_EX)
            context = _RegistryContext(registry=Path(registry_path), dir_fd=dir_fd)
            context.cleanup_stale_temps()
            yield context
        finally:
            try:
                fcntl.flock(dir_fd, fcntl.LOCK_UN)
            finally:
                os.close(dir_fd)


def _normalize_registry_path(registry: Path) -> str:
    if not isinstance(registry, Path):
        registry = Path(registry)
    if not registry.is_absolute():
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry must be an absolute path"
        )
    value = os.fsdecode(registry)
    if "\x00" in value:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry path is invalid"
        )
    return value


def _dir_open_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _assert_registry_fd(dir_fd: int) -> None:
    metadata = os.fstat(dir_fd)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry must be an owner-only real directory"
        )


def _assert_registry_filename(filename: str) -> None:
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\x00" in filename
    ):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry filename is invalid"
        )


def _is_registry_artifact_filename(filename: str) -> bool:
    if filename in {_PLAN_NAME, _PREFLIGHT_NAME, _TERMINAL_NAME}:
        return True
    if _EXECUTION_CLAIM_FILENAME_RE.fullmatch(filename):
        return True
    return any(
        filename
        in {
            _grant_name(ordinal),
            _claim_name(ordinal),
            _started_name(ordinal),
            _outcome_name(ordinal),
            _execution_grant_name(ordinal),
            _runner_admission_name(ordinal),
        }
        for ordinal in range(1, AV1_V4R3_OW_ORDINAL_COUNT + 1)
    )


def _assert_clock(clock: Clock) -> None:
    if not callable(clock):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry clock must be callable"
        )


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry timestamp is invalid"
        ) from exc
    if parsed.microsecond != 0:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry timestamp must use whole seconds"
        )
    return parsed.astimezone(UTC)


def _admission_opens(grant: Mapping[str, Any]) -> datetime:
    return _parse_ts(grant["authorized_at"]) + timedelta(
        seconds=AV1_V4R3_OW_SETUP_SECONDS
    )


def _admission_closes(grant: Mapping[str, Any]) -> datetime:
    return _parse_ts(grant["valid_until"]) - timedelta(
        seconds=AV1_V4R3_OW_TEARDOWN_SECONDS
    )


def _format_ts(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry timestamp must be timezone-aware"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assert_binding_matches_plan(
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
) -> None:
    if plan.get("run_registry_id") != binding.run_registry_id:
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 ordinal window registry binding does not match the plan"
        )


def _assert_same_record(
    canonical: Mapping[str, Any],
    candidate: Mapping[str, Any],
    label: str,
) -> None:
    if canonical_json_bytes(canonical) != canonical_json_bytes(candidate):
        raise AV1V4R3OrdinalWindowRegistryError(
            f"AV1 v4 r3 ordinal window {label} does not match registry custody"
        )


def _assert_record_binds_plan(
    record: Mapping[str, Any],
    plan: Mapping[str, Any],
    label: str,
) -> None:
    if record.get("plan_id") != plan.get("plan_id") or record.get(
        "plan_payload_sha256"
    ) != plan.get("payload_sha256"):
        raise AV1V4R3OrdinalWindowRegistryError(
            f"AV1 v4 r3 ordinal window {label} does not bind the registry plan"
        )


def _assert_record_binds_grant(
    record: Mapping[str, Any],
    grant: Mapping[str, Any],
    label: str,
) -> None:
    if record.get("grant_id") != grant.get("grant_id") or record.get(
        "grant_payload_sha256"
    ) != grant.get("payload_sha256"):
        raise AV1V4R3OrdinalWindowRegistryError(
            f"AV1 v4 r3 ordinal window {label} does not bind the registry grant"
        )


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_uid == second.st_uid
        and first.st_mode == second.st_mode
        and first.st_nlink == second.st_nlink
    )


def _grant_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.grant.json"


def _claim_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.claim.json"


def _started_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.started.json"


def _outcome_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.outcome.json"


def _execution_grant_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.execution-grant.json"


def _runner_admission_name(ordinal: int) -> str:
    return f"ordinal_{ordinal:02d}.runner-admission.json"


def _execution_claim_name(grant_id: str) -> str:
    filename = f"{grant_id}.claim.json"
    if not _EXECUTION_CLAIM_FILENAME_RE.fullmatch(filename):
        raise AV1V4R3OrdinalWindowRegistryError(
            "AV1 v4 r3 execution claim filename is invalid"
        )
    return filename
