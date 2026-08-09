"""Owner-only execution-grant publication and claim custody for AV1 v4 r3."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
from typing import Any

from mediaforce.tuning.av1_validation_v4r3_execution_grant import (
    AV1V4R3ExecutionGrantError,
    assert_av1_v4r3_execution_grant_chain,
    build_av1_v4r3_execution_grant,
    deserialize_av1_v4r3_execution_grant,
    serialize_av1_v4r3_execution_grant,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight_operation import (
    AV1V4R3ExecutionPreflightOperationError,
    _assert_distinct_registry_bindings,
    _load_preparation_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1V4R3OrdinalWindowError,
    AV1_V4R3_OW_ORDINAL_COUNT,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    AV1V4R3OrdinalWindowRegistryError,
    _assert_binding_matches_plan,
    _execution_claim_name,
    _execution_grant_name,
    _format_ts,
    _locked_registry,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    AV1V4R3PreparationCustodyRegistryBinding,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_operation import (
    _measure_clean_repository,
)
from mediaforce.tuning.av1_validation_v4r3_runner_admission import (
    AV1V4R3RunnerAdmissionError,
    assert_av1_v4r3_execution_claim_chain,
    build_av1_v4r3_execution_claim,
    deserialize_av1_v4r3_execution_claim,
    serialize_av1_v4r3_execution_claim,
)


Clock = Callable[[], datetime]


class AV1V4R3ExecutionCustodyError(RuntimeError):
    """Raised when owner-only execution custody fails."""


@dataclass(frozen=True, slots=True)
class AV1V4R3ExecutionCustodyResult:
    grant: Mapping[str, Any]
    claim: Mapping[str, Any]
    grant_created: bool


def claim_av1_v4r3_execution_grant(
    *,
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    confirmed_owner_principal: str,
    ordinal: int,
    valid_until: str,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3ExecutionCustodyResult:
    """Publish one canonical execution grant and irreversibly claim it."""

    if (
        not isinstance(owner_principal, str)
        or not isinstance(confirmed_owner_principal, str)
        or not hmac.compare_digest(owner_principal, confirmed_owner_principal)
    ):
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody owner confirmation is invalid"
        )
    if type(ordinal) is not int or not 1 <= ordinal <= AV1_V4R3_OW_ORDINAL_COUNT:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody ordinal is invalid"
        )
    if not callable(clock):
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody clock is invalid"
        )
    try:
        _assert_distinct_registry_bindings(preparation_binding, ordinal_binding)
    except AV1V4R3ExecutionPreflightOperationError as exc:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody registries must be distinct"
        ) from exc
    try:
        snapshot = _load_preparation_snapshot(
            preparation_binding=preparation_binding,
            ordinal_binding=ordinal_binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
        )
    except AV1V4R3ExecutionPreflightOperationError as exc:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody preparation chain is invalid"
        ) from exc

    try:
        with _locked_registry(ordinal_binding.registry) as context:
            context.assert_no_terminal()
            plan, preflight = context.load_registry_plan_preflight_pair()
            _assert_binding_matches_plan(ordinal_binding, plan)
            repository_commit, repository_tree = _measure_clean_repository(
                preparation_binding.repository_root
            )
            repository = {"commit": repository_commit, "tree": repository_tree}
            if not (
                repository
                == {
                    "commit": snapshot.repository_commit,
                    "tree": snapshot.repository_tree,
                }
                == plan.get("r3_repository")
                == preflight.get("reviewed_repository")
            ):
                raise AV1V4R3ExecutionCustodyError(
                    "AV1 v4 r3 execution custody repository binding is invalid"
                )
            if preflight.get("owner_principal") != owner_principal:
                raise AV1V4R3ExecutionCustodyError(
                    "AV1 v4 r3 execution custody owner binding is invalid"
                )
            sequencing_grant = context.load_grant(ordinal)
            grant_time = _clock_now(clock)
            context.assert_next_ordinal_admissible(plan, ordinal, grant_time)
            context.assert_grant_open_for_admission(sequencing_grant, grant_time)
            grant, grant_created = _load_or_publish_execution_grant(
                context=context,
                plan=plan,
                preflight=preflight,
                sequencing_grant=sequencing_grant,
                owner_principal=owner_principal,
                ordinal=ordinal,
                authorized_at=_format_ts(grant_time),
                valid_until=valid_until,
            )
            claim_name = _execution_claim_name(str(grant["grant_id"]))
            if context.exists(claim_name):
                raise AV1V4R3ExecutionCustodyError(
                    "AV1 v4 r3 execution grant has already been consumed"
                )
            burn_time = _clock_now(clock)
            context.assert_next_ordinal_admissible(plan, ordinal, burn_time)
            context.assert_grant_open_for_admission(sequencing_grant, burn_time)
            claim = build_av1_v4r3_execution_claim(
                execution_grant=grant,
                sequencing_grant=sequencing_grant,
                claimed_at=_format_ts(burn_time),
            )
            claim_bytes = serialize_av1_v4r3_execution_claim(claim)
            try:
                context.write_burn(claim_name, claim_bytes)
                retained_bytes = context.read(claim_name)
                retained_claim = deserialize_av1_v4r3_execution_claim(retained_bytes)
                assert_av1_v4r3_execution_claim_chain(
                    execution_claim=retained_claim,
                    execution_grant=grant,
                    sequencing_grant=sequencing_grant,
                )
                if retained_bytes != claim_bytes:
                    raise AV1V4R3ExecutionCustodyError(
                        "AV1 v4 r3 execution claim custody does not match"
                    )
            except Exception as exc:
                if context.exists(claim_name):
                    raise AV1V4R3ExecutionCustodyError(
                        "AV1 v4 r3 execution custody failed after consuming the grant"
                    ) from exc
                raise
            return AV1V4R3ExecutionCustodyResult(
                grant=grant,
                claim=claim,
                grant_created=grant_created,
            )
    except AV1V4R3ExecutionCustodyError:
        raise
    except (
        AV1V4R3OrdinalWindowRegistryError,
        AV1V4R3OrdinalWindowError,
        AV1V4R3ExecutionGrantError,
        AV1V4R3RunnerAdmissionError,
        OSError,
        ValueError,
    ) as exc:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody operation failed"
        ) from exc


def _clock_now(clock: Clock) -> datetime:
    current = clock()
    if current.tzinfo is None or current.utcoffset() is None:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution custody clock must be timezone-aware"
        )
    return current.astimezone(UTC).replace(microsecond=0)


def _load_or_publish_execution_grant(
    *,
    context: Any,
    plan: Mapping[str, Any],
    preflight: Mapping[str, Any],
    sequencing_grant: Mapping[str, Any],
    owner_principal: str,
    ordinal: int,
    authorized_at: str,
    valid_until: str,
) -> tuple[dict[str, Any], bool]:
    filename = _execution_grant_name(ordinal)
    if context.exists(filename):
        existing_bytes = context.read(filename)
        existing = deserialize_av1_v4r3_execution_grant(existing_bytes)
        expected = build_av1_v4r3_execution_grant(
            plan=plan,
            preflight=preflight,
            sequencing_grant=sequencing_grant,
            owner_principal=owner_principal,
            authorized_at=str(existing["authorized_at"]),
            valid_until=valid_until,
        )
        if existing_bytes != serialize_av1_v4r3_execution_grant(expected):
            raise AV1V4R3ExecutionCustodyError(
                "AV1 v4 r3 execution grant registry artifact conflicts"
            )
        assert_av1_v4r3_execution_grant_chain(
            plan=plan,
            preflight=preflight,
            sequencing_grant=sequencing_grant,
            execution_grant=existing,
        )
        return existing, False
    grant = build_av1_v4r3_execution_grant(
        plan=plan,
        preflight=preflight,
        sequencing_grant=sequencing_grant,
        owner_principal=owner_principal,
        authorized_at=authorized_at,
        valid_until=valid_until,
    )
    grant_bytes = serialize_av1_v4r3_execution_grant(grant)
    context.write(filename, grant_bytes)
    retained_bytes = context.read(filename)
    retained = deserialize_av1_v4r3_execution_grant(retained_bytes)
    assert_av1_v4r3_execution_grant_chain(
        plan=plan,
        preflight=preflight,
        sequencing_grant=sequencing_grant,
        execution_grant=retained,
    )
    if retained_bytes != grant_bytes:
        raise AV1V4R3ExecutionCustodyError(
            "AV1 v4 r3 execution grant custody does not match"
        )
    return retained, True
