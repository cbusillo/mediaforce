import copy
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Literal, Mapping

from mediaforce.core.evidence import stable_json_hash, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.encoding.streams import PlannedStream, ProductionStreamPlan, resolve_stream_plan
from mediaforce.tuning.size_goals import ResolvedSizeGoal, operator_intent_from_policy


STREAM_BUDGET_SCHEMA_VERSION = 1
DEFAULT_CONTAINER_OVERHEAD_BYTES = 4_000_000
DEFAULT_TEXT_SUBTITLE_BYTES = 128 * 1024
DEFAULT_IMAGE_SUBTITLE_BYTES = 4 * 1024 * 1024
DEFAULT_UNKNOWN_ATTACHMENT_BYTES = 4_000_000
AGGRESSIVE_VIDEO_BITRATE_BPS = 500_000
AGGRESSIVE_TOTAL_SOURCE_PERCENT = 10.0
AGGRESSIVE_SOURCE_VIDEO_RATIO = 0.20

BudgetCategory = Literal["audio", "subtitle", "attachment", "container"]
EstimateConfidence = Literal["exact", "high", "medium", "low", "unknown"]
FeasibilityStatus = Literal[
    "feasible",
    "aggressive_but_measurable",
    "arithmetically_infeasible",
    "requires_measurement",
]
SourceCapStatus = Literal["available", "arithmetically_infeasible", "requires_measurement", "unavailable"]


class StreamBudgetIdentityError(ValueError):
    pass


