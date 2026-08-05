from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Mapping

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_qualification import AV1ValidationV3QualificationPlan
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1CommandExecutor,
)
from mediaforce.tuning.av1_validation_v3_tier1_residual_authorization import (
    AV1ValidationV3Tier1ResidualAuthorizationError,
    AV1ValidationV3Tier1ResidualGrant,
    AV1ValidationV3Tier1ResidualRequest,
    assert_av1_validation_v3_tier1_residual_grant_active,
)


AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256 = (
    "sha256:c0dd5c4811c13a12e312839857a0078e64951601f74a2062218d13e0d43819a6"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_residual_probe_matrix"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_VERSION = 1
AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS = (
    "tier1_flat_field",
    "tier1_high_motion",
    "tier1_high_detail_noise",
    "tier1_scene_change",
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_REPRESENTATION_ID = (
    "residual_ffv1_matroska_explicit"
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS = (
    "residual_probe_stream",
    "residual_probe_frame",
    "residual_probe_hash",
)
AV1_VALIDATION_V3_TIER1_RESIDUAL_VARIANT_IDS = tuple(
    f"{fixture_id}:{surface_id}"
    for fixture_id in AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS
    for surface_id in AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS
)

_EXPECTED_FRAME_COUNT = 288
AV1_VALIDATION_V3_TIER1_RESIDUAL_EXPECTED_DECODED_BYTE_COUNT = (
    1280 * 720 * 3 * _EXPECTED_FRAME_COUNT
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EXPECTED_OUTPUT_COLOR_ARGS = (
    "-color_primaries",
    "bt709",
    "-color_trc",
    "bt709",
    "-colorspace",
    "bt709",
    "-color_range",
    "tv",
)
_EXPECTED_FRAME_SPEC = {
    "color_matrix": "bt709",
    "color_primaries": "bt709",
    "color_range": "limited",
    "color_transfer": "bt709",
    "fps": 24,
    "frame_count": _EXPECTED_FRAME_COUNT,
    "height": 720,
    "pixel_format": "yuv420p10le",
    "seconds": 12,
    "width": 1280,
}

if AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256 == AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
    raise RuntimeError("AV1 v3 residual-probe matrix must be distinct from Tier 1")


class AV1ValidationV3Tier1ResidualProbeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualExecutionContext:
    protocol: AV1ValidationProtocolV3
    qualification_plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier1ResidualRequest
    grant: AV1ValidationV3Tier1ResidualGrant
    as_of: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualSurfacePlan:
    surface_id: str
    collection: str
    command_args: tuple[str, ...]
    streaming: bool
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualFixturePlan:
    fixture_id: str
    representation_id: str
    output_path: Path
    generate_args: tuple[str, ...]
    surfaces: tuple[AV1ValidationV3Tier1ResidualSurfacePlan, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1ResidualVariantOutcome:
    variant_id: str
    fixture_id: str
    representation_id: str
    surface_id: str
    command_plan_sha256: str
    observation: Mapping[str, object]
    content_sha256: str
    content_byte_count: int
    passed: bool
    failures: tuple[str, ...]


def load_av1_validation_v3_tier1_residual_probe_matrix(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        payload = object_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe matrix is invalid"
        ) from exc
    _assert_frozen_matrix(payload)
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe matrix bytes are not canonical"
        )
    return payload


def build_av1_validation_v3_tier1_residual_probe_plans(
    matrix: Mapping[str, Any],
    *,
    output_directory: Path,
    repository_root: Path,
) -> tuple[AV1ValidationV3Tier1ResidualFixturePlan, ...]:
    _assert_frozen_matrix(matrix)
    output_root = _validated_output_root(output_directory, repository_root=repository_root)
    frame_limit = object_dict(matrix.get("frame_limit"))
    representation = object_dict(matrix.get("representation"))
    content_hash = object_dict(matrix.get("content_hash"))
    surface_values = object_list(matrix.get("probe_surfaces"))
    plans: list[AV1ValidationV3Tier1ResidualFixturePlan] = []
    for raw_fixture in object_list(matrix.get("fixtures")):
        fixture = object_dict(raw_fixture)
        fixture_id = str(fixture.get("fixture_id") or "")
        output_path = output_root / f"{fixture_id}{representation.get('output_suffix') or ''}"
        generate_args = (
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-f",
            "lavfi",
            "-i",
            str(fixture.get("lavfi_graph") or ""),
            "-frames:v",
            str(frame_limit.get("value")),
            "-an",
            "-c:v",
            str(representation.get("codec") or ""),
            *(str(item) for item in object_list(representation.get("output_color_args"))),
            "-f",
            str(representation.get("container") or ""),
            str(output_path),
        )
        surfaces: list[AV1ValidationV3Tier1ResidualSurfacePlan] = []
        for raw_surface in surface_values:
            surface = object_dict(raw_surface)
            surfaces.append(_surface_plan(surface, output_path, streaming=False))
        surfaces.append(_surface_plan(content_hash, output_path, streaming=True))
        semantic = {
            "fixture_id": fixture_id,
            "representation_id": AV1_VALIDATION_V3_TIER1_RESIDUAL_REPRESENTATION_ID,
            "generate_args": list(generate_args),
            "surfaces": [item.payload_sha256 for item in surfaces],
        }
        if generate_args[-1] != str(output_path):
            raise AV1ValidationV3Tier1ResidualProbeError(
                "AV1 v3 residual-probe output path is not command-final"
            )
        plans.append(
            AV1ValidationV3Tier1ResidualFixturePlan(
                fixture_id=fixture_id,
                representation_id=AV1_VALIDATION_V3_TIER1_RESIDUAL_REPRESENTATION_ID,
                output_path=output_path,
                generate_args=generate_args,
                surfaces=tuple(surfaces),
                payload_sha256=f"sha256:{stable_json_hash(semantic)}",
            )
        )
    if tuple(item.fixture_id for item in plans) != AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS:
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe fixtures are invalid"
        )
    if any(
        tuple(surface.surface_id for surface in plan.surfaces)
        != AV1_VALIDATION_V3_TIER1_RESIDUAL_SURFACE_IDS
        for plan in plans
    ):
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe surfaces are invalid"
        )
    return tuple(plans)


def execute_av1_validation_v3_tier1_residual_fixture(
    plan: AV1ValidationV3Tier1ResidualFixturePlan,
    *,
    matrix: Mapping[str, Any],
    context: AV1ValidationV3Tier1ResidualExecutionContext,
    executor: AV1ValidationV3Tier1CommandExecutor,
    clock: Callable[[], str] | None = None,
) -> tuple[AV1ValidationV3Tier1ResidualVariantOutcome, ...]:
    now = clock or (lambda: context.as_of)
    _assert_frozen_matrix(matrix)
    _assert_execution_authorized(replace(context, as_of=now()))
    _assert_output_path_available(plan.output_path)
    generation = executor.run(plan.generate_args)
    if generation.returncode != 0 or not plan.output_path.is_file() or plan.output_path.is_symlink():
        return tuple(_failed_outcome(plan, surface, "generation_failed") for surface in plan.surfaces)
    outcomes: list[AV1ValidationV3Tier1ResidualVariantOutcome] = []
    for surface in plan.surfaces:
        _assert_execution_authorized(replace(context, as_of=now()))
        if surface.streaming:
            outcomes.append(_hash_outcome(plan, surface, executor))
        else:
            outcomes.append(_probe_outcome(plan, surface, matrix, executor))
    return tuple(outcomes)


def _surface_plan(
    surface: Mapping[str, Any],
    output_path: Path,
    *,
    streaming: bool,
) -> AV1ValidationV3Tier1ResidualSurfacePlan:
    surface_id = "residual_probe_hash" if streaming else str(surface.get("surface_id") or "")
    command_args = tuple(
        str(item).replace("{fixture_path}", str(output_path))
        for item in object_list(surface.get("command"))
    )
    semantic = {
        "surface_id": surface_id,
        "collection": str(surface.get("collection") or ""),
        "command_args": list(command_args),
        "streaming": streaming,
    }
    return AV1ValidationV3Tier1ResidualSurfacePlan(
        surface_id=surface_id,
        collection=str(surface.get("collection") or ""),
        command_args=command_args,
        streaming=streaming,
        payload_sha256=f"sha256:{stable_json_hash(semantic)}",
    )


def _probe_outcome(
    plan: AV1ValidationV3Tier1ResidualFixturePlan,
    surface: AV1ValidationV3Tier1ResidualSurfacePlan,
    matrix: Mapping[str, Any],
    executor: AV1ValidationV3Tier1CommandExecutor,
) -> AV1ValidationV3Tier1ResidualVariantOutcome:
    result = executor.run(surface.command_args)
    if result.returncode != 0:
        return _failed_outcome(plan, surface, "probe_failed")
    try:
        payload = object_dict(json.loads(result.stdout.decode("utf-8")))
        observations = object_list(payload.get(surface.collection))
        if len(observations) != 1:
            raise ValueError("invalid observation count")
        raw_observation = object_dict(observations[0])
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return _failed_outcome(plan, surface, "probe_output_invalid")
    expected = object_dict(matrix.get("frame_spec"))
    observation = _observation_for_surface(surface.surface_id, raw_observation)
    failures = _probe_failures(surface.surface_id, observation, expected)
    return _outcome(
        plan,
        surface,
        observation=observation,
        content_sha256="",
        content_byte_count=0,
        failures=failures,
    )


def _hash_outcome(
    plan: AV1ValidationV3Tier1ResidualFixturePlan,
    surface: AV1ValidationV3Tier1ResidualSurfacePlan,
    executor: AV1ValidationV3Tier1CommandExecutor,
) -> AV1ValidationV3Tier1ResidualVariantOutcome:
    result = executor.run_streaming_sha256(surface.command_args)
    failures: list[str] = []
    content_sha256 = result.content_sha256
    if result.returncode != 0:
        failures.append("content_hash_failed")
        content_sha256 = ""
    elif not _SHA256_RE.fullmatch(content_sha256):
        failures.append("content_hash_invalid")
        content_sha256 = ""
    if result.byte_count != AV1_VALIDATION_V3_TIER1_RESIDUAL_EXPECTED_DECODED_BYTE_COUNT:
        failures.append("content_byte_count_mismatch")
    return _outcome(
        plan,
        surface,
        observation={},
        content_sha256=content_sha256,
        content_byte_count=result.byte_count,
        failures=tuple(failures),
    )


def _assert_frozen_matrix(matrix: Mapping[str, Any]) -> None:
    representation = object_dict(matrix.get("representation"))
    informed_by = object_dict(matrix.get("informed_by"))
    if (
        matrix.get("schema") != AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SCHEMA
        or matrix.get("schema_version") != AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_VERSION
        or matrix.get("probe_scope") != "diagnostic_public_synthetic_residual_only"
        or matrix.get("diagnostic_only") is not True
        or matrix.get("evidence_eligible") is not False
        or matrix.get("fixture_scope") != "deterministic_synthetic_public_only"
        or matrix.get("generator_contract") != "mediaforce.synthetic_fixture.v3.residual_preflight"
        or object_dict(matrix.get("frame_limit")) != {"method": "frames_v", "value": _EXPECTED_FRAME_COUNT}
        or object_dict(matrix.get("frame_spec")) != _EXPECTED_FRAME_SPEC
        or representation
        != {
            "codec": "ffv1",
            "container": "matroska",
            "file_hash_authoritative": False,
            "output_color_args": list(_EXPECTED_OUTPUT_COLOR_ARGS),
            "output_suffix": ".mkv",
        }
        or informed_by.get("matrix_v3_sha256") != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256
        or f"sha256:{stable_json_hash(matrix)}" != AV1_VALIDATION_V3_TIER1_RESIDUAL_MATRIX_SHA256
    ):
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe matrix identity is invalid"
        )
    fixtures = tuple(str(object_dict(item).get("fixture_id") or "") for item in object_list(matrix.get("fixtures")))
    if fixtures != AV1_VALIDATION_V3_TIER1_RESIDUAL_FIXTURE_IDS:
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe fixture order is invalid"
        )


