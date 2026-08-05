from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any, Sequence

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_cold_start import assert_av1_cold_start_public_payload_safe
from mediaforce.tuning.av1_validation_v3 import AV1ValidationProtocolV3, av1_validation_v3_id
from mediaforce.tuning.av1_validation_v3_qualification import AV1ValidationV3QualificationPlan
from mediaforce.tuning.av1_validation_v3_tier1_compat_authorization import (
    AV1ValidationV3Tier1CompatGrant,
    AV1ValidationV3Tier1CompatRequest,
    assert_av1_validation_v3_tier1_compat_grant_active,
)
from mediaforce.tuning.av1_validation_v3_tier1_compat_probe import (
    AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256,
    AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS,
    AV1ValidationV3Tier1CompatExecutionContext,
    AV1ValidationV3Tier1CompatVariantOutcome,
    build_av1_validation_v3_tier1_compat_probe_plans,
    execute_av1_validation_v3_tier1_compat_representation,
)
from mediaforce.tuning.av1_validation_v3_tier1_diagnostics import (
    AV1ValidationV3Tier1CommandRecord,
)
from mediaforce.tuning.av1_validation_v3_tier1_runtime import (
    AV1ValidationV3Tier1ToolchainBinding,
    paused_av1_validation_v3_tier1_compat_runtime,
)


AV1_VALIDATION_V3_TIER1_COMPAT_CLAIM_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_compat_claim"
)
AV1_VALIDATION_V3_TIER1_COMPAT_RESULT_SCHEMA = (
    "mediaforce.av1_cold_start_v3_tier1_compat_result"
)
AV1_VALIDATION_V3_TIER1_COMPAT_OPERATION_SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBSERVATION_KEYS = frozenset({
    "width",
    "height",
    "pix_fmt",
    "color_primaries",
    "color_transfer",
    "color_space",
    "color_range",
})
_MAX_COMMAND_RECORDS = 3 * 3


class AV1ValidationV3Tier1CompatOperationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatClaim:
    claim_id: str
    plan_id: str
    plan_payload_sha256: str
    request_id: str
    request_payload_sha256: str
    grant_id: str
    grant_payload_sha256: str
    probe_matrix_sha256: str
    claimed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.claim_id.startswith("av1vtier1compatclaim3_"):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility claim ID is invalid"
            )
        for value in (
            self.plan_payload_sha256,
            self.request_payload_sha256,
            self.grant_payload_sha256,
            self.probe_matrix_sha256,
            self.payload_sha256,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise AV1ValidationV3Tier1CompatOperationError(
                    "AV1 v3 Tier 1 compatibility claim digest is invalid"
                )
        _parse_timestamp(self.claimed_at)
        semantic = self.semantic_payload()
        if self.claim_id != av1_validation_v3_id("tier1compatclaim", semantic):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility claim ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"claim_id": self.claim_id, **semantic}
        ):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility claim digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_COMPAT_CLAIM_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_COMPAT_OPERATION_SCHEMA_VERSION,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "single_execution_claimed": True,
            "retry_authorized": False,
            **_false_authority_fields(),
            "plan_id": self.plan_id,
            "plan_payload_sha256": self.plan_payload_sha256,
            "request_id": self.request_id,
            "request_payload_sha256": self.request_payload_sha256,
            "grant_id": self.grant_id,
            "grant_payload_sha256": self.grant_payload_sha256,
            "probe_matrix_sha256": self.probe_matrix_sha256,
            "claimed_at": self.claimed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatVariantRecord:
    variant_id: str
    representation_id: str
    surface_id: str
    command_plan_sha256: str
    observation: Mapping[str, object]
    matches_expected_color_description: bool
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.variant_id != f"{self.representation_id}:{self.surface_id}":
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility variant binding is invalid"
            )
        if self.variant_id not in AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS:
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result variant is invalid"
            )
        if not _SHA256_RE.fullmatch(self.command_plan_sha256):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result command digest is invalid"
            )
        if not set(self.observation) <= _OBSERVATION_KEYS:
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result observation is invalid"
            )
        for value in self.observation.values():
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise AV1ValidationV3Tier1CompatOperationError(
                    "AV1 v3 Tier 1 compatibility observation value is invalid"
                )
            if isinstance(value, str) and len(value) > 64:
                raise AV1ValidationV3Tier1CompatOperationError(
                    "AV1 v3 Tier 1 compatibility observation value is invalid"
                )
        if self.failures != tuple(sorted(set(self.failures))):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility failures are invalid"
            )
        if self.matches_expected_color_description == bool(self.failures):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result contradicts its failures"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "representation_id": self.representation_id,
            "surface_id": self.surface_id,
            "command_plan_sha256": self.command_plan_sha256,
            "observation": dict(sorted(self.observation.items())),
            "matches_expected_color_description": self.matches_expected_color_description,
            "failures": list(self.failures),
        }


