from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
from typing import Any

from mediaforce.core.evidence import canonical_json_bytes, stable_json_hash
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.tuning.av1_validation_v4 import (
    AV1_VALIDATION_V4_CONFIGURATIONS,
    AV1_VALIDATION_V4_EXPERIMENT_ID,
    AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS,
    AV1_VALIDATION_V4_MANIFEST_REVISION,
    AV1_VALIDATION_V4_PROTOCOL_VERSION,
    AV1_VALIDATION_V4_SOURCE_IDS,
    AV1_VALIDATION_V4_TRAVERSAL_COUNT,
    av1_validation_v4_guided_warm_start_identities,
)
from mediaforce.tuning.av1_validation_v4_preparation_config import (
    assert_av1_validation_v4_effective_config_snapshot,
)


AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_SCHEMA = (
    "mediaforce.av1_cold_start_v4_production_execution_plan"
)
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_SCHEMA_VERSION = 1
AV1_VALIDATION_V4_PRODUCTION_EXECUTION_PLAN_CONTRACT_VERSION = "av1v4execplan1"
AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA = (
    "mediaforce.av1_cold_start_v4_qualification_search_invocation"
)
AV1_VALIDATION_V4_QUALIFICATION_SEARCH_CONTRACT_VERSION = "av1vq4s1"
AV1_VALIDATION_V4_QUALIFICATION_SOURCE = "av1_cold_start_v4_qualification"


class AV1ValidationV4ExecutionPlanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AV1ValidationV4ExecutionPlan:
    ordinal: int
    asset_id: str
    configuration: str
    mode: str
    source_path_hmac_id: str
    video_stream_index: int
    excluded_stream_indexes: tuple[int, ...]
    search_kwargs: Mapping[str, Any]
    warm_start: Mapping[str, Any] | None
    base_config_sha256: str
    expected_invocation_sha256: str
    preparation_invocation_sha256: str
    derived_invocation_sha256: str

    @property
    def digest_matches_request(self) -> bool:
        return self.derived_invocation_sha256 == self.expected_invocation_sha256

    @property
    def digest_matches_preparation(self) -> bool:
        return self.derived_invocation_sha256 == self.preparation_invocation_sha256

    @property
    def digest_matches_expected(self) -> bool:
        return self.digest_matches_request and self.digest_matches_preparation

    def public_payload(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "asset_id": self.asset_id,
            "configuration": self.configuration,
            "mode": self.mode,
            "source_path_hmac_id": self.source_path_hmac_id,
            "video_stream_index": self.video_stream_index,
            "excluded_stream_indexes": list(self.excluded_stream_indexes),
            "search_kwargs": deepcopy(dict(self.search_kwargs)),
            "warm_start": deepcopy(dict(self.warm_start)) if self.warm_start else None,
            "base_config_sha256": self.base_config_sha256,
            "expected_invocation_sha256": self.expected_invocation_sha256,
            "preparation_invocation_sha256": self.preparation_invocation_sha256,
            "derived_invocation_sha256": self.derived_invocation_sha256,
            "digest_matches_request": self.digest_matches_request,
            "digest_matches_preparation": self.digest_matches_preparation,
            "digest_matches_expected": self.digest_matches_expected,
        }


