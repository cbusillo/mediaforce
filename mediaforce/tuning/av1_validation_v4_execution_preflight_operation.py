from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import stat
import subprocess
import threading
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_REVISED_AT,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    AV1_VALIDATION_V4_VALID_UNTIL,
    av1_validation_v4_contains_private_text,
    load_av1_validation_manifest_v4,
)
from mediaforce.tuning.av1_validation_v4_execution_plan import (
    AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_CONTRACT_VERSION,
    av1_validation_v4_execution_plan_false_authority_payload,
    derive_av1_validation_v4_production_execution_plans,
)
from mediaforce.tuning.av1_validation_v4_freeze import (
    AV1ValidationV4FreezeError,
    assert_av1_validation_v4_manifest_freeze_binds_bundle,
    load_av1_validation_v4_manifest_freeze,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    assert_av1_validation_v4_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    load_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    load_av1_validation_v4_effective_config_snapshot,
    serialize_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_measurement import (
    load_av1_validation_v4_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    load_av1_validation_v4_rights_attestation,
)


AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA = (
    "mediaforce.av1_cold_start_v4_production_execution_readiness_preflight"
)
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_CONTRACT_VERSION = (
    "av1v4execpreflight1"
)
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_FILENAME = (
    f"{AV1_VALIDATION_V4_MANIFEST_ID}-revision-"
    f"{AV1_VALIDATION_V4_MANIFEST_REVISION}-production-execution-preflight.json"
)
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_BLOCKERS = (
    "video_policy_incomplete_for_production_search",
    "stream_budget_target_unresolvable",
    "grant_window_shorter_than_run_budget",
    "temp_isolation_not_identity_bound",
)

_RUN_BUDGET_SECONDS_MAX = 129_600
_GRANT_BUDGET_SECONDS_MAX = 14_400
_REQUIRED_VIDEO_POLICY_KEYS = (
    "pixel_format",
    "sample_every",
    "sample_duration",
    "max_encoded_percent",
)
_SHA256_RE = "sha256:"
_REQUEST_SCHEMA = "mediaforce.av1_cold_start_v4_qualification_request"
_REQUEST_CONTRACT_VERSION = "av1v4qreq1"
_REQUEST_STATE = "owner_submitted"
_REQUEST_ALLOWED_KEYS = (
    frozenset(
        {
            "config_canonical_file_sha256",
            "config_id",
            "consumption_registry",
            "contract_version",
            "discovery_public_sha256",
            "execution_repository",
            "experiment_id",
            "freeze_id",
            "freeze_payload_sha256",
            "invocations",
            "manifest_id",
            "manifest_payload_sha256",
            "manifest_revision",
            "owner_principal",
            "path_privacy_key_id",
            "payload_sha256",
            "preparation_id",
            "preparation_payload_sha256",
            "preparation_repository",
            "protocol_version",
            "request_id",
            "requested_at",
            "runtime_compatibility_id",
            "schema",
            "schema_version",
            "state",
            "valid_until",
        }
    )
    | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
)


class AV1ValidationV4ExecutionPreflightOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4ExecutionPreflightOperationInputs:
    repository_root: Path
    registry: Path
    manifest_path: Path
    freeze_path: Path
    rights_attestation_path: Path
    preparation_grant_path: Path
    effective_config_path: Path
    preparation_path: Path
    preparation_measurement_path: Path
    qualification_request_path: Path


@dataclass(frozen=True, slots=True)
class AV1ValidationV4ExecutionPreflightOperationResult:
    preflight: Mapping[str, Any]
    path: Path
    created: bool


Clock = Callable[[], datetime]
_PROCESS_REGISTRY_LOCK = threading.Lock()


def materialize_av1_validation_v4_execution_preflight(
    inputs: AV1ValidationV4ExecutionPreflightOperationInputs,
    *,
    now: Clock = lambda: datetime.now(UTC),
) -> AV1ValidationV4ExecutionPreflightOperationResult:
    _assert_registry(inputs.registry, repository_root=inputs.repository_root)
    with _registry_lock(inputs.registry):
        return _materialize_locked(inputs, now=now)


def serialize_av1_validation_v4_execution_preflight(
    payload: Mapping[str, Any],
) -> bytes:
    materialized = json.loads(canonical_json_bytes(payload))
    assert_av1_validation_v4_execution_preflight(materialized)
    return canonical_json_bytes(materialized) + b"\n"


def assert_av1_validation_v4_execution_preflight(
    payload: Mapping[str, Any],
) -> None:
    materialized = object_dict(payload)
    allowed = {
        "all_authority_fields_false",
        "all_invocation_digests_match",
        "blockers",
        "config_canonical_file_sha256",
        "config_id",
        "contract_version",
        "created_at",
        "execution_plan_contract_version",
        "execution_ready",
        "execution_repository",
        "experiment_id",
        "freeze_id",
        "freeze_payload_sha256",
        "manifest_id",
        "manifest_payload_sha256",
        "manifest_revision",
        "owner_principal",
        "payload_sha256",
        "plans",
        "preflight_id",
        "preparation_id",
        "preparation_payload_sha256",
        "protocol_version",
        "request_id",
        "request_payload_sha256",
        "schema",
        "schema_version",
        "state",
    } | AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    if not materialized or set(materialized) != allowed:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight shape is invalid"
        )
    _assert_no_private_text(materialized)
    expected = {
        "schema": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
        "execution_plan_contract_version": (
            AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_CONTRACT_VERSION
        ),
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": "blocked_protocol_revision_2",
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "blockers": list(AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_BLOCKERS),
        "execution_ready": False,
        "all_invocation_digests_match": True,
        "all_authority_fields_false": True,
    }
    if any(materialized.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if materialized.get(field) is not False:
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight cannot authorize operations"
            )
    plans = [object_dict(item) for item in object_list(materialized.get("plans"))]
    if len(plans) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight plan count is invalid"
        )
    for ordinal, plan in enumerate(plans, start=1):
        if (
            plan.get("ordinal") != ordinal
            or plan.get("digest_matches_request") is not True
            or plan.get("digest_matches_preparation") is not True
            or plan.get("digest_matches_expected") is not True
        ):
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight plan digest proof is invalid"
            )
    _assert_sha_fields(materialized)
    without_sha = {k: v for k, v in materialized.items() if k != "payload_sha256"}
    if materialized.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight payload SHA-256 does not match"
        )
    without_id = {
        k: v
        for k, v in materialized.items()
        if k not in {"preflight_id", "payload_sha256"}
    }
    expected_id = f"av1vexecpreflight4_{stable_json_hash(without_id)[:32]}"
    if materialized.get("preflight_id") != expected_id:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight ID does not match"
        )


