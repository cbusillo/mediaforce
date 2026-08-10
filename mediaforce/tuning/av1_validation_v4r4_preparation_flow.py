"""Owner-only AV1 v4 revision-4 preparation and custody flow."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.type_defs import object_dict
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryBinding,
    AV1V4R4OrdinalRegistryError,
    av1_v4r4_ordinal_registry_binding,
    build_av1_v4r4_ordinal_registry_plan,
    initialize_av1_v4r4_ordinal_registry,
    load_av1_v4r4_ordinal_registry_plan,
    publish_av1_v4r4_ordinal_registry_grant_with_status,
    publish_av1_v4r4_ordinal_registry_plan_with_status,
)
from mediaforce.tuning.av1_validation_v4r4_preparation import (
    AV1V4R4PreparationError,
    AV1_V4R4_PREPARATION_METHOD,
    assert_av1_v4r4_preparation_bundle,
    assert_av1_v4r4_preparation_bundle_private_invocations,
    assert_av1_v4r4_preparation_measurement,
    assert_av1_v4r4_rights_attestation,
    av1_v4r4_qualification_request_valid_until,
    av1_v4r4_path_privacy_key_id,
    AV1_V4R4_PREPARATION_SOURCE_SPECS,
    build_av1_v4r4_effective_config_snapshot,
    build_av1_v4r4_execution_preflight,
    build_av1_v4r4_owner_freeze,
    build_av1_v4r4_preparation_failure_measurement,
    build_av1_v4r4_preparation_bundle,
    build_av1_v4r4_preparation_success_measurement,
    build_av1_v4r4_qualification_request,
    deserialize_av1_v4r4_effective_config_snapshot,
    deserialize_av1_v4r4_execution_preflight,
    deserialize_av1_v4r4_owner_freeze,
    deserialize_av1_v4r4_preparation_bundle,
    deserialize_av1_v4r4_preparation_claim,
    deserialize_av1_v4r4_path_privacy_key_custody,
    deserialize_av1_v4r4_preparation_measurement,
    deserialize_av1_v4r4_qualification_request,
    serialize_av1_v4r4_effective_config_snapshot,
    serialize_av1_v4r4_execution_preflight,
    serialize_av1_v4r4_owner_freeze,
    serialize_av1_v4r4_preparation_bundle,
    serialize_av1_v4r4_preparation_measurement,
    serialize_av1_v4r4_qualification_request,
)
from mediaforce.tuning.av1_validation_v4r4_preparation_registry import (
    AV1V4R4PreparationRegistryBinding,
    _ATTEMPT_NAME,
    _BUNDLE_NAME,
    _CLAIM_NAME,
    _CONFIG_NAME,
    _CUSTODY_NAME,
    _FREEZE_NAME,
    _GRANT_NAME,
    _KEY_NAME,
    _MEASUREMENT_NAME,
    _PREFLIGHT_NAME,
    _REQUEST_NAME,
    _clock_timestamp,
    _locked_registry,
    assert_av1_v4r4_preparation_registry,
    initialize_av1_v4r4_preparation_registry,
    consume_av1_v4r4_preparation_grant,
    publish_av1_v4r4_preparation_grant,
)


Clock = Callable[[], datetime]


class AV1V4R4PreparationFlowError(RuntimeError):
    """Raised when the revision-4 owner preparation flow fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R4PreparationFlowResult:
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
    ordinal_registry_id: str
    created: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class _PreparationBundleResult:
    bundle: Mapping[str, Any]
    measurement: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class _ToolBinary:
    path: Path
    descriptor: int
    metadata: os.stat_result
    binary_sha256: str