class StreamBudgetInfeasibleError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StreamBudgetEntry:
    category: BudgetCategory
    source_index: int | None
    action: str
    source_codec: str | None
    output_codec: str | None
    budget_bytes: int | None
    lower_bound_bytes: int | None
    upper_bound_bytes: int | None
    bitrate_bps: int | None
    provenance: str
    confidence: EstimateConfidence
    requires_measurement: bool
    rationale: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "source_index": self.source_index,
            "action": self.action,
            "source_codec": self.source_codec,
            "output_codec": self.output_codec,
            "budget_bytes": self.budget_bytes,
            "lower_bound_bytes": self.lower_bound_bytes,
            "upper_bound_bytes": self.upper_bound_bytes,
            "bitrate_bps": self.bitrate_bps,
            "provenance": self.provenance,
            "confidence": self.confidence,
            "requires_measurement": self.requires_measurement,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class StreamBudgetLedger:
    ledger_id: str
    source_id: str
    source_fingerprint: str | None
    source_size_bytes: int | None
    source_video_bitrate_bps: int | None
    duration_seconds: float | None
    policy_hash: str
    size_goal: dict[str, Any]
    stream_plan: ProductionStreamPlan
    entries: tuple[StreamBudgetEntry, ...]
    total_target_bytes: int | None
    audio_bytes: int | None
    subtitle_bytes: int | None
    attachment_bytes: int | None
    container_bytes: int | None
    non_video_bytes: int | None
    minimum_non_video_bytes: int
    maximum_non_video_bytes: int | None
    remaining_video_bytes: int | None
    remaining_video_bitrate_bps: int | None
    source_cap_percent: float | None
    source_cap_total_bytes: int | None
    source_cap_video_bytes: int | None
    source_cap_video_bitrate_bps: int | None
    source_cap_video_percent: float | None
    source_cap_status: SourceCapStatus
    feasibility_status: FeasibilityStatus
    feasibility_reasons: tuple[str, ...]
    confidence: EstimateConfidence
    requires_measurement: bool

    @property
    def arithmetic_infeasible(self) -> bool:
        return self.feasibility_status == "arithmetically_infeasible"

    @property
    def aggressive(self) -> bool:
        return self.feasibility_status == "aggressive_but_measurable"

    def require_positive_target_video_budget(self) -> None:
        if not self.arithmetic_infeasible:
            return
        raise StreamBudgetInfeasibleError(
            "The requested total size leaves no positive video budget after the production non-video plan."
        )

    def require_positive_source_cap_video_budget(self) -> None:
        if self.source_cap_status != "arithmetically_infeasible":
            return
        raise StreamBudgetInfeasibleError(
            "The configured source-relative size cap leaves no positive video budget after non-video overhead."
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": STREAM_BUDGET_SCHEMA_VERSION,
            "ledger_id": self.ledger_id,
            "source": {
                "source_id": self.source_id,
                "source_fingerprint": self.source_fingerprint,
                "source_size_bytes": self.source_size_bytes,
                "source_video_bitrate_bps": self.source_video_bitrate_bps,
                "duration_seconds": self.duration_seconds,
            },
            "policy_hash": self.policy_hash,
            "size_goal": copy.deepcopy(self.size_goal),
            "stream_plan": self.stream_plan.to_payload(),
            "entries": [entry.to_payload() for entry in self.entries],
            "totals": {
                "total_target_bytes": self.total_target_bytes,
                "audio_bytes": self.audio_bytes,
                "subtitle_bytes": self.subtitle_bytes,
                "attachment_bytes": self.attachment_bytes,
                "container_bytes": self.container_bytes,
                "non_video_bytes": self.non_video_bytes,
                "minimum_non_video_bytes": self.minimum_non_video_bytes,
                "maximum_non_video_bytes": self.maximum_non_video_bytes,
                "remaining_video_bytes": self.remaining_video_bytes,
                "remaining_video_bitrate_bps": self.remaining_video_bitrate_bps,
            },
            "source_relative_cap": {
                "configured_total_percent": self.source_cap_percent,
                "total_cap_bytes": self.source_cap_total_bytes,
                "video_cap_bytes": self.source_cap_video_bytes,
                "video_cap_bitrate_bps": self.source_cap_video_bitrate_bps,
                "video_cap_percent": self.source_cap_video_percent,
                "status": self.source_cap_status,
            },
            "feasibility": {
                "status": self.feasibility_status,
                "reasons": list(self.feasibility_reasons),
                "arithmetic_infeasible": self.arithmetic_infeasible,
                "aggressive": self.aggressive,
                "requires_measurement": self.requires_measurement,
            },
            "uncertainty": {
                "confidence": self.confidence,
                "requires_measurement": self.requires_measurement,
                "minimum_non_video_bytes": self.minimum_non_video_bytes,
                "maximum_non_video_bytes": self.maximum_non_video_bytes,
            },
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StreamBudgetLedger":
        ledger_payload = object_dict(payload)
        if int_value(ledger_payload.get("schema_version")) != STREAM_BUDGET_SCHEMA_VERSION:
            raise StreamBudgetIdentityError("The stream budget ledger uses an unsupported schema version.")
        source = object_dict(ledger_payload.get("source"))
        totals = object_dict(ledger_payload.get("totals"))
        source_cap = object_dict(ledger_payload.get("source_relative_cap"))
        feasibility = object_dict(ledger_payload.get("feasibility"))
        stream_plan = ProductionStreamPlan.from_payload(object_dict(ledger_payload.get("stream_plan")))
        entries = tuple(_entry_from_payload(object_dict(entry)) for entry in object_list(ledger_payload.get("entries")))
        feasibility_status = str(feasibility.get("status") or "")
        if feasibility_status not in {
            "feasible",
            "aggressive_but_measurable",
            "arithmetically_infeasible",
            "requires_measurement",
        }:
            raise StreamBudgetIdentityError("The stream budget ledger has an unsupported feasibility status.")
        source_cap_status = str(source_cap.get("status") or "")
        if source_cap_status not in {
            "available",
            "arithmetically_infeasible",
            "requires_measurement",
            "unavailable",
        }:
            raise StreamBudgetIdentityError("The stream budget ledger has an unsupported source-cap status.")
        confidence = str(object_dict(ledger_payload.get("uncertainty")).get("confidence") or "unknown")
        if confidence not in {"exact", "high", "medium", "low", "unknown"}:
            raise StreamBudgetIdentityError("The stream budget ledger has an unsupported confidence value.")
        ledger = cls(
            ledger_id=str(ledger_payload.get("ledger_id") or ""),
            source_id=str(source.get("source_id") or ""),
            source_fingerprint=_optional_text(source.get("source_fingerprint")),
            source_size_bytes=_positive_int(source.get("source_size_bytes")),
            source_video_bitrate_bps=_positive_int(source.get("source_video_bitrate_bps")),
            duration_seconds=_positive_float(source.get("duration_seconds")),
            policy_hash=str(ledger_payload.get("policy_hash") or ""),
            size_goal=copy.deepcopy(object_dict(ledger_payload.get("size_goal"))),
            stream_plan=stream_plan,
            entries=entries,
            total_target_bytes=_positive_int(totals.get("total_target_bytes")),
            audio_bytes=_optional_nonnegative_int(totals.get("audio_bytes")),
            subtitle_bytes=_optional_nonnegative_int(totals.get("subtitle_bytes")),
            attachment_bytes=_optional_nonnegative_int(totals.get("attachment_bytes")),
            container_bytes=_optional_nonnegative_int(totals.get("container_bytes")),
            non_video_bytes=_optional_nonnegative_int(totals.get("non_video_bytes")),
            minimum_non_video_bytes=max(0, int_value(totals.get("minimum_non_video_bytes"))),
            maximum_non_video_bytes=_optional_nonnegative_int(totals.get("maximum_non_video_bytes")),
            remaining_video_bytes=_optional_int(totals.get("remaining_video_bytes")),
            remaining_video_bitrate_bps=_optional_int(totals.get("remaining_video_bitrate_bps")),
            source_cap_percent=_positive_float(source_cap.get("configured_total_percent")),
            source_cap_total_bytes=_optional_nonnegative_int(source_cap.get("total_cap_bytes")),
            source_cap_video_bytes=_optional_int(source_cap.get("video_cap_bytes")),
            source_cap_video_bitrate_bps=_optional_int(source_cap.get("video_cap_bitrate_bps")),
            source_cap_video_percent=_optional_float(source_cap.get("video_cap_percent")),
            source_cap_status=source_cap_status,  # type: ignore[arg-type]
            feasibility_status=feasibility_status,  # type: ignore[arg-type]
            feasibility_reasons=tuple(str(reason) for reason in object_list(feasibility.get("reasons"))),
            confidence=confidence,  # type: ignore[arg-type]
            requires_measurement=bool(feasibility.get("requires_measurement")),
        )
        if not ledger.ledger_id or ledger.ledger_id != _ledger_id(ledger._identity_payload()):
            raise StreamBudgetIdentityError("The stream budget ledger identity does not match its contents.")
        return ledger

    def validate_item(self, item: Mapping[str, Any]) -> None:
        item_payload = object_dict(item)
        if self.source_id != stable_source_id(item_payload):
            raise StreamBudgetIdentityError("The stream budget ledger does not match the source item.")
        if self.source_fingerprint != _optional_text(
                item_payload.get("source_fingerprint", item_payload.get("fingerprint"))
        ):
            raise StreamBudgetIdentityError("The stream budget ledger source fingerprint is stale.")
        policy = object_dict(item_payload.get("resolved_policy"))
        if self.policy_hash != stable_policy_hash(policy):
            raise StreamBudgetIdentityError("The stream budget ledger does not match the resolved policy.")
        source_size_bytes = _positive_int(item_payload.get("source_size_bytes", item_payload.get("size_bytes")))
        if self.source_size_bytes != source_size_bytes:
            raise StreamBudgetIdentityError("The stream budget ledger source size is stale.")
        source_video_bitrate_bps = _positive_int(item_payload.get("video_bitrate"))
        if self.source_video_bitrate_bps != source_video_bitrate_bps:
            raise StreamBudgetIdentityError("The stream budget ledger source video bitrate is stale.")
        duration_seconds = _positive_float(item_payload.get("duration_seconds"))
        if not _same_optional_float(self.duration_seconds, duration_seconds):
            raise StreamBudgetIdentityError("The stream budget ledger item runtime is stale.")
        self.stream_plan.validate_item(item_payload)

    def _identity_payload(self) -> dict[str, Any]:
        payload = self.to_payload()
        payload.pop("ledger_id", None)
        return payload


def resolve_stream_budget_ledger(
        item: Mapping[str, Any],
        *,
        default_video_policy: Mapping[str, Any] | None = None,
        output_container: str | None = None,
        resolved_size_goal: ResolvedSizeGoal | None = None,
        prefer_persisted: bool = True,
) -> StreamBudgetLedger:
    item_payload = dict(item)
    if output_container:
        item_payload["output_container"] = output_container
    persisted = object_dict(item_payload.get("stream_budget_ledger"))
    if prefer_persisted and persisted:
        ledger = StreamBudgetLedger.from_payload(persisted)
        ledger.validate_item(item_payload)
        return ledger

    policy = object_dict(item_payload.get("resolved_policy"))
    video_policy = object_dict(policy.get("video"))
    if resolved_size_goal is None:
        intent = operator_intent_from_policy(
            video_policy,
            default_video_policy=object_dict(default_video_policy),
            audio_policy=object_dict(policy.get("audio")),
            subtitle_policy=object_dict(policy.get("subtitle")),
        )
        resolved_size_goal = intent.size_goal.resolve(_positive_float(item_payload.get("duration_seconds")))
    stream_plan = resolve_stream_plan(item_payload)
    return build_stream_budget_ledger(
        item_payload,
        resolved_size_goal=resolved_size_goal,
        stream_plan=stream_plan,
    )


def build_stream_budget_ledger(
        item: Mapping[str, Any],
        *,
        resolved_size_goal: ResolvedSizeGoal,
        stream_plan: ProductionStreamPlan,
) -> StreamBudgetLedger:
    item_payload = object_dict(item)
    duration_seconds = _positive_float(item_payload.get("duration_seconds"))
    total_target_bytes = resolved_size_goal.target_size_bytes
    entries = [_entry_for_stream(stream, duration_seconds) for stream in stream_plan.streams]
    if stream_plan.copy_unknown_attachments:
        entries.append(_unknown_attachments_entry())
    entries.append(_container_entry(total_target_bytes))

    audio_bytes = _category_total(entries, "audio")
    subtitle_bytes = _category_total(entries, "subtitle")
    attachment_bytes = _category_total(entries, "attachment")
    container_bytes = _category_total(entries, "container")
    non_video_bytes = _known_total(entries)
    minimum_non_video_bytes = sum(entry.lower_bound_bytes or 0 for entry in entries)
    maximum_non_video_bytes = _upper_total(entries)
    remaining_video_bytes = (
        total_target_bytes - non_video_bytes
        if total_target_bytes is not None and non_video_bytes is not None
        else None
    )
    remaining_video_bitrate_bps = _bitrate_for_bytes(remaining_video_bytes, duration_seconds)

    source_size_bytes = _positive_int(item_payload.get("source_size_bytes", item_payload.get("size_bytes")))
    source_video_bitrate_bps = _positive_int(item_payload.get("video_bitrate"))
    video_policy = object_dict(object_dict(item_payload.get("resolved_policy")).get("video"))
    source_cap_percent = _positive_float(video_policy.get("max_encoded_percent"))
    source_cap_total_bytes = _percent_bytes(source_size_bytes, source_cap_percent)
    source_cap_video_bytes = (
        source_cap_total_bytes - non_video_bytes
        if source_cap_total_bytes is not None and non_video_bytes is not None
        else None
    )
    source_cap_video_bitrate_bps = _bitrate_for_bytes(source_cap_video_bytes, duration_seconds)
    source_cap_video_percent = _percent_of_source(source_cap_video_bytes, source_size_bytes, floor=True)
    source_cap_status = _source_cap_status(
        source_cap_total_bytes=source_cap_total_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
        minimum_non_video_bytes=minimum_non_video_bytes,
        non_video_bytes=non_video_bytes,
    )
    feasibility_status, feasibility_reasons = _feasibility(
        size_goal_status=resolved_size_goal.status,
        total_target_bytes=total_target_bytes,
        minimum_non_video_bytes=minimum_non_video_bytes,
        non_video_bytes=non_video_bytes,
        remaining_video_bytes=remaining_video_bytes,
        remaining_video_bitrate_bps=remaining_video_bitrate_bps,
        duration_seconds=duration_seconds,
        source_size_bytes=source_size_bytes,
        source_video_bitrate_bps=source_video_bitrate_bps,
        entries=entries,
    )
    confidence = _aggregate_confidence(entries)
    requires_measurement = feasibility_status == "requires_measurement"
    size_goal_payload = resolved_size_goal.to_payload()
    identity_payload = {
        "schema_version": STREAM_BUDGET_SCHEMA_VERSION,
        "source": {
            "source_id": stable_source_id(item_payload),
            "source_fingerprint": _optional_text(
                item_payload.get("source_fingerprint", item_payload.get("fingerprint"))
            ),
            "source_size_bytes": source_size_bytes,
            "source_video_bitrate_bps": source_video_bitrate_bps,
            "duration_seconds": duration_seconds,
        },
        "policy_hash": stable_policy_hash(object_dict(item_payload.get("resolved_policy"))),
        "size_goal": size_goal_payload,
        "stream_plan": stream_plan.to_payload(),
        "entries": [entry.to_payload() for entry in entries],
        "totals": {
            "total_target_bytes": total_target_bytes,
            "audio_bytes": audio_bytes,
            "subtitle_bytes": subtitle_bytes,
            "attachment_bytes": attachment_bytes,
            "container_bytes": container_bytes,
            "non_video_bytes": non_video_bytes,
            "minimum_non_video_bytes": minimum_non_video_bytes,
            "maximum_non_video_bytes": maximum_non_video_bytes,
            "remaining_video_bytes": remaining_video_bytes,
            "remaining_video_bitrate_bps": remaining_video_bitrate_bps,
        },
        "source_relative_cap": {
            "configured_total_percent": source_cap_percent,
            "total_cap_bytes": source_cap_total_bytes,
            "video_cap_bytes": source_cap_video_bytes,
            "video_cap_bitrate_bps": source_cap_video_bitrate_bps,
            "video_cap_percent": source_cap_video_percent,
            "status": source_cap_status,
        },
        "feasibility": {
            "status": feasibility_status,
            "reasons": list(feasibility_reasons),
            "arithmetic_infeasible": feasibility_status == "arithmetically_infeasible",
            "aggressive": feasibility_status == "aggressive_but_measurable",
            "requires_measurement": requires_measurement,
        },
        "uncertainty": {
            "confidence": confidence,
            "requires_measurement": requires_measurement,
            "minimum_non_video_bytes": minimum_non_video_bytes,
            "maximum_non_video_bytes": maximum_non_video_bytes,
        },
    }
    return StreamBudgetLedger(
        ledger_id=_ledger_id(identity_payload),
        source_id=str(object_dict(identity_payload["source"])["source_id"]),
        source_fingerprint=_optional_text(object_dict(identity_payload["source"]).get("source_fingerprint")),
        source_size_bytes=source_size_bytes,
        source_video_bitrate_bps=source_video_bitrate_bps,
        duration_seconds=duration_seconds,
        policy_hash=str(identity_payload["policy_hash"]),
        size_goal=size_goal_payload,
        stream_plan=stream_plan,
        entries=tuple(entries),
        total_target_bytes=total_target_bytes,
        audio_bytes=audio_bytes,
        subtitle_bytes=subtitle_bytes,
        attachment_bytes=attachment_bytes,
        container_bytes=container_bytes,
        non_video_bytes=non_video_bytes,
        minimum_non_video_bytes=minimum_non_video_bytes,
        maximum_non_video_bytes=maximum_non_video_bytes,
        remaining_video_bytes=remaining_video_bytes,
        remaining_video_bitrate_bps=remaining_video_bitrate_bps,
        source_cap_percent=source_cap_percent,
        source_cap_total_bytes=source_cap_total_bytes,
        source_cap_video_bytes=source_cap_video_bytes,
        source_cap_video_bitrate_bps=source_cap_video_bitrate_bps,
        source_cap_video_percent=source_cap_video_percent,
        source_cap_status=source_cap_status,
        feasibility_status=feasibility_status,
        feasibility_reasons=feasibility_reasons,
        confidence=confidence,
        requires_measurement=requires_measurement,
    )


def _entry_for_stream(stream: PlannedStream, duration_seconds: float | None) -> StreamBudgetEntry:
    if stream.kind == "audio":
        return _audio_entry(stream, duration_seconds)
    if stream.kind == "subtitle":
        return _subtitle_entry(stream, duration_seconds)
    return _attachment_entry(stream)


def _audio_entry(stream: PlannedStream, duration_seconds: float | None) -> StreamBudgetEntry:
    effective_duration = stream.source_duration_seconds or duration_seconds
    if stream.action == "transcode":
        budget_bytes = _bytes_for_bitrate(stream.output_bitrate_bps, effective_duration)
        if budget_bytes is None:
            return _unknown_entry(
                category="audio",
                stream=stream,
                provenance="production_audio_plan_missing_runtime",
                rationale="The fixed Opus bitrate is known, but the item runtime is not available.",
            )
        return StreamBudgetEntry(
            category="audio",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=budget_bytes,
            lower_bound_bytes=budget_bytes,
            upper_bound_bytes=budget_bytes,
            bitrate_bps=stream.output_bitrate_bps,
            provenance="production_audio_policy_bitrate",
            confidence="high",
            requires_measurement=False,
            rationale="The production plan transcodes this selected audio track at a fixed policy bitrate.",
        )
    if stream.source_size_bytes is not None:
        return StreamBudgetEntry(
            category="audio",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=stream.source_size_bytes,
            lower_bound_bytes=stream.source_size_bytes,
            upper_bound_bytes=stream.source_size_bytes,
            bitrate_bps=stream.source_bitrate_bps,
            provenance="ffprobe_stream_size",
            confidence="exact",
            requires_measurement=False,
            rationale="The production plan copies this selected audio track and ffprobe reported its stream bytes.",
        )
    bitrate_bytes = _bytes_for_bitrate(stream.source_bitrate_bps, effective_duration)
    if bitrate_bytes is not None:
        lower, upper = _percentage_bounds(bitrate_bytes, 0.90, 1.10)
        return StreamBudgetEntry(
            category="audio",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=bitrate_bytes,
            lower_bound_bytes=lower,
            upper_bound_bytes=upper,
            bitrate_bps=stream.source_bitrate_bps,
            provenance="ffprobe_stream_bitrate_times_runtime",
            confidence="medium",
            requires_measurement=False,
            rationale="The production plan copies this selected audio track; bytes are projected from its reported bitrate.",
        )
    if effective_duration is None:
        return _unknown_entry(
            category="audio",
            stream=stream,
            provenance="copy_audio_missing_bitrate_and_runtime",
            rationale="The copied audio track has neither measured bytes nor enough runtime metadata for a fallback.",
        )
    channels = stream.channels or 2
    fallback_bitrate = 640_000 if channels >= 6 else 192_000
    fallback_bytes = _bytes_for_bitrate(fallback_bitrate, effective_duration)
    assert fallback_bytes is not None
    lower, upper = _percentage_bounds(fallback_bytes, 0.50, 2.00)
    return StreamBudgetEntry(
        category="audio",
        source_index=stream.source_index,
        action=stream.action,
        source_codec=stream.source_codec,
        output_codec=stream.output_codec,
        budget_bytes=fallback_bytes,
        lower_bound_bytes=lower,
        upper_bound_bytes=upper,
        bitrate_bps=fallback_bitrate,
        provenance="channel_layout_copy_bitrate_fallback",
        confidence="low",
        requires_measurement=True,
        rationale="The copied audio bitrate is unknown, so the ledger uses the production channel-layout fallback.",
    )


def _subtitle_entry(stream: PlannedStream, duration_seconds: float | None) -> StreamBudgetEntry:
    if stream.action == "transcode":
        lower, upper = _percentage_bounds(DEFAULT_TEXT_SUBTITLE_BYTES, 0.25, 8.00)
        return StreamBudgetEntry(
            category="subtitle",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=DEFAULT_TEXT_SUBTITLE_BYTES,
            lower_bound_bytes=lower,
            upper_bound_bytes=upper,
            bitrate_bps=None,
            provenance="text_subtitle_transcode_fallback",
            confidence="low",
            requires_measurement=False,
            rationale="The production plan converts this text subtitle to SRT and reserves a bounded text fallback.",
        )
    if stream.source_size_bytes is not None:
        return StreamBudgetEntry(
            category="subtitle",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=stream.source_size_bytes,
            lower_bound_bytes=stream.source_size_bytes,
            upper_bound_bytes=stream.source_size_bytes,
            bitrate_bps=stream.source_bitrate_bps,
            provenance="ffprobe_stream_size",
            confidence="exact",
            requires_measurement=False,
            rationale="The production plan copies this image subtitle and ffprobe reported its stream bytes.",
        )
    bitrate_bytes = _bytes_for_bitrate(stream.source_bitrate_bps, stream.source_duration_seconds or duration_seconds)
    if bitrate_bytes is not None:
        lower, upper = _percentage_bounds(bitrate_bytes, 0.75, 1.50)
        return StreamBudgetEntry(
            category="subtitle",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=bitrate_bytes,
            lower_bound_bytes=lower,
            upper_bound_bytes=upper,
            bitrate_bps=stream.source_bitrate_bps,
            provenance="ffprobe_stream_bitrate_times_runtime",
            confidence="medium",
            requires_measurement=False,
            rationale="The production plan copies this image subtitle; bytes are projected from reported bitrate metadata.",
        )
    lower, upper = _percentage_bounds(DEFAULT_IMAGE_SUBTITLE_BYTES, 0.25, 4.00)
    return StreamBudgetEntry(
        category="subtitle",
        source_index=stream.source_index,
        action=stream.action,
        source_codec=stream.source_codec,
        output_codec=stream.output_codec,
        budget_bytes=DEFAULT_IMAGE_SUBTITLE_BYTES,
        lower_bound_bytes=lower,
        upper_bound_bytes=upper,
        bitrate_bps=None,
        provenance="image_subtitle_copy_fallback",
        confidence="low",
        requires_measurement=True,
        rationale="The copied image subtitle has no measured size, so the ledger reserves a bounded image fallback.",
    )


def _attachment_entry(stream: PlannedStream) -> StreamBudgetEntry:
    if stream.action == "drop":
        return StreamBudgetEntry(
            category="attachment",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=None,
            budget_bytes=0,
            lower_bound_bytes=0,
            upper_bound_bytes=0,
            bitrate_bps=None,
            provenance="production_container_plan",
            confidence="exact",
            requires_measurement=False,
            rationale="The selected output container cannot carry attachments, so production drops this stream.",
        )
    if stream.source_size_bytes is not None:
        return StreamBudgetEntry(
            category="attachment",
            source_index=stream.source_index,
            action=stream.action,
            source_codec=stream.source_codec,
            output_codec=stream.output_codec,
            budget_bytes=stream.source_size_bytes,
            lower_bound_bytes=stream.source_size_bytes,
            upper_bound_bytes=stream.source_size_bytes,
            bitrate_bps=None,
            provenance="ffprobe_attachment_size",
            confidence="exact",
            requires_measurement=False,
            rationale="The production plan copies this attachment and ffprobe reported its bytes.",
        )
    lower, upper = _percentage_bounds(DEFAULT_UNKNOWN_ATTACHMENT_BYTES, 0.0, 4.0)
    return StreamBudgetEntry(
        category="attachment",
        source_index=stream.source_index,
        action=stream.action,
        source_codec=stream.source_codec,
        output_codec=stream.output_codec,
        budget_bytes=DEFAULT_UNKNOWN_ATTACHMENT_BYTES,
        lower_bound_bytes=lower,
        upper_bound_bytes=upper,
        bitrate_bps=None,
        provenance="attachment_copy_fallback",
        confidence="low",
        requires_measurement=True,
        rationale="The copied attachment has no measured size, so the ledger reserves a bounded fallback.",
    )


def _unknown_attachments_entry() -> StreamBudgetEntry:
    lower, upper = _percentage_bounds(DEFAULT_UNKNOWN_ATTACHMENT_BYTES, 0.0, 4.0)
    return StreamBudgetEntry(
        category="attachment",
        source_index=None,
        action="copy",
        source_codec=None,
        output_codec=None,
        budget_bytes=DEFAULT_UNKNOWN_ATTACHMENT_BYTES,
        lower_bound_bytes=lower,
        upper_bound_bytes=upper,
        bitrate_bps=None,
        provenance="unmeasured_attachment_set_fallback",
        confidence="unknown",
        requires_measurement=True,
        rationale="The source predates attachment probing; production copies any attachments and reserves a bounded fallback.",
    )


def _container_entry(total_target_bytes: int | None) -> StreamBudgetEntry:
    percent_reserve = _ceiling_fraction(total_target_bytes, 100) if total_target_bytes is not None else 0
    budget_bytes = max(DEFAULT_CONTAINER_OVERHEAD_BYTES, percent_reserve)
    return StreamBudgetEntry(
        category="container",
        source_index=None,
        action="reserve",
        source_codec=None,
        output_codec=None,
        budget_bytes=budget_bytes,
        lower_bound_bytes=0,
        upper_bound_bytes=budget_bytes,
        bitrate_bps=None,
        provenance="fallback_max_4mb_or_1_percent_of_target",
        confidence="low",
        requires_measurement=False,
        rationale="Container overhead is not measured yet, so the ledger reserves the greater of 4 MB or 1% of target bytes.",
    )


def _unknown_entry(
        *,
        category: BudgetCategory,
        stream: PlannedStream,
        provenance: str,
        rationale: str,
) -> StreamBudgetEntry:
    return StreamBudgetEntry(
        category=category,
        source_index=stream.source_index,
        action=stream.action,
        source_codec=stream.source_codec,
        output_codec=stream.output_codec,
        budget_bytes=None,
        lower_bound_bytes=None,
        upper_bound_bytes=None,
        bitrate_bps=stream.output_bitrate_bps or stream.source_bitrate_bps,
        provenance=provenance,
        confidence="unknown",
        requires_measurement=True,
        rationale=rationale,
    )


def _feasibility(
        *,
        size_goal_status: str,
        total_target_bytes: int | None,
        minimum_non_video_bytes: int,
        non_video_bytes: int | None,
        remaining_video_bytes: int | None,
        remaining_video_bitrate_bps: int | None,
        duration_seconds: float | None,
        source_size_bytes: int | None,
        source_video_bitrate_bps: int | None,
        entries: list[StreamBudgetEntry],
) -> tuple[FeasibilityStatus, tuple[str, ...]]:
    if total_target_bytes is not None and total_target_bytes <= minimum_non_video_bytes:
        return "arithmetically_infeasible", ("target_consumed_by_minimum_non_video_bytes",)
    if remaining_video_bytes is not None and remaining_video_bytes <= 0:
        return "arithmetically_infeasible", ("target_consumed_by_reserved_non_video_bytes",)

    measurement_reasons: list[str] = []
    if total_target_bytes is None:
        measurement_reasons.append(f"size_goal_{size_goal_status}")
    if duration_seconds is None:
        measurement_reasons.append("item_runtime_unknown")
    measurement_reasons.extend(
        f"{entry.category}_{entry.provenance}"
        for entry in entries
        if entry.requires_measurement
    )
    if non_video_bytes is None:
        measurement_reasons.append("non_video_total_unknown")
    if measurement_reasons:
        return "requires_measurement", tuple(dict.fromkeys(measurement_reasons))

    aggressive_reasons: list[str] = []
    total_source_percent = _percent_of_source(total_target_bytes, source_size_bytes)
    if total_source_percent is not None and total_source_percent <= AGGRESSIVE_TOTAL_SOURCE_PERCENT:
        aggressive_reasons.append("target_at_or_below_10_percent_of_source")
    if remaining_video_bitrate_bps is not None and remaining_video_bitrate_bps <= AGGRESSIVE_VIDEO_BITRATE_BPS:
        aggressive_reasons.append("remaining_video_bitrate_at_or_below_500_kbps")
    if (
            remaining_video_bitrate_bps is not None
            and source_video_bitrate_bps is not None
            and remaining_video_bitrate_bps <= source_video_bitrate_bps * AGGRESSIVE_SOURCE_VIDEO_RATIO
    ):
        aggressive_reasons.append("remaining_video_bitrate_at_or_below_20_percent_of_source_video")
    if aggressive_reasons:
        return "aggressive_but_measurable", tuple(dict.fromkeys(aggressive_reasons))
    return "feasible", ()


def _source_cap_status(
        *,
        source_cap_total_bytes: int | None,
        source_cap_video_bytes: int | None,
        minimum_non_video_bytes: int,
        non_video_bytes: int | None,
) -> SourceCapStatus:
    if source_cap_total_bytes is None:
        return "unavailable"
    if source_cap_total_bytes <= minimum_non_video_bytes:
        return "arithmetically_infeasible"
    if source_cap_video_bytes is not None and source_cap_video_bytes <= 0:
        return "arithmetically_infeasible"
    if non_video_bytes is None or source_cap_video_bytes is None:
        return "requires_measurement"
    return "available"


def _category_total(entries: list[StreamBudgetEntry], category: BudgetCategory) -> int | None:
    selected = [entry.budget_bytes for entry in entries if entry.category == category]
    if any(value is None for value in selected):
        return None
    return sum(value or 0 for value in selected)


def _known_total(entries: list[StreamBudgetEntry]) -> int | None:
    if any(entry.budget_bytes is None for entry in entries):
        return None
    return sum(entry.budget_bytes or 0 for entry in entries)


def _upper_total(entries: list[StreamBudgetEntry]) -> int | None:
    if any(entry.upper_bound_bytes is None for entry in entries):
        return None
    return sum(entry.upper_bound_bytes or 0 for entry in entries)


def _aggregate_confidence(entries: list[StreamBudgetEntry]) -> EstimateConfidence:
    ranking = {"exact": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
    return min((entry.confidence for entry in entries), key=ranking.__getitem__, default="unknown")


def _entry_from_payload(payload: dict[str, Any]) -> StreamBudgetEntry:
    category = str(payload.get("category") or "")
    confidence = str(payload.get("confidence") or "")
    if category not in {"audio", "subtitle", "attachment", "container"}:
        raise StreamBudgetIdentityError("The stream budget ledger has an unsupported entry category.")
    if confidence not in {"exact", "high", "medium", "low", "unknown"}:
        raise StreamBudgetIdentityError("The stream budget ledger has an unsupported entry confidence.")
    return StreamBudgetEntry(
        category=category,  # type: ignore[arg-type]
        source_index=_optional_int(payload.get("source_index")),
        action=str(payload.get("action") or ""),
        source_codec=_optional_text(payload.get("source_codec")),
        output_codec=_optional_text(payload.get("output_codec")),
        budget_bytes=_optional_nonnegative_int(payload.get("budget_bytes")),
        lower_bound_bytes=_optional_nonnegative_int(payload.get("lower_bound_bytes")),
        upper_bound_bytes=_optional_nonnegative_int(payload.get("upper_bound_bytes")),
        bitrate_bps=_positive_int(payload.get("bitrate_bps")),
        provenance=str(payload.get("provenance") or ""),
        confidence=confidence,  # type: ignore[arg-type]
        requires_measurement=bool(payload.get("requires_measurement")),
        rationale=str(payload.get("rationale") or ""),
    )


def _ledger_id(payload: Mapping[str, Any]) -> str:
    return f"sb1_{stable_json_hash(payload)[:32]}"


def _bytes_for_bitrate(bitrate_bps: int | None, duration_seconds: float | None) -> int | None:
    if bitrate_bps is None or duration_seconds is None:
        return None
    value = Decimal(bitrate_bps) * Decimal(str(duration_seconds)) / Decimal(8)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _bitrate_for_bytes(byte_count: int | None, duration_seconds: float | None) -> int | None:
    if byte_count is None or duration_seconds is None:
        return None
    value = Decimal(byte_count) * Decimal(8) / Decimal(str(duration_seconds))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _percentage_bounds(value: int, lower_multiplier: float, upper_multiplier: float) -> tuple[int, int]:
    decimal_value = Decimal(value)
    lower = int((decimal_value * Decimal(str(lower_multiplier))).quantize(Decimal("1"), rounding=ROUND_FLOOR))
    upper = int((decimal_value * Decimal(str(upper_multiplier))).quantize(Decimal("1"), rounding=ROUND_CEILING))
    return lower, upper


def _percent_bytes(source_size_bytes: int | None, percent: float | None) -> int | None:
    if source_size_bytes is None or percent is None:
        return None
    value = Decimal(source_size_bytes) * Decimal(str(percent)) / Decimal(100)
    return int(value.quantize(Decimal("1"), rounding=ROUND_FLOOR))


def _percent_of_source(
        value_bytes: int | None,
        source_size_bytes: int | None,
        *,
        floor: bool = False,
) -> float | None:
    if value_bytes is None or source_size_bytes is None:
        return None
    value = Decimal(value_bytes) * Decimal(100) / Decimal(source_size_bytes)
    rounding = ROUND_FLOOR if floor else ROUND_HALF_UP
    return float(value.quantize(Decimal("0.000001"), rounding=rounding))


def _ceiling_fraction(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value: object) -> int | None:
    parsed = int_value(value)
    return parsed if parsed > 0 else None


def _positive_float(value: object) -> float | None:
    parsed = float_value(value)
    return parsed if parsed > 0 else None


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    parsed = int_value(value)
    return parsed if parsed >= 0 else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int_value(value)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float_value(value)


def _same_optional_float(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= 0.001
