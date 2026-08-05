from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, Mapping

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3
from mediaforce.tuning.av1_validation_v3_qualification import AV1ValidationV3QualificationPlan
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    AV1ValidationV3Tier1CompatAuthorizationError,
    AV1ValidationV3Tier1CompatGrant,
    AV1ValidationV3Tier1CompatRequest,
    assert_av1_validation_v3_tier1_compat_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_coverage import (
    AV1_VALIDATION_V3_TIER1_FIXTURE_IDS,
)
from mediaforce.tuning.av1_validation_v3_tier1_executor import (
    AV1_VALIDATION_V3_TIER1_MATRIX_SHA256,
    AV1ValidationV3Tier1CommandExecutor,
)


AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256 = (
    "sha256:2e6f515fa79224942805851ec0ed62b4a3d9db33931b37200fa9f27e514a36f1"
)
AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_compat_probe_matrix"
)
AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_VERSION = 1
AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS = (
    "compat_ffv1_nut_baseline",
    "compat_ffv1_nut_explicit",
    "compat_ffv1_matroska_explicit",
)
AV1_VALIDATION_V3_TIER1_COMPAT_SURFACE_IDS = (
    "compat_probe_stream",
    "compat_probe_frame",
)
AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS = tuple(
    f"{representation_id}:{surface_id}"
    for representation_id in AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS
    for surface_id in AV1_VALIDATION_V3_TIER1_COMPAT_SURFACE_IDS
)

if AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256 == AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
    raise RuntimeError("AV1 v3 compatibility-probe matrix must be distinct from Tier 1")
if set(AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS) & set(AV1_VALIDATION_V3_TIER1_FIXTURE_IDS):
    raise RuntimeError("AV1 v3 compatibility-probe variants must be disjoint from Tier 1")


class AV1ValidationV3Tier1CompatProbeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatExecutionContext:
    protocol: AV1ValidationProtocolV3
    qualification_plan: AV1ValidationV3QualificationPlan
    request: AV1ValidationV3Tier1CompatRequest
    grant: AV1ValidationV3Tier1CompatGrant
    as_of: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatSurfacePlan:
    surface_id: str
    collection: str
    probe_args: tuple[str, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatRepresentationPlan:
    representation_id: str
    output_path: Path
    generate_args: tuple[str, ...]
    surfaces: tuple[AV1ValidationV3Tier1CompatSurfacePlan, ...]
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatVariantOutcome:
    variant_id: str
    representation_id: str
    surface_id: str
    command_plan_sha256: str
    observation: Mapping[str, object]
    matches_expected_color_description: bool
    failures: tuple[str, ...]


def load_av1_validation_v3_tier1_compat_probe_matrix(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        payload = object_dict(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe matrix is invalid"
        ) from exc
    _assert_frozen_matrix(payload)
    if raw != canonical_json_bytes(payload) + b"\n":
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe matrix bytes are not canonical"
        )
    return payload


def build_av1_validation_v3_tier1_compat_probe_plans(
    matrix: Mapping[str, Any],
    *,
    output_directory: Path,
    repository_root: Path,
) -> tuple[AV1ValidationV3Tier1CompatRepresentationPlan, ...]:
    _assert_frozen_matrix(matrix)
    output_root = _validated_output_root(output_directory, repository_root=repository_root)
    graph = str(matrix.get("lavfi_graph") or "")
    frame_limit = object_dict(matrix.get("frame_limit"))
    surface_values = object_list(matrix.get("probe_surfaces"))
    plans: list[AV1ValidationV3Tier1CompatRepresentationPlan] = []
    for raw_representation in object_list(matrix.get("representations")):
        representation = object_dict(raw_representation)
        representation_id = str(representation.get("representation_id") or "")
        output_path = output_root / f"{representation_id}{representation.get('output_suffix') or ''}"
        generate_args = (
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            graph,
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
        surfaces: list[AV1ValidationV3Tier1CompatSurfacePlan] = []
        for raw_surface in surface_values:
            surface = object_dict(raw_surface)
            surface_id = str(surface.get("surface_id") or "")
            probe_args = tuple(
                str(item).replace("{variant_path}", str(output_path))
                for item in object_list(surface.get("command"))
            )
            semantic = {
                "representation_id": representation_id,
                "surface_id": surface_id,
                "collection": str(surface.get("collection") or ""),
                "probe_args": list(probe_args),
            }
            surfaces.append(
                AV1ValidationV3Tier1CompatSurfacePlan(
                    surface_id=surface_id,
                    collection=str(surface.get("collection") or ""),
                    probe_args=probe_args,
                    payload_sha256=f"sha256:{stable_json_hash(semantic)}",
                )
            )
        semantic = {
            "representation_id": representation_id,
            "generate_args": list(generate_args),
            "surfaces": [item.payload_sha256 for item in surfaces],
        }
        if generate_args[-1] != str(output_path):
            raise AV1ValidationV3Tier1CompatProbeError(
                "AV1 v3 compatibility-probe output path is not command-final"
            )
        plans.append(
            AV1ValidationV3Tier1CompatRepresentationPlan(
                representation_id=representation_id,
                output_path=output_path,
                generate_args=generate_args,
                surfaces=tuple(surfaces),
                payload_sha256=f"sha256:{stable_json_hash(semantic)}",
            )
        )
    if tuple(item.representation_id for item in plans) != AV1_VALIDATION_V3_TIER1_COMPAT_REPRESENTATION_IDS:
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe representations are invalid"
        )
    if any(
        tuple(surface.surface_id for surface in plan.surfaces)
        != AV1_VALIDATION_V3_TIER1_COMPAT_SURFACE_IDS
        for plan in plans
    ):
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe surfaces are invalid"
        )
    return tuple(plans)