def _validated_output_root(output_directory: Path, *, repository_root: Path) -> Path:
    if not output_directory.is_absolute() or output_directory.is_symlink():
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe output root is invalid"
        )
    try:
        output_root = output_directory.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe paths are invalid"
        ) from exc
    if (
        not output_root.is_dir()
        or output_root == repository
        or output_root.is_relative_to(repository)
        or repository.is_relative_to(output_root)
    ):
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe outputs must remain outside the repository"
        )
    return output_root


def _assert_execution_authorized(context: AV1ValidationV3Tier1ResidualExecutionContext) -> None:
    try:
        assert_av1_validation_v3_tier1_residual_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1ResidualAuthorizationError as exc:
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe command execution is not authorized"
        ) from exc


def _assert_output_path_available(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise AV1ValidationV3Tier1ResidualProbeError(
            "AV1 v3 Tier 1 residual-probe output path already exists"
        )


def _observation_for_surface(surface_id: str, raw_observation: Mapping[str, Any]) -> dict[str, object]:
    observation = {
        "width": _safe_int(raw_observation.get("width")),
        "height": _safe_int(raw_observation.get("height")),
        "pix_fmt": str(raw_observation.get("pix_fmt") or ""),
        "color_primaries": str(raw_observation.get("color_primaries") or ""),
        "color_transfer": str(raw_observation.get("color_transfer") or ""),
        "color_space": str(raw_observation.get("color_space") or ""),
        "color_range": _normalize_color_range(str(raw_observation.get("color_range") or "")),
    }
    if surface_id == "residual_probe_stream":
        observation.update({
            "r_frame_rate": str(raw_observation.get("r_frame_rate") or ""),
            "nb_read_frames": _safe_int(raw_observation.get("nb_read_frames")),
        })
    return observation


def _probe_failures(
    surface_id: str,
    observation: Mapping[str, object],
    expected: Mapping[str, Any],
) -> tuple[str, ...]:
    checks = (
        ("width", expected.get("width")),
        ("height", expected.get("height")),
        ("pix_fmt", expected.get("pixel_format")),
        ("color_primaries", expected.get("color_primaries")),
        ("color_transfer", expected.get("color_transfer")),
        ("color_space", expected.get("color_matrix")),
        ("color_range", expected.get("color_range")),
    )
    failures = [f"{field}_mismatch" for field, wanted in checks if observation.get(field) != wanted]
    if surface_id == "residual_probe_stream":
        if observation.get("nb_read_frames") != expected.get("frame_count"):
            failures.append("nb_read_frames_mismatch")
        if not _frame_rate_matches(str(observation.get("r_frame_rate") or ""), int(expected.get("fps") or 0)):
            failures.append("frame_rate_mismatch")
    return tuple(sorted(failures))


def _failed_outcome(
    plan: AV1ValidationV3Tier1ResidualFixturePlan,
    surface: AV1ValidationV3Tier1ResidualSurfacePlan,
    reason: str,
) -> AV1ValidationV3Tier1ResidualVariantOutcome:
    return _outcome(
        plan,
        surface,
        observation={},
        content_sha256="",
        content_byte_count=0,
        failures=(reason,),
    )


def _outcome(
    plan: AV1ValidationV3Tier1ResidualFixturePlan,
    surface: AV1ValidationV3Tier1ResidualSurfacePlan,
    *,
    observation: Mapping[str, object],
    content_sha256: str,
    content_byte_count: int,
    failures: tuple[str, ...],
) -> AV1ValidationV3Tier1ResidualVariantOutcome:
    return AV1ValidationV3Tier1ResidualVariantOutcome(
        variant_id=f"{plan.fixture_id}:{surface.surface_id}",
        fixture_id=plan.fixture_id,
        representation_id=plan.representation_id,
        surface_id=surface.surface_id,
        command_plan_sha256=surface.payload_sha256,
        observation=observation,
        content_sha256=content_sha256,
        content_byte_count=content_byte_count,
        passed=not failures,
        failures=failures,
    )


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