def _materialize_locked(
    inputs: AV1ValidationV4ExecutionPreflightOperationInputs,
    *,
    now: Clock,
) -> AV1ValidationV4ExecutionPreflightOperationResult:
    manifest = _load_manifest(inputs)
    rights = load_av1_validation_v4_rights_attestation(inputs.rights_attestation_path)
    grant = load_av1_validation_v4_preparation_grant(inputs.preparation_grant_path)
    claim = load_av1_validation_v4_preparation_claim(
        _claim_registry_path(grant, repository_root=inputs.repository_root)
        / f"{grant['grant_id']}.json"
    )
    config = load_av1_validation_v4_effective_config_snapshot(
        inputs.effective_config_path
    )
    _assert_fixed_blocker_assumptions(manifest, config)
    preparation = _load_canonical_object(
        inputs.preparation_path, "AV1 v4 preparation record"
    )
    measurement = load_av1_validation_v4_preparation_measurement(
        inputs.preparation_measurement_path
    )
    assert_av1_validation_v4_preparation_bundle(preparation, rights, grant, claim)
    freeze = _load_freeze(
        inputs.freeze_path,
        rights=rights,
        grant=grant,
        claim=claim,
        config=config,
        preparation=preparation,
        measurement=measurement,
    )
    request = _load_qualification_request(inputs.qualification_request_path)
    as_of = _canonical_timestamp(now())
    _assert_request_active(request, as_of=as_of)
    execution_commit, execution_tree = _measure_repository_identity(
        inputs.repository_root
    )
    _assert_request_identity(
        request,
        freeze=freeze,
        preparation=preparation,
        config=config,
        config_file_sha256=_sha256_bytes(
            serialize_av1_validation_v4_effective_config_snapshot(config)
        ),
        execution_commit=execution_commit,
        execution_tree=execution_tree,
    )
    plans = derive_av1_validation_v4_production_execution_plans(
        manifest=manifest,
        effective_config=config,
        preparation=preparation,
        qualification_request=request,
    )
    if not all(plan.digest_matches_expected for plan in plans):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight invocation digest proof failed"
        )
    preflight = _build_preflight(
        freeze=freeze,
        config=config,
        preparation=preparation,
        request=request,
        plans=[plan.public_payload() for plan in plans],
        execution_commit=execution_commit,
        execution_tree=execution_tree,
        created_at=as_of,
    )
    serialized = serialize_av1_validation_v4_execution_preflight(preflight)
    path = inputs.registry / AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_FILENAME
    _cleanup_stale_temp_files(path)
    if path.exists() or path.is_symlink():
        existing = _reconcile_existing_preflight(path, expected=preflight, as_of=as_of)
        return AV1ValidationV4ExecutionPreflightOperationResult(
            preflight=existing,
            path=path,
            created=False,
        )
    if not _atomic_publish(path, serialized):
        existing = _reconcile_existing_preflight(path, expected=preflight, as_of=as_of)
        return AV1ValidationV4ExecutionPreflightOperationResult(
            preflight=existing,
            path=path,
            created=False,
        )
    _assert_file_custody(path, expected_size=len(serialized))
    return AV1ValidationV4ExecutionPreflightOperationResult(
        preflight=preflight,
        path=path,
        created=True,
    )


