"""Owner-only non-media cohort preparation for AV1 v4 revision 3."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
from pathlib import Path
from typing import Any

from mediaforce.tuning.av1_validation_v4r3_execution_preflight_operation import (
    materialize_av1_v4r3_execution_preflight,
)
from mediaforce.tuning.av1_validation_v4r3_freeze_operation import (
    load_av1_v4r3_owner_freeze,
    materialize_av1_v4r3_owner_freeze,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    assert_av1_v4r3_ordinal_window_registry,
    av1_v4r3_ordinal_window_registry_hmac_id,
    publish_av1_v4r3_ordinal_window_grant_with_status,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    deserialize_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody import (
    deserialize_av1_v4r3_path_privacy_key_custody,
    deserialize_av1_v4r3_preparation_claim,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    av1_v4r3_path_privacy_key_id,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    deserialize_av1_v4r3_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
    _BUNDLE_NAME,
    _CLAIM_NAME,
    _CONFIG_NAME,
    _CUSTODY_NAME,
    _FREEZE_NAME,
    _GRANT_NAME,
    _KEY_NAME,
    _MEASUREMENT_NAME,
    _REQUEST_NAME,
    _locked_registry,
    assert_av1_v4r3_preparation_custody_registry,
    consume_av1_v4r3_preparation_grant,
    publish_av1_v4r3_preparation_grant,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_measurement import (
    deserialize_av1_v4r3_preparation_measurement,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_operation import (
    _measure_clean_repository,
    run_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_qualification_request_operation import (
    load_av1_v4r3_qualification_request,
    materialize_av1_v4r3_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    assert_av1_v4r3_rights_attestation,
)


Clock = Callable[[], datetime]


class AV1V4R3DogfoodPreparationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AV1V4R3DogfoodPreparationResult:
    grant: Mapping[str, Any]
    claim: Mapping[str, Any]
    custody: Mapping[str, Any]
    bundle: Mapping[str, Any]
    measurement: Mapping[str, Any]
    freeze: Mapping[str, Any]
    request: Mapping[str, Any]
    plan: Mapping[str, Any]
    preflight: Mapping[str, Any]
    ordinal_grant: Mapping[str, Any]
    created: Mapping[str, bool]


def prepare_av1_v4r3_dogfood(
    *,
    repository_root: Path,
    preparation_registry: Path,
    ordinal_registry: Path,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    confirmed_owner_principal: str,
    preparation_grant_valid_until: str,
    ordinal_1_valid_until: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
    tool_paths: Mapping[str, Path],
    clock: Clock = lambda: datetime.now(UTC).replace(microsecond=0),
) -> AV1V4R3DogfoodPreparationResult:
    if not hmac.compare_digest(owner_principal, confirmed_owner_principal):
        raise AV1V4R3DogfoodPreparationError("owner confirmation is invalid")
    assert_av1_v4r3_rights_attestation(rights_attestation)
    preparation_binding = AV1V4R3PreparationCustodyRegistryBinding(
        registry=preparation_registry,
        repository_root=repository_root,
    )
    assert_av1_v4r3_preparation_custody_registry(preparation_binding)
    assert_av1_v4r3_ordinal_window_registry(ordinal_registry)
    if preparation_registry == ordinal_registry or preparation_registry.samefile(
        ordinal_registry
    ):
        raise AV1V4R3DogfoodPreparationError("registries must be distinct")
    repository_commit, repository_tree = _measure_clean_repository(repository_root)

    with _locked_registry(preparation_binding) as context:
        has_grant = context.exists(_GRANT_NAME)
        has_claim = context.exists(_CLAIM_NAME)
        has_measurement = context.exists(_MEASUREMENT_NAME)
        has_freeze = context.exists(_FREEZE_NAME)
        has_request = context.exists(_REQUEST_NAME)
    created: dict[str, bool] = {}
    if not has_claim:
        if has_grant:
            with _locked_registry(preparation_binding) as context:
                retained_grant = context.load_grant()
            if retained_grant.get("valid_until") != preparation_grant_valid_until:
                raise AV1V4R3DogfoodPreparationError(
                    "retained preparation grant conflicts with the request"
                )
            created["preparation_grant"] = False
        else:
            authorized = _whole_second(clock)
            publication = publish_av1_v4r3_preparation_grant(
                binding=preparation_binding,
                rights_attestation=rights_attestation,
                owner_principal=owner_principal,
                repository_commit=repository_commit,
                repository_tree=repository_tree,
                authorized_at=_format_ts(authorized),
                valid_until=preparation_grant_valid_until,
                clock=lambda: authorized,
            )
            created["preparation_grant"] = publication.created
        custody_result = consume_av1_v4r3_preparation_grant(
            binding=preparation_binding,
            rights_attestation=rights_attestation,
            clock=clock,
        )
        created["preparation_custody"] = True
        grant = custody_result.grant
        claim = custody_result.claim
        custody = custody_result.key_custody
    else:
        grant, claim, custody = _load_consumed_preparation(preparation_binding)
        created["preparation_grant"] = False
        created["preparation_custody"] = False

    if not has_measurement:
        bundle_result = run_av1_v4r3_preparation_bundle(
            binding=preparation_binding,
            repository_commit=repository_commit,
            repository_tree=repository_tree,
            source_paths=source_paths,
            dedicated_instance_paths=dedicated_instance_paths,
            quality_temp_paths=quality_temp_paths,
            tool_paths=tool_paths,
            clock=clock,
        )
        bundle = bundle_result.preparation_bundle
        measurement = bundle_result.measurement
        created["preparation_bundle"] = True
    else:
        bundle, measurement = _load_successful_bundle(
            preparation_binding,
            source_paths=source_paths,
            dedicated_instance_paths=dedicated_instance_paths,
            quality_temp_paths=quality_temp_paths,
        )
        created["preparation_bundle"] = False

    if has_freeze:
        freeze = load_av1_v4r3_owner_freeze(preparation_binding)
        if freeze is None:
            raise AV1V4R3DogfoodPreparationError("owner freeze is unavailable")
        created["freeze"] = False
    else:
        freeze_result = materialize_av1_v4r3_owner_freeze(
            binding=preparation_binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
            clock=clock,
        )
        freeze = freeze_result.freeze
        created["freeze"] = freeze_result.created
    if has_request:
        request = load_av1_v4r3_qualification_request(preparation_binding)
        if request is None:
            raise AV1V4R3DogfoodPreparationError("qualification request is unavailable")
        created["request"] = False
    else:
        request_result = materialize_av1_v4r3_qualification_request(
            binding=preparation_binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
            clock=clock,
        )
        request = request_result.request
        created["request"] = request_result.created
    ordinal_binding = _ordinal_binding(preparation_binding, ordinal_registry)
    preflight_result = materialize_av1_v4r3_execution_preflight(
        preparation_binding=preparation_binding,
        ordinal_binding=ordinal_binding,
        rights_attestation=rights_attestation,
        owner_principal=owner_principal,
        clock=clock,
    )
    created["plan"] = preflight_result.plan_created
    created["preflight"] = preflight_result.preflight_created
    ordinal_publication = publish_av1_v4r3_ordinal_window_grant_with_status(
        binding=ordinal_binding,
        plan=preflight_result.plan,
        ordinal=1,
        clock=clock,
        valid_until=ordinal_1_valid_until,
    )
    ordinal_grant = ordinal_publication.grant
    created["ordinal_1_grant"] = ordinal_publication.created
    return AV1V4R3DogfoodPreparationResult(
        grant=grant,
        claim=claim,
        custody=custody,
        bundle=bundle,
        measurement=measurement,
        freeze=freeze,
        request=request,
        plan=preflight_result.plan,
        preflight=preflight_result.preflight,
        ordinal_grant=ordinal_grant,
        created=created,
    )


def _load_consumed_preparation(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with _locked_registry(binding) as context:
        grant = context.load_grant()
        claim = deserialize_av1_v4r3_preparation_claim(context.read(_CLAIM_NAME))
        custody = deserialize_av1_v4r3_path_privacy_key_custody(
            context.read(_CUSTODY_NAME)
        )
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        return grant, claim, custody


def _load_successful_bundle(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    *,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _locked_registry(binding) as context:
        measurement = deserialize_av1_v4r3_preparation_measurement(
            context.read(_MEASUREMENT_NAME)
        )
        if measurement.get("state") != "terminal_success":
            raise AV1V4R3DogfoodPreparationError("preparation is terminally failed")
        bundle = deserialize_av1_v4r3_preparation_bundle(context.read(_BUNDLE_NAME))
        config = deserialize_av1_v4r3_effective_config_snapshot(
            context.read(_CONFIG_NAME)
        )
        if (
            config.get("source_paths") != dict(source_paths)
            or config.get("dedicated_instance_paths") != dict(dedicated_instance_paths)
            or config.get("quality_temp_paths") != dict(quality_temp_paths)
        ):
            raise AV1V4R3DogfoodPreparationError(
                "prepared private path bindings do not match"
            )
        return bundle, measurement


def _ordinal_binding(
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_registry: Path,
) -> AV1V4R3OrdinalWindowRegistryBinding:
    with _locked_registry(preparation_binding) as context:
        custody = deserialize_av1_v4r3_path_privacy_key_custody(
            context.read(_CUSTODY_NAME)
        )
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        key = context.read(_KEY_NAME)
        if custody.get("key_id") != av1_v4r3_path_privacy_key_id(key):
            raise AV1V4R3DogfoodPreparationError("path key custody is invalid")
        run_registry_id = av1_v4r3_ordinal_window_registry_hmac_id(
            ordinal_registry,
            key=key,
        )
    return AV1V4R3OrdinalWindowRegistryBinding(
        registry=ordinal_registry,
        run_registry_id=run_registry_id,
    )


def _whole_second(clock: Clock) -> datetime:
    value = clock().astimezone(UTC)
    if value.microsecond != 0:
        raise AV1V4R3DogfoodPreparationError("clock must use whole seconds")
    return value


def _format_ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
