from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_qualification import AV1ValidationV3QualificationPlan
from mediaforce.tuning.av1_validation_v3_tier1_grant import (
    AV1ValidationV3Tier1ExecutionGrant,
    AV1ValidationV3Tier1GrantError,
    assert_av1_validation_v3_tier1_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_request import (
    AV1ValidationV3Tier1AuthorizationRequest,
)


AV1_VALIDATION_V3_TIER1_MATRIX_SHA256 = (
    "sha256:faa40e0ee1dfd71715440d413b4d8e138266b27202a1e3c995ddffcf1a370572"
)
AV1_VALIDATION_V3_TIER1_MATRIX_SCHEMA = "mediaforce.av1_cold_start_v3_tier1_fixture_matrix"
AV1_VALIDATION_V3_TIER1_MATRIX_VERSION = 2

_EXPECTED_FRAME_COUNT = 288
_EXPECTED_DECODED_BYTE_COUNT = 1280 * 720 * 3 * _EXPECTED_FRAME_COUNT
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class AV1ValidationV3Tier1ExecutorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CommandResult:
    returncode: int
    stdout: bytes
    stderr: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1HashResult:
    returncode: int
    content_sha256: str
    byte_count: int
    stderr: str


class AV1ValidationV3Tier1CommandExecutor(Protocol):
    def run(self, args: Sequence[str]) -> AV1ValidationV3Tier1CommandResult: ...

    def run_sha256(self, args: Sequence[str]) -> AV1ValidationV3Tier1HashResult: ...


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ExecutionContext:
    protocol: AV1ValidationProtocolV3
    qualification_plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier1AuthorizationRequest
    grant: AV1ValidationV3Tier1ExecutionGrant
    as_of: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixturePlan:
    fixture_id: str
    matrix_sha256: str
    output_path: Path
    generate_args: tuple[str, ...]
    probe_args: tuple[str, ...]
    content_hash_args: tuple[str, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixtureOutcome:
    fixture_id: str
    matrix_sha256: str
    request_id: str
    grant_id: str
    repository_commit: str
    toolchain_sha256: str
    command_plan_sha256: str
    content_sha256: str
    content_byte_count: int
    passed: bool
    failures: tuple[str, ...]
    observation: Mapping[str, object]


def load_av1_validation_v3_tier1_fixture_matrix(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        payload = object_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix is invalid") from exc
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix is not canonical")
    _assert_frozen_matrix(payload)
    return payload


def build_av1_validation_v3_tier1_fixture_plans(
    matrix: Mapping[str, Any],
    *,
    output_directory: Path,
    repository_root: Path,
) -> tuple[AV1ValidationV3Tier1FixturePlan, ...]:
    _assert_frozen_matrix(matrix)
    output_root = _validated_output_root(output_directory, repository_root=repository_root)
    frame_limit = object_dict(matrix.get("frame_limit"))
    intermediate = object_dict(matrix.get("intermediate"))
    probe = object_dict(matrix.get("probe"))
    content_hash = object_dict(matrix.get("content_hash"))

    plans: list[AV1ValidationV3Tier1FixturePlan] = []
    for item in object_list(matrix.get("fixtures")):
        fixture = object_dict(item)
        fixture_id = str(fixture.get("fixture_id") or "")
        lavfi_graph = str(fixture.get("lavfi_graph") or "")
        if not fixture_id or not lavfi_graph:
            raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture entry is invalid")
        output_path = output_root / f"{fixture_id}.nut"
        _assert_output_path_available(output_path)
        generate_args = (
            "ffmpeg", "-v", "error", "-n", "-f", "lavfi", "-i", lavfi_graph,
            "-frames:v", str(frame_limit["value"]), "-an", "-c:v",
            str(intermediate.get("codec") or ""), "-f",
            str(intermediate.get("container") or ""), str(output_path),
        )
        probe_args = _substitute_path(probe.get("command"), output_path)
        content_hash_args = _substitute_path(content_hash.get("command"), output_path)
        semantic_payload = {
            "fixture_id": fixture_id,
            "matrix_sha256": AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            "output_path": str(output_path),
            "generate_args": list(generate_args),
            "probe_args": list(probe_args),
            "content_hash_args": list(content_hash_args),
        }
        plans.append(AV1ValidationV3Tier1FixturePlan(
            fixture_id=fixture_id,
            matrix_sha256=AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
            output_path=output_path,
            generate_args=generate_args,
            probe_args=probe_args,
            content_hash_args=content_hash_args,
            payload_sha256=f"sha256:{stable_json_hash(semantic_payload)}",
        ))
    if len(plans) != 4 or len({plan.fixture_id for plan in plans}) != 4:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture plan count is invalid")
    return tuple(plans)


def execute_av1_validation_v3_tier1_fixture(
    fixture_id: str,
    *,
    matrix: Mapping[str, Any],
    output_directory: Path,
    repository_root: Path,
    context: AV1ValidationV3Tier1ExecutionContext,
    executor: AV1ValidationV3Tier1CommandExecutor,
) -> AV1ValidationV3Tier1FixtureOutcome:
    _assert_execution_authorized(context)
    if context.qualification_plan.fixture_matrix_sha256 != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
        raise AV1ValidationV3Tier1ExecutorError(
            "AV1 v3 Tier 1 authorization is not bound to the frozen fixture matrix"
        )
    plans = build_av1_validation_v3_tier1_fixture_plans(
        matrix,
        output_directory=output_directory,
        repository_root=repository_root,
    )
    plan = next((item for item in plans if item.fixture_id == fixture_id), None)
    if plan is None:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture ID is invalid")

    _assert_execution_authorized(context)
    _assert_output_path_available(plan.output_path)
    generation_result = executor.run(plan.generate_args)
    if generation_result.returncode != 0:
        return _failed_outcome(plan, context, "generation_failed")
    if not plan.output_path.is_file() or plan.output_path.is_symlink():
        return _failed_outcome(plan, context, "generated_output_invalid")
    return _verify_fixture(plan, matrix=matrix, context=context, executor=executor)


def _verify_fixture(
    plan: AV1ValidationV3Tier1FixturePlan,
    *,
    matrix: Mapping[str, Any],
    context: AV1ValidationV3Tier1ExecutionContext,
    executor: AV1ValidationV3Tier1CommandExecutor,
) -> AV1ValidationV3Tier1FixtureOutcome:
    _assert_frozen_matrix(matrix)
    _assert_execution_authorized(context)
    if not plan.output_path.is_file() or plan.output_path.is_symlink():
        return _failed_outcome(plan, context, "generated_output_invalid")
    probe_result = executor.run(plan.probe_args)
    if probe_result.returncode != 0:
        return _failed_outcome(plan, context, "probe_failed")
    try:
        streams = object_list(object_dict(json.loads(probe_result.stdout.decode("utf-8"))).get("streams"))
        if len(streams) != 1:
            return _failed_outcome(plan, context, "probe_stream_count_invalid")
        stream = object_dict(streams[0])
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return _failed_outcome(plan, context, "probe_output_invalid")

    expected = object_dict(matrix.get("frame_spec"))
    observation = {
        "width": _safe_int(stream.get("width")),
        "height": _safe_int(stream.get("height")),
        "r_frame_rate": str(stream.get("r_frame_rate") or ""),
        "pix_fmt": str(stream.get("pix_fmt") or ""),
        "color_primaries": str(stream.get("color_primaries") or ""),
        "color_transfer": str(stream.get("color_transfer") or ""),
        "color_space": str(stream.get("color_space") or ""),
        "color_range": _normalize_color_range(str(stream.get("color_range") or "")),
        "nb_read_frames": _safe_int(stream.get("nb_read_frames")),
    }
    failures: list[str] = []
    checks = (
        ("width", expected.get("width")),
        ("height", expected.get("height")),
        ("pix_fmt", expected.get("pixel_format")),
        ("color_primaries", expected.get("color_primaries")),
        ("color_transfer", expected.get("color_transfer")),
        ("color_space", expected.get("color_matrix")),
        ("color_range", expected.get("color_range")),
        ("nb_read_frames", expected.get("frame_count")),
    )
    for field, wanted in checks:
        if observation[field] != wanted:
            failures.append(f"{field}_mismatch")
    if not _frame_rate_matches(str(observation["r_frame_rate"]), int(expected.get("fps") or 0)):
        failures.append("frame_rate_mismatch")

    _assert_execution_authorized(context)
    if not plan.output_path.is_file() or plan.output_path.is_symlink():
        return _failed_outcome(plan, context, "generated_output_invalid")
    hash_result = executor.run_sha256(plan.content_hash_args)
    content_sha256 = hash_result.content_sha256
    if hash_result.returncode != 0:
        failures.append("content_hash_failed")
        content_sha256 = ""
    elif not _SHA256_RE.fullmatch(content_sha256):
        failures.append("content_hash_invalid")
        content_sha256 = ""
    if hash_result.byte_count != _EXPECTED_DECODED_BYTE_COUNT:
        failures.append("content_byte_count_mismatch")
    return _outcome(
        plan,
        context,
        content_sha256=content_sha256,
        content_byte_count=hash_result.byte_count,
        failures=tuple(failures),
        observation=observation,
    )


def _assert_frozen_matrix(matrix: Mapping[str, Any]) -> None:
    if (
        matrix.get("schema") != AV1_VALIDATION_V3_TIER1_MATRIX_SCHEMA
        or matrix.get("schema_version") != AV1_VALIDATION_V3_TIER1_MATRIX_VERSION
        or matrix.get("fixture_scope") != "deterministic_synthetic_only"
        or matrix.get("generator_contract") != "mediaforce.synthetic_fixture.v2"
        or object_dict(matrix.get("frame_limit")) != {"method": "frames_v", "value": 288}
        or object_dict(matrix.get("frame_spec")).get("frame_count") != _EXPECTED_FRAME_COUNT
    ):
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix identity is invalid")
    if f"sha256:{stable_json_hash(matrix)}" != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix digest is invalid")


def _validated_output_root(output_directory: Path, *, repository_root: Path) -> Path:
    if not output_directory.is_absolute() or not repository_root.is_absolute():
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 paths must be absolute")
    if output_directory.is_symlink():
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 output root cannot be a symlink")
    try:
        output_root = output_directory.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 paths are invalid") from exc
    if not output_root.is_dir():
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 output root must be a directory")
    if output_root == repository or output_root.is_relative_to(repository):
        raise AV1ValidationV3Tier1ExecutorError(
            "AV1 v3 Tier 1 outputs must remain outside the repository"
        )
    return output_root


def _assert_output_path_available(output_path: Path) -> None:
    if output_path.exists() or output_path.is_symlink():
        raise AV1ValidationV3Tier1ExecutorError(
            "AV1 v3 Tier 1 output path must not already exist"
        )


def _assert_execution_authorized(context: AV1ValidationV3Tier1ExecutionContext) -> None:
    try:
        assert_av1_validation_v3_tier1_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1GrantError as exc:
        raise AV1ValidationV3Tier1ExecutorError(
            "AV1 v3 Tier 1 command execution is not authorized"
        ) from exc


def _substitute_path(value: object, output_path: Path) -> tuple[str, ...]:
    command = object_list(value)
    if not command or not all(isinstance(part, str) for part in command):
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 command contract is invalid")
    return tuple(str(part).replace("{fixture_path}", str(output_path)) for part in command)


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _frame_rate_matches(value: str, expected: int) -> bool:
    if "/" not in value:
        return value == str(expected)
    numerator, denominator = value.split("/", 1)
    try:
        numerator_value = int(numerator)
        denominator_value = int(denominator)
    except ValueError:
        return False
    return denominator_value > 0 and numerator_value == expected * denominator_value


def _normalize_color_range(value: str) -> str:
    if value in {"tv", "mpeg"}:
        return "limited"
    if value in {"pc", "jpeg"}:
        return "full"
    return value


def _failed_outcome(
    plan: AV1ValidationV3Tier1FixturePlan,
    context: AV1ValidationV3Tier1ExecutionContext,
    reason: str,
) -> AV1ValidationV3Tier1FixtureOutcome:
    return _outcome(
        plan,
        context,
        content_sha256="",
        content_byte_count=0,
        failures=(reason,),
        observation={},
    )


def _outcome(
    plan: AV1ValidationV3Tier1FixturePlan,
    context: AV1ValidationV3Tier1ExecutionContext,
    *,
    content_sha256: str,
    content_byte_count: int,
    failures: tuple[str, ...],
    observation: Mapping[str, object],
) -> AV1ValidationV3Tier1FixtureOutcome:
    return AV1ValidationV3Tier1FixtureOutcome(
        fixture_id=plan.fixture_id,
        matrix_sha256=plan.matrix_sha256,
        request_id=context.request.request_id,
        grant_id=context.grant.grant_id,
        repository_commit=context.qualification_plan.repository_commit,
        toolchain_sha256=context.qualification_plan.toolchain_sha256,
        command_plan_sha256=plan.payload_sha256,
        content_sha256=content_sha256,
        content_byte_count=content_byte_count,
        passed=not failures,
        failures=failures,
        observation=observation,
    )
