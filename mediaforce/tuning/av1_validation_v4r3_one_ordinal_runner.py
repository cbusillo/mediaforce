"""Owner-invoked one-ordinal production qualification for AV1 v4 revision 3."""

from __future__ import annotations

import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.quality import (
    QualitySearchError,
    QualitySearchResult,
    QualitySearchWarmStart,
    SampleEncodeError,
)
from mediaforce.encoding.streams import ProductionStreamPlan
from mediaforce.tuning.av1_validation_v4 import AV1_VALIDATION_V4_SOURCE_IDS
from mediaforce.tuning.av1_validation_v4_qualification_search import (
    V4QualificationContractError,
    V4QualificationOperationResult,
    run_v4_qualification_search,
)
from mediaforce.tuning.av1_validation_v4r3_execution_preflight_operation import (
    _assert_distinct_registry_bindings,
    _load_preparation_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_invocation_closure import (
    AV1_V4_R3_FULL_VIDEO_POLICY,
    AV1_V4_R3_SOURCE_MANIFEST_FACTS,
    av1_v4_r3_quality_temp_hmac_id,
    av1_v4_r3_quality_temp_key_id,
    av1_v4_r3_transform_plan_payload,
    build_av1_v4_r3_all_closure_payloads,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window import (
    AV1_V4R3_OW_FAILURE_SEARCH_STATUSES,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    AV1V4R3OrdinalWindowRegistryBinding,
    _execution_claim_name,
    publish_av1_v4r3_ordinal_window_outcome,
    publish_av1_v4r3_ordinal_window_terminal,
    publish_av1_v4r3_runner_admission_and_started,
)
from mediaforce.tuning.av1_validation_v4r3_ordinal_window_registry import (
    _locked_registry as _locked_ordinal_registry,
)
from mediaforce.tuning.av1_validation_v4r3_path_privacy import (
    AV1_V4R3_INSTANCE_PATH_ROLES,
    av1_v4r3_instance_path_hmac_id,
    av1_v4r3_path_privacy_key_id,
    av1_v4r3_source_path_hmac_id,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_bundle import (
    av1_v4r3_effective_config_hmac_id,
    deserialize_av1_v4r3_preparation_bundle,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_config import (
    deserialize_av1_v4r3_effective_config_snapshot,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    _BUNDLE_NAME,
    _CONFIG_NAME,
    _KEY_NAME,
    AV1V4R3PreparationCustodyRegistryBinding,
)
from mediaforce.tuning.av1_validation_v4r3_preparation_custody_registry import (
    _locked_registry as _locked_preparation_registry,
)
from mediaforce.tuning.av1_validation_v4r3_rights import (
    assert_av1_v4r3_rights_attestation,
)
from mediaforce.tuning.size_goals import size_goal_from_policy
from mediaforce.tuning.stream_budget import (
    StreamBudgetLedger,
    resolve_stream_budget_ledger,
)
from mediaforce.tuning.target_size_search import TargetSizeSearchError

Clock = Callable[[], datetime]
SearchQualityForSource = Callable[..., QualitySearchResult]

_SOURCE_MEDIA_SHA256 = {
    "av1v4_animation_primary_sintel": (
        "sha256:97f1dbc66231df42ad49bd8c29aa174b8f48933058e47e7157d4ba63d93a8efa"
    ),
    "av1v4_animation_confirmation_cosmos_laundromat": (
        "sha256:96ace08a4d61878cfb7cc8b850b7f5aa7ba304de45017eaf832ccd3d52893130"
    ),
    "av1v4_live_action_primary_tears_of_steel": (
        "sha256:bd2b5bc6c16d4085034f47ef7e4b3938afe86b4eec4ac3cf2685367d3b0b23b0"
    ),
    "av1v4_live_action_confirmation_nasa_earth_views": (
        "sha256:184b9ebaf6624b219455b0d4a72967e05d3e6cebc034ac8036fcc0198a17a8ef"
    ),
}


class AV1V4R3OneOrdinalRunnerError(RuntimeError):
    """Raised with path-free text after a one-ordinal attempt fails."""

    def __init__(
        self,
        message: str,
        *,
        failure_phase: str | None = None,
        failure_class: str | None = None,
        failure_search_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_phase = failure_phase
        self.failure_class = failure_class
        self.failure_search_status = failure_search_status


class _ExecutionAuthorityError(RuntimeError):
    def __init__(self, *, burned: bool) -> None:
        super().__init__("execution authority is unavailable")
        self.burned = burned


@dataclass(frozen=True, slots=True)
class AV1V4R3OneOrdinalResult:
    ordinal: int
    asset_id: str
    success: bool
    admission: Mapping[str, Any]
    started: Mapping[str, Any]
    outcome: Mapping[str, Any]
    public_summary: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class _PrivateRuntime:
    source_path: Path
    quality_temp_path: Path
    source_path_hmac_id: str
    instance_path_hmac_ids: Mapping[str, str]
    quality_temp_hmac_id: str
    quality_temp_key_id: str
    stream_plan: ProductionStreamPlan
    stream_budget_ledger: StreamBudgetLedger


@dataclass(frozen=True, slots=True)
class _FailureClassification:
    phase: str
    failure_class: str
    search_status: str | None = None


def run_av1_v4r3_one_ordinal(
    *,
    preparation_binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    rights_attestation: Mapping[str, Any],
    owner_principal: str,
    confirmed_owner_principal: str,
    ordinal: int,
    search_quality_for_source: SearchQualityForSource,
    clock: Clock = lambda: datetime.now(UTC),
) -> AV1V4R3OneOrdinalResult:
    """Consume the already-burned authority and execute exactly one ordinal."""

    if (
        not isinstance(owner_principal, str)
        or not isinstance(confirmed_owner_principal, str)
        or not hmac.compare_digest(owner_principal, confirmed_owner_principal)
    ):
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal owner confirmation is invalid"
        )
    if type(ordinal) is not int or not 1 <= ordinal <= 8:
        raise AV1V4R3OneOrdinalRunnerError("AV1 v4 r3 one-ordinal selection is invalid")
    if not callable(search_quality_for_source) or not callable(clock):
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal runtime dependency is invalid"
        )
    plan: Mapping[str, Any] | None = None
    try:
        assert_av1_v4r3_rights_attestation(rights_attestation)
        _assert_distinct_registry_bindings(preparation_binding, ordinal_binding)
        plan = _load_registry_plan(ordinal_binding)
        sequencing_grant, execution_grant, execution_claim = _load_execution_authority(
            ordinal_binding=ordinal_binding,
            plan=plan,
            owner_principal=owner_principal,
            ordinal=ordinal,
        )
        _load_preparation_snapshot(
            preparation_binding=preparation_binding,
            ordinal_binding=ordinal_binding,
            rights_attestation=rights_attestation,
            owner_principal=owner_principal,
        )
        runtime = _load_private_runtime(preparation_binding, ordinal)
        closure = build_av1_v4_r3_all_closure_payloads()[ordinal - 1]
        if (
            closure["asset_id"] != execution_grant["asset_id"]
            or closure["closure_id"] != execution_grant["closure_id"]
        ):
            raise AV1V4R3OneOrdinalRunnerError(
                "AV1 v4 r3 one-ordinal frozen invocation is invalid"
            )
        transform_plan_id = str(av1_v4_r3_transform_plan_payload()["transform_plan_id"])
        publication = publish_av1_v4r3_runner_admission_and_started(
            binding=ordinal_binding,
            plan=plan,
            sequencing_grant=sequencing_grant,
            execution_grant=execution_grant,
            execution_claim=execution_claim,
            source_path_hmac_id=runtime.source_path_hmac_id,
            instance_path_hmac_ids=runtime.instance_path_hmac_ids,
            quality_temp_hmac_id=runtime.quality_temp_hmac_id,
            quality_temp_key_id=runtime.quality_temp_key_id,
            production_stream_plan=runtime.stream_plan.to_payload(),
            stream_budget_ledger=runtime.stream_budget_ledger.to_payload(),
            transform_plan_id=transform_plan_id,
            returned_stream_budget_ledger=runtime.stream_budget_ledger.to_payload(),
            clock=clock,
        )
    except _ExecutionAuthorityError as exc:
        if plan is not None and exc.burned:
            _seal_terminal(ordinal_binding, plan, ordinal, clock)
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal execution authority is unavailable"
        ) from exc
    except AV1V4R3OneOrdinalRunnerError:
        if plan is not None:
            _seal_terminal(ordinal_binding, plan, ordinal, clock)
        raise
    except Exception as exc:
        if plan is not None:
            _seal_terminal(ordinal_binding, plan, ordinal, clock)
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal admission failed"
        ) from exc

    failure: _FailureClassification | None = None
    failure_exception: BaseException | None = None
    try:
        search_result = _execute_search(
            closure=closure,
            runtime=runtime,
            search_quality_for_source=search_quality_for_source,
        )
    except BaseException as exc:
        failure = _classify_failure(exc, phase="production_search")
        failure_exception = exc
    if failure is None:
        try:
            _assert_returned_runtime_identity(search_result, publication.admission)
        except BaseException as exc:
            failure = _classify_failure(exc, phase="runtime_identity")
            failure_exception = exc
    if failure is not None:
        try:
            publish_av1_v4r3_ordinal_window_outcome(
                binding=ordinal_binding,
                plan=plan,
                started=publication.started,
                clock=clock,
                success=False,
                failure_phase=failure.phase,
                failure_class=failure.failure_class,
                failure_search_status=failure.search_status,
            )
        except Exception as terminal_exc:
            _seal_terminal(ordinal_binding, plan, ordinal, clock)
            raise AV1V4R3OneOrdinalRunnerError(
                "AV1 v4 r3 one-ordinal failure could not be sealed"
            ) from terminal_exc
        if isinstance(failure_exception, (KeyboardInterrupt, SystemExit)):
            raise failure_exception
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal execution failed",
            failure_phase=failure.phase,
            failure_class=failure.failure_class,
            failure_search_status=failure.search_status,
        ) from failure_exception

    try:
        outcome = publish_av1_v4r3_ordinal_window_outcome(
            binding=ordinal_binding,
            plan=plan,
            started=publication.started,
            clock=clock,
            success=True,
        )
    except Exception as exc:
        _seal_terminal(ordinal_binding, plan, ordinal, clock)
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal success could not be sealed"
        ) from exc
    return AV1V4R3OneOrdinalResult(
        ordinal=ordinal,
        asset_id=str(closure["asset_id"]),
        success=True,
        admission=publication.admission,
        started=publication.started,
        outcome=outcome,
        public_summary=search_result.public_summary,
    )