def prepare_av1_v4r4_preparation_custody_readiness(
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
) -> AV1V4R4PreparationFlowResult:
    clock = _utc_now
    if not hmac.compare_digest(owner_principal, confirmed_owner_principal):
        raise AV1V4R4PreparationFlowError("owner confirmation is invalid")
    try:
        assert_av1_v4r4_rights_attestation(rights_attestation)
    except AV1V4R4PreparationError as exc:
        raise AV1V4R4PreparationFlowError("rights attestation is invalid") from exc
    preparation_binding = AV1V4R4PreparationRegistryBinding(
        registry=Path(preparation_registry),
        repository_root=Path(repository_root),
    )
    _assert_registry_layout(
        repository_root=Path(repository_root),
        preparation_registry=Path(preparation_registry),
        ordinal_registry=Path(ordinal_registry),
    )
    initialize_av1_v4r4_preparation_registry(preparation_binding)
    assert_av1_v4r4_preparation_registry(preparation_binding)
    try:
        repository_commit, repository_tree = _repository_identity(repository_root)
    except Exception as exc:
        raise AV1V4R4PreparationFlowError("repository must be clean and canonical") from exc
    created: dict[str, bool] = {}

    grant, claim, custody = _ensure_custody(
        preparation_binding=preparation_binding,
        rights_attestation=rights_attestation,
        owner_principal=owner_principal,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        preparation_grant_valid_until=preparation_grant_valid_until,
        clock=clock,
        created=created,
    )
    bundle_result = _ensure_preparation_bundle(
        preparation_binding=preparation_binding,
        grant=grant,
        claim=claim,
        custody=custody,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
        source_paths=source_paths,
        dedicated_instance_paths=dedicated_instance_paths,
        quality_temp_paths=quality_temp_paths,
        tool_paths=tool_paths,
        clock=clock,
    )
    bundle = dict(bundle_result.bundle)
    measurement = dict(bundle_result.measurement)
    created["preparation_bundle"] = bundle_result.created

    freeze = _ensure_owner_freeze(
        preparation_binding=preparation_binding,
        rights_attestation=rights_attestation,
        grant=grant,
        claim=claim,
        custody=custody,
        bundle=bundle,
        measurement=measurement,
        owner_principal=owner_principal,
        materializer_repository_commit=repository_commit,
        materializer_repository_tree=repository_tree,
        clock=clock,
    )
    created["freeze"] = freeze[1]
    freeze_payload = freeze[0]

    request = _ensure_qualification_request(
        preparation_binding=preparation_binding,
        freeze=freeze_payload,
        owner_principal=owner_principal,
        requesting_repository_commit=repository_commit,
        requesting_repository_tree=repository_tree,
        clock=clock,
    )
    created["request"] = request[1]
    request_payload = request[0]

    ordinal_binding = _ordinal_binding(preparation_binding, Path(ordinal_registry))
    plan, plan_created = _ensure_plan(
        ordinal_binding=ordinal_binding,
        request=request_payload,
        ordinal_1_valid_until=ordinal_1_valid_until,
        clock=clock,
    )
    created["plan"] = plan_created

    preflight, preflight_created = _ensure_execution_preflight(
        preparation_binding=preparation_binding,
        request=request_payload,
        freeze=freeze_payload,
        owner_principal=owner_principal,
        ordinal_registry_id=ordinal_binding.registry_id,
        plan=plan,
        preflight_repository_commit=repository_commit,
        preflight_repository_tree=repository_tree,
        clock=clock,
    )
    created["preflight"] = preflight_created
    try:
        ordinal_publication = publish_av1_v4r4_ordinal_registry_grant_with_status(
            binding=ordinal_binding,
            plan=plan,
            ordinal=1,
            clock=clock,
            valid_until=ordinal_1_valid_until,
        )
    except AV1V4R4OrdinalRegistryError as exc:
        raise AV1V4R4PreparationFlowError(
            "ordinal sequencing grant publication failed"
        ) from exc
    created["ordinal_1_grant"] = ordinal_publication.created
    ordinal_grant = dict(ordinal_publication.grant)
    if _parse_timestamp(str(ordinal_grant["authorized_at"])) < _parse_timestamp(
        str(preflight["created_at"])
    ):
        raise AV1V4R4PreparationFlowError(
            "ordinal sequencing grant predates execution preflight"
        )
    return AV1V4R4PreparationFlowResult(
        grant=grant,
        claim=claim,
        custody=custody,
        bundle=bundle,
        measurement=measurement,
        freeze=freeze_payload,
        request=request_payload,
        plan=plan,
        preflight=preflight,
        ordinal_grant=ordinal_grant,
        ordinal_registry_id=ordinal_binding.registry_id,
        created=created,
    )


