from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from mediaforce.core.evidence import stable_json_hash
from mediaforce.core.type_defs import int_value, object_dict


COMPRESSION_INTENT_SCHEMA = "mediaforce.compression_intent"
COMPRESSION_INTENT_SCHEMA_VERSION = 1

CompressionIntentLevel = Literal[
    "reference",
    "transparent",
    "balanced",
    "perceptual_floor",
    "legacy_unconfirmed",
]
CompressionEvidenceKind = Literal[
    "approved_visual_result",
    "rejected_visual_result",
    "measured_quality_floor_violation",
    "measured_item_variance",
    "arithmetic_infeasibility",
    "operator_override",
]
CompressionDecisionDirection = Literal["smaller", "same", "larger"]
CompressionDecisionOutcome = Literal["authorized", "measure_more", "needs_operator", "hard_conflict"]
CompressionEscalationScope = Literal["none", "item"]

COMPRESSION_INTENT_LEVELS = (
    "reference",
    "transparent",
    "balanced",
    "perceptual_floor",
)
COMPRESSION_EVIDENCE_KINDS = (
    "approved_visual_result",
    "rejected_visual_result",
    "measured_quality_floor_violation",
    "measured_item_variance",
    "arithmetic_infeasibility",
    "operator_override",
)
GROWTH_AUTHORIZING_EVIDENCE_KINDS = frozenset(
    {
        "rejected_visual_result",
        "measured_quality_floor_violation",
        "measured_item_variance",
        "arithmetic_infeasibility",
        "operator_override",
    }
)

_INTENT_TITLES = {
    "reference": "Preserve the reference",
    "transparent": "No visible difference",
    "balanced": "Balance size and detail",
    "perceptual_floor": "Smallest that still looks good",
    "legacy_unconfirmed": "Choose a compression goal",
}
_INTENT_DETAILS = {
    "reference": "Prefer the highest measured fidelity that fits within the size limit.",
    "transparent": "Use the stricter source-indistinguishability rule, then prefer the smallest result that clears it.",
    "balanced": "Prefer the result closest to the requested size after measured quality clears.",
    "perceptual_floor": "Prefer the smallest result that clears the measured quality floor.",
    "legacy_unconfirmed": "This older policy needs a compression goal before Mediaforce can change its size direction.",
}


@dataclass(frozen=True, slots=True)
class CompressionIntentV1:
    level: CompressionIntentLevel
    source: str
    confirmed: bool

    @property
    def requires_confirmation(self) -> bool:
        return self.level == "legacy_unconfirmed" or not self.confirmed

    @property
    def accepts_under_target_result(self) -> bool:
        return not self.requires_confirmation and self.level in {"transparent", "perceptual_floor"}

    @property
    def semantic_id(self) -> str:
        payload = {
            "schema": COMPRESSION_INTENT_SCHEMA,
            "schema_version": COMPRESSION_INTENT_SCHEMA_VERSION,
            "level": self.level,
        }
        return f"ci1_{stable_json_hash(payload)[:32]}"

    @property
    def snapshot_id(self) -> str:
        payload = {
            "semantic_id": self.semantic_id,
            "source": self.source,
            "confirmed": self.confirmed,
        }
        return f"cis1_{stable_json_hash(payload)[:32]}"

    def policy_fragment(self) -> dict[str, Any]:
        if self.requires_confirmation:
            return {"video": {}}
        return {
            "video": {
                "compression_intent_schema_version": COMPRESSION_INTENT_SCHEMA_VERSION,
                "compression_intent": self.level,
                "compression_intent_source": self.source,
                "compression_intent_confirmed": self.confirmed,
            }
        }

    def request_payload(self) -> dict[str, Any]:
        return {
            "schema_version": COMPRESSION_INTENT_SCHEMA_VERSION,
            "level": self.level,
            "confirmed": self.confirmed,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.request_payload(),
            "source": self.source,
            "requires_confirmation": self.requires_confirmation,
            "accepts_under_target_result": self.accepts_under_target_result,
            "semantic_id": self.semantic_id,
            "snapshot_id": self.snapshot_id,
            "title": _INTENT_TITLES[self.level],
            "detail": _INTENT_DETAILS[self.level],
        }