def _classify_failure(exc: BaseException, *, phase: str) -> _FailureClassification:
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return _FailureClassification(phase=phase, failure_class="interrupted")
    if phase == "runtime_identity":
        return _FailureClassification(phase=phase, failure_class="runtime_identity_error")
    if isinstance(exc, TargetSizeSearchError):
        return _FailureClassification(
            phase=phase,
            failure_class="target_size_search_error",
            search_status=(
                exc.status if exc.status in AV1_V4R3_OW_FAILURE_SEARCH_STATUSES else None
            ),
        )
    if isinstance(exc, SampleEncodeError):
        failure_class = "sample_encode_error"
    elif isinstance(exc, V4QualificationContractError):
        failure_class = "qualification_contract_error"
    elif isinstance(exc, QualitySearchError):
        failure_class = "quality_search_error"
    else:
        failure_class = "unexpected_error"
    return _FailureClassification(phase=phase, failure_class=failure_class)


def _load_execution_authority(
    *,
    ordinal_binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    owner_principal: str,
    ordinal: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with _locked_ordinal_registry(ordinal_binding.registry) as context:
        context.assert_no_terminal()
        canonical_plan, _preflight = context.load_registry_plan_preflight_pair()
        if canonical_plan != plan:
            raise _ExecutionAuthorityError(burned=False)
        try:
            sequencing_grant = context.load_grant(ordinal)
            execution_grant = context.load_execution_grant(ordinal)
        except (OSError, ValueError) as exc:
            raise _ExecutionAuthorityError(burned=False) from exc
        claim_name = _execution_claim_name(str(execution_grant["grant_id"]))
        burned = context.exists(claim_name)
        if not burned:
            raise _ExecutionAuthorityError(burned=False)
        try:
            execution_claim = context.load_execution_claim(
                str(execution_grant["grant_id"])
            )
        except (OSError, ValueError) as exc:
            raise _ExecutionAuthorityError(burned=True) from exc
        context.assert_file_custody(claim_name)
        if execution_grant.get("owner_principal") != owner_principal:
            raise _ExecutionAuthorityError(burned=True)
        return sequencing_grant, execution_grant, execution_claim


def _load_registry_plan(
    binding: AV1V4R3OrdinalWindowRegistryBinding,
) -> dict[str, Any]:
    with _locked_ordinal_registry(binding.registry) as context:
        plan, _preflight = context.load_registry_plan_preflight_pair()
        return plan


def _load_private_runtime(
    binding: AV1V4R3PreparationCustodyRegistryBinding,
    ordinal: int,
) -> _PrivateRuntime:
    closure = build_av1_v4_r3_all_closure_payloads()[ordinal - 1]
    asset_id = str(closure["asset_id"])
    with _locked_preparation_registry(binding) as context:
        context.assert_file_custody(_CONFIG_NAME)
        context.assert_file_custody(_BUNDLE_NAME)
        context.assert_file_custody(_KEY_NAME, expected_size=32)
        config = deserialize_av1_v4r3_effective_config_snapshot(
            context.read(_CONFIG_NAME)
        )
        bundle = deserialize_av1_v4r3_preparation_bundle(context.read(_BUNDLE_NAME))
        key = context.read(_KEY_NAME)
    if av1_v4r3_path_privacy_key_id(key) != bundle["path_privacy_key_id"]:
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal path key binding is invalid"
        )
    if (
        av1_v4r3_effective_config_hmac_id(key, config)
        != bundle["effective_config_hmac_id"]
    ):
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal effective config binding is invalid"
        )
    source_paths = object_dict(config["source_paths"])
    instance_paths = object_dict(config["dedicated_instance_paths"])
    quality_temp_paths = object_dict(config["quality_temp_paths"])
    source_path_hmac_id = av1_v4r3_source_path_hmac_id(
        key,
        asset_id=asset_id,
        normalized_path=str(source_paths[asset_id]),
    )
    instance_path_hmac_ids = {
        role: av1_v4r3_instance_path_hmac_id(
            key,
            role=role,
            normalized_path=str(instance_paths[role]),
        )
        for role in sorted(AV1_V4R3_INSTANCE_PATH_ROLES)
    }
    quality_temp_hmac_id = av1_v4_r3_quality_temp_hmac_id(
        key,
        asset_id=asset_id,
        normalized_path=str(quality_temp_paths[asset_id]),
    )
    if (
        source_path_hmac_id != object_dict(bundle["source_path_hmac_ids"])[asset_id]
        or instance_path_hmac_ids != object_dict(bundle["dedicated_instance_hmac_ids"])
        or quality_temp_hmac_id
        != object_dict(bundle["quality_temp_hmac_ids"])[asset_id]
    ):
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal private path binding is invalid"
        )
    stream_plan, ledger = _build_runtime_ledger(asset_id)
    return _PrivateRuntime(
        source_path=Path(str(source_paths[asset_id])),
        quality_temp_path=Path(str(quality_temp_paths[asset_id])),
        source_path_hmac_id=source_path_hmac_id,
        instance_path_hmac_ids=instance_path_hmac_ids,
        quality_temp_hmac_id=quality_temp_hmac_id,
        quality_temp_key_id=av1_v4_r3_quality_temp_key_id(key),
        stream_plan=stream_plan,
        stream_budget_ledger=ledger,
    )


