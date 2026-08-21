from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.db import DBClient
from mediaforce.core.evidence import stable_policy_hash
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.library.media_scopes import media_scope_from_prefix
from mediaforce.tuning.quality_observations import load_current_quality_search_observations
from mediaforce.tuning.size_goals import bytes_to_megabytes, operator_intent_from_policy

TARGET_SIZE_PROVENANCE_SCHEMA_VERSION = 1
EXACT_ITEM_TARGET_BELOW_QUALITY_SAFE_MINIMUM = "exact_item_target_below_quality_safe_minimum"


class ExactItemTargetProvenanceBlocked(RuntimeError):
    def __init__(self, blocker: "ExactItemTargetProvenanceBlocker") -> None:
        super().__init__(blocker.message)
        self.blocker = blocker


@dataclass(frozen=True, slots=True)
class ExactItemTargetProvenanceBlocker:
    requested_target_bytes: int
    quality_safe_minimum_bytes: int
    source: str
    override_prefix: str | None
    evidence_observation_id: str

    @property
    def code(self) -> str:
        return EXACT_ITEM_TARGET_BELOW_QUALITY_SAFE_MINIMUM

    @property
    def message(self) -> str:
        requested_mb = bytes_to_megabytes(self.requested_target_bytes) or 0
        minimum_mb = bytes_to_megabytes(self.quality_safe_minimum_bytes) or 0
        origin = f" from {self.override_prefix}" if self.override_prefix else ""
        return (
            f"The inherited {requested_mb:g} MB target{origin} is below this file's measured "
            f"quality-safe minimum of {minimum_mb:g} MB. Choose a new exact-item target and test it; "
            "Mediaforce will not widen the target automatically."
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TARGET_SIZE_PROVENANCE_SCHEMA_VERSION,
            "code": self.code,
            "message": self.message,
            "requested_target_bytes": self.requested_target_bytes,
            "quality_safe_minimum_bytes": self.quality_safe_minimum_bytes,
            "target_source": self.source,
            "override_prefix": self.override_prefix,
            "evidence_observation_id": self.evidence_observation_id,
        }


def target_size_provenance(
        *,
        source_config: MediaforceConfig,
        rel_path: str,
        effective_policy: Mapping[str, Any],
        duration_seconds: float | None,
) -> dict[str, Any]:
    source_policy, source = source_config.resolve_policy_with_target_provenance(rel_path)
    video = object_dict(effective_policy.get("video"))
    intent = operator_intent_from_policy(
        video,
        default_video_policy=source_config.video,
        audio_policy=object_dict(effective_policy.get("audio")),
        subtitle_policy=object_dict(effective_policy.get("subtitle")),
    )
    resolved = intent.size_goal.resolve(duration_seconds)
    source_intent = operator_intent_from_policy(
        object_dict(source_policy.get("video")),
        default_video_policy=source_config.video,
        audio_policy=object_dict(source_policy.get("audio")),
        subtitle_policy=object_dict(source_policy.get("subtitle")),
    )
    source_resolved = source_intent.size_goal.resolve(duration_seconds)
    if (
            source_resolved.target_size_bytes != resolved.target_size_bytes
            or source_resolved.intent.mode != resolved.intent.mode
    ):
        source = {
            "schema_version": TARGET_SIZE_PROVENANCE_SCHEMA_VERSION,
            "source": "exact_override",
            "override_prefix": rel_path.strip("/"),
        }
    return {
        "schema_version": TARGET_SIZE_PROVENANCE_SCHEMA_VERSION,
        "source": source["source"],
        "override_prefix": source["override_prefix"],
        "requested_target_bytes": resolved.target_size_bytes,
        "status": resolved.status,
        "size_goal_mode": resolved.intent.mode,
        "policy_hash": stable_policy_hash(dict(effective_policy)),
    }


def exact_item_target_provenance_blocker(
        connection: DBClient,
        item: Mapping[str, Any],
) -> ExactItemTargetProvenanceBlocker | None:
    provenance = object_dict(item.get("target_size_provenance"))
    if provenance.get("source") not in {"ancestor_override", "config_default"}:
        return None
    requested_target_bytes = int_value(provenance.get("requested_target_bytes"))
    rel_path = str(item.get("rel_path") or "").strip().strip("/")
    source_fingerprint = str(item.get("source_fingerprint") or "").strip()
    policy_hash = str(provenance.get("policy_hash") or "").strip()
    if requested_target_bytes <= 0 or not rel_path or not source_fingerprint or not policy_hash:
        return None

    scope = media_scope_from_prefix(rel_path, match="exact_item")
    compatible_candidates: list[tuple[int, str]] = []
    for observation in load_current_quality_search_observations(connection, scope=scope):
        if str(observation.get("source_rel_path") or "").strip().strip("/") != rel_path:
            continue
        if str(observation.get("source_fingerprint") or "").strip() != source_fingerprint:
            continue
        if str(observation.get("policy_hash") or "").strip() != policy_hash:
            continue
        if str(observation.get("search_objective") or "").strip() != "target_size":
            continue
        trace = _json_object(observation.get("candidate_trace_json"))
        if trace.get("objective") != "target_size" or trace.get("truncated") is not False:
            continue
        observation_id = str(observation.get("observation_id") or "").strip()
        if not observation_id:
            continue
        for candidate in object_list(trace.get("candidates")):
            candidate_payload = object_dict(candidate)
            predicted_bytes = int_value(candidate_payload.get("predicted_whole_episode_bytes"))
            if (
                candidate_payload.get("quality_floor_met") is True
                and candidate_payload.get("violates_source_cap") is not True
                and predicted_bytes > 0
            ):
                compatible_candidates.append((predicted_bytes, observation_id))
    if not compatible_candidates:
        return None
    quality_safe_minimum_bytes, observation_id = min(compatible_candidates)
    if requested_target_bytes >= quality_safe_minimum_bytes:
        return None
    return ExactItemTargetProvenanceBlocker(
        requested_target_bytes=requested_target_bytes,
        quality_safe_minimum_bytes=quality_safe_minimum_bytes,
        source=str(provenance["source"]),
        override_prefix=str(provenance.get("override_prefix") or "") or None,
        evidence_observation_id=observation_id,
    )


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return object_dict(decoded)