def _build_preflight(
    *,
    freeze: Mapping[str, Any],
    config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    request: Mapping[str, Any],
    plans: list[dict[str, Any]],
    execution_commit: str,
    execution_tree: str,
    created_at: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_SCHEMA_VERSION,
        "contract_version": AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_CONTRACT_VERSION,
        "execution_plan_contract_version": (
            AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_CONTRACT_VERSION
        ),
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": "blocked_protocol_revision_2",
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
        "owner_principal": freeze["owner_principal"],
        "created_at": created_at,
        "freeze_id": freeze["freeze_id"],
        "freeze_payload_sha256": freeze["payload_sha256"],
        "config_id": config["config_id"],
        "config_canonical_file_sha256": request["config_canonical_file_sha256"],
        "preparation_id": preparation["preparation_id"],
        "preparation_payload_sha256": preparation["payload_sha256"],
        "request_id": request["request_id"],
        "request_payload_sha256": request["payload_sha256"],
        "execution_repository": {
            "commit": execution_commit,
            "tree": execution_tree,
        },
        "plans": plans,
        "blockers": list(AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PREFLIGHT_BLOCKERS),
        "execution_ready": False,
        "all_invocation_digests_match": True,
        "all_authority_fields_false": True,
    }
    payload.update(av1_validation_v4_execution_plan_false_authority_payload())
    bound = dict(payload)
    bound["preflight_id"] = _preflight_id(bound)
    bound["payload_sha256"] = f"sha256:{stable_json_hash(bound)}"
    materialized = json.loads(canonical_json_bytes(bound))
    assert_av1_validation_v4_execution_preflight(materialized)
    return materialized


def _preflight_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"preflight_id", "payload_sha256"}
    }
    return f"av1vexecpreflight4_{stable_json_hash(semantic)[:32]}"


def _load_manifest(
    inputs: AV1ValidationV4ExecutionPreflightOperationInputs,
) -> dict[str, Any]:
    _assert_manifest_location(
        inputs.manifest_path, repository_root=inputs.repository_root
    )
    manifest = load_av1_validation_manifest_v4(inputs.manifest_path)
    if (
        manifest.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or manifest.get("payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
        or manifest.get("state") != "draft_unapproved"
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight manifest binding is invalid"
        )
    missing = [
        key
        for key in _REQUIRED_VIDEO_POLICY_KEYS
        if key
        not in object_dict(manifest.get("qualification_invocation")).get(
            "video_policy", {}
        )
    ]
    if tuple(missing) != _REQUIRED_VIDEO_POLICY_KEYS:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight blocker assumptions changed"
        )
    return manifest


