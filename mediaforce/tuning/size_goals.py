import copy
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from mediaforce.core.type_defs import float_value, int_value, object_dict
from mediaforce.tuning.compression_intent import (
    CompressionIntentV1,
    compression_intent_from_policy,
    compression_intent_from_request,
    legacy_compression_intent,
)


DECIMAL_MEGABYTE_BYTES = 1_000_000
SIZE_GOAL_SCHEMA_VERSION = 1
OPERATOR_INTENT_SCHEMA_VERSION = 2
DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT = 10.0
DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT = 5.0

SizeGoalMode = Literal["normalized", "absolute", "ambiguous"]
ResolutionIntentMode = Literal["source", "max_height", "ambiguous"]

_AUDIO_INTENT_KEYS = (
    "keep_languages",
    "copy_codecs",
    "convert_to_opus_codecs",
    "stereo_opus_bitrate",
    "surround_5_1_opus_bitrate",
    "surround_7_1_opus_bitrate",
)
_SUBTITLE_INTENT_KEYS = (
    "keep_languages",
    "prefer_text",
    "keep_forced",
    "default_mode",
)


def megabytes_to_bytes(value: float) -> int:
    decimal_value = max(Decimal(0), Decimal(str(value))) * Decimal(DECIMAL_MEGABYTE_BYTES)
    return int(decimal_value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def bytes_to_megabytes(value: int | float | None) -> float | None:
    if value is None or value <= 0:
        return None
    return round(float(value) / DECIMAL_MEGABYTE_BYTES, 3)


def _positive_int(value: Any) -> int | None:
    parsed = int_value(value)
    return parsed if parsed > 0 else None


def _positive_float(value: Any) -> float | None:
    parsed = float_value(value)
    return parsed if parsed > 0 else None


def _normalized_tolerance(value: Any, fallback: float) -> float:
    parsed = _positive_float(value)
    if parsed is None:
        return fallback
    return round(min(parsed, 100.0), 3)


def _same_number(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= 0.001


@dataclass(frozen=True, slots=True)
class SizeGoalIntent:
    mode: SizeGoalMode
    value_bytes: int | None
    reference_runtime_seconds: float | None
    sample_projection_tolerance_percent: float
    final_output_tolerance_percent: float
    source: str
    requires_confirmation: bool = False

    def resolve(self, item_runtime_seconds: float | None) -> "ResolvedSizeGoal":
        runtime_seconds = item_runtime_seconds if item_runtime_seconds is not None and item_runtime_seconds > 0 else None
        target_size_bytes = None
        status = "resolved"
        if self.mode == "ambiguous" or self.requires_confirmation:
            status = "ambiguous"
        elif self.value_bytes is None or self.value_bytes <= 0:
            status = "missing_target"
        elif self.mode == "absolute":
            target_size_bytes = self.value_bytes
        elif self.reference_runtime_seconds is None or self.reference_runtime_seconds <= 0:
            status = "missing_reference_runtime"
        elif runtime_seconds is None:
            status = "missing_item_runtime"
        else:
            target_size_bytes = int(round(self.value_bytes * runtime_seconds / self.reference_runtime_seconds))

        if status == "ambiguous":
            rationale = (
                "This legacy size value does not say whether it is a normalized runtime goal or an absolute "
                "per-episode target. Choose a size again before starting new work."
            )
        elif status == "missing_target":
            rationale = "A positive size value is required before Mediaforce can start a test."
        elif status == "missing_reference_runtime":
            rationale = "A normalized size goal needs a positive reference runtime."
        elif status == "missing_item_runtime":
            rationale = "Mediaforce needs the episode runtime before it can resolve this normalized size goal."
        elif self.mode == "absolute":
            target_mb = bytes_to_megabytes(target_size_bytes) or 0
            rationale = f"{target_mb:g} MB is an absolute per-episode target and does not scale with runtime."
        else:
            reference_mb = bytes_to_megabytes(self.value_bytes) or 0
            reference_minutes = (self.reference_runtime_seconds or 0) / 60.0
            target_mb = bytes_to_megabytes(target_size_bytes) or 0
            item_minutes = (runtime_seconds or 0) / 60.0
            rationale = (
                f"{reference_mb:g} MB per {reference_minutes:g} minutes scales to about {target_mb:.0f} MB "
                f"for this {item_minutes:.0f}-minute episode."
            )

        return ResolvedSizeGoal(
            intent=self,
            item_runtime_seconds=runtime_seconds,
            target_size_bytes=target_size_bytes,
            status=status,
            rationale=rationale,
        )

    def policy_fragment(self, *, item_runtime_seconds: float | None = None) -> dict[str, Any]:
        video: dict[str, Any] = {
            "size_goal_schema_version": SIZE_GOAL_SCHEMA_VERSION,
            "size_goal_mode": self.mode,
            "size_goal_source": self.source,
            "sample_projection_tolerance_percent": self.sample_projection_tolerance_percent,
            "final_output_tolerance_percent": self.final_output_tolerance_percent,
        }
        value_mb = bytes_to_megabytes(self.value_bytes)
        if self.value_bytes is not None and self.value_bytes > 0:
            video["target_size_bytes"] = self.value_bytes
        if value_mb is not None:
            video["target_size_mb"] = value_mb
        if self.mode == "normalized" and self.reference_runtime_seconds is not None:
            video["target_runtime_minutes"] = round(self.reference_runtime_seconds / 60.0, 3)
        elif self.mode == "absolute" and item_runtime_seconds is not None and item_runtime_seconds > 0:
            video["target_runtime_minutes"] = round(item_runtime_seconds / 60.0, 3)
        return {"video": video}

    def request_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode,
            "value_mb": bytes_to_megabytes(self.value_bytes),
            "sample_projection_tolerance_percent": self.sample_projection_tolerance_percent,
            "final_output_tolerance_percent": self.final_output_tolerance_percent,
        }
        if self.reference_runtime_seconds is not None:
            payload["reference_runtime_minutes"] = round(self.reference_runtime_seconds / 60.0, 3)
        return payload


@dataclass(frozen=True, slots=True)
class ResolvedSizeGoal:
    intent: SizeGoalIntent
    item_runtime_seconds: float | None
    target_size_bytes: int | None
    status: str
    rationale: str

    @property
    def requires_confirmation(self) -> bool:
        return self.intent.requires_confirmation or self.status != "resolved"

    def _bounds(self, tolerance_percent: float) -> tuple[int | None, int | None]:
        if self.target_size_bytes is None:
            return None, None
        tolerance = tolerance_percent / 100.0
        return (
            int(round(self.target_size_bytes * (1.0 - tolerance))),
            int(round(self.target_size_bytes * (1.0 + tolerance))),
        )

    def to_payload(self) -> dict[str, Any]:
        sample_lower, sample_upper = self._bounds(self.intent.sample_projection_tolerance_percent)
        final_lower, final_upper = self._bounds(self.intent.final_output_tolerance_percent)
        return {
            "schema_version": SIZE_GOAL_SCHEMA_VERSION,
            "mode": self.intent.mode,
            "source": self.intent.source,
            "status": self.status,
            "requires_confirmation": self.requires_confirmation,
            "reference_size_mb": bytes_to_megabytes(self.intent.value_bytes),
            "reference_runtime_minutes": (
                round(self.intent.reference_runtime_seconds / 60.0, 3)
                if self.intent.reference_runtime_seconds is not None
                else None
            ),
            "item_runtime_seconds": self.item_runtime_seconds,
            "target_size_bytes": self.target_size_bytes,
            "target_size_mb": bytes_to_megabytes(self.target_size_bytes),
            "sample_projection_tolerance_percent": self.intent.sample_projection_tolerance_percent,
            "sample_lower_bound_bytes": sample_lower,
            "sample_upper_bound_bytes": sample_upper,
            "final_output_tolerance_percent": self.intent.final_output_tolerance_percent,
            "final_lower_bound_bytes": final_lower,
            "final_upper_bound_bytes": final_upper,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ResolutionIntent:
    mode: ResolutionIntentMode
    max_height: int | None
    source: str
    requires_confirmation: bool = False

    def policy_fragment(self) -> dict[str, Any]:
        video: dict[str, Any] = {
            "resolution_intent_mode": self.mode,
            "resolution_intent_source": self.source,
        }
        if self.mode == "source":
            video["max_height"] = 0
        elif self.mode == "max_height" and self.max_height is not None:
            video["max_height"] = self.max_height
        return {"video": video}

    def request_payload(self) -> dict[str, Any]:
        return {"mode": self.mode, "max_height": self.max_height}

    def to_payload(self) -> dict[str, Any]:
        if self.mode == "source":
            rationale = "Keep the source resolution."
        elif self.mode == "max_height" and self.max_height is not None:
            rationale = f"Limit the encoded height to {self.max_height}p."
        else:
            rationale = "The legacy resolution value needs confirmation before new work starts."
        return {
            "mode": self.mode,
            "max_height": self.max_height,
            "source": self.source,
            "requires_confirmation": self.requires_confirmation,
            "rationale": rationale,
        }


@dataclass(frozen=True, slots=True)
class QualityIntent:
    metric: str
    target_vmaf: float | None
    min_target_vmaf: float | None
    target_xpsnr: float | None
    min_target_xpsnr: float | None
    source: str

    def policy_fragment(self) -> dict[str, Any]:
        video: dict[str, Any] = {"quality_metric": self.metric}
        for key, value in (
            ("target_vmaf", self.target_vmaf),
            ("min_target_vmaf", self.min_target_vmaf),
            ("target_xpsnr", self.target_xpsnr),
            ("min_target_xpsnr", self.min_target_xpsnr),
        ):
            if value is not None:
                video[key] = value
        return {"video": video}

    def request_payload(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "target_vmaf": self.target_vmaf,
            "min_target_vmaf": self.min_target_vmaf,
            "target_xpsnr": self.target_xpsnr,
            "min_target_xpsnr": self.min_target_xpsnr,
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.request_payload(), "source": self.source}


@dataclass(frozen=True, slots=True)
class StreamIntent:
    audio: dict[str, Any]
    subtitle: dict[str, Any]
    source: str

    def policy_fragment(self) -> dict[str, Any]:
        fragment: dict[str, Any] = {}
        if self.audio:
            fragment["audio"] = copy.deepcopy(self.audio)
        if self.subtitle:
            fragment["subtitle"] = copy.deepcopy(self.subtitle)
        return fragment

    def request_payload(self) -> dict[str, Any]:
        return {
            "audio": copy.deepcopy(self.audio),
            "subtitle": copy.deepcopy(self.subtitle),
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.request_payload(), "source": self.source}


@dataclass(frozen=True, slots=True)
class OperatorIntent:
    size_goal: SizeGoalIntent
    resolution: ResolutionIntent
    compression_intent: CompressionIntentV1 = field(default_factory=legacy_compression_intent)
    quality: QualityIntent | None = None
    streams: StreamIntent | None = None

    @property
    def requires_confirmation(self) -> bool:
        return (
            self.size_goal.requires_confirmation
            or self.resolution.requires_confirmation
            or self.compression_intent.requires_confirmation
        )

    def policy_fragment(self, *, item_runtime_seconds: float | None = None) -> dict[str, Any]:
        video = dict(self.size_goal.policy_fragment(item_runtime_seconds=item_runtime_seconds)["video"])
        video.update(self.resolution.policy_fragment()["video"])
        video.update(self.compression_intent.policy_fragment()["video"])
        if self.quality is not None:
            video.update(self.quality.policy_fragment()["video"])
        fragment: dict[str, Any] = {"video": video}
        if self.streams is not None:
            fragment.update(self.streams.policy_fragment())
        return fragment

    def request_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": OPERATOR_INTENT_SCHEMA_VERSION,
            "size_goal": self.size_goal.request_payload(),
            "resolution": self.resolution.request_payload(),
            "compression_intent": self.compression_intent.request_payload(),
        }
        if self.quality is not None:
            payload["quality"] = self.quality.request_payload()
        if self.streams is not None:
            payload["streams"] = self.streams.request_payload()
        return payload

    def to_payload(self, *, item_runtime_seconds: float | None) -> dict[str, Any]:
        size_goal = self.size_goal.resolve(item_runtime_seconds)
        payload: dict[str, Any] = {
            "schema_version": OPERATOR_INTENT_SCHEMA_VERSION,
            "requires_confirmation": self.requires_confirmation or size_goal.requires_confirmation,
            "size_goal": size_goal.to_payload(),
            "resolution": self.resolution.to_payload(),
            "compression_intent": self.compression_intent.to_payload(),
            "request": (
                None
                if self.requires_confirmation or size_goal.requires_confirmation
                else self.request_payload()
            ),
        }
        if self.quality is not None:
            payload["quality"] = self.quality.to_payload()
        if self.streams is not None:
            payload["streams"] = self.streams.to_payload()
        return payload


def size_goal_from_policy(
        video_policy: dict[str, Any],
        *,
        default_video_policy: dict[str, Any] | None = None,
) -> SizeGoalIntent:
    video = object_dict(video_policy)
    default_video = object_dict(default_video_policy)
    raw_mode = str(video.get("size_goal_mode") or "").strip().lower()
    mode: SizeGoalMode
    source = str(video.get("size_goal_source") or "policy").strip() or "policy"
    requires_confirmation = False
    if raw_mode in {"normalized", "absolute", "ambiguous"}:
        mode = raw_mode  # type: ignore[assignment]
        requires_confirmation = mode == "ambiguous"
    else:
        target_size = _positive_float(video.get("target_size_mb"))
        target_runtime = _positive_float(video.get("target_runtime_minutes"))
        default_size = _positive_float(default_video.get("target_size_mb"))
        default_runtime = _positive_float(default_video.get("target_runtime_minutes"))
        if _same_number(target_size, default_size) and _same_number(target_runtime, default_runtime):
            mode = "normalized"
            source = "legacy_default"
        elif target_runtime is not None and default_runtime is not None and not _same_number(
                target_runtime, default_runtime
        ):
            mode = "absolute"
            source = "legacy_inferred_absolute"
        else:
            mode = "ambiguous"
            source = "legacy_ambiguous"
            requires_confirmation = True

    target_size_bytes = _positive_int(video.get("target_size_bytes"))
    target_size_mb = _positive_float(video.get("target_size_mb"))
    target_runtime_minutes = _positive_float(video.get("target_runtime_minutes"))
    reference_runtime_seconds = (
        target_runtime_minutes * 60.0
        if mode in {"normalized", "ambiguous"} and target_runtime_minutes is not None
        else None
    )
    return SizeGoalIntent(
        mode=mode,
        value_bytes=(
            target_size_bytes
            if target_size_bytes is not None
            else megabytes_to_bytes(target_size_mb) if target_size_mb is not None else None
        ),
        reference_runtime_seconds=reference_runtime_seconds,
        sample_projection_tolerance_percent=_normalized_tolerance(
            video.get("sample_projection_tolerance_percent"),
            DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
        ),
        final_output_tolerance_percent=_normalized_tolerance(
            video.get("final_output_tolerance_percent"),
            DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
        ),
        source=source,
        requires_confirmation=requires_confirmation,
    )


def resolution_intent_from_policy(video_policy: dict[str, Any]) -> ResolutionIntent:
    video = object_dict(video_policy)
    raw_mode = str(video.get("resolution_intent_mode") or "").strip().lower()
    source = str(video.get("resolution_intent_source") or "policy").strip() or "policy"
    max_height = int_value(video.get("max_height")) if video.get("max_height") is not None else None
    if raw_mode == "source":
        return ResolutionIntent("source", None, source)
    if raw_mode == "max_height" and max_height is not None and max_height > 0:
        return ResolutionIntent("max_height", max_height, source)
    if raw_mode == "ambiguous":
        return ResolutionIntent("ambiguous", max_height, source, True)
    if max_height == 0:
        return ResolutionIntent("source", None, "legacy_inferred")
    if max_height is not None and max_height > 0:
        return ResolutionIntent("max_height", max_height, "legacy_inferred")
    return ResolutionIntent("ambiguous", None, "legacy_ambiguous", True)


def quality_intent_from_policy(video_policy: dict[str, Any], *, source: str = "policy") -> QualityIntent:
    video = object_dict(video_policy)
    metric = str(video.get("quality_metric") or video.get("metric") or "auto").strip().lower()
    if metric not in {"auto", "vmaf", "xpsnr"}:
        metric = "auto"
    return QualityIntent(
        metric=metric,
        target_vmaf=_positive_float(video.get("target_vmaf")),
        min_target_vmaf=_positive_float(video.get("min_target_vmaf")),
        target_xpsnr=_positive_float(video.get("target_xpsnr")),
        min_target_xpsnr=_positive_float(video.get("min_target_xpsnr")),
        source=source,
    )


def stream_intent_from_policy(
        audio_policy: dict[str, Any] | None,
        subtitle_policy: dict[str, Any] | None,
        *,
        source: str = "policy",
) -> StreamIntent:
    return StreamIntent(
        audio=_allowed_policy_values(object_dict(audio_policy), _AUDIO_INTENT_KEYS),
        subtitle=_allowed_policy_values(object_dict(subtitle_policy), _SUBTITLE_INTENT_KEYS),
        source=source,
    )


def operator_intent_from_policy(
        video_policy: dict[str, Any],
        *,
        default_video_policy: dict[str, Any] | None = None,
        audio_policy: dict[str, Any] | None = None,
        subtitle_policy: dict[str, Any] | None = None,
) -> OperatorIntent:
    return OperatorIntent(
        size_goal=size_goal_from_policy(video_policy, default_video_policy=default_video_policy),
        resolution=resolution_intent_from_policy(video_policy),
        compression_intent=compression_intent_from_policy(
            video_policy,
            default_video_policy=default_video_policy,
        ),
        quality=quality_intent_from_policy(video_policy),
        streams=stream_intent_from_policy(audio_policy, subtitle_policy),
    )


def operator_intent_from_request(payload: dict[str, Any]) -> OperatorIntent:
    request = object_dict(payload)
    schema_version = int_value(request.get("schema_version"))
    if schema_version == SIZE_GOAL_SCHEMA_VERSION:
        raise ValueError(
            "This size request predates compression goals. Refresh and choose the size and compression goal again."
        )
    if schema_version != OPERATOR_INTENT_SCHEMA_VERSION:
        raise ValueError("The size request uses an unsupported schema version. Refresh and choose the size again.")
    size_payload = object_dict(request.get("size_goal"))
    mode = str(size_payload.get("mode") or "").strip().lower()
    if mode not in {"normalized", "absolute"}:
        raise ValueError("Choose whether the size is runtime-normalized or an absolute per-episode target.")
    value_mb = _positive_float(size_payload.get("value_mb"))
    if value_mb is None:
        raise ValueError("Choose a positive episode size before starting the test.")
    reference_runtime_minutes = _positive_float(size_payload.get("reference_runtime_minutes"))
    if mode == "normalized" and reference_runtime_minutes is None:
        raise ValueError("A runtime-normalized size needs a positive reference runtime.")
    size_goal = SizeGoalIntent(
        mode=mode,  # type: ignore[arg-type]
        value_bytes=megabytes_to_bytes(value_mb),
        reference_runtime_seconds=(
            reference_runtime_minutes * 60.0 if reference_runtime_minutes is not None else None
        ),
        sample_projection_tolerance_percent=_normalized_tolerance(
            size_payload.get("sample_projection_tolerance_percent"),
            DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT,
        ),
        final_output_tolerance_percent=_normalized_tolerance(
            size_payload.get("final_output_tolerance_percent"),
            DEFAULT_FINAL_OUTPUT_TOLERANCE_PERCENT,
        ),
        source="operator",
    )

    resolution_payload = object_dict(request.get("resolution"))
    resolution_mode = str(resolution_payload.get("mode") or "source").strip().lower()
    if resolution_mode == "source":
        resolution = ResolutionIntent("source", None, "operator")
    elif resolution_mode == "max_height":
        max_height = int_value(resolution_payload.get("max_height"))
        if max_height <= 0:
            raise ValueError("Choose a positive maximum height or keep the source resolution.")
        resolution = ResolutionIntent("max_height", max_height, "operator")
    else:
        raise ValueError("Choose a supported resolution preference before starting the test.")

    quality_payload = object_dict(request.get("quality"))
    quality = quality_intent_from_policy(quality_payload, source="operator") if quality_payload else None
    streams_payload = object_dict(request.get("streams"))
    streams = (
        stream_intent_from_policy(
            object_dict(streams_payload.get("audio")),
            object_dict(streams_payload.get("subtitle")),
            source="operator",
        )
        if streams_payload
        else None
    )
    compression_intent = compression_intent_from_request(object_dict(request.get("compression_intent")))
    return OperatorIntent(
        size_goal=size_goal,
        resolution=resolution,
        compression_intent=compression_intent,
        quality=quality,
        streams=streams,
    )


def guided_size_goal_options(
        video_policy: dict[str, Any],
        *,
        item_runtime_seconds: float | None,
        default_video_policy: dict[str, Any],
        audio_policy: dict[str, Any] | None = None,
        subtitle_policy: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    intent = size_goal_from_policy(video_policy, default_video_policy=default_video_policy)
    resolved = intent.resolve(item_runtime_seconds)
    if intent.requires_confirmation or intent.mode == "ambiguous":
        return _legacy_confirmation_options(
            intent,
            video_policy=video_policy,
            default_video_policy=default_video_policy,
            item_runtime_seconds=item_runtime_seconds,
            audio_policy=audio_policy,
            subtitle_policy=subtitle_policy,
        )
    if intent.value_bytes is None or intent.value_bytes <= 0:
        return []
    if resolved.target_size_bytes is None:
        return []
    context_intent = operator_intent_from_policy(
        video_policy,
        default_video_policy=default_video_policy,
        audio_policy=audio_policy,
        subtitle_policy=subtitle_policy,
    )
    options = []
    for key, title, multiplier in (
            ("recommended", "Recommended", 1.0),
            ("smaller", "Save more space", 0.75),
            ("roomier", "Keep more detail", 1.5),
    ):
        option_intent = SizeGoalIntent(
            mode=intent.mode,
            value_bytes=int(round(intent.value_bytes * multiplier)),
            reference_runtime_seconds=intent.reference_runtime_seconds,
            sample_projection_tolerance_percent=intent.sample_projection_tolerance_percent,
            final_output_tolerance_percent=intent.final_output_tolerance_percent,
            source="guided_preset",
        )
        operator_intent = OperatorIntent(
            size_goal=option_intent,
            resolution=ResolutionIntent("source", None, "guided_preset"),
            compression_intent=context_intent.compression_intent,
            quality=context_intent.quality,
            streams=context_intent.streams,
        )
        option_goal = option_intent.resolve(item_runtime_seconds)
        options.append(
            {
                "key": key,
                "title": title,
                "detail": option_goal.rationale,
                "requires_explicit_selection": False,
                "operator_intent": operator_intent.request_payload(),
                "resolved_size_goal": option_goal.to_payload(),
            }
        )
    return options


def _legacy_confirmation_options(
        intent: SizeGoalIntent,
        *,
        video_policy: dict[str, Any],
        default_video_policy: dict[str, Any],
        item_runtime_seconds: float | None,
        audio_policy: dict[str, Any] | None,
        subtitle_policy: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if intent.value_bytes is None or intent.value_bytes <= 0:
        return []
    context_intent = OperatorIntent(
        size_goal=intent,
        resolution=resolution_intent_from_policy(video_policy),
        compression_intent=compression_intent_from_policy(
            video_policy,
            default_video_policy=default_video_policy,
        ),
        quality=quality_intent_from_policy(video_policy),
        streams=stream_intent_from_policy(audio_policy, subtitle_policy),
    )
    candidates: list[tuple[str, str, SizeGoalIntent]] = []
    if intent.reference_runtime_seconds is not None and intent.reference_runtime_seconds > 0:
        candidates.append(
            (
                "confirm_normalized",
                "Scale with runtime",
                SizeGoalIntent(
                    mode="normalized",
                    value_bytes=intent.value_bytes,
                    reference_runtime_seconds=intent.reference_runtime_seconds,
                    sample_projection_tolerance_percent=intent.sample_projection_tolerance_percent,
                    final_output_tolerance_percent=intent.final_output_tolerance_percent,
                    source="legacy_confirmation",
                ),
            )
        )
    candidates.append(
        (
            "confirm_absolute",
            "Keep this size per episode",
            SizeGoalIntent(
                mode="absolute",
                value_bytes=intent.value_bytes,
                reference_runtime_seconds=None,
                sample_projection_tolerance_percent=intent.sample_projection_tolerance_percent,
                final_output_tolerance_percent=intent.final_output_tolerance_percent,
                source="legacy_confirmation",
            ),
        )
    )
    options = []
    for key, title, confirmed_goal in candidates:
        operator_intent = OperatorIntent(
            size_goal=confirmed_goal,
            resolution=ResolutionIntent("source", None, "legacy_confirmation"),
            compression_intent=context_intent.compression_intent,
            quality=context_intent.quality,
            streams=context_intent.streams,
        )
        resolved_goal = confirmed_goal.resolve(item_runtime_seconds)
        options.append(
            {
                "key": key,
                "title": title,
                "detail": resolved_goal.rationale,
                "requires_explicit_selection": True,
                "operator_intent": operator_intent.request_payload(),
                "resolved_size_goal": resolved_goal.to_payload(),
            }
        )
    return options


def _allowed_policy_values(policy: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(policy[key])
        for key in keys
        if key in policy
    }
