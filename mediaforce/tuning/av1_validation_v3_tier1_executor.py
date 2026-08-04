from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list


AV1_VALIDATION_V3_TIER1_MATRIX_SHA256 = (
    "sha256:faa40e0ee1dfd71715440d413b4d8e138266b27202a1e3c995ddffcf1a370572"
)
AV1_VALIDATION_V3_TIER1_MATRIX_SCHEMA = "mediaforce.av1_cold_start_v3_tier1_fixture_matrix"
AV1_VALIDATION_V3_TIER1_MATRIX_VERSION = 2


class AV1ValidationV3Tier1ExecutorError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CommandResult:
    returncode: int
    stdout: bytes
    stderr: str


class AV1ValidationV3Tier1CommandExecutor(Protocol):
    def run(self, args: Sequence[str]) -> AV1ValidationV3Tier1CommandResult: ...


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixturePlan:
    fixture_id: str
    matrix_sha256: str
    output_path: Path
    generate_args: tuple[str, ...]
    probe_args: tuple[str, ...]
    content_hash_args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1FixtureOutcome:
    fixture_id: str
    matrix_sha256: str
    content_sha256: str
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
    if (
        payload.get("schema") != AV1_VALIDATION_V3_TIER1_MATRIX_SCHEMA
        or payload.get("schema_version") != AV1_VALIDATION_V3_TIER1_MATRIX_VERSION
        or payload.get("fixture_scope") != "deterministic_synthetic_only"
        or payload.get("generator_contract") != "mediaforce.synthetic_fixture.v2"
    ):
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix identity is invalid")
    digest = f"sha256:{stable_json_hash(payload)}"
    if digest != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix digest is invalid")
    return payload


def build_av1_validation_v3_tier1_fixture_plans(
    matrix: Mapping[str, Any],
    *,
    output_directory: Path,
    repository_root: Path,
) -> tuple[AV1ValidationV3Tier1FixturePlan, ...]:
    output_root = output_directory.resolve()
    repository = repository_root.resolve()
    try:
        if output_root == repository or output_root.is_relative_to(repository):
            raise AV1ValidationV3Tier1ExecutorError(
                "AV1 v3 Tier 1 outputs must remain outside the repository"
            )
    except OSError as exc:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 output root is invalid") from exc
    if output_directory.is_symlink():
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 output root cannot be a symlink")

    matrix_sha256 = f"sha256:{stable_json_hash(matrix)}"
    if matrix_sha256 != AV1_VALIDATION_V3_TIER1_MATRIX_SHA256:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture matrix is not frozen")
    frame_limit = object_dict(matrix.get("frame_limit"))
    frame_spec = object_dict(matrix.get("frame_spec"))
    intermediate = object_dict(matrix.get("intermediate"))
    probe = object_dict(matrix.get("probe"))
    content_hash = object_dict(matrix.get("content_hash"))
    if frame_limit != {"method": "frames_v", "value": 288}:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 frame limit is invalid")

    plans: list[AV1ValidationV3Tier1FixturePlan] = []
    for item in object_list(matrix.get("fixtures")):
        fixture = object_dict(item)
        fixture_id = str(fixture.get("fixture_id") or "")
        lavfi_graph = str(fixture.get("lavfi_graph") or "")
        if not fixture_id or not lavfi_graph:
            raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture entry is invalid")
        output_path = output_root / f"{fixture_id}.nut"
        generate_args = (
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", lavfi_graph,
            "-frames:v", str(frame_limit["value"]), "-an", "-c:v",
            str(intermediate.get("codec") or ""), "-f",
            str(intermediate.get("container") or ""), str(output_path),
        )
        plans.append(AV1ValidationV3Tier1FixturePlan(
            fixture_id=fixture_id,
            matrix_sha256=matrix_sha256,
            output_path=output_path,
            generate_args=generate_args,
            probe_args=_substitute_path(probe.get("command"), output_path),
            content_hash_args=_substitute_path(content_hash.get("command"), output_path),
        ))
    if len(plans) != 4 or len({plan.fixture_id for plan in plans}) != 4:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 fixture plan count is invalid")
    if frame_spec.get("frame_count") != 288:
        raise AV1ValidationV3Tier1ExecutorError("AV1 v3 Tier 1 frame specification is invalid")
    return tuple(plans)


def verify_av1_validation_v3_tier1_fixture(
    plan: AV1ValidationV3Tier1FixturePlan,
    *,
    matrix: Mapping[str, Any],
    executor: AV1ValidationV3Tier1CommandExecutor,
) -> AV1ValidationV3Tier1FixtureOutcome:
    probe_result = executor.run(plan.probe_args)
    if probe_result.returncode != 0:
        return _failed_outcome(plan, "probe_failed")
    try:
        streams = object_list(object_dict(json.loads(probe_result.stdout.decode("utf-8"))).get("streams"))
        stream = object_dict(streams[0]) if streams else {}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return _failed_outcome(plan, "probe_output_invalid")

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

    hash_result = executor.run(plan.content_hash_args)
    if hash_result.returncode != 0:
        failures.append("content_hash_failed")
        content_sha256 = ""
    else:
        content_sha256 = f"sha256:{hashlib.sha256(hash_result.stdout).hexdigest()}"
    return AV1ValidationV3Tier1FixtureOutcome(
        fixture_id=plan.fixture_id,
        matrix_sha256=plan.matrix_sha256,
        content_sha256=content_sha256,
        passed=not failures,
        failures=tuple(failures),
        observation=observation,
    )


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
    reason: str,
) -> AV1ValidationV3Tier1FixtureOutcome:
    return AV1ValidationV3Tier1FixtureOutcome(
        fixture_id=plan.fixture_id,
        matrix_sha256=plan.matrix_sha256,
        content_sha256="",
        passed=False,
        failures=(reason,),
        observation={},
    )