def _ensure_custody(
    *,
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    repository_commit: str,
    repository_tree: str,
    preparation_grant_valid_until: str,
    clock: Clock,
    created: dict[str, bool],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with _locked_registry(preparation_binding) as context:
        context.reconcile_custody_attempt()
        has_grant = context.exists(_GRANT_NAME)
        has_claim = context.exists(_CLAIM_NAME)
    if not has_claim:
        if has_grant:
            with _locked_registry(preparation_binding) as context:
                retained_grant = context.load_grant()
            if (
                retained_grant.get("valid_until") != preparation_grant_valid_until
                or retained_grant.get("owner_principal") != owner_principal
                or object_dict(retained_grant.get("repository"))
                != {"commit": repository_commit, "tree": repository_tree}
            ):
                raise AV1V4R4PreparationFlowError(
                    "retained preparation grant conflicts with the request"
                )
            created["preparation_grant"] = False
        else:
            authorized = _whole_second(clock)
            publication = publish_av1_v4r4_preparation_grant(
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
        custody_result = consume_av1_v4r4_preparation_grant(
            binding=preparation_binding,
            rights_attestation=rights_attestation,
            clock=clock,
        )
        created["preparation_custody"] = True
        return (
            dict(custody_result.grant),
            dict(custody_result.claim),
            dict(custody_result.key_custody),
        )
    grant, claim, custody = _load_consumed_preparation(
        preparation_binding,
        repository_commit=repository_commit,
        repository_tree=repository_tree,
    )
    created["preparation_grant"] = False
    created["preparation_custody"] = False
    return grant, claim, custody


def _load_consumed_preparation(
    binding: AV1V4R4PreparationRegistryBinding,
    *,
    repository_commit: str,
    repository_tree: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with _locked_registry(binding) as context:
        grant = context.load_grant()
        claim = context.read(_CLAIM_NAME)
        custody = context.read(_CUSTODY_NAME)
        try:
            claim_payload = deserialize_av1_v4r4_preparation_claim(claim)
            custody_payload = deserialize_av1_v4r4_path_privacy_key_custody(custody)
        except AV1V4R4PreparationError as exc:
            raise AV1V4R4PreparationFlowError(
                "retained preparation custody is invalid"
            ) from exc
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        expected_repository = {"commit": repository_commit, "tree": repository_tree}
        if any(
            object_dict(payload.get("repository")) != expected_repository
            for payload in (grant, claim_payload, custody_payload)
        ):
            raise AV1V4R4PreparationFlowError(
                "retained preparation grant conflicts with the request"
            )
        return dict(grant), dict(claim_payload), dict(custody_payload)


def _ensure_preparation_bundle(
    *,
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    repository_commit: str,
    repository_tree: str,
    source_paths: Mapping[str, str],
    dedicated_instance_paths: Mapping[str, str],
    quality_temp_paths: Mapping[str, str],
    tool_paths: Mapping[str, Path],
    clock: Clock,
) -> _PreparationBundleResult:
    with _locked_registry(preparation_binding) as context:
        if context.exists(_MEASUREMENT_NAME):
            try:
                measurement = deserialize_av1_v4r4_preparation_measurement(
                    context.read(_MEASUREMENT_NAME)
                )
                assert_av1_v4r4_preparation_measurement(measurement)
                if measurement.get("state") != "terminal_success":
                    raise AV1V4R4PreparationFlowError("preparation is terminally failed")
                bundle = deserialize_av1_v4r4_preparation_bundle(
                    context.read(_BUNDLE_NAME)
                )
                assert_av1_v4r4_preparation_bundle(bundle)
                config = deserialize_av1_v4r4_effective_config_snapshot(
                    context.read(_CONFIG_NAME)
                )
                key = context.read(_KEY_NAME)
            except AV1V4R4PreparationError as exc:
                raise AV1V4R4PreparationFlowError(
                    "retained preparation is invalid"
                ) from exc
            if (
                config.get("source_paths") != dict(source_paths)
                or config.get("dedicated_instance_paths") != dict(dedicated_instance_paths)
                or config.get("quality_temp_paths") != dict(quality_temp_paths)
            ):
                raise AV1V4R4PreparationFlowError(
                    "prepared private path bindings do not match"
                )
            try:
                assert_av1_v4r4_preparation_bundle_private_invocations(
                    preparation_bundle=bundle,
                    effective_config=config,
                    path_privacy_key=key,
                )
            except AV1V4R4PreparationError as exc:
                raise AV1V4R4PreparationFlowError(
                    "prepared invocation bindings do not match"
                ) from exc
            return _PreparationBundleResult(
                bundle=dict(bundle), measurement=dict(measurement), created=False
            )

        started_at = _clock_timestamp(clock)
        if context.exists(_ATTEMPT_NAME):
            _recover_interrupted_preparation_attempt(
                context=context,
                grant=grant,
                claim=claim,
                custody=custody,
                completed_at=started_at,
            )
            raise AV1V4R4PreparationFlowError(
                "preparation attempt was already started"
            )
        context.write_exclusive(
            _ATTEMPT_NAME,
            _attempt_bytes(
                grant=grant,
                claim=claim,
                custody=custody,
                started_at=started_at,
            ),
        )
        completed: list[str] = ["validate_custody_chain"]
        probes: list[dict[str, Any]] = []
        config_created = False
        bundle_created = False
        key = context.read(_KEY_NAME)
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        if custody.get("key_id") != av1_v4r4_path_privacy_key_id(key):
            raise AV1V4R4PreparationFlowError("path key custody is invalid")
        try:
            source_digests = _reverify_source_digests(source_paths)
            completed.append("reverify_source_digests")
            config = build_av1_v4r4_effective_config_snapshot(
                repository_commit=repository_commit,
                repository_tree=repository_tree,
                source_paths=source_paths,
                dedicated_instance_paths=dedicated_instance_paths,
                quality_temp_paths=quality_temp_paths,
            )
            completed.append("build_effective_config")
            live_commit, live_tree = _repository_identity(preparation_binding.repository_root)
            if (live_commit, live_tree) != (repository_commit, repository_tree):
                raise AV1V4R4PreparationFlowError(
                    "live repository identity does not match the grant"
                )
            completed.append("measure_repository")
            tool_handles = _open_tool_binaries(tool_paths)
            completed.append("hash_toolchain")
            try:
                probes = _probe_tool_versions(grant, tool_handles)
                completed.append("probe_toolchain")
                runtime = {
                    "python_implementation": platform.python_implementation(),
                    "python_version": platform.python_version(),
                    "platform_system": platform.system(),
                    "platform_machine": platform.machine(),
                }
                bundle = build_av1_v4r4_preparation_bundle(
                    preparation_grant=grant,
                    preparation_claim=claim,
                    key_custody=custody,
                    effective_config=config,
                    path_privacy_key=key,
                    toolchain={
                        name: {
                            "binary_sha256": tool_handles[name].binary_sha256,
                            "version": probe["version"],
                        }
                        for name, probe in zip(
                            ("ab_av1", "ffmpeg", "ffprobe"),
                            probes,
                            strict=True,
                        )
                    },
                    runtime=runtime,
                    source_digests=source_digests,
                )
                assert_av1_v4r4_preparation_bundle_private_invocations(
                    preparation_bundle=bundle,
                    effective_config=config,
                    path_privacy_key=key,
                )
            finally:
                _close_tool_binaries(tool_handles)
            completed.append("derive_private_identities")
            completed.append("build_prepared_bundle")
            context.write_exclusive(_CONFIG_NAME, serialize_av1_v4r4_effective_config_snapshot(config))
            config_created = True
            context.write_exclusive(_BUNDLE_NAME, serialize_av1_v4r4_preparation_bundle(bundle))
            bundle_created = True
            completed.append("publish")
            measurement = build_av1_v4r4_preparation_success_measurement(
                preparation_grant=grant,
                preparation_claim=claim,
                key_custody=custody,
                preparation_bundle=bundle,
                started_at=started_at,
                completed_at=_clock_timestamp(clock),
                probes=probes,
            )
            context.write_exclusive(
                _MEASUREMENT_NAME,
                serialize_av1_v4r4_preparation_measurement(measurement),
            )
            context.unlink_owned(_ATTEMPT_NAME, [])
            return _PreparationBundleResult(
                bundle=dict(bundle), measurement=dict(measurement), created=True
            )
        except BaseException as exc:
            rollback_errors: list[OSError] = []
            removed_artifacts: list[str] = []
            if bundle_created:
                prior_error_count = len(rollback_errors)
                context.unlink_owned(_BUNDLE_NAME, rollback_errors)
                if len(rollback_errors) == prior_error_count:
                    removed_artifacts.append("preparation_bundle")
            if config_created:
                prior_error_count = len(rollback_errors)
                context.unlink_owned(_CONFIG_NAME, rollback_errors)
                if len(rollback_errors) == prior_error_count:
                    removed_artifacts.append("effective_config")
            failure_stage = _failure_stage(completed)
            failure = build_av1_v4r4_preparation_failure_measurement(
                preparation_grant=grant,
                preparation_claim=claim,
                started_at=started_at,
                completed_at=_clock_timestamp(clock),
                probes=probes,
                stages_completed=tuple(completed),
                failure_stage=failure_stage,
                reason_code="rollback_incomplete" if rollback_errors else "stage_failed",
                error_class=type(exc).__name__,
                message_sha256="sha256:" + hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest(),
                rollback={
                    "removed_artifacts": removed_artifacts,
                    "retained_artifacts": [
                        name for name, flag in (("preparation_claim", True), ("path_privacy_key", True), ("key_custody", True)) if flag
                    ],
                },
                path_privacy_key_id=(
                    str(custody["key_id"]) if isinstance(custody.get("key_id"), str) else None
                ),
            )
            if not context.exists(_MEASUREMENT_NAME):
                context.write_exclusive(
                    _MEASUREMENT_NAME,
                    serialize_av1_v4r4_preparation_measurement(failure),
                )
            context.unlink_owned(_ATTEMPT_NAME, [])
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, AV1V4R4PreparationFlowError):
                raise
            raise AV1V4R4PreparationFlowError("preparation attempt terminated") from exc


def _ensure_owner_freeze(
    *,
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    rights_attestation: Mapping[str, Any],
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    bundle: Mapping[str, Any],
    measurement: Mapping[str, Any],
    owner_principal: str,
    materializer_repository_commit: str,
    materializer_repository_tree: str,
    clock: Clock,
) -> tuple[dict[str, Any], bool]:
    with _locked_registry(preparation_binding) as context:
        if context.exists(_FREEZE_NAME):
            existing_bytes = context.read(_FREEZE_NAME)
            existing = deserialize_av1_v4r4_owner_freeze(existing_bytes)
            expected = build_av1_v4r4_owner_freeze(
                rights_attestation=rights_attestation,
                preparation_grant=grant,
                preparation_claim=claim,
                key_custody=custody,
                preparation_bundle=bundle,
                preparation_measurement=measurement,
                owner_principal=owner_principal,
                decided_at=str(existing["decided_at"]),
                materializer_repository_commit=materializer_repository_commit,
                materializer_repository_tree=materializer_repository_tree,
            )
            if existing_bytes != serialize_av1_v4r4_owner_freeze(expected):
                raise AV1V4R4PreparationFlowError(
                    "owner freeze conflicts with the registry singleton"
                )
            return dict(existing), False
        decided_at = _clock_timestamp(clock)
        freeze = build_av1_v4r4_owner_freeze(
            rights_attestation=rights_attestation,
            preparation_grant=grant,
            preparation_claim=claim,
            key_custody=custody,
            preparation_bundle=bundle,
            preparation_measurement=measurement,
            owner_principal=owner_principal,
            decided_at=decided_at,
            materializer_repository_commit=materializer_repository_commit,
            materializer_repository_tree=materializer_repository_tree,
        )
        context.write_exclusive(_FREEZE_NAME, serialize_av1_v4r4_owner_freeze(freeze))
        context.assert_file_custody(_FREEZE_NAME)
        return dict(freeze), True


def _ensure_qualification_request(
    *,
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    freeze: Mapping[str, Any],
    owner_principal: str,
    requesting_repository_commit: str,
    requesting_repository_tree: str,
    clock: Clock,
) -> tuple[dict[str, Any], bool]:
    with _locked_registry(preparation_binding) as context:
        if context.exists(_REQUEST_NAME):
            existing_bytes = context.read(_REQUEST_NAME)
            existing = deserialize_av1_v4r4_qualification_request(existing_bytes)
            expected = build_av1_v4r4_qualification_request(
                owner_freeze=freeze,
                owner_principal=owner_principal,
                requested_at=str(existing["requested_at"]),
                valid_until=str(existing["valid_until"]),
                requesting_repository_commit=requesting_repository_commit,
                requesting_repository_tree=requesting_repository_tree,
            )
            if existing_bytes != serialize_av1_v4r4_qualification_request(expected):
                raise AV1V4R4PreparationFlowError(
                    "qualification request conflicts with the registry singleton"
                )
            return dict(existing), False
        requested_at = _clock_timestamp(clock)
        request = build_av1_v4r4_qualification_request(
            owner_freeze=freeze,
            owner_principal=owner_principal,
            requested_at=requested_at,
            valid_until=av1_v4r4_qualification_request_valid_until(requested_at),
            requesting_repository_commit=requesting_repository_commit,
            requesting_repository_tree=requesting_repository_tree,
        )
        context.write_exclusive(
            _REQUEST_NAME, serialize_av1_v4r4_qualification_request(request)
        )
        context.assert_file_custody(_REQUEST_NAME)
        return dict(request), True


def _ensure_plan(
    *,
    ordinal_binding: AV1V4R4OrdinalRegistryBinding,
    request: Mapping[str, Any],
    ordinal_1_valid_until: str,
    clock: Clock,
) -> tuple[dict[str, Any], bool]:
    initialize_av1_v4r4_ordinal_registry(ordinal_binding.registry)
    retained_plan = load_av1_v4r4_ordinal_registry_plan(ordinal_binding)
    opens_at = (
        _parse_timestamp(str(retained_plan["plan_opens_at"]))
        if retained_plan is not None
        else _whole_second(clock)
    )
    valid_until = _parse_timestamp(ordinal_1_valid_until)
    if valid_until <= opens_at:
        raise AV1V4R4PreparationFlowError("ordinal-1 sequencing window is invalid")
    plan_closes_at = str(request["valid_until"])
    if valid_until > _parse_timestamp(plan_closes_at):
        raise AV1V4R4PreparationFlowError(
            "ordinal-1 sequencing window exceeds the preparation request"
        )
    plan = build_av1_v4r4_ordinal_registry_plan(
        registry_id=ordinal_binding.registry_id,
        plan_opens_at=_format_ts(opens_at),
        plan_closes_at=plan_closes_at,
    )
    try:
        published = publish_av1_v4r4_ordinal_registry_plan_with_status(
            binding=ordinal_binding,
            plan=plan,
        )
    except AV1V4R4OrdinalRegistryError as exc:
        raise AV1V4R4PreparationFlowError("ordinal registry plan publication failed") from exc
    return dict(published.plan), published.created


def _ensure_execution_preflight(
    *,
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    request: Mapping[str, Any],
    freeze: Mapping[str, Any],
    owner_principal: str,
    ordinal_registry_id: str,
    plan: Mapping[str, Any],
    preflight_repository_commit: str,
    preflight_repository_tree: str,
    clock: Clock,
) -> tuple[dict[str, Any], bool]:
    with _locked_registry(preparation_binding) as context:
        if context.exists(_PREFLIGHT_NAME):
            existing_bytes = context.read(_PREFLIGHT_NAME)
            existing = deserialize_av1_v4r4_execution_preflight(existing_bytes)
            expected = build_av1_v4r4_execution_preflight(
                qualification_request=request,
                owner_freeze=freeze,
                owner_principal=owner_principal,
                ordinal_registry_id=ordinal_registry_id,
                plan=plan,
                preflight_repository_commit=preflight_repository_commit,
                preflight_repository_tree=preflight_repository_tree,
                created_at=str(existing["created_at"]),
            )
            if existing_bytes != serialize_av1_v4r4_execution_preflight(expected):
                raise AV1V4R4PreparationFlowError(
                    "execution preflight conflicts with the registry singleton"
                )
            return dict(existing), False
        created_at = _clock_timestamp(clock)
        preflight = build_av1_v4r4_execution_preflight(
            qualification_request=request,
            owner_freeze=freeze,
            owner_principal=owner_principal,
            ordinal_registry_id=ordinal_registry_id,
            plan=plan,
            preflight_repository_commit=preflight_repository_commit,
            preflight_repository_tree=preflight_repository_tree,
            created_at=created_at,
        )
        context.write_exclusive(
            _PREFLIGHT_NAME, serialize_av1_v4r4_execution_preflight(preflight)
        )
        context.assert_file_custody(_PREFLIGHT_NAME)
        return dict(preflight), True


def _ordinal_binding(
    preparation_binding: AV1V4R4PreparationRegistryBinding,
    ordinal_registry: Path,
) -> AV1V4R4OrdinalRegistryBinding:
    resolved_registry = ordinal_registry.resolve(strict=False)
    with _locked_registry(preparation_binding) as context:
        custody = deserialize_av1_v4r4_path_privacy_key_custody(
            context.read(_CUSTODY_NAME)
        )
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        key = context.read(_KEY_NAME)
        if custody.get("key_id") != av1_v4r4_path_privacy_key_id(key):
            raise AV1V4R4PreparationFlowError("path key custody is invalid")
        return av1_v4r4_ordinal_registry_binding(resolved_registry, key=key)


def _attempt_bytes(
    *,
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    started_at: str,
) -> bytes:
    payload = {
        "schema": "mediaforce.av1_cold_start_v4r4_preparation_attempt_started",
        "schema_version": 1,
        "state": "attempt_started",
        "grant_id": grant["grant_id"],
        "grant_payload_sha256": grant["payload_sha256"],
        "claim_id": claim["claim_id"],
        "claim_payload_sha256": claim["payload_sha256"],
        "custody_id": custody["custody_id"],
        "custody_payload_sha256": custody["payload_sha256"],
        "started_at": started_at,
    }
    return canonical_json_bytes(payload) + b"\n"


def _recover_interrupted_preparation_attempt(
    *,
    context: Any,
    grant: Mapping[str, Any],
    claim: Mapping[str, Any],
    custody: Mapping[str, Any],
    completed_at: str,
) -> None:
    if context.exists(_MEASUREMENT_NAME):
        return
    rollback_errors: list[OSError] = []
    removed_artifacts: list[str] = []
    for filename, label in (
        (_CONFIG_NAME, "effective_config"),
        (_BUNDLE_NAME, "preparation_bundle"),
    ):
        if context.exists(filename):
            prior_error_count = len(rollback_errors)
            context.unlink_owned(filename, rollback_errors)
            if len(rollback_errors) == prior_error_count:
                removed_artifacts.append(label)
    failure = build_av1_v4r4_preparation_failure_measurement(
        preparation_grant=grant,
        preparation_claim=claim,
        started_at=_started_at_from_attempt(context.read(_ATTEMPT_NAME)),
        completed_at=completed_at,
        probes=(),
        stages_completed=("validate_custody_chain",),
        failure_stage="reverify_source_digests",
        reason_code="partial_restart",
        error_class="PartialRestart",
        message_sha256="sha256:" + hashlib.sha256(b"partial_restart").hexdigest(),
        rollback={
            "removed_artifacts": removed_artifacts,
            "retained_artifacts": ["preparation_claim", "path_privacy_key", "key_custody"],
        },
        path_privacy_key_id=(
            str(custody["key_id"]) if isinstance(custody.get("key_id"), str) else None
        ),
    )
    context.write_exclusive(
        _MEASUREMENT_NAME,
        serialize_av1_v4r4_preparation_measurement(failure),
    )
    context.unlink_owned(_ATTEMPT_NAME, [])


def _started_at_from_attempt(data: bytes) -> str:
    try:
        payload = json.loads(data)
    except Exception as exc:
        raise AV1V4R4PreparationFlowError("preparation attempt marker is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema")
        != "mediaforce.av1_cold_start_v4r4_preparation_attempt_started"
        or payload.get("state") != "attempt_started"
        or not isinstance(payload.get("started_at"), str)
    ):
        raise AV1V4R4PreparationFlowError("preparation attempt marker is invalid")
    return str(payload["started_at"])


def _reverify_source_digests(source_paths: Mapping[str, str]) -> dict[str, str]:
    expected = {
        asset_id: str(spec["media_sha256"])
        for asset_id, spec in AV1_V4R4_PREPARATION_SOURCE_SPECS.items()
    }
    if set(source_paths) != set(expected):
        raise AV1V4R4PreparationFlowError("source path set is invalid")
    observed: dict[str, str] = {}
    for asset_id, expected_digest in expected.items():
        observed_digest = _sha256_file(Path(source_paths[asset_id]))
        if observed_digest != expected_digest:
            raise AV1V4R4PreparationFlowError("source digest re-verification failed")
        observed[asset_id] = observed_digest
    return observed


def _open_tool_binaries(tool_paths: Mapping[str, Path]) -> dict[str, _ToolBinary]:
    expected = ("ab_av1", "ffmpeg", "ffprobe")
    if set(tool_paths) != set(expected):
        raise AV1V4R4PreparationFlowError("tool path set is invalid")
    handles: dict[str, _ToolBinary] = {}
    descriptor: int | None = None
    try:
        for name in expected:
            descriptor = os.open(Path(tool_paths[name]), os.O_RDONLY | _no_follow_flag())
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or metadata.st_mode & 0o111 == 0
            ):
                raise AV1V4R4PreparationFlowError("tool binary custody is invalid")
            handles[name] = _ToolBinary(
                path=Path(tool_paths[name]),
                descriptor=descriptor,
                metadata=metadata,
                binary_sha256=_hash_open_file(descriptor, metadata, "tool binary"),
            )
            descriptor = None
    except BaseException as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        for handle in handles.values():
            try:
                os.close(handle.descriptor)
            except OSError:
                pass
        if isinstance(exc, AV1V4R4PreparationFlowError):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise AV1V4R4PreparationFlowError("tool binary is unavailable") from exc
    return handles


