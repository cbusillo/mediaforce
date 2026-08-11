"""Owner-only execution authority issuance for AV1 protocol-v4 revision 4."""

from __future__ import annotations

import hmac
from contextlib import suppress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
from mediaforce.tuning.av1_validation_v4r4_contract import (
    AV1_V4R4_EXPERIMENT_ID,
    AV1_V4R4_MANIFEST_ID,
    AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
    AV1_V4R4_MANIFEST_REVISION,
    AV1_V4R4_POLICY_VALUES_SHA256,
    AV1_V4R4_PROTOCOL_VERSION,
    av1_v4r4_ordinal_layout,
)
from mediaforce.tuning.av1_validation_v4r4_execution_authority import (
    AV1_V4R4_EXECUTION_CLAIM_CONTRACT_VERSION,
    AV1_V4R4_EXECUTION_CLAIM_SCHEMA,
    AV1_V4R4_EXECUTION_CLAIM_SCHEMA_VERSION,
    AV1_V4R4_EXECUTION_GRANT_CONTRACT_VERSION,
    AV1_V4R4_EXECUTION_GRANT_SCHEMA,
    AV1_V4R4_EXECUTION_GRANT_SCHEMA_VERSION,
    AV1_V4R4_EXECUTION_GRANTED_AUTHORITY_FIELDS,
    assert_av1_v4r4_execution_chain,
    assert_av1_v4r4_execution_claim,
    assert_av1_v4r4_execution_grant,
    assert_av1_v4r4_execution_preparation_chain_active,
    av1_v4r4_execution_authority_payload_sha256,
    av1_v4r4_execution_claim_public_id,
    av1_v4r4_execution_grant_public_id,
    deserialize_av1_v4r4_execution_claim,
    deserialize_av1_v4r4_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r4_ordinal_registry import (
    AV1V4R4OrdinalRegistryBinding,
    _TERMINAL_NAME,
    _admission_name,
    _assert_binding_matches_plan,
    _claim_name,
    _execution_claim_name,
    _execution_grant_name,
    _format_ts,
    _locked_registry,
    _outcome_name,
    _started_name,
)
from mediaforce.tuning.av1_validation_v4r4_preparation_flow import (
    AV1V4R4PreparationFlowError,
    _repository_identity,
)


Clock = Callable[[], datetime]


class AV1V4R4ExecutionIssuerError(RuntimeError):
    """Raised when owner-only execution authority issuance fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R4ExecutionGrantIssuance:
    grant: Mapping[str, Any]
    created: bool


@dataclass(frozen=True, slots=True)
class AV1V4R4ExecutionClaimIssuance:
    claim: Mapping[str, Any]
    created: bool


def issue_av1_v4r4_execution_grant(
    *,
    repository_root: Path,
    binding: AV1V4R4OrdinalRegistryBinding,
    owner_principal: str,
    confirmed_owner_principal: str,
    ordinal: int,
    confirmed_ordinal: int,
    valid_until: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R4ExecutionGrantIssuance:
    _assert_confirmations(
        owner_principal=owner_principal,
        confirmed_owner_principal=confirmed_owner_principal,
        ordinal=ordinal,
        confirmed_ordinal=confirmed_ordinal,
    )
    if not isinstance(valid_until, str) or not valid_until:
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer grant window is invalid"
        )
    repository = _live_repository(repository_root)
    try:
        with _locked_registry(binding.registry) as context:
            context.assert_supported_artifacts()
            if context.exists(_TERMINAL_NAME):
                raise AV1V4R4ExecutionIssuerError(
                    "AV1 v4 r4 execution issuer registry is sealed"
                )
            plan = context.load_plan()
            _assert_binding_matches_plan(binding, plan)
            request, preflight = context.load_preparation(plan)
            sequencing_grant = context.load_grant(ordinal)
            _assert_owner_repository(
                owner_principal=owner_principal,
                repository=repository,
                qualification_request=request,
                execution_preflight=preflight,
            )
            now = context.read_clock(clock)
            filename = _execution_grant_name(ordinal)
            if context.exists(filename):
                retained_bytes = context.read(filename)
                retained = deserialize_av1_v4r4_execution_grant(retained_bytes)
                expected = _build_execution_grant(
                    qualification_request=request,
                    execution_preflight=preflight,
                    plan=plan,
                    sequencing_grant=sequencing_grant,
                    owner_principal=owner_principal,
                    authorized_at=str(retained["authorized_at"]),
                    valid_until=valid_until,
                )
                if retained_bytes != _serialize_execution_grant(expected):
                    raise AV1V4R4ExecutionIssuerError(
                        "AV1 v4 r4 execution issuer grant conflicts"
                    )
                assert_av1_v4r4_execution_preparation_chain_active(
                    qualification_request=request,
                    execution_preflight=preflight,
                    plan=plan,
                    sequencing_grant=sequencing_grant,
                    execution_grant=retained,
                    now=now,
                )
                return AV1V4R4ExecutionGrantIssuance(grant=retained, created=False)
            if context.exists(_claim_name(ordinal)) or context.exists(
                _execution_claim_name(ordinal)
            ):
                raise AV1V4R4ExecutionIssuerError(
                    "AV1 v4 r4 execution issuer grant is already consumed"
                )
            grant = _build_execution_grant(
                qualification_request=request,
                execution_preflight=preflight,
                plan=plan,
                sequencing_grant=sequencing_grant,
                owner_principal=owner_principal,
                authorized_at=_format_ts(now),
                valid_until=valid_until,
            )
            assert_av1_v4r4_execution_preparation_chain_active(
                qualification_request=request,
                execution_preflight=preflight,
                plan=plan,
                sequencing_grant=sequencing_grant,
                execution_grant=grant,
                now=now,
            )
            grant_bytes = _serialize_execution_grant(grant)
            context.write(filename, grant_bytes)
            retained_bytes = context.read(filename)
            retained = deserialize_av1_v4r4_execution_grant(retained_bytes)
            if retained_bytes != grant_bytes:
                raise AV1V4R4ExecutionIssuerError(
                    "AV1 v4 r4 execution issuer grant custody does not match"
                )
            return AV1V4R4ExecutionGrantIssuance(grant=retained, created=True)
    except AV1V4R4ExecutionIssuerError:
        raise
    except Exception as exc:
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer grant operation failed"
        ) from exc


def issue_av1_v4r4_execution_claim(
    *,
    repository_root: Path,
    binding: AV1V4R4OrdinalRegistryBinding,
    owner_principal: str,
    confirmed_owner_principal: str,
    ordinal: int,
    confirmed_ordinal: int,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R4ExecutionClaimIssuance:
    _assert_confirmations(
        owner_principal=owner_principal,
        confirmed_owner_principal=confirmed_owner_principal,
        ordinal=ordinal,
        confirmed_ordinal=confirmed_ordinal,
    )
    repository = _live_repository(repository_root)
    try:
        with _locked_registry(binding.registry) as context:
            context.assert_supported_artifacts()
            if context.exists(_TERMINAL_NAME):
                raise AV1V4R4ExecutionIssuerError(
                    "AV1 v4 r4 execution issuer registry is sealed"
                )
            plan = context.load_plan()
            _assert_binding_matches_plan(binding, plan)
            request, preflight = context.load_preparation(plan)
            sequencing_grant = context.load_grant(ordinal)
            sequencing_claim = context.load_claim(ordinal)
            execution_grant = context.load_execution_grant(ordinal)
            _assert_owner_repository(
                owner_principal=owner_principal,
                repository=repository,
                qualification_request=request,
                execution_preflight=preflight,
            )
            now = context.read_clock(clock)
            filename = _execution_claim_name(ordinal)
            if context.exists(filename):
                try:
                    retained_bytes = context.read(filename)
                    retained = deserialize_av1_v4r4_execution_claim(retained_bytes)
                    expected = _build_execution_claim(
                        plan=plan,
                        sequencing_claim=sequencing_claim,
                        execution_grant=execution_grant,
                        claimed_at=str(retained["claimed_at"]),
                    )
                    if retained_bytes != _serialize_execution_claim(expected):
                        raise AV1V4R4ExecutionIssuerError(
                            "AV1 v4 r4 execution issuer claim conflicts"
                        )
                except BaseException as exc:
                    with suppress(BaseException):
                        context.publish_terminal(plan, now)
                    raise AV1V4R4ExecutionIssuerError(
                        "AV1 v4 r4 execution issuer claim burn is invalid"
                    ) from exc
                assert_av1_v4r4_execution_chain(
                    qualification_request=request,
                    execution_preflight=preflight,
                    plan=plan,
                    sequencing_grant=sequencing_grant,
                    sequencing_claim=sequencing_claim,
                    execution_grant=execution_grant,
                    execution_claim=retained,
                    now=now,
                )
                return AV1V4R4ExecutionClaimIssuance(claim=retained, created=False)
            if any(
                context.exists(name)
                for name in (
                    _admission_name(ordinal),
                    _started_name(ordinal),
                    _outcome_name(ordinal),
                )
            ):
                context.publish_terminal(plan, now)
                raise AV1V4R4ExecutionIssuerError(
                    "AV1 v4 r4 execution issuer has downstream artifacts before the claim"
                )
            claim = _build_execution_claim(
                plan=plan,
                sequencing_claim=sequencing_claim,
                execution_grant=execution_grant,
                claimed_at=_format_ts(now),
            )
            assert_av1_v4r4_execution_chain(
                qualification_request=request,
                execution_preflight=preflight,
                plan=plan,
                sequencing_grant=sequencing_grant,
                sequencing_claim=sequencing_claim,
                execution_grant=execution_grant,
                execution_claim=claim,
                now=now,
            )
            claim_bytes = _serialize_execution_claim(claim)
            try:
                context.write_burn(filename, claim_bytes)
                retained_bytes = context.read(filename)
                retained = deserialize_av1_v4r4_execution_claim(retained_bytes)
                if retained_bytes != claim_bytes:
                    raise AV1V4R4ExecutionIssuerError(
                        "AV1 v4 r4 execution issuer claim custody does not match"
                    )
            except BaseException as exc:
                if context.exists(filename):
                    with suppress(BaseException):
                        context.publish_terminal(plan, now)
                    raise AV1V4R4ExecutionIssuerError(
                        "AV1 v4 r4 execution issuer failed after consuming the grant"
                    ) from exc
                raise
            return AV1V4R4ExecutionClaimIssuance(claim=retained, created=True)
    except AV1V4R4ExecutionIssuerError:
        raise
    except Exception as exc:
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer claim operation failed"
        ) from exc


def _build_execution_grant(
    *,
    qualification_request: Mapping[str, Any],
    execution_preflight: Mapping[str, Any],
    plan: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    owner_principal: str,
    authorized_at: str,
    valid_until: str,
) -> dict[str, Any]:
    ordinal = int(sequencing_grant["ordinal"])
    layout = av1_v4r4_ordinal_layout()[ordinal - 1]
    invocation = next(
        item
        for item in qualification_request["invocation_digests"]
        if item["ordinal"] == ordinal
    )
    payload = {
        "schema": AV1_V4R4_EXECUTION_GRANT_SCHEMA,
        "schema_version": AV1_V4R4_EXECUTION_GRANT_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_EXECUTION_GRANT_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        "owner_principal": owner_principal,
        "qualification_request_id": qualification_request["request_id"],
        "qualification_request_payload_sha256": qualification_request["payload_sha256"],
        "execution_preflight_id": execution_preflight["preflight_id"],
        "execution_preflight_payload_sha256": execution_preflight["payload_sha256"],
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "sequencing_grant_id": sequencing_grant["grant_id"],
        "sequencing_grant_payload_sha256": sequencing_grant["payload_sha256"],
        "prepared_invocation_sha256": invocation["invocation_sha256"],
        "ordinal": ordinal,
        "asset_id": layout["asset_id"],
        "content_class": layout["content_class"],
        "role": layout["role"],
        "configuration": layout["configuration"],
        "target_size_bytes": layout["target_size_bytes"],
        "source_cap_total_bytes": layout["source_cap_total_bytes"],
        "policy_values_sha256": AV1_V4R4_POLICY_VALUES_SHA256,
        "authorized_at": authorized_at,
        "valid_until": valid_until,
        **_authority_fields(),
    }
    payload["execution_grant_id"] = av1_v4r4_execution_grant_public_id(payload)
    payload["payload_sha256"] = av1_v4r4_execution_authority_payload_sha256(payload)
    assert_av1_v4r4_execution_grant(payload)
    return payload


def _build_execution_claim(
    *,
    plan: Mapping[str, Any],
    sequencing_claim: Mapping[str, Any],
    execution_grant: Mapping[str, Any],
    claimed_at: str,
) -> dict[str, Any]:
    payload = {
        "schema": AV1_V4R4_EXECUTION_CLAIM_SCHEMA,
        "schema_version": AV1_V4R4_EXECUTION_CLAIM_SCHEMA_VERSION,
        "contract_version": AV1_V4R4_EXECUTION_CLAIM_CONTRACT_VERSION,
        "protocol_version": AV1_V4R4_PROTOCOL_VERSION,
        "manifest_revision": AV1_V4R4_MANIFEST_REVISION,
        "experiment_id": AV1_V4R4_EXPERIMENT_ID,
        "manifest_id": AV1_V4R4_MANIFEST_ID,
        "manifest_payload_sha256": AV1_V4R4_MANIFEST_PAYLOAD_SHA256,
        "owner_principal": execution_grant["owner_principal"],
        "plan_id": plan["plan_id"],
        "plan_payload_sha256": plan["payload_sha256"],
        "sequencing_claim_id": sequencing_claim["claim_id"],
        "sequencing_claim_payload_sha256": sequencing_claim["payload_sha256"],
        "execution_grant_id": execution_grant["execution_grant_id"],
        "execution_grant_payload_sha256": execution_grant["payload_sha256"],
        "ordinal": execution_grant["ordinal"],
        "claimed_at": claimed_at,
        **_authority_fields(),
    }
    payload["execution_claim_id"] = av1_v4r4_execution_claim_public_id(payload)
    payload["payload_sha256"] = av1_v4r4_execution_authority_payload_sha256(payload)
    return payload


def _serialize_execution_grant(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_execution_grant(payload)
    return canonical_json_bytes(dict(payload)) + b"\n"


def _serialize_execution_claim(payload: Mapping[str, Any]) -> bytes:
    assert_av1_v4r4_execution_claim(payload)
    return canonical_json_bytes(dict(payload)) + b"\n"


def _authority_fields() -> dict[str, bool]:
    return {
        field: field in AV1_V4R4_EXECUTION_GRANTED_AUTHORITY_FIELDS
        for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    }


def _assert_confirmations(
    *,
    owner_principal: str,
    confirmed_owner_principal: str,
    ordinal: int,
    confirmed_ordinal: int,
) -> None:
    if (
        not isinstance(owner_principal, str)
        or not isinstance(confirmed_owner_principal, str)
        or not hmac.compare_digest(owner_principal, confirmed_owner_principal)
    ):
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer owner confirmation is invalid"
        )
    if (
        type(ordinal) is not int
        or type(confirmed_ordinal) is not int
        or ordinal != confirmed_ordinal
        or not 1 <= ordinal <= len(av1_v4r4_ordinal_layout())
    ):
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer ordinal confirmation is invalid"
        )


def _live_repository(repository_root: Path) -> dict[str, str]:
    if not isinstance(repository_root, Path) or not repository_root.is_absolute():
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer repository is invalid"
        )
    try:
        commit, tree = _repository_identity(repository_root)
    except AV1V4R4PreparationFlowError as exc:
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer repository is invalid"
        ) from exc
    return {"commit": commit, "tree": tree}


def _assert_owner_repository(
    *,
    owner_principal: str,
    repository: Mapping[str, str],
    qualification_request: Mapping[str, Any],
    execution_preflight: Mapping[str, Any],
) -> None:
    if (
        qualification_request["owner_principal"] != owner_principal
        or execution_preflight["owner_principal"] != owner_principal
        or qualification_request["reviewed_repository"] != repository
        or execution_preflight["reviewed_repository"] != repository
    ):
        raise AV1V4R4ExecutionIssuerError(
            "AV1 v4 r4 execution issuer identity binding is invalid"
        )