@dataclass(frozen=True, slots=True)
class AV1ValidationV3Tier1CompatResult:
    result_id: str
    claim_id: str
    claim_payload_sha256: str
    request_id: str
    grant_id: str
    repository_commit: str
    repository_tree: str
    config_sha256: str
    toolchain_sha256: str
    probe_matrix_sha256: str
    variants: tuple[AV1ValidationV3Tier1CompatVariantRecord, ...]
    commands: tuple[AV1ValidationV3Tier1CommandRecord, ...]
    cleanup_passed: bool
    runtime_paused: bool
    completed_at: str
    payload_sha256: str

    def __post_init__(self) -> None:
        if not self.result_id.startswith("av1vtier1compatresult3_"):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result ID is invalid"
            )
        if tuple(item.variant_id for item in self.variants) != AV1_VALIDATION_V3_TIER1_COMPAT_VARIANT_IDS:
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result set is incomplete"
            )
        if len(self.commands) > _MAX_COMMAND_RECORDS or not self.cleanup_passed or not self.runtime_paused:
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility completion state is invalid"
            )
        _parse_timestamp(self.completed_at)
        semantic = self.semantic_payload()
        if self.result_id != av1_validation_v3_id("tier1compatresult", semantic):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result ID does not match its payload"
            )
        if self.payload_sha256 != _payload_sha256(
            {"result_id": self.result_id, **semantic}
        ):
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility result digest does not match its payload"
            )
        assert_av1_cold_start_public_payload_safe(self.to_payload())

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "schema": AV1_VALIDATION_V3_TIER1_COMPAT_RESULT_SCHEMA,
            "schema_version": AV1_VALIDATION_V3_TIER1_COMPAT_OPERATION_SCHEMA_VERSION,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "single_execution_claimed": True,
            "retry_authorized": False,
            "qualification_complete": False,
            "path_matrix_coverage_claimed": False,
            **_false_authority_fields(),
            "claim_id": self.claim_id,
            "claim_payload_sha256": self.claim_payload_sha256,
            "request_id": self.request_id,
            "grant_id": self.grant_id,
            "repository_commit": self.repository_commit,
            "repository_tree": self.repository_tree,
            "config_sha256": self.config_sha256,
            "toolchain_sha256": self.toolchain_sha256,
            "probe_matrix_sha256": self.probe_matrix_sha256,
            "variants": [item.to_payload() for item in self.variants],
            "commands": [item.to_payload() for item in self.commands],
            "cleanup_passed": self.cleanup_passed,
            "runtime_paused": self.runtime_paused,
            "completed_at": self.completed_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            **self.semantic_payload(),
            "payload_sha256": self.payload_sha256,
        }

    def to_public_summary(self) -> dict[str, object]:
        summary = {
            "result_id": self.result_id,
            "payload_sha256": self.payload_sha256,
            "gate": "A0",
            "tier": "none",
            "diagnostic_only": True,
            "variants_matching": sum(
                item.matches_expected_color_description for item in self.variants
            ),
            "variants_total": len(self.variants),
            "qualification_complete": False,
            "evidence_eligible": False,
        }
        assert_av1_cold_start_public_payload_safe(summary)
        return summary