def _build_runtime_ledger(
    asset_id: str,
) -> tuple[ProductionStreamPlan, StreamBudgetLedger]:
    facts = AV1_V4_R3_SOURCE_MANIFEST_FACTS[asset_id]
    item = {
        "library_item_id": asset_id,
        "source_fingerprint": _SOURCE_MEDIA_SHA256[asset_id],
        "source_size_bytes": facts["media_bytes"],
        "duration_seconds": facts["duration_seconds"],
        "video_bitrate": None,
        "resolved_policy": {
            "video": dict(AV1_V4_R3_FULL_VIDEO_POLICY),
            "audio": {},
            "subtitle": {},
        },
        "output_container": "mkv",
        "audio_summary": [],
        "subtitle_summary": [],
        "attachment_summary": [],
    }
    size_goal = size_goal_from_policy(dict(AV1_V4_R3_FULL_VIDEO_POLICY)).resolve(
        float(facts["duration_seconds"])
    )
    ledger = resolve_stream_budget_ledger(
        item,
        output_container="mkv",
        resolved_size_goal=size_goal,
        prefer_persisted=False,
    )
    return ledger.stream_plan, ledger


def _execute_search(
    *,
    closure: Mapping[str, Any],
    runtime: _PrivateRuntime,
    search_quality_for_source: SearchQualityForSource,
) -> V4QualificationOperationResult:
    configuration = str(closure["configuration"])
    mode = "baseline" if configuration == "balanced_full_search_baseline" else "guided"
    warm_payload = closure.get("warm_start")
    warm_start = None
    if warm_payload is not None:
        warm = object_dict(warm_payload)
        warm_start = QualitySearchWarmStart(
            requested_crf=float(warm["requested_crf"]),
            candidate_crf=int(warm["candidate_crf"]),
            search_signature_id=str(warm["search_signature_id"]),
            cohort_id=str(warm["cohort_id"]),
            source=str(warm["source"]),
            confidence=warm.get("confidence"),
            provenance_id=warm.get("provenance_id"),
            review_risks=tuple(str(item) for item in object_list(warm["review_risks"])),
        )
    facts = object_dict(closure["source_facts"])
    return run_v4_qualification_search(
        search_quality_for_source,
        runtime.source_path,
        dict(AV1_V4_R3_FULL_VIDEO_POLICY),
        mode=mode,
        stream_budget_ledger=runtime.stream_budget_ledger,
        warm_start=warm_start,
        extra_search_kwargs={
            "width": facts["width"],
            "height": facts["height"],
            "source_codec": facts["codec"],
            "quality_temp_dir": runtime.quality_temp_path,
        },
    )