def derive_av1_validation_v4_production_execution_plans(
    *,
    manifest: Mapping[str, Any],
    effective_config: Mapping[str, Any],
    preparation: Mapping[str, Any],
    qualification_request: Mapping[str, Any],
) -> tuple[AV1ValidationV4ExecutionPlan, ...]:
    try:
        assert_av1_validation_v4_effective_config_snapshot(effective_config)
    except Exception as exc:
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 execution plan requires a valid effective config"
        ) from exc
    config = object_dict(effective_config)
    video_policy = object_dict(config.get("video_policy"))
    source_paths = object_dict(config.get("source_paths"))
    search_kwargs = object_dict(config.get("frozen_extra_search_kwargs"))
    stream_constraints = object_dict(config.get("source_stream_constraints"))
    if (
        set(source_paths) != set(AV1_VALIDATION_V4_SOURCE_IDS)
        or set(search_kwargs) != set(AV1_VALIDATION_V4_SOURCE_IDS)
        or set(stream_constraints) != set(AV1_VALIDATION_V4_SOURCE_IDS)
    ):
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 execution plan config source set is incomplete"
        )
    traversals = _manifest_traversals(manifest)
    prepared_by_ordinal = _invocations_by_ordinal(preparation, "preparation")
    request_by_ordinal = _invocations_by_ordinal(
        qualification_request,
        "qualification request",
    )
    warm_starts = av1_validation_v4_guided_warm_start_identities()
    plans: list[AV1ValidationV4ExecutionPlan] = []
    for traversal in traversals:
        ordinal = int(traversal["ordinal"])
        asset_id = str(traversal["asset_id"])
        configuration = str(traversal["configuration"])
        mode = _mode_for_configuration(configuration)
        prepared = prepared_by_ordinal[ordinal]
        requested = request_by_ordinal[ordinal]
        extra_kwargs = object_dict(search_kwargs[asset_id])
        constraints = object_dict(stream_constraints[asset_id])
        warm_start = None if mode == "baseline" else object_dict(warm_starts[asset_id])
        source_path = str(source_paths[asset_id])
        derived = _qualification_search_invocation_sha256(
            source_path=source_path,
            video_policy=video_policy,
            mode=mode,
            warm_start=warm_start,
            extra_search_kwargs=extra_kwargs,
        )
        base_config_sha256 = "sha256:" + stable_json_hash(
            {
                "source_path": source_path,
                "video_policy": video_policy,
                "extra_search_kwargs": extra_kwargs,
                "video_stream_index": constraints["video_stream_index"],
                "excluded_stream_indexes": constraints["excluded_stream_indexes"],
            }
        )
        _assert_invocation_binding(
            prepared,
            ordinal=ordinal,
            asset_id=asset_id,
            configuration=configuration,
            mode=mode,
            constraints=constraints,
            base_config_sha256=base_config_sha256,
        )
        _assert_invocation_binding(
            requested,
            ordinal=ordinal,
            asset_id=asset_id,
            configuration=configuration,
            mode=mode,
            constraints=constraints,
            base_config_sha256=base_config_sha256,
        )
        if prepared.get("source_path_hmac_id") != requested.get("source_path_hmac_id"):
            raise AV1ValidationV4ExecutionPlanError(
                "AV1 v4 execution plan request source identity differs"
            )
        plans.append(
            AV1ValidationV4ExecutionPlan(
                ordinal=ordinal,
                asset_id=asset_id,
                configuration=configuration,
                mode=mode,
                source_path_hmac_id=str(prepared["source_path_hmac_id"]),
                video_stream_index=int(constraints["video_stream_index"]),
                excluded_stream_indexes=tuple(
                    int(value) for value in constraints["excluded_stream_indexes"]
                ),
                search_kwargs=json.loads(canonical_json_bytes(extra_kwargs)),
                warm_start=(
                    json.loads(canonical_json_bytes(warm_start)) if warm_start else None
                ),
                base_config_sha256=base_config_sha256,
                expected_invocation_sha256=str(requested["sha256"]),
                preparation_invocation_sha256=str(prepared["sha256"]),
                derived_invocation_sha256=derived,
            )
        )
    if len(plans) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 execution plan traversal count is invalid"
        )
    return tuple(plans)


def av1_validation_v4_execution_plan_false_authority_payload() -> dict[str, bool]:
    return {field: False for field in AV1_VALIDATION_V4_FALSE_AUTHORITY_FIELDS}


def _manifest_traversals(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    matrix = object_dict(object_dict(manifest).get("qualification_matrix"))
    traversals = [object_dict(item) for item in object_list(matrix.get("traversals"))]
    if len(traversals) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 execution plan traversal set is incomplete"
        )
    for ordinal, traversal in enumerate(traversals, start=1):
        if (
            traversal.get("ordinal") != ordinal
            or traversal.get("asset_id") not in AV1_VALIDATION_V4_SOURCE_IDS
            or traversal.get("configuration") not in AV1_VALIDATION_V4_CONFIGURATIONS
        ):
            raise AV1ValidationV4ExecutionPlanError(
                "AV1 v4 execution plan traversal order is invalid"
            )
    return [dict(item) for item in traversals]