def build_av1_validation_v3_tier1_compat_claim(
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1CompatRequest,
    grant: AV1ValidationV3Tier1CompatGrant,
    claimed_at: str,
) -> AV1ValidationV3Tier1CompatClaim:
    assert_av1_validation_v3_tier1_compat_grant_active(
        protocol,
        plan,
        request,
        grant,
        as_of=claimed_at,
    )
    values = {
        "plan_id": plan.plan_id,
        "plan_payload_sha256": plan.payload_sha256,
        "request_id": request.request_id,
        "request_payload_sha256": request.payload_sha256,
        "grant_id": grant.grant_id,
        "grant_payload_sha256": grant.payload_sha256,
        "probe_matrix_sha256": request.probe_matrix_sha256,
        "claimed_at": claimed_at,
    }
    semantic = _claim_semantic(**values)
    claim_id = av1_validation_v3_id("tier1compatclaim", semantic)
    return AV1ValidationV3Tier1CompatClaim(
        claim_id=claim_id,
        **values,
        payload_sha256=_payload_sha256({"claim_id": claim_id, **semantic}),
    )


def run_av1_validation_v3_tier1_compat_probe(
    config: MediaforceConfig,
    *,
    protocol: AV1ValidationProtocolV3,
    plan: AV1ValidationV3QualificationPlan,
    request: AV1ValidationV3Tier1CompatRequest,
    grant: AV1ValidationV3Tier1CompatGrant,
    claim: AV1ValidationV3Tier1CompatClaim,
    config_snapshot_bytes: bytes,
    matrix: Mapping[str, object],
    output_directory: Path,
    repository_root: Path,
    toolchain: AV1ValidationV3Tier1ToolchainBinding,
    claim_execution: Callable[[], str],
    clock: Callable[[], str] | None = None,
) -> AV1ValidationV3Tier1CompatResult:
    now = clock or _canonical_utc_now
    matrix_sha256 = f"sha256:{stable_json_hash(matrix)}"
    if (
        matrix_sha256 != AV1_VALIDATION_V3_TIER1_COMPAT_MATRIX_SHA256
        or request.probe_matrix_sha256 != matrix_sha256
    ):
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility matrix is not bound to the active request"
        )
    expected_claim = build_av1_validation_v3_tier1_compat_claim(
        protocol=protocol,
        plan=plan,
        request=request,
        grant=grant,
        claimed_at=claim.claimed_at,
    )
    if claim != expected_claim:
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility claim is not bound to the active run"
        )
    initial_context = AV1ValidationV3Tier1CompatExecutionContext(
        protocol=protocol,
        qualification_plan=plan,
        request=request,
        grant=grant,
        as_of=claim.claimed_at,
    )
    outcomes: list[AV1ValidationV3Tier1CompatVariantOutcome] = []
    with paused_av1_validation_v3_tier1_compat_runtime(
        config,
        config_snapshot_bytes=config_snapshot_bytes,
        context=initial_context,
        matrix=matrix,
        output_directory=output_directory,
        repository_root=repository_root,
        toolchain=toolchain,
    ) as session:
        assert_av1_validation_v3_tier1_compat_grant_active(
            protocol,
            plan,
            request,
            grant,
            as_of=now(),
        )
        if claim_execution() != claim.claim_id:
            raise AV1ValidationV3Tier1CompatOperationError(
                "AV1 v3 Tier 1 compatibility claim was not published"
            )
        plans = build_av1_validation_v3_tier1_compat_probe_plans(
            matrix,
            output_directory=output_directory,
            repository_root=repository_root,
        )
        for representation in plans:
            context = replace(initial_context, as_of=now())
            outcomes.extend(
                execute_av1_validation_v3_tier1_compat_representation(
                    representation,
                    matrix=matrix,
                    context=context,
                    executor=session.executor,
                    clock=now,
                )
            )
    if not session.cleanup_passed:
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility-probe cleanup did not complete"
        )
    completed_at = now()
    assert_av1_validation_v3_tier1_compat_grant_active(
        protocol,
        plan,
        request,
        grant,
        as_of=completed_at,
    )
    records = tuple(
        AV1ValidationV3Tier1CompatVariantRecord(
            variant_id=item.variant_id,
            representation_id=item.representation_id,
            surface_id=item.surface_id,
            command_plan_sha256=item.command_plan_sha256,
            observation=item.observation,
            matches_expected_color_description=item.matches_expected_color_description,
            failures=item.failures,
        )
        for item in outcomes
    )
    commands = tuple(
        AV1ValidationV3Tier1CommandRecord(
            program=item.program,
            argv_sha256=item.argv_sha256,
            outcome=item.outcome,
            returncode=item.returncode,
            stdout_bytes=item.stdout_bytes,
            stderr_bytes=item.stderr_bytes,
            stderr_truncated=item.stderr_truncated,
        )
        for item in session.diagnostics
    )
    semantic = _result_semantic(
        claim=claim,
        request=request,
        grant=grant,
        plan=plan,
        variants=records,
        commands=commands,
        completed_at=completed_at,
    )
    result_id = av1_validation_v3_id("tier1compatresult", semantic)
    return AV1ValidationV3Tier1CompatResult(
        result_id=result_id,
        claim_id=claim.claim_id,
        claim_payload_sha256=claim.payload_sha256,
        request_id=request.request_id,
        grant_id=grant.grant_id,
        repository_commit=plan.repository_commit,
        repository_tree=plan.repository_tree,
        config_sha256=plan.config_sha256,
        toolchain_sha256=plan.toolchain_sha256,
        probe_matrix_sha256=request.probe_matrix_sha256,
        variants=records,
        commands=commands,
        cleanup_passed=True,
        runtime_paused=True,
        completed_at=completed_at,
        payload_sha256=_payload_sha256({"result_id": result_id, **semantic}),
    )


