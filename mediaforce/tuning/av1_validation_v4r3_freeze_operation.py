"""Owner-only atomic freeze materialization for AV1 protocol-v4 revision 3."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
from mediaforce.tuning.av1_validation_v4r3_rights import (
    AV1V4R3RightsError,
    assert_av1_v4r3_rights_attestation,
)


Clock = Callable[[], datetime]


class AV1V4R3FreezeOperationError(RuntimeError):
    """Raised when owner-only revision-3 freeze materialization fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R3FreezeOperationResult:
    freeze: Mapping[str, Any]
    path: Path
    created: bool


def materialize_av1_v4r3_owner_freeze(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3FreezeOperationResult:
    try:
        return _materialize_av1_v4r3_owner_freeze(
            binding=binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
            clock=clock,
        )
    except AV1V4R3FreezeOperationError:
        raise
    except AV1V4R3FreezeError as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze artifact serialization failed"
        ) from exc
    except AV1V4R3PreparationCustodyRegistryError as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze registry operation failed"
        ) from exc


def _materialize_av1_v4r3_owner_freeze(
    *,
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3FreezeOperationResult:
    """Review one successful preparation cohort and publish one freeze singleton."""

    try:
        assert_av1_v4r3_rights_attestation(rights_attestation)
    except AV1V4R3RightsError as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze rights input is invalid"
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
        except (
            AV1V4R3PreparationCustodyError,
            AV1V4R3PreparationBundleError,
            AV1V4R3PreparationMeasurementError,
            AV1V4R3PreparationCustodyRegistryError,
            ValueError,
        ) as exc:
            raise AV1V4R3FreezeOperationError(
                "AV1 v4 r3 owner freeze preparation cohort is unavailable"
            ) from exc
        try:
            materializer_commit, materializer_tree = _measure_clean_repository(
                binding.repository_root
            )
        except AV1V4R3PreparationCustodyRegistryError as exc:
            raise AV1V4R3FreezeOperationError(
                "AV1 v4 r3 owner freeze materializer repository is invalid"
            ) from exc
        if context.exists(_FREEZE_NAME):
            return _reconcile_existing(
                context=context,
                rights_attestation=rights_attestation,
                grant=grant,
                claim=claim,
                custody=custody,
                bundle=bundle,
                measurement=measurement,
                owner_principal=owner_principal,
                materializer_commit=materializer_commit,
                materializer_tree=materializer_tree,
            )
        try:
            decided_at = _clock_timestamp(clock)
        except Exception as exc:
            raise AV1V4R3FreezeOperationError(
                "AV1 v4 r3 owner freeze decision clock failed"
            ) from exc
        freeze = _build_freeze(
            rights_attestation=rights_attestation,
            grant=grant,
            claim=claim,
            custody=custody,
            bundle=bundle,
            measurement=measurement,
            owner_principal=owner_principal,
            decided_at=decided_at,
            materializer_commit=materializer_commit,
            materializer_tree=materializer_tree,
        )
        context.write_exclusive(_FREEZE_NAME, serialize_av1_v4r3_owner_freeze(freeze))
        context.assert_file_custody(_FREEZE_NAME)
        return AV1V4R3FreezeOperationResult(
            freeze=freeze,
            path=context.registry / _FREEZE_NAME,
            created=True,
        )


def load_av1_v4r3_owner_freeze(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> dict[str, Any] | None:
    try:
        with _locked_registry(binding) as context:
            if not context.exists(_FREEZE_NAME):
                return None
            try:
                return deserialize_av1_v4r3_owner_freeze(context.read(_FREEZE_NAME))
            except AV1V4R3FreezeError as exc:
                raise AV1V4R3FreezeOperationError(
                    "AV1 v4 r3 owner freeze registry artifact is invalid"
                ) from exc
    except AV1V4R3FreezeOperationError:
        raise
    except AV1V4R3PreparationCustodyRegistryError as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze registry operation failed"
        ) from exc


def _reconcile_existing(
    *,
    context: Any,
    rights_attestation: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    bundle: Mapping[str, Any],
    measurement: Mapping[str, Any],
    owner_principal: str,
    materializer_commit: str,
    materializer_tree: str,
) -> AV1V4R3FreezeOperationResult:
    try:
        existing_bytes = context.read(_FREEZE_NAME)
        existing = deserialize_av1_v4r3_owner_freeze(existing_bytes)
        expected = _build_freeze(
            rights_attestation=rights_attestation,
            grant=grant,
            claim=claim,
            custody=custody,
            bundle=bundle,
            measurement=measurement,
            owner_principal=owner_principal,
            decided_at=str(existing["decided_at"]),
            materializer_commit=materializer_commit,
            materializer_tree=materializer_tree,
        )
    except (AV1V4R3FreezeError, KeyError) as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze singleton is invalid"
        ) from exc
    if existing_bytes != serialize_av1_v4r3_owner_freeze(expected):
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze conflicts with the registry singleton"
        )
    return AV1V4R3FreezeOperationResult(
        freeze=existing,
        path=context.registry / _FREEZE_NAME,
        created=False,
    )


def _build_freeze(**kwargs: Any) -> dict[str, Any]:
    try:
        return build_av1_v4r3_owner_freeze(
            rights_attestation=kwargs["rights_attestation"],
            preparation_grant=kwargs["grant"],
            preparation_claim=kwargs["claim"],
            key_custody=kwargs["custody"],
            preparation_bundle=kwargs["bundle"],
            preparation_measurement=kwargs["measurement"],
            owner_principal=kwargs["owner_principal"],
            decided_at=kwargs["decided_at"],
            materializer_repository_commit=kwargs["materializer_commit"],
            materializer_repository_tree=kwargs["materializer_tree"],
        )
    except (
        AV1V4R3FreezeError,
        AV1V4R3PreparationGrantError,
        AV1V4R3PreparationCustodyError,
        AV1V4R3PreparationBundleError,
        AV1V4R3PreparationMeasurementError,
        AV1V4R3RightsError,
    ) as exc:
        raise AV1V4R3FreezeOperationError(
            "AV1 v4 r3 owner freeze evidence review failed"
        ) from exc