def _invocations_by_ordinal(
    payload: Mapping[str, Any],
    label: str,
) -> dict[int, dict[str, Any]]:
    invocations = [
        object_dict(item) for item in object_list(payload.get("invocations"))
    ]
    if len(invocations) != AV1_VALIDATION_V4_TRAVERSAL_COUNT:
        raise AV1ValidationV4ExecutionPlanError(
            f"AV1 v4 execution plan {label} invocations are incomplete"
        )
    result: dict[int, dict[str, Any]] = {}
    for invocation in invocations:
        ordinal = invocation.get("ordinal")
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal in result
        ):
            raise AV1ValidationV4ExecutionPlanError(
                f"AV1 v4 execution plan {label} invocation ordinal is invalid"
            )
        result[ordinal] = dict(invocation)
    if set(result) != set(range(1, AV1_VALIDATION_V4_TRAVERSAL_COUNT + 1)):
        raise AV1ValidationV4ExecutionPlanError(
            f"AV1 v4 execution plan {label} invocation coverage is invalid"
        )
    return result


def _mode_for_configuration(configuration: str) -> str:
    if configuration == "balanced_full_search_baseline":
        return "baseline"
    if configuration == "balanced_frozen_search_hint":
        return "guided"
    raise AV1ValidationV4ExecutionPlanError(
        "AV1 v4 execution plan configuration is invalid"
    )


def _assert_invocation_binding(
    invocation: Mapping[str, Any],
    *,
    ordinal: int,
    asset_id: str,
    configuration: str,
    mode: str,
    constraints: Mapping[str, Any],
    base_config_sha256: str,
) -> None:
    if (
        invocation.get("ordinal") != ordinal
        or invocation.get("asset_id") != asset_id
        or invocation.get("configuration") != configuration
        or invocation.get("mode") != mode
        or invocation.get("base_config_sha256") != base_config_sha256
        or invocation.get("video_stream_index") != constraints.get("video_stream_index")
        or invocation.get("excluded_stream_indexes")
        != constraints.get("excluded_stream_indexes")
    ):
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 execution plan invocation binding is invalid"
        )


def _qualification_search_invocation_sha256(
    *,
    source_path: str,
    video_policy: Mapping[str, Any],
    mode: str,
    warm_start: Mapping[str, Any] | None,
    extra_search_kwargs: Mapping[str, Any],
) -> str:
    return "sha256:" + stable_json_hash(
        _qualification_search_invocation_payload(
            source_path=source_path,
            video_policy=video_policy,
            mode=mode,
            warm_start=warm_start,
            extra_search_kwargs=extra_search_kwargs,
        )
    )


def _qualification_search_invocation_payload(
    *,
    source_path: str,
    video_policy: Mapping[str, Any],
    mode: str,
    warm_start: Mapping[str, Any] | None,
    extra_search_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    if mode not in {"baseline", "guided"}:
        raise AV1ValidationV4ExecutionPlanError("AV1 v4 execution plan mode is invalid")
    if mode == "baseline" and warm_start is not None:
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 baseline execution plan cannot carry a warm start"
        )
    if mode == "guided" and warm_start is None:
        raise AV1ValidationV4ExecutionPlanError(
            "AV1 v4 guided execution plan requires a warm start"
        )
    return {
        "schema": AV1_VALIDATION_V4_QUALIFICATION_SEARCH_SCHEMA,
        "schema_version": 1,
        "contract_version": AV1_VALIDATION_V4_QUALIFICATION_SEARCH_CONTRACT_VERSION,
        "protocol_version": AV1_VALIDATION_V4_PROTOCOL_VERSION,
        "experiment_id": AV1_VALIDATION_V4_EXPERIMENT_ID,
        "mode": mode,
        "planner_bypassed": True,
        "stream_budget_target_size_path_required": True,
        "source_path": source_path,
        "video_policy": deepcopy(dict(video_policy)),
        "search_kwargs": deepcopy(dict(extra_search_kwargs)),
        "warm_start": _warm_start_payload(warm_start),
    }


def _warm_start_payload(warm_start: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if warm_start is None:
        return None
    return {
        "requested_crf": warm_start.get("requested_crf"),
        "candidate_crf": warm_start.get("candidate_crf"),
        "search_signature_id": warm_start.get("search_signature_id"),
        "expected_search_signature_id": warm_start.get("search_signature_id"),
        "cohort_id": warm_start.get("cohort_id"),
        "source": warm_start.get("source"),
        "confidence": warm_start.get("confidence"),
        "provenance_id": warm_start.get("provenance_id"),
        "review_risks": list(object_list(warm_start.get("review_risks"))),
    }