def serialize_av1_validation_v3_tier1_compat_claim(
    claim: AV1ValidationV3Tier1CompatClaim,
) -> bytes:
    return canonical_json_bytes(claim.to_payload()) + b"\n"


def serialize_av1_validation_v3_tier1_compat_result(
    result: AV1ValidationV3Tier1CompatResult,
) -> bytes:
    return canonical_json_bytes(result.to_payload()) + b"\n"


def load_av1_validation_v3_tier1_compat_result(
    path: Path,
) -> AV1ValidationV3Tier1CompatResult:
    raw = path.read_bytes()
    try:
        result = av1_validation_v3_tier1_compat_result_from_payload(
            object_dict(json.loads(raw.decode("utf-8")))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility result is invalid"
        ) from exc
    if raw != serialize_av1_validation_v3_tier1_compat_result(result):
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility result bytes are not canonical"
        )
    return result


def av1_validation_v3_tier1_compat_result_from_payload(
    payload: Mapping[str, Any],
) -> AV1ValidationV3Tier1CompatResult:
    value = object_dict(payload)
    variants = tuple(_variant_from_payload(item) for item in object_list(value.get("variants")))
    commands = tuple(_command_from_payload(item) for item in object_list(value.get("commands")))
    result = AV1ValidationV3Tier1CompatResult(
        result_id=str(value.get("result_id") or ""),
        claim_id=str(value.get("claim_id") or ""),
        claim_payload_sha256=str(value.get("claim_payload_sha256") or ""),
        request_id=str(value.get("request_id") or ""),
        grant_id=str(value.get("grant_id") or ""),
        repository_commit=str(value.get("repository_commit") or ""),
        repository_tree=str(value.get("repository_tree") or ""),
        config_sha256=str(value.get("config_sha256") or ""),
        toolchain_sha256=str(value.get("toolchain_sha256") or ""),
        probe_matrix_sha256=str(value.get("probe_matrix_sha256") or ""),
        variants=variants,
        commands=commands,
        cleanup_passed=value.get("cleanup_passed") is True,
        runtime_paused=value.get("runtime_paused") is True,
        completed_at=str(value.get("completed_at") or ""),
        payload_sha256=str(value.get("payload_sha256") or ""),
    )
    if value != result.to_payload():
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility result fields are invalid"
        )
    return result