def _assert_fixed_blocker_assumptions(
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
) -> None:
    invocation = object_dict(manifest.get("qualification_invocation"))
    video_policy = object_dict(invocation.get("video_policy"))
    missing = [key for key in _REQUIRED_VIDEO_POLICY_KEYS if key not in video_policy]
    resources = object_dict(manifest.get("resource_limits"))
    config_payload = object_dict(config)
    if (
        tuple(missing) != _REQUIRED_VIDEO_POLICY_KEYS
        or any(key in video_policy for key in ("target_size", "target_size_bytes"))
        or resources.get("public_run_seconds_max") != _RUN_BUDGET_SECONDS_MAX
        or _GRANT_BUDGET_SECONDS_MAX >= _RUN_BUDGET_SECONDS_MAX
        or "quality_temp_dir"
        not in object_list(config_payload.get("omitted_search_kwargs"))
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight blocker assumptions changed"
        )


def _load_freeze(
    path: Path,
    *,
    rights: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    measurement: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        freeze = load_av1_validation_v4_manifest_freeze(
            path,
            rights_attestation=rights,
            preparation_grant=grant,
            preparation_claim=claim,
            effective_config=config,
            preparation=preparation,
            preparation_measurement=measurement,
        )
        assert_av1_validation_v4_manifest_freeze_binds_bundle(
            freeze,
            rights_attestation=rights,
            preparation_grant=grant,
            preparation_claim=claim,
            effective_config=config,
            preparation=preparation,
            preparation_measurement=measurement,
        )
    except AV1ValidationV4FreezeError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight freeze does not bind the full bundle"
        ) from exc
    return freeze


def _assert_request_identity(
    request: Mapping[str, Any],
    *,
    freeze: Mapping[str, Any],
    preparation: Mapping[str, Any],
    config: Mapping[str, Any],
    config_file_sha256: str,
    execution_commit: str,
    execution_tree: str,
) -> None:
    if (
        request.get("freeze_id") != freeze.get("freeze_id")
        or request.get("freeze_payload_sha256") != freeze.get("payload_sha256")
        or request.get("preparation_id") != preparation.get("preparation_id")
        or request.get("preparation_payload_sha256")
        != preparation.get("payload_sha256")
        or request.get("config_id") != config.get("config_id")
        or request.get("config_canonical_file_sha256") != config_file_sha256
        or object_dict(request.get("execution_repository"))
        != {"commit": execution_commit, "tree": execution_tree}
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight request identity does not match"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if request.get(field) is not False:
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight request must not carry authority"
            )


def _assert_request_active(request: Mapping[str, Any], *, as_of: str) -> None:
    checked_at = _parse_timestamp(as_of)
    requested_at = _parse_timestamp(request.get("requested_at"))
    valid_until = _parse_timestamp(request.get("valid_until"))
    if checked_at < requested_at or checked_at >= valid_until:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight request is not active"
        )


def _reconcile_existing_preflight(
    path: Path,
    *,
    expected: Mapping[str, Any],
    as_of: str,
) -> dict[str, Any]:
    existing, existing_size = _load_preflight_with_custody(path)
    if _parse_timestamp(existing["created_at"]) > _parse_timestamp(as_of):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight singleton timestamp is invalid"
        )
    expected_existing_time = dict(expected)
    expected_existing_time["created_at"] = existing["created_at"]
    expected_existing_time["preflight_id"] = _preflight_id(expected_existing_time)
    expected_without_sha = {
        key: value
        for key, value in expected_existing_time.items()
        if key != "payload_sha256"
    }
    expected_existing_time["payload_sha256"] = (
        f"sha256:{stable_json_hash(expected_without_sha)}"
    )
    expected_existing_time = json.loads(canonical_json_bytes(expected_existing_time))
    if existing != expected_existing_time:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight singleton is different"
        )
    expected_size = len(serialize_av1_validation_v4_execution_preflight(existing))
    if existing_size != expected_size:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight singleton custody is invalid"
        )
    _assert_file_custody(path, expected_size=expected_size)
    return existing


def _assert_registry(path: Path, *, repository_root: Path) -> None:
    if not path.is_absolute() or not repository_root.is_absolute():
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight paths must be absolute"
        )
    try:
        metadata = path.lstat()
        resolved_registry = path.resolve(strict=True)
        resolved_repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight registry is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or path != resolved_registry
        or repository_root != resolved_repository
        or resolved_registry.is_relative_to(resolved_repository)
        or resolved_repository.is_relative_to(resolved_registry)
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight registry must be an owner-only directory outside the repository"
        )


