from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    av1_validation_v4_contains_private_text,
)
from mediaforce.tuning.av1_validation_v4_qualification_authority import (
    AV1ValidationV4QualificationAuthorityError,
    begin_av1_validation_v4_qualification_run,
    assert_av1_validation_v4_qualification_claim_custody,
    assert_av1_validation_v4_qualification_claim,
    assert_av1_validation_v4_qualification_grant_active,
    assert_av1_validation_v4_qualification_grant_for_request,
    assert_av1_validation_v4_qualification_request,
    load_av1_validation_v4_qualification_claim,
)


AV1_VALIDATION_V4_TRAVERSAL_OUTCOME_SCHEMA = (
    "mediaforce.av1_cold_start_v4_traversal_outcome"
)
AV1_VALIDATION_V4_TRAVERSAL_OUTCOME_SCHEMA_VERSION = 1

AV1_VALIDATION_V4_RUN_OUTCOME_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_run_outcome"
)
AV1_VALIDATION_V4_RUN_OUTCOME_SCHEMA_VERSION = 1

_SHA256_RE_STR = r"sha256:[0-9a-f]{64}"


class AV1ValidationV4QualificationRunnerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4TraversalSpec:
    ordinal: int
    asset_id: str
    configuration: str
    mode: str
    invocation_sha256: str


@dataclass(frozen=True, slots=True)
class AV1ValidationV4TraversalCallbackResult:
    success: bool
    public_summary: Mapping[str, Any]


TraversalCallback = Callable[
    [AV1ValidationV4TraversalSpec],
    AV1ValidationV4TraversalCallbackResult,
]


def av1_validation_v4_traversal_specs_from_request(
    request: Mapping[str, Any],
) -> tuple[AV1ValidationV4TraversalSpec, ...]:
    """Extract the ordered nonsecret traversal specs bound by a request."""
    try:
        assert_av1_validation_v4_qualification_request(request)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification request is invalid"
        ) from exc
    invocations = [
        object_dict(item) for item in object_list(request.get("invocations"))
    ]
    if len(invocations) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 preparation record must contain exactly "
            f"{AV1_VALIDATION_V4_TRAVERSAL_COUNT} invocations"
        )
    result = []
    for inv in invocations:
        ordinal = inv.get("ordinal")
        asset_id = str(inv.get("asset_id") or "")
        configuration = str(inv.get("configuration") or "")
        mode = str(inv.get("mode") or "")
        sha256 = str(inv.get("sha256") or "")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or asset_id not in AV1_VALIDATION_V4_SOURCE_IDS
            or configuration not in AV1_VALIDATION_V4_CONFIGURATIONS
            or mode not in ("baseline", "guided")
            or re.fullmatch(_SHA256_RE_STR, sha256) is None
        ):
            raise AV1ValidationV4QualificationRunnerError(
                f"AV1 v4 preparation invocation at ordinal {ordinal!r} is invalid"
            )
        result.append(AV1ValidationV4TraversalSpec(
            ordinal=int(ordinal),
            asset_id=asset_id,
            configuration=configuration,
            mode=mode,
            invocation_sha256=sha256,
        ))
    return tuple(result)