def _claim_semantic(**values: Any) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_COMPAT_CLAIM_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_COMPAT_OPERATION_SCHEMA_VERSION,
        "gate": "A0",
        "tier": "none",
        "diagnostic_only": True,
        "single_execution_claimed": True,
        "retry_authorized": False,
        **_false_authority_fields(),
        **values,
    }


def _result_semantic(
    *,
    claim: AV1ValidationV3Tier1CompatClaim,
    request: AV1ValidationV3Tier1CompatRequest,
    grant: AV1ValidationV3Tier1CompatGrant,
    plan: AV1ValidationV3QualificationPlan,
    variants: Sequence[AV1ValidationV3Tier1CompatVariantRecord],
    commands: Sequence[AV1ValidationV3Tier1CommandRecord],
    completed_at: str,
) -> dict[str, Any]:
    return {
        "schema": AV1_VALIDATION_V3_TIER1_COMPAT_RESULT_SCHEMA,
        "schema_version": AV1_VALIDATION_V3_TIER1_COMPAT_OPERATION_SCHEMA_VERSION,
        "gate": "A0",
        "tier": "none",
        "diagnostic_only": True,
        "single_execution_claimed": True,
        "retry_authorized": False,
        "qualification_complete": False,
        "path_matrix_coverage_claimed": False,
        **_false_authority_fields(),
        "claim_id": claim.claim_id,
        "claim_payload_sha256": claim.payload_sha256,
        "request_id": request.request_id,
        "grant_id": grant.grant_id,
        "repository_commit": plan.repository_commit,
        "repository_tree": plan.repository_tree,
        "config_sha256": plan.config_sha256,
        "toolchain_sha256": plan.toolchain_sha256,
        "probe_matrix_sha256": request.probe_matrix_sha256,
        "variants": [item.to_payload() for item in variants],
        "commands": [item.to_payload() for item in commands],
        "cleanup_passed": True,
        "runtime_paused": True,
        "completed_at": completed_at,
    }


def _variant_from_payload(value: object) -> AV1ValidationV3Tier1CompatVariantRecord:
    item = object_dict(value)
    return AV1ValidationV3Tier1CompatVariantRecord(
        variant_id=str(item.get("variant_id") or ""),
        representation_id=str(item.get("representation_id") or ""),
        surface_id=str(item.get("surface_id") or ""),
        command_plan_sha256=str(item.get("command_plan_sha256") or ""),
        observation=object_dict(item.get("observation")),
        matches_expected_color_description=(
            item.get("matches_expected_color_description") is True
        ),
        failures=tuple(str(value) for value in object_list(item.get("failures"))),
    )


def _command_from_payload(value: object) -> AV1ValidationV3Tier1CommandRecord:
    item = object_dict(value)
    return AV1ValidationV3Tier1CommandRecord(
        program=str(item.get("program") or ""),
        argv_sha256=str(item.get("argv_sha256") or ""),
        outcome=str(item.get("outcome") or ""),
        returncode=int(item.get("returncode") or 0),
        stdout_bytes=int(item.get("stdout_bytes") or 0),
        stderr_bytes=int(item.get("stderr_bytes") or 0),
        stderr_truncated=item.get("stderr_truncated") is True,
    )


def _false_authority_fields() -> dict[str, bool]:
    return {
        "tier1_execution_authorized": False,
        "tier1_coverage_eligible": False,
        "tier2_execution_authorized": False,
        "qualification_execution_authorized": False,
        "evidence_creation_authorized": False,
        "evidence_eligible": False,
        "empirical_authority_conferred": False,
        "derivation_authorized": False,
        "holdout_authorized": False,
        "publication_authorized": False,
        "activation_authorized": False,
        "public_bundle_activation_allowed": False,
        "private_inventory_read_authorized": False,
        "media_library_read_authorized": False,
        "key_creation_authorized": False,
    }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility timestamp is invalid"
        ) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(parsed)
        or parsed.microsecond
        or not value.endswith("Z")
        or parsed.isoformat().replace("+00:00", "Z") != value
    ):
        raise AV1ValidationV3Tier1CompatOperationError(
            "AV1 v3 Tier 1 compatibility timestamp is invalid"
        )
    return parsed


def _canonical_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return f"sha256:{stable_json_hash(payload)}"
