"""Owner-only AV1 v4 revision-3 plan/preflight pair materialization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4r3_execution_preflight import (
    AV1V4R3ExecutionPreflightError,
    assert_av1_v4r3_ordinal_plan_preflight_pair,
    build_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_freeze import (
    AV1V4R3FreezeError,
    deserialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_AGGREGATE_SECONDS_MAX,
    build_av1_v4r3_ordinal_window_plan_draft,
    finalize_av1_v4r3_ordinal_window_plan,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    AV1V4R3OrdinalWindowRegistryError,
    _PLAN_NAME,
    _PREFLIGHT_NAME,
    _assert_binding_matches_plan,
    _format_ts,
    _locked_registry as _locked_ordinal_registry,
    av1_v4r3_ordinal_window_registry_hmac_id,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    av1_v4r3_path_privacy_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    AV1V4R3PreparationBundleError,
    deserialize_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    AV1V4R3PreparationCustodyError,
    deserialize_av1_v4r3_path_privacy_key_custody,
    deserialize_av1_v4r3_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
    AV1V4R3PreparationCustodyRegistryError,
    _BUNDLE_NAME,
    _CLAIM_NAME,
    _CUSTODY_NAME,
    _FREEZE_NAME,
    _KEY_NAME,
    _MEASUREMENT_NAME,
    _REQUEST_NAME,
    _locked_registry as _locked_preparation_registry,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_measurement import (
    AV1V4R3PreparationMeasurementError,
    deserialize_av1_v4r3_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_operation import (
    _measure_clean_repository,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request import (
    AV1V4R3QualificationRequestError,
    build_av1_v4r3_qualification_request,
    deserialize_av1_v4r3_qualification_request,
    serialize_av1_v4r3_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request_operation import (
    _assert_freeze_chain,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    assert_av1_v4r3_rights_attestation,
)


Clock = Callable[[], datetime]


class AV1V4R3ExecutionPreflightOperationError(RuntimeError):
    """Raised when owner-only revision-3 pair materialization fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R3ExecutionPreflightOperationResult:
    plan: Mapping[str, Any]
    preflight: Mapping[str, Any]
    plan_path: Path
    preflight_path: Path
    plan_created: bool
    preflight_created: bool


@dataclass(frozen=True, slots=True)
class _PreparationSnapshot:
    request: Mapping[str, Any]
    repository_commit: str
    repository_tree: str


def materialize_av1_v4r3_execution_preflight(
    *,
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3ExecutionPreflightOperationResult:
    """Publish one matched, non-authorizing ordinal plan/preflight pair."""

    try:
        return _materialize_av1_v4r3_execution_preflight(
            preparation_binding=preparation_binding,
            ordinal_binding=ordinal_binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
            clock=clock,
        )
    except AV1V4R3ExecutionPreflightOperationError:
        raise
    except Exception as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight operation failed"
        ) from exc