@contextmanager
def _registry_lock(registry: Path) -> Iterator[None]:
    with _PROCESS_REGISTRY_LOCK:
        directory_descriptor = os.open(
            registry,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            lock_descriptor = os.open(
                ".av1-v4-production-execution-preflight.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_descriptor,
            )
            try:
                metadata = os.fstat(lock_descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_nlink != 1
                ):
                    raise AV1ValidationV4ExecutionPreflightOperationError(
                        "AV1 v4 execution preflight registry lock is invalid"
                    )
                os.fsync(directory_descriptor)
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(lock_descriptor)
        finally:
            os.close(directory_descriptor)


def _claim_registry_path(
    grant: Mapping[str, Any],
    *,
    repository_root: Path,
) -> Path:
    value = grant.get("consumption_registry")
    if not isinstance(value, str):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight preparation claim registry is invalid"
        )
    pure = PurePosixPath(value)
    if (
        not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "//" in value
        or value.endswith("/")
        or ".." in pure.parts
        or pure.as_posix() != value
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight preparation claim registry is invalid"
        )
    path = Path(value)
    _assert_registry(path, repository_root=repository_root)
    return path.resolve(strict=True)


def _measure_repository_identity(repository_root: Path) -> tuple[str, str]:
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
    }
    try:
        identity = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "rev-parse",
                "--show-toplevel",
                "HEAD",
                "HEAD^{tree}",
            ],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        status_result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight repository measurement failed"
        ) from exc
    values = identity.stdout.splitlines()
    if (
        identity.returncode != 0
        or len(values) != 3
        or Path(values[0]).resolve() != repository_root.resolve()
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight repository measurement failed"
        )
    if status_result.returncode != 0 or status_result.stdout:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight repository must be clean"
        )
    return values[1].strip(), values[2].strip()


def _assert_manifest_location(path: Path, *, repository_root: Path) -> None:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_root = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight manifest path is unavailable"
        ) from exc
    expected = resolved_root / "docs/validation/av1-cold-start-preregistration-v4.json"
    if resolved_path != expected:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight manifest is outside the repository"
        )


def _load_canonical_object(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            f"{label} is unreadable"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4ExecutionPreflightOperationError(
            f"{label} bytes are not canonical"
        )
    return payload


def _load_qualification_request(path: Path) -> dict[str, Any]:
    payload = _load_canonical_object(path, "AV1 v4 qualification request")
    _assert_qualification_request(payload)
    return payload


def _assert_qualification_request(payload: Mapping[str, Any]) -> None:
    request = object_dict(payload)
    if set(request) != _REQUEST_ALLOWED_KEYS:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight qualification request shape is invalid"
        )
    _assert_no_private_text(
        {key: value for key, value in request.items() if key != "consumption_registry"}
    )
    expected = {
        "schema": _REQUEST_SCHEMA,
        "schema_version": 1,
        "contract_version": _REQUEST_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "manifest_revision": AV1_VALIDATION_V4_MANIFEST_REVISION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "state": _REQUEST_STATE,
        "manifest_id": AV1_VALIDATION_V4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_VALIDATION_V4_PAYLOAD_SHA256,
    }
    if any(request.get(key) != value for key, value in expected.items()):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight qualification request binding is invalid"
        )
    for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
        if request.get(field) is not False:
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight qualification request carries authority"
            )
    _assert_repository_mapping(request.get("preparation_repository"))
    _assert_repository_mapping(request.get("execution_repository"))
    _assert_request_window(request)
    request_id = str(request.get("request_id") or "")
    if (
        not request_id.startswith("av1vqualreq4_")
        or len(request_id) != len("av1vqualreq4_") + 32
        or request_id != _qualification_request_id(request)
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight qualification request ID is invalid"
        )
    without_sha = {
        key: value for key, value in request.items() if key != "payload_sha256"
    }
    if request.get("payload_sha256") != f"sha256:{stable_json_hash(without_sha)}":
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight qualification request digest is invalid"
        )


def _qualification_request_id(payload: Mapping[str, Any]) -> str:
    semantic = {
        key: value
        for key, value in payload.items()
        if key not in {"request_id", "payload_sha256"}
    }
    return f"av1vqualreq4_{stable_json_hash(semantic)[:32]}"


def _assert_request_window(payload: Mapping[str, Any]) -> None:
    revised_at = _parse_timestamp(AV1_VALIDATION_V4_REVISED_AT)
    manifest_valid_until = _parse_timestamp(AV1_VALIDATION_V4_VALID_UNTIL)
    requested_at = _parse_timestamp(payload.get("requested_at"))
    valid_until = _parse_timestamp(payload.get("valid_until"))
    if not revised_at <= requested_at < valid_until <= manifest_valid_until:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight qualification request window is invalid"
        )