@dataclass(frozen=True, slots=True)
class CompressionEvidenceRef:
    kind: CompressionEvidenceKind
    evidence_id: str
    intent_id: str
    observed_bytes: int | None = None
    source_id: str | None = None
    policy_hash: str | None = None
    job_id: str | None = None

    def compatible_with(
            self,
            intent: CompressionIntentV1,
            *,
            source_id: str | None = None,
            policy_hash: str | None = None,
            job_id: str | None = None,
    ) -> bool:
        if not self.evidence_id or self.intent_id != intent.semantic_id:
            return False
        for recorded, expected in (
            (self.source_id, source_id),
            (self.policy_hash, policy_hash),
            (self.job_id, job_id),
        ):
            if not recorded or not expected or recorded != expected:
                return False
        return True

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "evidence_id": self.evidence_id,
            "intent_id": self.intent_id,
            "observed_bytes": self.observed_bytes,
            "source_id": self.source_id,
            "policy_hash": self.policy_hash,
            "job_id": self.job_id,
        }


@dataclass(frozen=True, slots=True)
class CompressionAuthorizationDecision:
    intent_id: str
    authoritative_anchor_bytes: int
    candidate_bytes: int
    direction: CompressionDecisionDirection
    outcome: CompressionDecisionOutcome
    reason_code: str
    evidence_ids: tuple[str, ...]
    escalation_scope: CompressionEscalationScope

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "mediaforce.compression_authorization",
            "schema_version": 1,
            "intent_id": self.intent_id,
            "authoritative_anchor_bytes": self.authoritative_anchor_bytes,
            "candidate_bytes": self.candidate_bytes,
            "direction": self.direction,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "evidence_ids": list(self.evidence_ids),
            "escalation_scope": self.escalation_scope,
        }


def legacy_compression_intent(*, source: str = "legacy_unconfirmed") -> CompressionIntentV1:
    return CompressionIntentV1(level="legacy_unconfirmed", source=source, confirmed=False)


LEGACY_COMPRESSION_INTENT_ID = legacy_compression_intent().semantic_id


def compression_intent_from_policy(
        video_policy: Mapping[str, Any] | None,
        *,
        default_video_policy: Mapping[str, Any] | None = None,
) -> CompressionIntentV1:
    video = object_dict(video_policy)
    default_video = object_dict(default_video_policy)
    if "compression_intent" not in video and "compression_intent" in default_video:
        video = {
            **{
                key: default_video[key]
                for key in (
                    "compression_intent_schema_version",
                    "compression_intent",
                    "compression_intent_source",
                    "compression_intent_confirmed",
                )
                if key in default_video
            },
            **video,
        }
    level = str(video.get("compression_intent") or "").strip().lower()
    version = int_value(video.get("compression_intent_schema_version"))
    if level not in COMPRESSION_INTENT_LEVELS:
        source = "legacy_missing" if not level else "legacy_unknown"
        return legacy_compression_intent(source=source)
    if version != COMPRESSION_INTENT_SCHEMA_VERSION:
        return legacy_compression_intent(source="legacy_version")
    return CompressionIntentV1(
        level=level,  # type: ignore[arg-type]
        source=str(video.get("compression_intent_source") or "policy").strip() or "policy",
        confirmed=_confirmed_value(video.get("compression_intent_confirmed"), fallback=False),
    )


def compression_intent_from_request(payload: Mapping[str, Any] | None) -> CompressionIntentV1:
    request = object_dict(payload)
    if int_value(request.get("schema_version")) != COMPRESSION_INTENT_SCHEMA_VERSION:
        raise ValueError("The compression goal uses an unsupported schema version. Refresh and choose it again.")
    level = str(request.get("level") or "").strip().lower()
    if level not in COMPRESSION_INTENT_LEVELS:
        raise ValueError("Choose a supported compression goal before starting the test.")
    if not _confirmed_value(request.get("confirmed"), fallback=False):
        raise ValueError("Confirm the compression goal before starting the test.")
    return CompressionIntentV1(level=level, source="operator", confirmed=True)  # type: ignore[arg-type]


def compression_intent_from_payload(payload: Mapping[str, Any] | None) -> CompressionIntentV1:
    value = object_dict(payload)
    if int_value(value.get("schema_version")) != COMPRESSION_INTENT_SCHEMA_VERSION:
        return legacy_compression_intent(source="legacy_snapshot")
    level = str(value.get("level") or "").strip().lower()
    if level not in {*COMPRESSION_INTENT_LEVELS, "legacy_unconfirmed"}:
        return legacy_compression_intent(source="legacy_snapshot")
    return CompressionIntentV1(
        level=level,  # type: ignore[arg-type]
        source=str(value.get("source") or "snapshot").strip() or "snapshot",
        confirmed=_confirmed_value(value.get("confirmed"), fallback=False),
    )


def compression_intent_from_item(item: Mapping[str, Any] | None) -> CompressionIntentV1:
    value = object_dict(item)
    snapshot = object_dict(value.get("compression_intent"))
    if snapshot:
        return compression_intent_from_payload(snapshot)
    resolved = object_dict(object_dict(value.get("resolved_operator_intent")).get("compression_intent"))
    if resolved:
        return compression_intent_from_payload(resolved)
    policy = object_dict(object_dict(value.get("resolved_policy")).get("video"))
    if policy:
        return compression_intent_from_policy(policy)
    return legacy_compression_intent(source="legacy_item")