def _materialize_av1_v4r3_execution_preflight(
    *,
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock,
) -> AV1V4R3ExecutionPreflightOperationResult:
    if preparation_binding.registry == ordinal_binding.registry:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight registries must be distinct"
        )
    try:
        if preparation_binding.registry.samefile(ordinal_binding.registry):
            raise AV1V4R3ExecutionPreflightOperationError(
                "AV1 v4 r3 execution preflight registries must be distinct"
            )
    except OSError:
        pass
    snapshot = _load_preparation_snapshot(
        preparation_binding=preparation_binding,
        ordinal_binding=ordinal_binding,
        rights_attestation=rights_attestation,
        owner_principal=owner_principal,
    )
    try:
        with _locked_ordinal_registry(ordinal_binding.registry) as context:
            context.assert_no_terminal()
            now = context.now(clock)
            _assert_request_active(snapshot.request, now)
            plan_exists = context.exists(_PLAN_NAME)
            preflight_exists = context.exists(_PREFLIGHT_NAME)
            if preflight_exists and not plan_exists:
                raise AV1V4R3ExecutionPreflightOperationError(
                    "AV1 v4 r3 execution preflight exists without its plan"
                )
            plan_opens = now
            if plan_exists:
                existing_plan = context.load_plan()
                _assert_binding_matches_plan(ordinal_binding, existing_plan)
                plan_opens = _parse_timestamp(existing_plan["plan_opens_at"])
            plan, preflight = _build_pair(
                request=snapshot.request,
                owner_principal=owner_principal,
                repository_commit=snapshot.repository_commit,
                repository_tree=snapshot.repository_tree,
                run_registry_id=ordinal_binding.run_registry_id,
                plan_opens=plan_opens,
            )
            publication = context.publish_plan_preflight_pair(
                ordinal_binding,
                plan,
                preflight,
            )
            return AV1V4R3ExecutionPreflightOperationResult(
                plan=publication.plan,
                preflight=publication.preflight,
                plan_path=context.registry / _PLAN_NAME,
                preflight_path=context.registry / _PREFLIGHT_NAME,
                plan_created=publication.plan_created,
                preflight_created=publication.preflight_created,
            )
    except AV1V4R3ExecutionPreflightOperationError:
        raise
    except (AV1V4R3OrdinalWindowRegistryError, OSError) as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight ordinal registry operation failed"
        ) from exc


def _load_preparation_snapshot(
    *,
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
) -> _PreparationSnapshot:
    try:
        assert_av1_v4r3_rights_attestation(rights_attestation)
    except AV1V4R3RightsError as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight rights input is invalid"
        ) from exc
    try:
        with _locked_preparation_registry(preparation_binding) as context:
            context.load_binding()
            grant = context.load_grant()
            claim = deserialize_av1_v4r3_preparation_claim(context.read(_CLAIM_NAME))
            custody = deserialize_av1_v4r3_path_privacy_key_custody(
                context.read(_CUSTODY_NAME)
            )
            bundle = deserialize_av1_v4r3_preparation_bundle(context.read(_BUNDLE_NAME))
            measurement = deserialize_av1_v4r3_preparation_measurement(
                context.read(_MEASUREMENT_NAME)
            )
            freeze_bytes = context.read(_FREEZE_NAME)
            freeze = deserialize_av1_v4r3_owner_freeze(freeze_bytes)
            request_bytes = context.read(_REQUEST_NAME)
            request = deserialize_av1_v4r3_qualification_request(request_bytes)
            _assert_freeze_chain(
                freeze_bytes=freeze_bytes,
                freeze=freeze,
                rights_attestation=rights_attestation,
                grant=grant,
                claim=claim,
                custody=custody,
                bundle=bundle,
                measurement=measurement,
            )
            _assert_request_chain(
                request_bytes=request_bytes,
                request=request,
                freeze=freeze,
                owner_principal=owner_principal,
            )
            repository_commit, repository_tree = _measure_clean_repository(
                preparation_binding.repository_root
            )
            repository = {"commit": repository_commit, "tree": repository_tree}
            if not (
                request.get("reviewed_repository")
                == request.get("requesting_repository")
                == repository
            ):
                raise AV1V4R3ExecutionPreflightOperationError(
                    "AV1 v4 r3 execution preflight repository does not match the request"
                )
            context.assert_file_custody(_KEY_NAME, expected_size=32)
            if not hmac.compare_digest(
                str(custody.get("key_id") or ""),
                str(request.get("path_privacy_key_id") or ""),
            ):
                raise AV1V4R3ExecutionPreflightOperationError(
                    "AV1 v4 r3 execution preflight key custody does not match"
                )
            key = context.read(_KEY_NAME)
            if not hmac.compare_digest(
                av1_v4r3_path_privacy_key_id(key),
                str(custody.get("key_id") or ""),
            ):
                raise AV1V4R3ExecutionPreflightOperationError(
                    "AV1 v4 r3 execution preflight key custody does not match"
                )
            derived_registry_id = av1_v4r3_ordinal_window_registry_hmac_id(
                ordinal_binding.registry,
                key=key,
            )
            if not hmac.compare_digest(
                derived_registry_id,
                ordinal_binding.run_registry_id,
            ):
                raise AV1V4R3ExecutionPreflightOperationError(
                    "AV1 v4 r3 execution preflight ordinal registry binding does not match"
                )
            return _PreparationSnapshot(
                request=request,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
            )
    except AV1V4R3ExecutionPreflightOperationError:
        raise
    except (
        AV1V4R3PreparationCustodyRegistryError,
        AV1V4R3PreparationCustodyError,
        AV1V4R3PreparationBundleError,
        AV1V4R3PreparationMeasurementError,
        AV1V4R3FreezeError,
        AV1V4R3QualificationRequestError,
        AV1V4R3OrdinalWindowRegistryError,
        ValueError,
        OSError,
    ) as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight evidence cohort is unavailable"
        ) from exc


