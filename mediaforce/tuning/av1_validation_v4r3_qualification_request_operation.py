"""Owner-only qualification-request materialization for AV1 protocol-v4 r3."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4r3_freeze import (
    AV1V4R3FreezeError,
    build_av1_v4r3_owner_freeze,
    deserialize_av1_v4r3_owner_freeze,
    serialize_av1_v4r3_owner_freeze,
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
    _MEASUREMENT_NAME,
    _REQUEST_NAME,
    _clock_timestamp,
    _locked_registry,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_grant import (
    AV1V4R3PreparationGrantError,
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
    av1_v4r3_qualification_request_valid_until,
    build_av1_v4r3_qualification_request,
    deserialize_av1_v4r3_qualification_request,
    serialize_av1_v4r3_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    assert_av1_v4r3_rights_attestation,
)


Clock = Callable[[], datetime]


class AV1V4R3QualificationRequestOperationError(RuntimeError):
    """Raised when owner-only revision-3 request materialization fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R3QualificationRequestOperationResult:
    request: Mapping[str, Any]
    path: Path
    created: bool


def materialize_av1_v4r3_qualification_request(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3QualificationRequestOperationResult:
    try:
        return _materialize_av1_v4r3_qualification_request(
            binding=binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
            clock=clock,
        )
    except AV1V4R3QualificationRequestOperationError:
        raise
    except (
        AV1V4R3FreezeError,
        AV1V4R3QualificationRequestError,
        AV1V4R3PreparationCustodyRegistryError,
        OSError,
    ) as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request operation failed"
        ) from exc


def _materialize_av1_v4r3_qualification_request(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock,
) -> AV1V4R3QualificationRequestOperationResult:
    try:
        assert_av1_v4r3_rights_attestation(rights_attestation)
    except AV1V4R3RightsError as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request rights input is invalid"
        ) from exc
    with _locked_registry(binding) as context:
        try:
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
        except (
            AV1V4R3PreparationCustodyError,
            AV1V4R3PreparationBundleError,
            AV1V4R3PreparationMeasurementError,
            AV1V4R3PreparationCustodyRegistryError,
            AV1V4R3FreezeError,
            ValueError,
        ) as exc:
            raise AV1V4R3QualificationRequestOperationError(
                "AV1 v4 r3 qualification request evidence cohort is unavailable"
            ) from exc
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
        try:
            requesting_commit, requesting_tree = _measure_clean_repository(
                binding.repository_root
            )
        except AV1V4R3PreparationCustodyRegistryError as exc:
            raise AV1V4R3QualificationRequestOperationError(
                "AV1 v4 r3 qualification request repository is invalid"
            ) from exc
        try:
            as_of = _clock_timestamp(clock)
        except Exception as exc:
            raise AV1V4R3QualificationRequestOperationError(
                "AV1 v4 r3 qualification request clock failed"
            ) from exc
        if context.exists(_REQUEST_NAME):
            return _reconcile_existing(
                context=context,
                owner_freeze=freeze,
                owner_principal=owner_principal,
                requesting_commit=requesting_commit,
                requesting_tree=requesting_tree,
                as_of=as_of,
            )
        request = _build_request(
            owner_freeze=freeze,
            owner_principal=owner_principal,
            requesting_commit=requesting_commit,
            requesting_tree=requesting_tree,
            requested_at=as_of,
        )
        context.write_exclusive(
            _REQUEST_NAME, serialize_av1_v4r3_qualification_request(request)
        )
        context.assert_file_custody(_REQUEST_NAME)
        return AV1V4R3QualificationRequestOperationResult(
            request=request,
            path=context.registry / _REQUEST_NAME,
            created=True,
        )