def _close_tool_binaries(handles: Mapping[str, _ToolBinary]) -> None:
    errors: list[OSError] = []
    for handle in handles.values():
        try:
            os.close(handle.descriptor)
        except OSError as exc:
            errors.append(exc)
    if errors:
        raise AV1V4R4PreparationFlowError("tool binary custody close failed")


def _probe_tool_versions(
    grant: Mapping[str, Any],
    tool_handles: Mapping[str, _ToolBinary],
) -> list[dict[str, Any]]:
    probes: list[dict[str, Any]] = []
    scope = object_dict(grant.get("operation_scope"))
    argv_map = object_dict(AV1_V4R4_PREPARATION_METHOD["tool_version_probe_argv"])
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        if scope.get(f"{name}_version_probe_argv") != argv_map[name]:
            raise AV1V4R4PreparationFlowError("tool probe scope is invalid")
    for name in ("ab_av1", "ffmpeg", "ffprobe"):
        argv = list(argv_map[name])
        handle = tool_handles[name]
        version = _probe_open_tool_version(handle, argv)
        probes.append(
            {
                "tool": name,
                "argv": argv,
                "version": version,
                "binary_sha256": handle.binary_sha256,
            }
        )
        if _hash_open_file(handle.descriptor, handle.metadata, "tool binary") != handle.binary_sha256:
            raise AV1V4R4PreparationFlowError("tool binary changed during probing")
        try:
            path_metadata = os.stat(handle.path, follow_symlinks=False)
        except OSError as exc:
            raise AV1V4R4PreparationFlowError("tool binary changed during probing") from exc
        if not _same_file_snapshot(handle.metadata, path_metadata):
            raise AV1V4R4PreparationFlowError("tool binary changed during probing")
    return probes