def _assert_request_chain(
    *,
    request_bytes: bytes,
    request: Mapping[str, Any],
    freeze: Mapping[str, Any],
    owner_principal: str,
) -> None:
    requesting_repository = object_dict(request.get("requesting_repository"))
    try:
        expected = build_av1_v4r3_qualification_request(
            owner_freeze=freeze,
            owner_principal=owner_principal,
            requested_at=str(request["requested_at"]),
            valid_until=str(request["valid_until"]),
            requesting_repository_commit=str(requesting_repository["commit"]),
            requesting_repository_tree=str(requesting_repository["tree"]),
        )
    except (AV1V4R3QualificationRequestError, KeyError) as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight request chain is invalid"
        ) from exc
    if request_bytes != serialize_av1_v4r3_qualification_request(expected):
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight request chain does not match"
        )


def _build_pair(
    *,
    request: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    run_registry_id: str,
    plan_opens: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_closes = plan_opens + timedelta(seconds=AV1_V4R3_OW_AGGREGATE_SECONDS_MAX)
    plan_opens_at = _format_ts(plan_opens)
    plan_closes_at = _format_ts(plan_closes)
    try:
        draft = build_av1_v4r3_ordinal_window_plan_draft(
            r3_manifest_id=str(request["manifest_id"]),
            r3_manifest_valid_until=str(request["manifest_valid_until"]),
            r3_request_id=str(request["request_id"]),
            r3_request_valid_until=str(request["valid_until"]),
            r3_repository=object_dict(request["reviewed_repository"]),
            r3_toolchain_id=str(request["toolchain_id"]),
            r3_closure_ids=[str(item) for item in object_list(request["closure_ids"])],
            r3_invocation_digests=[
                dict(object_dict(item))
                for item in object_list(request["invocation_digests"])
            ],
            plan_opens_at=plan_opens_at,
            plan_closes_at=plan_closes_at,
            run_registry_id=run_registry_id,
        )
        preflight = build_av1_v4r3_execution_preflight(
            qualification_request=request,
            ordinal_window_plan_draft=draft,
            owner_principal=owner_principal,
            preflight_repository_commit=repository_commit,
            preflight_repository_tree=repository_tree,
            created_at=plan_opens_at,
        )
        plan = finalize_av1_v4r3_ordinal_window_plan(
            draft=draft,
            r3_preflight_id=str(preflight["preflight_id"]),
        )
        assert_av1_v4r3_ordinal_plan_preflight_pair(
            plan=plan,
            preflight=preflight,
        )
        return plan, preflight
    except (
        AV1V4R3OrdinalWindowError,
        AV1V4R3ExecutionPreflightError,
        KeyError,
        ValueError,
    ) as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight pair construction failed"
        ) from exc


def _assert_request_active(request: Mapping[str, Any], as_of: datetime) -> None:
    try:
        requested_at = _parse_timestamp(request["requested_at"])
        valid_until = _parse_timestamp(request["valid_until"])
    except (KeyError, ValueError) as exc:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight request timeline is invalid"
        ) from exc
    if not requested_at <= as_of < valid_until:
        raise AV1V4R3ExecutionPreflightOperationError(
            "AV1 v4 r3 execution preflight request is not active"
        )


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("invalid timestamp")
    return normalized