def compression_intent_options(current: CompressionIntentV1) -> list[dict[str, Any]]:
    return [
        {
            "key": level,
            "title": _INTENT_TITLES[level],
            "detail": _INTENT_DETAILS[level],
            "selected": current.confirmed and current.level == level,
            "accepts_under_target_result": CompressionIntentV1(
                level=level,  # type: ignore[arg-type]
                source="operator",
                confirmed=True,
            ).accepts_under_target_result,
            "compression_intent": CompressionIntentV1(
                level=level,  # type: ignore[arg-type]
                source="operator",
                confirmed=True,
            ).request_payload(),
        }
        for level in COMPRESSION_INTENT_LEVELS
    ]


def authorize_compression_change(
        intent: CompressionIntentV1,
        *,
        authoritative_anchor_bytes: int,
        candidate_bytes: int,
        evidence: Sequence[CompressionEvidenceRef] = (),
        source_id: str | None = None,
        policy_hash: str | None = None,
        job_id: str | None = None,
        hard_conflict_reason: str | None = None,
) -> CompressionAuthorizationDecision:
    direction = _direction(authoritative_anchor_bytes, candidate_bytes)
    if authoritative_anchor_bytes <= 0 or candidate_bytes <= 0:
        return _decision(
            intent,
            authoritative_anchor_bytes,
            candidate_bytes,
            direction,
            "needs_operator",
            "invalid_size_anchor",
        )
    if hard_conflict_reason:
        return _decision(
            intent,
            authoritative_anchor_bytes,
            candidate_bytes,
            direction,
            "hard_conflict",
            hard_conflict_reason,
        )
    if direction in {"same", "smaller"}:
        return _decision(
            intent,
            authoritative_anchor_bytes,
            candidate_bytes,
            direction,
            "authorized",
            "same_anchor" if direction == "same" else "toward_smaller_result",
        )
    if intent.requires_confirmation:
        return _decision(
            intent,
            authoritative_anchor_bytes,
            candidate_bytes,
            direction,
            "needs_operator",
            "compression_intent_unconfirmed",
        )
    compatible = tuple(
        item
        for item in evidence
        if item.compatible_with(intent, source_id=source_id, policy_hash=policy_hash, job_id=job_id)
    )
    authorizing = tuple(item for item in compatible if item.kind in GROWTH_AUTHORIZING_EVIDENCE_KINDS)
    if not authorizing:
        return _decision(
            intent,
            authoritative_anchor_bytes,
            candidate_bytes,
            direction,
            "measure_more",
            "larger_result_requires_typed_evidence",
        )
    return _decision(
        intent,
        authoritative_anchor_bytes,
        candidate_bytes,
        direction,
        "authorized",
        f"authorized_by_{authorizing[0].kind}",
        evidence_ids=tuple(item.evidence_id for item in authorizing),
        escalation_scope="item",
    )


def compression_candidate_rank(
        intent: CompressionIntentV1,
        *,
        target_distance_bytes: int,
        predicted_bytes: int,
        metric_score: float,
        crf: float,
) -> tuple[float | int, ...]:
    if intent.requires_confirmation:
        return target_distance_bytes, predicted_bytes, -metric_score, crf
    if intent.level in {"transparent", "perceptual_floor"}:
        return predicted_bytes, -metric_score, crf
    if intent.level == "reference":
        return -metric_score, predicted_bytes, target_distance_bytes, crf
    return target_distance_bytes, predicted_bytes, -metric_score, crf


def _decision(
        intent: CompressionIntentV1,
        anchor: int,
        candidate: int,
        direction: CompressionDecisionDirection,
        outcome: CompressionDecisionOutcome,
        reason_code: str,
        *,
        evidence_ids: tuple[str, ...] = (),
        escalation_scope: CompressionEscalationScope = "none",
) -> CompressionAuthorizationDecision:
    return CompressionAuthorizationDecision(
        intent_id=intent.semantic_id,
        authoritative_anchor_bytes=anchor,
        candidate_bytes=candidate,
        direction=direction,
        outcome=outcome,
        reason_code=reason_code,
        evidence_ids=evidence_ids,
        escalation_scope=escalation_scope,
    )


def _direction(anchor: int, candidate: int) -> CompressionDecisionDirection:
    if candidate < anchor:
        return "smaller"
    if candidate > anchor:
        return "larger"
    return "same"


def _confirmed_value(value: object, *, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return fallback