def run_av1_validation_v4_qualification(
    *,
    request: Mapping[str, Any],
    grant: Mapping[str, Any],
    registry: Path,
    callback: TraversalCallback,
) -> dict[str, Any]:
    """
    Simulate eight sequential qualification traversals via injected callback.

    The grant must be active at the claim's claimed_at timestamp. The claim
    must bind the grant. On the first callback failure the runner stops; later
    ordinals are never invoked. The terminal run outcome is always
    non-comparable when any traversal fails.

    Returns a run outcome artifact (dict) with all AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS
    set to False. No retry, resume, substitution, backfill, or adaptive continuation
    is authorized.
    """
    req = object_dict(request)
    grt = object_dict(grant)
    if not registry.is_absolute() or str(registry) != req.get("consumption_registry"):
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner registry does not bind the request"
        )
    grant_id = str(grt.get("grant_id") or "")
    claim_path = registry / f"{grant_id}.claim.json"
    try:
        assert_av1_validation_v4_qualification_claim_custody(claim_path)
        clm = load_av1_validation_v4_qualification_claim(claim_path)
        assert_av1_validation_v4_qualification_claim_custody(claim_path)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner requires a consumed registry claim"
        ) from exc

    # Validate chain
    claimed_at = str(clm.get("claimed_at") or "")
    try:
        assert_av1_validation_v4_qualification_grant_for_request(
            request=req,
            grant=grt,
        )
        assert_av1_validation_v4_qualification_grant_active(grt, as_of=claimed_at)
        assert_av1_validation_v4_qualification_claim(clm)
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner received an invalid grant or claim"
        ) from exc
    if (
        grt.get("request_id") != req.get("request_id")
        or grt.get("request_payload_sha256") != req.get("payload_sha256")
        or grt.get("owner_principal") != req.get("owner_principal")
        or clm.get("request_id") != req.get("request_id")
        or clm.get("request_payload_sha256") != req.get("payload_sha256")
        or clm.get("grant_id") != grt.get("grant_id")
        or clm.get("grant_payload_sha256") != grt.get("payload_sha256")
        or clm.get("owner_principal") != req.get("owner_principal")
    ):
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner request, grant, and claim do not bind"
        )

    specs = list(av1_validation_v4_traversal_specs_from_request(req))
    ordinals = [spec.ordinal for spec in specs]
    if ordinals != list(range(1, AV1_VALIDATION_V4_TRAVERSAL_COUNT + 1)):
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner traversal specs must be in ordinal order 1..8"
        )

    try:
        begin_av1_validation_v4_qualification_run(
            registry=registry,
            request=req,
            grant=grt,
            claim=clm,
        )
    except AV1ValidationV4QualificationAuthorityError as exc:
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification runner claim has already started"
        ) from exc

    traversal_outcomes: list[dict[str, Any]] = []
    failed_at_ordinal: int | None = None

    for spec in specs:
        if failed_at_ordinal is not None:
            break
        cb_result = callback(spec)
        _assert_public_summary(cb_result.public_summary)
        outcome_type = "success" if cb_result.success else "failure"
        traversal_outcome: dict[str, Any] = {
            "schema": AV1_VALIDATION_V4_TRAVERSAL_OUTCOME_SCHEMA,
            "schema_version": AV1_VALIDATION_V4_TRAVERSAL_OUTCOME_SCHEMA_VERSION,
            "ordinal": spec.ordinal,
            "asset_id": spec.asset_id,
            "configuration": spec.configuration,
            "mode": spec.mode,
            "invocation_sha256": spec.invocation_sha256,
            "outcome": outcome_type,
            "public_summary": dict(object_dict(cb_result.public_summary)),
        }
        traversal_outcome.update(
            {field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS}
        )
        traversal_outcomes.append(
            json.loads(canonical_json_bytes(traversal_outcome))
        )
        if not cb_result.success:
            failed_at_ordinal = spec.ordinal

    all_succeeded = failed_at_ordinal is None and len(traversal_outcomes) == AV1_VALIDATION_V4_TRAVERSAL_COUNT
    run_outcome_type = (
        "success_all_traversals" if all_succeeded else "failed_partial_non_comparable"
    )

    run_outcome: dict[str, Any] = {
        "schema": AV1_VALIDATION_V4_RUN_OUTCOME_SCHEMA,
        "schema_version": AV1_VALIDATION_V4_RUN_OUTCOME_SCHEMA_VERSION,
        "claim_id": clm.get("claim_id"),
        "claim_payload_sha256": clm.get("payload_sha256"),
        "grant_id": grt.get("grant_id"),
        "traversal_count": AV1_VALIDATION_V4_TRAVERSAL_COUNT,
        "traversals_attempted": len(traversal_outcomes),
        "failed_at_ordinal": failed_at_ordinal,
        "outcome": run_outcome_type,
        "traversal_outcomes": traversal_outcomes,
    }
    run_outcome.update(
        {field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS}
    )
    return json.loads(canonical_json_bytes(run_outcome))


def _assert_public_summary(payload: Mapping[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if set(value) & AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS:
                raise AV1ValidationV4QualificationRunnerError(
                    "AV1 v4 qualification public summary contains authority fields"
                )
            for key, child in value.items():
                if isinstance(key, str) and av1_validation_v4_contains_private_text(key):
                    raise AV1ValidationV4QualificationRunnerError(
                        "AV1 v4 qualification public summary exposes machine-local text"
                    )
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        elif isinstance(value, str) and av1_validation_v4_contains_private_text(value):
            raise AV1ValidationV4QualificationRunnerError(
                "AV1 v4 qualification public summary exposes machine-local text"
            )

    if not isinstance(payload, Mapping):
        raise AV1ValidationV4QualificationRunnerError(
            "AV1 v4 qualification public summary must be a mapping"
        )
    visit(payload)
