from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import time
from typing import Any

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.quality import QualitySearchWarmStart
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_MANIFEST_ID,
    AV1_VALIDATION_V4_PAYLOAD_SHA256,
    AV1_VALIDATION_V4_SOURCE_IDS,
    av1_validation_v4_contains_private_text,
    av1_validation_v4_guided_warm_start_identities,
    av1_validation_v4_source_stream_selection_payload,
    load_av1_validation_manifest_v4,
)
from mediaforce.tuning.av1_validation_v4_path_privacy import (
    AV1_VALIDATION_V4_INSTANCE_PATH_ROLES,
    av1_validation_v4_instance_path_hmac_id,
    av1_validation_v4_path_privacy_key_id,
    av1_validation_v4_source_path_hmac_id,
)
from mediaforce.tuning.av1_validation_v4_preparation import (
    AV1ValidationV4InvocationIdentity,
    AV1ValidationV4PreparationInputs,
    AV1ValidationV4ToolIdentity,
    build_av1_validation_v4_preparation_record,
    serialize_av1_validation_v4_preparation_record,
)
from mediaforce.tuning.av1_validation_v4_preparation_claim import (
    build_av1_validation_v4_preparation_claim,
    serialize_av1_validation_v4_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    build_av1_validation_v4_effective_config_snapshot,
    serialize_av1_validation_v4_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4_preparation_grant import (
    assert_av1_validation_v4_preparation_grant_active,
    load_av1_validation_v4_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4_preparation_measurement import (
    build_av1_validation_v4_preparation_failure_measurement,
    build_av1_validation_v4_preparation_success_measurement,
    serialize_av1_validation_v4_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    av1_validation_v4_qualification_search_invocation_sha256,
)
from mediaforce.tuning.av1_validation_v4_rights import (
    load_av1_validation_v4_rights_attestation,
)
from mediaforce.tuning.av1_validation_v4_runtime_compatibility import (
    av1_validation_v4_runtime_compatibility_payload,
)


_ARTIFACT_RELATIVE_PATHS = {
    "config": Path("configuration/effective-config-snapshot.json"),
    "key": Path("secrets/path-privacy.key"),
    "measurement": Path("measurements/preparation-measurement.json"),
    "preparation": Path("preparation/preparation-record.json"),
}
_TOOL_PROBES = {
    "ab_av1": ("--version",),
    "ffmpeg": ("-version",),
    "ffprobe": ("-version",),
}


class AV1ValidationV4PreparationOperationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4PreparationOperationInputs:
    workspace: Path
    manifest_path: Path
    rights_attestation_path: Path
    preparation_grant_path: Path
    repository_commit: str
    repository_tree: str
    source_paths: Mapping[str, Path]
    dedicated_instance_paths: Mapping[str, Path]
    ffmpeg_path: Path
    ffprobe_path: Path
    ab_av1_path: Path
    operating_system: str
    operating_system_version: str
    architecture: str
    python_version: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV4PreparationOperationResult:
    claim: Mapping[str, Any]
    preparation: Mapping[str, Any]
    measurement: Mapping[str, Any]
    artifact_paths: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class _PreflightResult:
    manifest: Mapping[str, Any]
    rights: Mapping[str, Any]
    grant: Mapping[str, Any]
    tool_paths: Mapping[str, Path]
    tool_signatures: Mapping[str, tuple[int, int, int, int, int]]
    artifact_paths: Mapping[str, Path]
    search_kwargs: Mapping[str, Mapping[str, Any]]
    stream_constraints: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class _RollbackResult:
    removed_artifacts: tuple[str, ...]
    retained_artifacts: tuple[str, ...]


ProbeTool = Callable[[str, Path, Sequence[str]], Mapping[str, Any]]
Clock = Callable[[], datetime]
RandomBytes = Callable[[int], bytes]
BinaryDigest = Callable[[Path], str]


def default_av1_validation_v4_preparation_operation_inputs(
    *,
    workspace: Path,
    manifest_path: Path,
    rights_attestation_path: Path,
    preparation_grant_path: Path,
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, Path],
    dedicated_instance_paths: Mapping[str, Path],
    ffmpeg_path: Path,
    ffprobe_path: Path,
    ab_av1_path: Path,
) -> AV1ValidationV4PreparationOperationInputs:
    return AV1ValidationV4PreparationOperationInputs(
        workspace=workspace,
        manifest_path=manifest_path,
        rights_attestation_path=rights_attestation_path,
        preparation_grant_path=preparation_grant_path,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        source_paths=source_paths,
        dedicated_instance_paths=dedicated_instance_paths,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        ab_av1_path=ab_av1_path,
        operating_system=platform.system() or "unknown",
        operating_system_version=platform.platform() or "unknown",
        architecture=platform.machine() or "unknown",
        python_version=platform.python_version(),
    )


def run_av1_validation_v4_preparation_operation(
    inputs: AV1ValidationV4PreparationOperationInputs,
    *,
    now: Clock = lambda: datetime.now(UTC),
    random_bytes: RandomBytes = os.urandom,
    probe_tool: ProbeTool | None = None,
    binary_digest: BinaryDigest | None = None,
) -> AV1ValidationV4PreparationOperationResult:
    with _workspace_lock(inputs.workspace):
        return _run_locked_preparation_operation(
            inputs,
            now=now,
            random_bytes=random_bytes,
            probe_tool=probe_tool,
            binary_digest=binary_digest,
        )


def _run_locked_preparation_operation(
    inputs: AV1ValidationV4PreparationOperationInputs,
    *,
    now: Clock,
    random_bytes: RandomBytes,
    probe_tool: ProbeTool | None,
    binary_digest: BinaryDigest | None,
) -> AV1ValidationV4PreparationOperationResult:
    probe = probe_tool or _probe_tool
    digest_binary = binary_digest or _sha256_file
    started_at = _canonical_timestamp(now())
    claim: dict[str, Any] | None = None
    claim_published = False
    preflight: _PreflightResult | None = None
    path_privacy_key_id: str | None = None
    created_artifacts: set[str] = set()
    probes: list[Mapping[str, Any]] = []
    stages_completed: list[str] = []
    current_stage = "validate_inputs"
    try:
        preflight = _preflight(
            inputs,
            as_of=started_at,
        )
        stages_completed.append(current_stage)

        current_stage = "claim_grant"
        claim = build_av1_validation_v4_preparation_claim(
            preparation_grant=preflight.grant,
            rights_attestation=preflight.rights,
            claimed_at=_canonical_timestamp(now()),
        )
        _exclusive_write(
            preflight.artifact_paths["claim"],
            serialize_av1_validation_v4_preparation_claim(claim),
        )
        claim_published = True
        stages_completed.append(current_stage)

        current_stage = "create_key"
        key = random_bytes(32)
        if not isinstance(key, bytes) or len(key) != 32:
            raise AV1ValidationV4PreparationOperationError(
                "path privacy key source returned invalid material"
            )
        _exclusive_write(preflight.artifact_paths["key"], key)
        created_artifacts.add("path_privacy_key")
        key_custody = _key_custody(preflight.artifact_paths["key"])
        path_privacy_key_id = av1_validation_v4_path_privacy_key_id(key)
        stages_completed.append(current_stage)

        current_stage = "measure_config"
        config_snapshot = build_av1_validation_v4_effective_config_snapshot(
            repository_commit=inputs.repository_commit,
            repository_tree=inputs.repository_tree,
            source_paths={
                asset_id: str(inputs.source_paths[asset_id])
                for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
            },
            dedicated_instance_paths={
                role: str(inputs.dedicated_instance_paths[role])
                for role in sorted(AV1_VALIDATION_V4_INSTANCE_PATH_ROLES)
            },
            frozen_extra_search_kwargs=preflight.search_kwargs,
        )
        config_bytes = serialize_av1_validation_v4_effective_config_snapshot(
            config_snapshot
        )
        effective_config_sha256 = _sha256_bytes(config_bytes)
        _exclusive_write(preflight.artifact_paths["config"], config_bytes)
        created_artifacts.add("effective_config_snapshot")
        stages_completed.append(current_stage)

        current_stage = "probe_toolchain"
        toolchain: dict[str, dict[str, str]] = {}
        for tool_name in ("ffmpeg", "ffprobe", "ab_av1"):
            binary_sha256 = digest_binary(preflight.tool_paths[tool_name])
            measured = object_dict(
                probe(
                    tool_name,
                    preflight.tool_paths[tool_name],
                    _TOOL_PROBES[tool_name],
                )
            )
            measured["binary_sha256"] = binary_sha256
            probes.append(measured)
            if (
                _tool_signature(preflight.tool_paths[tool_name])
                != preflight.tool_signatures[tool_name]
            ):
                raise AV1ValidationV4PreparationOperationError(
                    f"{tool_name} changed during preparation"
                )
            toolchain[tool_name] = {
                "version": str(measured.get("stdout_first_line") or ""),
                "binary_sha256": binary_sha256,
            }
        stages_completed.append(current_stage)

        current_stage = "derive_path_identities"
        source_path_ids = {
            asset_id: av1_validation_v4_source_path_hmac_id(
                key,
                asset_id=asset_id,
                normalized_path=str(inputs.source_paths[asset_id]),
            )
            for asset_id in AV1_VALIDATION_V4_SOURCE_IDS
        }
        instance_path_ids = {
            role: av1_validation_v4_instance_path_hmac_id(
                key,
                role=role,
                normalized_path=str(inputs.dedicated_instance_paths[role]),
            )
            for role in sorted(AV1_VALIDATION_V4_INSTANCE_PATH_ROLES)
        }
        stages_completed.append(current_stage)

        current_stage = "derive_runtime_compatibility"
        runtime_payload = av1_validation_v4_runtime_compatibility_payload(
            effective_config_sha256=effective_config_sha256,
            toolchain=toolchain,
            operating_system=inputs.operating_system,
            operating_system_version=inputs.operating_system_version,
            architecture=inputs.architecture,
            python_version=inputs.python_version,
        )
        stages_completed.append(current_stage)

        current_stage = "derive_invocations"
        invocations = _derive_invocations(
            inputs,
            preflight.manifest,
            source_path_ids,
            preflight.stream_constraints,
            preflight.search_kwargs,
        )
        stages_completed.append(current_stage)

        current_stage = "build_record"
        preparation = build_av1_validation_v4_preparation_record(
            inputs=AV1ValidationV4PreparationInputs(
                prepared_at=_canonical_timestamp(now()),
                repository_commit=inputs.repository_commit,
                repository_tree=inputs.repository_tree,
                effective_config_sha256=effective_config_sha256,
                ffmpeg=_tool_identity(toolchain["ffmpeg"]),
                ffprobe=_tool_identity(toolchain["ffprobe"]),
                ab_av1=_tool_identity(toolchain["ab_av1"]),
                dedicated_instance_path_hmac_ids=instance_path_ids,
                source_path_hmac_ids=source_path_ids,
                runtime_compatibility_payload=runtime_payload,
                guided_warm_start_identities=(
                    av1_validation_v4_guided_warm_start_identities()
                ),
                invocations=invocations,
                path_privacy_key_id=path_privacy_key_id,
                tool_version_probe_subprocess_executed=True,
            ),
            rights_attestation=preflight.rights,
            preparation_grant=preflight.grant,
            preparation_claim=claim,
        )
        preparation_bytes = serialize_av1_validation_v4_preparation_record(
            preparation,
            rights_attestation=preflight.rights,
            preparation_grant=preflight.grant,
            preparation_claim=claim,
        )
        stages_completed.append(current_stage)

        current_stage = "publish"
        _exclusive_write(preflight.artifact_paths["preparation"], preparation_bytes)
        created_artifacts.add("preparation_record")
        measurement = build_av1_validation_v4_preparation_success_measurement(
            claim=claim,
            preparation_grant=preflight.grant,
            preparation=preparation,
            started_at=started_at,
            completed_at=_canonical_timestamp(now()),
            probes=probes,
            key_custody=key_custody,
            source_stream_constraints=preflight.stream_constraints,
        )
        _exclusive_write(
            preflight.artifact_paths["measurement"],
            serialize_av1_validation_v4_preparation_measurement(measurement),
        )
        stages_completed.append(current_stage)
        return AV1ValidationV4PreparationOperationResult(
            claim=claim,
            preparation=preparation,
            measurement=measurement,
            artifact_paths=preflight.artifact_paths,
        )
    except Exception as exc:
        if not claim_published:
            raise
        if claim is None or preflight is None:
            raise AV1ValidationV4PreparationOperationError(
                "AV1 v4 preparation claim state is inconsistent"
            ) from exc
        rollback = _rollback({
            name: path
            for name, path in {
                "path_privacy_key": preflight.artifact_paths["key"],
                "effective_config_snapshot": preflight.artifact_paths["config"],
                "preparation_record": preflight.artifact_paths["preparation"],
            }.items()
            if name in created_artifacts
        })
        failure_measurement = build_av1_validation_v4_preparation_failure_measurement(
            claim=claim,
            preparation_grant=preflight.grant,
            started_at=started_at,
            completed_at=_canonical_timestamp(now()),
            probes=probes,
            stages_completed=stages_completed,
            failure_stage=current_stage,
            reason_code="operation_stage_failed",
            error_class=type(exc).__name__,
            message_sha256=_sha256_bytes(str(exc).encode()),
            rollback={
                "claim_retained": True,
                "grant_consumed": True,
                "path_privacy_key_id": path_privacy_key_id,
                "preparation_record_retained": (
                    "preparation_record" in rollback.retained_artifacts
                ),
                "removed_artifacts": list(rollback.removed_artifacts),
                "retained_artifacts": list(rollback.retained_artifacts),
            },
        )
        try:
            _exclusive_write(
                preflight.artifact_paths["measurement"],
                serialize_av1_validation_v4_preparation_measurement(
                    failure_measurement
                ),
            )
        except Exception as measurement_exc:
            raise AV1ValidationV4PreparationOperationError(
                "AV1 v4 preparation failed after consuming the grant; "
                "failure measurement publication also failed"
            ) from measurement_exc
        raise AV1ValidationV4PreparationOperationError(
            "AV1 v4 preparation failed after consuming the grant"
        ) from exc


def _preflight(
    inputs: AV1ValidationV4PreparationOperationInputs,
    *,
    as_of: str,
) -> _PreflightResult:
    if not hasattr(os, "O_NOFOLLOW"):
        raise AV1ValidationV4PreparationOperationError(
            "preparation requires O_NOFOLLOW support"
        )
    if not inputs.workspace.is_absolute():
        raise AV1ValidationV4PreparationOperationError(
            "preparation workspace must be absolute"
        )
    try:
        workspace_metadata = inputs.workspace.lstat()
    except OSError as exc:
        raise AV1ValidationV4PreparationOperationError(
            "preparation workspace is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
    ):
        raise AV1ValidationV4PreparationOperationError(
            "preparation workspace must be a canonical real directory"
        )
    if set(inputs.source_paths) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1ValidationV4PreparationOperationError(
            "source path set does not match the frozen manifest"
        )
    if set(inputs.dedicated_instance_paths) != set(
        AV1_VALIDATION_V4_INSTANCE_PATH_ROLES
    ):
        raise AV1ValidationV4PreparationOperationError(
            "dedicated instance path set is invalid"
        )
    for path in (*inputs.source_paths.values(), *inputs.dedicated_instance_paths.values()):
        _assert_canonical_absolute_path(path)
    manifest = load_av1_validation_manifest_v4(inputs.manifest_path)
    rights = load_av1_validation_v4_rights_attestation(inputs.rights_attestation_path)
    grant = load_av1_validation_v4_preparation_grant(inputs.preparation_grant_path)
    assert_av1_validation_v4_preparation_grant_active(grant, as_of=as_of)
    repository = object_dict(grant.get("repository"))
    if repository != {
        "commit": inputs.repository_commit,
        "tree": inputs.repository_tree,
    }:
        raise AV1ValidationV4PreparationOperationError(
            "preparation grant repository binding does not match"
        )
    if (
        manifest.get("manifest_id") != AV1_VALIDATION_V4_MANIFEST_ID
        or manifest.get("payload_sha256") != AV1_VALIDATION_V4_PAYLOAD_SHA256
    ):
        raise AV1ValidationV4PreparationOperationError(
            "preparation manifest binding does not match"
        )
    search_kwargs = _source_search_kwargs(manifest)
    stream_constraints = _source_stream_constraints(manifest)
    _validate_manifest_traversals(manifest)
    _assert_public_platform_fields(inputs)
    tool_paths = {
        "ffmpeg": _regular_tool_path(inputs.ffmpeg_path),
        "ffprobe": _regular_tool_path(inputs.ffprobe_path),
        "ab_av1": _regular_tool_path(inputs.ab_av1_path),
    }
    tool_signatures = {
        name: _tool_signature(path) for name, path in tool_paths.items()
    }
    consumption_registry = Path(str(grant["consumption_registry"]))
    _assert_private_directory(consumption_registry)
    repository_root = _repository_root(inputs.manifest_path)
    _assert_runtime_paths_outside_repository(
        inputs,
        consumption_registry=consumption_registry,
        repository_root=repository_root,
    )
    artifact_paths = {
        name: inputs.workspace / relative
        for name, relative in _ARTIFACT_RELATIVE_PATHS.items()
    }
    artifact_paths["claim"] = consumption_registry / f"{grant['grant_id']}.json"
    for path in artifact_paths.values():
        _ensure_private_directory(path.parent)
        if path.exists() or path.is_symlink():
            raise AV1ValidationV4PreparationOperationError(
                f"preparation artifact already exists: {path.name}"
            )
    return _PreflightResult(
        manifest=manifest,
        rights=rights,
        grant=grant,
        tool_paths=tool_paths,
        tool_signatures=tool_signatures,
        artifact_paths=artifact_paths,
        search_kwargs=search_kwargs,
        stream_constraints=stream_constraints,
    )


def _derive_invocations(
    inputs: AV1ValidationV4PreparationOperationInputs,
    manifest: Mapping[str, Any],
    source_path_ids: Mapping[str, str],
    stream_constraints: Mapping[str, Mapping[str, Any]],
    search_kwargs: Mapping[str, Mapping[str, Any]],
) -> tuple[AV1ValidationV4InvocationIdentity, ...]:
    invocation_policy = object_dict(manifest.get("qualification_invocation"))
    video_policy = object_dict(invocation_policy.get("video_policy"))
    warm_starts = av1_validation_v4_guided_warm_start_identities()
    traversals = [
        object_dict(item)
        for item in object_list(
            object_dict(manifest.get("qualification_matrix")).get("traversals")
        )
    ]
    result: list[AV1ValidationV4InvocationIdentity] = []
    for traversal in traversals:
        asset_id = str(traversal.get("asset_id") or "")
        configuration = str(traversal.get("configuration") or "")
        if configuration not in AV1_VALIDATION_V4_CONFIGURATIONS:
            raise AV1ValidationV4PreparationOperationError(
                "manifest traversal configuration is invalid"
            )
        mode = (
            "baseline"
            if configuration == "balanced_full_search_baseline"
            else "guided"
        )
        warm_start = (
            None
            if mode == "baseline"
            else QualitySearchWarmStart(**warm_starts[asset_id])
        )
        source_path = inputs.source_paths[asset_id]
        extra_kwargs = object_dict(search_kwargs[asset_id])
        constraints = object_dict(stream_constraints[asset_id])
        base_config_sha256 = "sha256:" + stable_json_hash({
            "source_path": str(source_path),
            "video_policy": video_policy,
            "extra_search_kwargs": extra_kwargs,
            "video_stream_index": constraints["video_stream_index"],
            "excluded_stream_indexes": constraints["excluded_stream_indexes"],
        })
        result.append(AV1ValidationV4InvocationIdentity(
            ordinal=int(traversal["ordinal"]),
            asset_id=asset_id,
            configuration=configuration,
            source_path_hmac_id=source_path_ids[asset_id],
            invocation_sha256=(
                av1_validation_v4_qualification_search_invocation_sha256(
                    source_path=source_path,
                    video_policy=video_policy,
                    mode=mode,
                    warm_start=warm_start,
                    extra_search_kwargs=extra_kwargs,
                )
            ),
            base_config_sha256=base_config_sha256,
            video_stream_index=int(constraints["video_stream_index"]),
            excluded_stream_indexes=tuple(
                int(value) for value in constraints["excluded_stream_indexes"]
            ),
        ))
    return tuple(result)


def _source_search_kwargs(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in object_list(manifest.get("sources")):
        source_payload = object_dict(source)
        asset_id = str(source_payload.get("asset_id") or "")
        video_streams = [
            object_dict(stream)
            for stream in object_list(source_payload.get("observed_streams"))
            if object_dict(stream).get("codec_type") == "video"
        ]
        if len(video_streams) != 1:
            raise AV1ValidationV4PreparationOperationError(
                "manifest source must contain exactly one observed video stream"
            )
        video = video_streams[0]
        result[asset_id] = {
            "height": video.get("height"),
            "source_codec": video.get("codec_name"),
            "width": video.get("width"),
        }
    if set(result) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1ValidationV4PreparationOperationError(
            "manifest source search kwargs are incomplete"
        )
    return result


def _source_stream_constraints(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in object_list(manifest.get("sources")):
        source_payload = object_dict(source)
        asset_id = str(source_payload.get("asset_id") or "")
        streams = [
            object_dict(stream)
            for stream in object_list(source_payload.get("observed_streams"))
        ]
        try:
            video_indexes = [
                int(stream["index"])
                for stream in streams
                if stream.get("codec_type") == "video"
            ]
            excluded_indexes = [
                int(stream["index"])
                for stream in streams
                if int(stream["index"]) not in video_indexes
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise AV1ValidationV4PreparationOperationError(
                "manifest source stream indexes are invalid"
            ) from exc
        if len(video_indexes) != 1:
            raise AV1ValidationV4PreparationOperationError(
                "manifest source stream selection is ambiguous"
            )
        constraint = {
            "video_stream_index": video_indexes[0],
            "excluded_stream_indexes": excluded_indexes,
        }
        expected = av1_validation_v4_source_stream_selection_payload().get(asset_id)
        if constraint != expected:
            raise AV1ValidationV4PreparationOperationError(
                "manifest source stream selection is not frozen"
            )
        result[asset_id] = constraint
    if set(result) != set(AV1_VALIDATION_V4_SOURCE_IDS):
        raise AV1ValidationV4PreparationOperationError(
            "manifest source stream constraints are incomplete"
        )
    return result


def _validate_manifest_traversals(manifest: Mapping[str, Any]) -> None:
    traversals = [
        object_dict(item)
        for item in object_list(
            object_dict(manifest.get("qualification_matrix")).get("traversals")
        )
    ]
    if len(traversals) != 8:
        raise AV1ValidationV4PreparationOperationError(
            "manifest traversal set is incomplete"
        )
    for ordinal, traversal in enumerate(traversals, start=1):
        if (
            traversal.get("ordinal") != ordinal
            or traversal.get("asset_id") not in AV1_VALIDATION_V4_SOURCE_IDS
            or traversal.get("configuration") not in AV1_VALIDATION_V4_CONFIGURATIONS
        ):
            raise AV1ValidationV4PreparationOperationError(
                "manifest traversal binding is invalid"
            )


def _assert_public_platform_fields(
    inputs: AV1ValidationV4PreparationOperationInputs,
) -> None:
    values = (
        inputs.operating_system,
        inputs.operating_system_version,
        inputs.architecture,
        inputs.python_version,
    )
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or av1_validation_v4_contains_private_text(value)
        for value in values
    ):
        raise AV1ValidationV4PreparationOperationError(
            "runtime platform fields are invalid"
        )


def _probe_tool(tool_name: str, path: Path, argv: Sequence[str]) -> Mapping[str, Any]:
    started = time.monotonic()
    result = subprocess.run(
        [str(path), *argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={"LANG": "C", "LC_ALL": "C"},
    )
    elapsed_ms = max(0, round((time.monotonic() - started) * 1000))
    output = result.stdout or result.stderr
    encoded = output.encode(errors="replace")
    if len(encoded) > 65_536:
        raise AV1ValidationV4PreparationOperationError(
            f"{tool_name} version probe output exceeded the limit"
        )
    first_line = output.splitlines()[0].strip() if output.splitlines() else ""
    if result.returncode != 0 or not first_line:
        raise AV1ValidationV4PreparationOperationError(
            f"{tool_name} version probe failed"
        )
    return {
        "tool": tool_name,
        "argv": list(argv),
        "exit_code": result.returncode,
        "duration_ms": elapsed_ms,
        "stdout_first_line": first_line,
        "truncated": False,
    }


def _tool_identity(payload: Mapping[str, str]) -> AV1ValidationV4ToolIdentity:
    return AV1ValidationV4ToolIdentity(
        version=payload["version"],
        binary_sha256=payload["binary_sha256"],
    )


def _regular_tool_path(path: Path) -> Path:
    if not path.is_absolute():
        raise AV1ValidationV4PreparationOperationError(
            "tool paths must be absolute"
        )
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise AV1ValidationV4PreparationOperationError(
            "tool path is unavailable"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AV1ValidationV4PreparationOperationError(
            "tool path must resolve to a regular file"
        )
    return resolved


def _tool_signature(path: Path) -> tuple[int, int, int, int, int]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_canonical_absolute_path(path: Path) -> None:
    value = str(path)
    pure = PurePosixPath(value)
    if (
        not path.is_absolute()
        or value == "/"
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or "//" in value
        or value.endswith("/")
        or ".." in pure.parts
        or pure.as_posix() != value
    ):
        raise AV1ValidationV4PreparationOperationError(
            "operation paths must be canonical absolute POSIX paths"
        )


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_private_directory(path)


def _assert_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AV1ValidationV4PreparationOperationError(
            "owner-only artifact directory is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AV1ValidationV4PreparationOperationError(
            "artifact parent must be an owner-only real directory"
        )


def _repository_root(manifest_path: Path) -> Path:
    try:
        resolved = manifest_path.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV4PreparationOperationError(
            "manifest path is unavailable"
        ) from exc
    expected = Path("docs/validation/av1-cold-start-preregistration-v4.json")
    if Path(*resolved.parts[-3:]) != expected:
        raise AV1ValidationV4PreparationOperationError(
            "manifest path is outside the bound repository layout"
        )
    return resolved.parents[2]


def _assert_runtime_paths_outside_repository(
    inputs: AV1ValidationV4PreparationOperationInputs,
    *,
    consumption_registry: Path,
    repository_root: Path,
) -> None:
    existing_paths = (inputs.workspace.resolve(), consumption_registry.resolve())
    if any(
        path.is_relative_to(repository_root)
        or repository_root.is_relative_to(path)
        for path in existing_paths
    ):
        raise AV1ValidationV4PreparationOperationError(
            "runtime artifacts must remain outside the repository"
        )
    repository_text = str(repository_root)
    for path in (*inputs.source_paths.values(), *inputs.dedicated_instance_paths.values()):
        value = str(path)
        if value == repository_text or value.startswith(repository_text + "/"):
            raise AV1ValidationV4PreparationOperationError(
                "runtime media and instance paths must remain outside the repository"
            )


def _exclusive_write(path: Path, data: bytes) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(path.parent, directory_flags)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        except FileExistsError as exc:
            raise AV1ValidationV4PreparationOperationError(
                f"preparation artifact already exists: {path.name}"
            ) from exc
        created = True
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(parent_descriptor)
        except Exception:
            if created:
                try:
                    os.unlink(path.name, dir_fd=parent_descriptor)
                    os.fsync(parent_descriptor)
                except FileNotFoundError:
                    pass
            raise
    finally:
        os.close(parent_descriptor)


@contextmanager
def _workspace_lock(workspace: Path) -> Iterator[None]:
    if not workspace.is_absolute():
        raise AV1ValidationV4PreparationOperationError(
            "preparation workspace must be absolute"
        )
    _assert_private_directory(workspace)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    workspace_descriptor = os.open(workspace, directory_flags)
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_descriptor = os.open(
            ".av1-v4-preparation.lock",
            lock_flags,
            0o600,
            dir_fd=workspace_descriptor,
        )
        try:
            lock_metadata = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or stat.S_IMODE(lock_metadata.st_mode) != 0o600
                or lock_metadata.st_nlink != 1
            ):
                raise AV1ValidationV4PreparationOperationError(
                    "preparation workspace lock is invalid"
                )
            os.fsync(workspace_descriptor)
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(lock_descriptor)
    finally:
        os.close(workspace_descriptor)


def _key_custody(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_size != 32
    ):
        raise AV1ValidationV4PreparationOperationError(
            "path privacy key custody verification failed"
        )
    return {
        "measured": {
            "bytes": 32,
            "hard_link_count": 1,
            "mode": "0600",
            "regular_file": True,
        },
        "method": {
            "created_exclusive": True,
            "no_follow": True,
        },
        "policy": {
            "material_recorded": False,
            "persistence": "machine_local_only",
        },
    }


def _rollback(paths: Mapping[str, Path]) -> _RollbackResult:
    removed: list[str] = []
    retained: list[str] = []
    for name, path in paths.items():
        try:
            _unlink_artifact(path)
        except FileNotFoundError:
            continue
        except OSError:
            retained.append(name)
            continue
        removed.append(name)
    return _RollbackResult(
        removed_artifacts=tuple(removed),
        retained_artifacts=tuple(retained),
    )


def _unlink_artifact(path: Path) -> None:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(path.parent, directory_flags)
    try:
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sha256_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise AV1ValidationV4PreparationOperationError(
            "operation clock must return timezone-aware timestamps"
        )
    return value.astimezone(UTC).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