def _assert_repository_mapping(value: Any) -> None:
    repository = object_dict(value)
    if set(repository) != {"commit", "tree"} or any(
        not isinstance(repository.get(field), str)
        or len(str(repository[field])) not in {40, 64}
        or any(
            character not in "0123456789abcdef" for character in str(repository[field])
        )
        for field in ("commit", "tree")
    ):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight repository identity is invalid"
        )


def _atomic_publish(path: Path, data: bytes) -> bool:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    temp_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory_descriptor)
        except Exception as write_exc:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError as cleanup_exc:
                raise AV1ValidationV4ExecutionPreflightOperationError(
                    "AV1 v4 execution preflight temporary write cleanup failed"
                ) from ExceptionGroup(
                    "AV1 v4 execution preflight write and cleanup failures",
                    [write_exc, cleanup_exc],
                )
            raise
        try:
            os.link(
                temp_name,
                path.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError:
            os.unlink(temp_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
            return False
        except OSError as link_exc:
            try:
                os.unlink(temp_name, dir_fd=directory_descriptor)
                os.fsync(directory_descriptor)
            except OSError as cleanup_exc:
                raise AV1ValidationV4ExecutionPreflightOperationError(
                    "AV1 v4 execution preflight link cleanup failed"
                ) from ExceptionGroup(
                    "AV1 v4 execution preflight link and cleanup failures",
                    [link_exc, cleanup_exc],
                )
            raise
        os.fsync(directory_descriptor)
        try:
            os.unlink(temp_name, dir_fd=directory_descriptor)
            os.fsync(directory_descriptor)
        except OSError as exc:
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight materialized but temporary remains"
            ) from exc
        return True
    finally:
        os.close(directory_descriptor)


def _load_preflight_with_custody(path: Path) -> tuple[dict[str, Any], int]:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size <= 0
            ):
                raise AV1ValidationV4ExecutionPreflightOperationError(
                    "AV1 v4 execution preflight candidate custody is invalid"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read()
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight candidate is unavailable"
        ) from exc
    finally:
        os.close(directory_descriptor)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight bytes are not canonical"
        ) from exc
    if not isinstance(payload, dict) or raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight bytes are not canonical"
        )
    assert_av1_validation_v4_execution_preflight(payload)
    return payload, len(raw)


def _cleanup_stale_temp_files(path: Path) -> None:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        prefix = f".{path.name}."
        removed = False
        for name in os.listdir(directory_descriptor):
            if not name.startswith(prefix) or not name.endswith(".tmp"):
                continue
            candidate = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(candidate.st_mode)
                or stat.S_IMODE(candidate.st_mode) != 0o600
                or candidate.st_uid != os.geteuid()
            ):
                raise AV1ValidationV4ExecutionPreflightOperationError(
                    "AV1 v4 execution preflight registry contains unexpected temp data"
                )
            os.unlink(name, dir_fd=directory_descriptor)
            removed = True
        if removed:
            os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _assert_file_custody(path: Path, *, expected_size: int) -> None:
    directory_descriptor = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_nlink != 1
                or metadata.st_size != expected_size
            ):
                raise AV1ValidationV4ExecutionPreflightOperationError(
                    "AV1 v4 execution preflight custody verification failed"
                )
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight custody verification failed"
        ) from exc
    finally:
        os.close(directory_descriptor)


def _assert_sha_fields(payload: Mapping[str, Any]) -> None:
    for key, value in payload.items():
        if key.endswith("sha256") and (
            not isinstance(value, str)
            or not value.startswith(_SHA256_RE)
            or len(value) != 71
        ):
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight digest field is invalid"
            )


def _assert_no_private_text(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4ExecutionPreflightOperationError(
                "AV1 v4 execution preflight exposes private text"
            )

    visit(payload)


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight clock must be timezone-aware"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight timestamp is invalid"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight timestamp is invalid"
        ) from exc
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond != 0 or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AV1ValidationV4ExecutionPreflightOperationError(
            "AV1 v4 execution preflight timestamp must be canonical UTC seconds"
        )
    return normalized