def _probe_open_tool_version(handle: _ToolBinary, argv: list[str]) -> str:
    with TemporaryDirectory(prefix="mediaforce-v4r4-tool-probe-") as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        metadata = directory.stat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AV1V4R4PreparationFlowError("tool snapshot directory custody is invalid")
        snapshot = directory / "tool"
        descriptor = os.open(
            snapshot,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o500,
        )
        try:
            os.fchmod(descriptor, 0o500)
            os.lseek(handle.descriptor, 0, os.SEEK_SET)
            while chunk := os.read(handle.descriptor, 64 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short tool snapshot write")
                    view = view[written:]
            os.fsync(descriptor)
            written_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(written_metadata.st_mode)
                or stat.S_IMODE(written_metadata.st_mode) != 0o500
                or written_metadata.st_uid != os.geteuid()
                or written_metadata.st_nlink != 1
            ):
                raise AV1V4R4PreparationFlowError("tool snapshot custody is invalid")
            os.close(descriptor)
            descriptor = -1
            descriptor = os.open(snapshot, os.O_RDONLY | _no_follow_flag())
            snapshot_metadata = os.fstat(descriptor)
            if (
                not _same_file_snapshot(written_metadata, snapshot_metadata)
                or _hash_open_file(descriptor, snapshot_metadata, "tool snapshot")
                != handle.binary_sha256
            ):
                raise AV1V4R4PreparationFlowError("tool snapshot custody is invalid")
            version = _probe_tool_version_path(snapshot, argv)
            try:
                path_metadata = os.stat(snapshot, follow_symlinks=False)
            except OSError as exc:
                raise AV1V4R4PreparationFlowError(
                    "tool snapshot changed during probing"
                ) from exc
            if (
                not _same_file_snapshot(snapshot_metadata, path_metadata)
                or _hash_open_file(descriptor, snapshot_metadata, "tool snapshot")
                != handle.binary_sha256
            ):
                raise AV1V4R4PreparationFlowError(
                    "tool snapshot changed during probing"
                )
            return version
        except OSError as exc:
            raise AV1V4R4PreparationFlowError("tool snapshot creation failed") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def _probe_tool_version_path(path: Path, argv: list[str]) -> str:
    try:
        result = subprocess.run(
            [str(path), *argv],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AV1V4R4PreparationFlowError("tool version probe failed") from exc
    output = result.stdout or result.stderr
    lines = output.splitlines()
    if result.returncode != 0 or not lines or not lines[0].strip():
        raise AV1V4R4PreparationFlowError("tool version probe failed")
    return lines[0].strip()


def _repository_identity(repository_root: Path) -> tuple[str, str]:
    git = Path("/usr/bin/git")
    if not git.is_file():
        raise AV1V4R4PreparationFlowError("repository measurement tool is unavailable")
    try:
        identity = subprocess.run(
            [str(git), "rev-parse", "--show-toplevel", "HEAD", "HEAD^{tree}"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = subprocess.run(
            [str(git), "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        raise AV1V4R4PreparationFlowError("repository must be clean and canonical") from exc
    values = identity.stdout.splitlines()
    if (
        identity.returncode != 0
        or status.returncode != 0
        or status.stdout
        or len(values) != 3
        or Path(values[0]).resolve() != Path(repository_root).resolve()
    ):
        raise AV1V4R4PreparationFlowError("repository must be clean and canonical")
    return values[1].strip(), values[2].strip()


def _assert_registry_layout(
    *,
    repository_root: Path,
    preparation_registry: Path,
    ordinal_registry: Path,
) -> None:
    try:
        repository = repository_root.resolve(strict=True)
        preparation = preparation_registry.resolve(strict=False)
        ordinal = ordinal_registry.resolve(strict=False)
    except OSError as exc:
        raise AV1V4R4PreparationFlowError("registry layout is invalid") from exc
    if any(
        _paths_overlap(first, second)
        for first, second in (
            (repository, preparation),
            (repository, ordinal),
            (preparation, ordinal),
        )
    ):
        raise AV1V4R4PreparationFlowError(
            "registries must be distinct and outside the repository"
        )


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _sha256_file(path: Path) -> str:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | _no_follow_flag())
    except OSError as exc:
        raise AV1V4R4PreparationFlowError("source digest re-verification failed") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise AV1V4R4PreparationFlowError(
                "source digest re-verification failed"
            )
        digest = _hash_open_file(descriptor, metadata, "source")
        path_metadata = os.stat(path, follow_symlinks=False)
        if not _same_file_snapshot(metadata, path_metadata):
            raise AV1V4R4PreparationFlowError(
                "source digest re-verification failed"
            )
        return digest
    except OSError as exc:
        raise AV1V4R4PreparationFlowError("source digest re-verification failed") from exc
    finally:
        os.close(descriptor)


def _hash_open_file(descriptor: int, expected: os.stat_result, label: str) -> str:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 64 * 1024):
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise AV1V4R4PreparationFlowError(f"{label} measurement failed") from exc
    if not _same_file_snapshot(expected, after):
        raise AV1V4R4PreparationFlowError(f"{label} changed during measurement")
    return f"sha256:{digest.hexdigest()}"


def _same_file_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_mode == second.st_mode
        and first.st_uid == second.st_uid
        and first.st_gid == second.st_gid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
        and first.st_nlink == second.st_nlink
    )


def _no_follow_flag() -> int:
    try:
        return os.O_NOFOLLOW
    except AttributeError as exc:
        raise AV1V4R4PreparationFlowError("O_NOFOLLOW is required") from exc


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _whole_second(clock: Clock) -> datetime:
    value = clock().astimezone(UTC)
    if value.microsecond != 0:
        raise AV1V4R4PreparationFlowError("clock must use whole seconds")
    return value


def _format_ts(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AV1V4R4PreparationFlowError("timestamp is invalid") from exc


def _failure_stage(completed: list[str]) -> str:
    ordered = [
        "validate_custody_chain",
        "reverify_source_digests",
        "build_effective_config",
        "measure_repository",
        "hash_toolchain",
        "probe_toolchain",
        "derive_private_identities",
        "build_prepared_bundle",
        "publish",
    ]
    index = min(len(completed), len(ordered) - 1)
    return ordered[index]