def load_av1_v4r3_qualification_request(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> dict[str, Any] | None:
    try:
        with _locked_registry(binding) as context:
            if not context.exists(_REQUEST_NAME):
                return None
            try:
                return deserialize_av1_v4r3_qualification_request(
                    context.read(_REQUEST_NAME)
                )
            except AV1V4R3QualificationRequestError as exc:
                raise AV1V4R3QualificationRequestOperationError(
                    "AV1 v4 r3 qualification request registry artifact is invalid"
                ) from exc
    except AV1V4R3QualificationRequestOperationError:
        raise
    except AV1V4R3PreparationCustodyRegistryError as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request registry operation failed"
        ) from exc
    except OSError as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request filesystem operation failed"
        ) from exc


def _assert_freeze_chain(
    *,
    freeze_bytes: bytes,
    freeze: Mapping[str, Any],
    rights_attestation: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    bundle: Mapping[str, Any],
    measurement: Mapping[str, Any],
) -> None:
    materializer_repository = object_dict(freeze.get("materializer_repository"))
    try:
        expected = build_av1_v4r3_owner_freeze(
            rights_attestation=rights_attestation,
            preparation_grant=grant,
            preparation_claim=claim,
            key_custody=custody,
            preparation_bundle=bundle,
            preparation_measurement=measurement,
            owner_principal=str(freeze["owner_principal"]),
            decided_at=str(freeze["decided_at"]),
            materializer_repository_commit=str(materializer_repository["commit"]),
            materializer_repository_tree=str(materializer_repository["tree"]),
        )
    except (
        AV1V4R3FreezeError,
        AV1V4R3PreparationGrantError,
        AV1V4R3PreparationCustodyError,
        AV1V4R3PreparationBundleError,
        AV1V4R3PreparationMeasurementError,
        AV1V4R3RightsError,
        KeyError,
    ) as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request freeze chain is invalid"
        ) from exc
    if freeze_bytes != serialize_av1_v4r3_owner_freeze(expected):
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request freeze chain does not match"
        )


def _reconcile_existing(
    *,
    context: Any,
    owner_freeze: Mapping[str, Any],
    owner_principal: str,
    requesting_commit: str,
    requesting_tree: str,
    as_of: str,
) -> AV1V4R3QualificationRequestOperationResult:
    try:
        existing_bytes = context.read(_REQUEST_NAME)
        existing = deserialize_av1_v4r3_qualification_request(existing_bytes)
        if _parse_timestamp(existing["valid_until"]) <= _parse_timestamp(as_of):
            raise AV1V4R3QualificationRequestOperationError(
                "AV1 v4 r3 qualification request singleton is expired"
            )
        expected = _build_request(
            owner_freeze=owner_freeze,
            owner_principal=owner_principal,
            requesting_commit=requesting_commit,
            requesting_tree=requesting_tree,
            requested_at=str(existing["requested_at"]),
        )
    except AV1V4R3QualificationRequestOperationError:
        raise
    except (AV1V4R3QualificationRequestError, KeyError, ValueError) as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request singleton is invalid"
        ) from exc
    if existing_bytes != serialize_av1_v4r3_qualification_request(expected):
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request conflicts with the registry singleton"
        )
    return AV1V4R3QualificationRequestOperationResult(
        request=existing,
        path=context.registry / _REQUEST_NAME,
        created=False,
    )


def _build_request(
    *,
    owner_freeze: Mapping[str, Any],
    owner_principal: str,
    requesting_commit: str,
    requesting_tree: str,
    requested_at: str,
) -> dict[str, Any]:
    try:
        return build_av1_v4r3_qualification_request(
            owner_freeze=owner_freeze,
            owner_principal=owner_principal,
            requested_at=requested_at,
            valid_until=av1_v4r3_qualification_request_valid_until(requested_at),
            requesting_repository_commit=requesting_commit,
            requesting_repository_tree=requesting_tree,
        )
    except AV1V4R3QualificationRequestError as exc:
        raise AV1V4R3QualificationRequestOperationError(
            "AV1 v4 r3 qualification request evidence review failed"
        ) from exc


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    normalized = parsed.astimezone(UTC)
    if parsed.microsecond or normalized.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError("invalid timestamp")
    return normalized