def _assert_returned_runtime_identity(
    result: V4QualificationOperationResult,
    admission: Mapping[str, Any],
) -> None:
    trace = object_dict(result.quality_result.target_size_trace)
    ledger = object_dict(trace.get("ledger"))
    transform = object_dict(trace.get("transform_plan"))
    expected = (
        (ledger.get("ledger_id"), admission.get("stream_budget_ledger_id")),
        (ledger.get("source_id"), admission.get("production_source_id")),
        (ledger.get("stream_plan_id"), admission.get("production_stream_plan_id")),
        (transform.get("transform_plan_id"), admission.get("transform_plan_id")),
    )
    if any(left != right for left, right in expected):
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal returned runtime identity is invalid"
        )


def _seal_terminal(
    binding: AV1V4R3OrdinalWindowRegistryBinding,
    plan: Mapping[str, Any],
    ordinal: int,
    clock: Clock,
) -> None:
    try:
        with _locked_ordinal_registry(binding.registry) as context:
            if (
                context.exists(f"ordinal_{ordinal:02d}.outcome.json")
                and context.load_outcome(ordinal).get("success") is True
            ):
                return
        publish_av1_v4r3_ordinal_window_terminal(
            binding=binding,
            plan=plan,
            clock=clock,
        )
    except Exception as exc:
        raise AV1V4R3OneOrdinalRunnerError(
            "AV1 v4 r3 one-ordinal terminal seal failed"
        ) from exc


if set(_SOURCE_MEDIA_SHA256) != set(AV1_VALIDATION_V4_SOURCE_IDS):
    raise RuntimeError("AV1 v4 r3 runner source fingerprint set is incomplete")