def execute_av1_validation_v3_tier1_compat_representation(
    plan: AV1ValidationV3Tier1CompatRepresentationPlan,
    *,
    matrix: Mapping[str, Any],
    context: AV1ValidationV3Tier1CompatExecutionContext,
    executor: AV1ValidationV3Tier1CommandExecutor,
    clock: Callable[[], str] | None = None,
) -> tuple[AV1ValidationV3Tier1CompatVariantOutcome, ...]:
    now = clock or (lambda: context.as_of)
    _assert_execution_authorized(replace(context, as_of=now()))
    _assert_output_path_available(plan.output_path)
    generation = executor.run(plan.generate_args)
    if generation.returncode != 0 or not plan.output_path.is_file() or plan.output_path.is_symlink():
        return tuple(
            _failed_outcome(plan, surface, "generation_failed") for surface in plan.surfaces
        )
    expected = object_dict(matrix.get("expected_color_description"))
    outcomes: list[AV1ValidationV3Tier1CompatVariantOutcome] = []
    for surface in plan.surfaces:
        _assert_execution_authorized(replace(context, as_of=now()))
        result = executor.run(surface.probe_args)
        if result.returncode != 0:
            outcomes.append(_failed_outcome(plan, surface, "probe_failed"))
            continue
        try:
            payload = object_dict(json.loads(result.stdout.decode("utf-8")))
            observations = object_list(payload.get(surface.collection))
            if len(observations) != 1:
                raise ValueError("invalid observation count")
            raw_observation = object_dict(observations[0])
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            outcomes.append(_failed_outcome(plan, surface, "probe_output_invalid"))
            continue
        observation = {
            "width": _safe_int(raw_observation.get("width")),
            "height": _safe_int(raw_observation.get("height")),
            "pix_fmt": str(raw_observation.get("pix_fmt") or ""),
            "color_primaries": str(raw_observation.get("color_primaries") or ""),
            "color_transfer": str(raw_observation.get("color_transfer") or ""),
            "color_space": str(raw_observation.get("color_space") or ""),
            "color_range": _normalize_color_range(
                str(raw_observation.get("color_range") or "")
            ),
        }
        failures = tuple(
            sorted(
                f"{field}_mismatch"
                for field, wanted in expected.items()
                if observation.get(field) != wanted
            )
        )
        outcomes.append(
            AV1ValidationV3Tier1CompatVariantOutcome(
                variant_id=f"{plan.representation_id}:{surface.surface_id}",
                representation_id=plan.representation_id,
                surface_id=surface.surface_id,
                command_plan_sha256=surface.payload_sha256,
                observation=observation,
                matches_expected_color_description=not failures,
                failures=failures,
            )
        )
    return tuple(outcomes)


def _assert_frozen_matrix(matrix: Mapping[str, Any]) -> None:
    if (
        matrix.get("schema") != AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SCHEMA
        or matrix.get("schema_version") != AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_VERSION
        or matrix.get("probe_scope") != "diagnostic_public_synthetic_only"
        or matrix.get("diagnostic_only") is not True
        or matrix.get("evidence_eligible") is not False
        or object_dict(matrix.get("frame_limit")) != {"method": "frames_v", "value": 2}
        or f"sha256:{stable_json_hash(matrix)}"
        != AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256
    ):
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe matrix identity is invalid"
        )


def _validated_output_root(output_directory: Path, *, repository_root: Path) -> Path:
    if not output_directory.is_absolute() or output_directory.is_symlink():
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe output root is invalid"
        )
    try:
        output_root = output_directory.resolve(strict=True)
        repository = repository_root.resolve(strict=True)
    except OSError as exc:
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe paths are invalid"
        ) from exc
    if (
        not output_root.is_dir()
        or output_root == repository
        or output_root.is_relative_to(repository)
        or repository.is_relative_to(output_root)
    ):
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe outputs must remain outside the repository"
        )
    return output_root


def _assert_execution_authorized(context: AV1ValidationV3Tier1CompatExecutionContext) -> None:
    try:
        assert_av1_validation_v3_tier1_compat_grant_active(
            context.protocol,
            context.qualification_plan,
            context.request,
            context.grant,
            as_of=context.as_of,
        )
    except AV1ValidationV3Tier1CompatAuthorizationError as exc:
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe command execution is not authorized"
        ) from exc


def _assert_output_path_available(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise AV1ValidationV3Tier1CompatProbeError(
            "AV1 v3 Tier 1 compatibility-probe output path already exists"
        )


def _failed_outcome(
    plan: AV1ValidationV3Tier1CompatRepresentationPlan,
    surface: AV1ValidationV3Tier1CompatSurfacePlan,
    reason: str,
) -> AV1ValidationV3Tier1CompatVariantOutcome:
    return AV1ValidationV3Tier1CompatVariantOutcome(
        variant_id=f"{plan.representation_id}:{surface.surface_id}",
        representation_id=plan.representation_id,
        surface_id=surface.surface_id,
        command_plan_sha256=surface.payload_sha256,
        observation={},
        matches_expected_color_description=False,
        failures=(reason,),
    )


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _normalize_color_range(value: str) -> str:
    if value in {"tv", "mpeg"}:
        return "limited"
    if value in {"pc", "jpeg"}:
        return "full"
    return value
