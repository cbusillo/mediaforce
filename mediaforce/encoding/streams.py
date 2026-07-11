import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import item_events
from mediaforce.core.evidence import stable_json_hash, stable_policy_hash, stable_source_id
from mediaforce.core.type_defs import float_value, int_value, object_dict, object_list
from mediaforce.core.utils import timestamp
from mediaforce.reviewing.helpers import planned_audio_action, planned_opus_bitrate, select_primary_audio_track


TEXT_SUBTITLE_CODECS = frozenset({"ass", "mov_text", "srt", "ssa", "subrip", "text", "webvtt"})
STREAM_PLAN_SCHEMA_VERSION = 1
ATTACHMENT_CAPABLE_CONTAINERS = frozenset({"matroska", "mka", "mks", "mkv"})

StreamKind = Literal["audio", "subtitle", "attachment"]
StreamAction = Literal["copy", "transcode", "drop"]


class StreamPlanIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PlannedStream:
    kind: StreamKind
    source_index: int
    source_codec: str
    action: StreamAction
    output_codec: str | None
    codec_argument: str | None
    output_bitrate_bps: int | None = None
    output_bitrate_text: str | None = None
    channels: int | None = None
    language: str | None = None
    default: bool = False
    forced: bool = False
    source_bitrate_bps: int | None = None
    source_duration_seconds: float | None = None
    source_size_bytes: int | None = None
    file_name: str | None = None
    mime_type: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "source_index": self.source_index,
            "source_codec": self.source_codec,
            "action": self.action,
            "output_codec": self.output_codec,
            "codec_argument": self.codec_argument,
            "output_bitrate_bps": self.output_bitrate_bps,
            "output_bitrate_text": self.output_bitrate_text,
            "channels": self.channels,
            "language": self.language,
            "default": self.default,
            "forced": self.forced,
            "source_bitrate_bps": self.source_bitrate_bps,
            "source_duration_seconds": self.source_duration_seconds,
            "source_size_bytes": self.source_size_bytes,
            "file_name": self.file_name,
            "mime_type": self.mime_type,
        }


@dataclass(frozen=True, slots=True)
class ProductionStreamPlan:
    plan_id: str
    source_id: str
    source_fingerprint: str | None
    policy_hash: str
    output_container: str
    attachments_known: bool
    copy_unknown_attachments: bool
    streams: tuple[PlannedStream, ...]

    @property
    def audio_streams(self) -> tuple[PlannedStream, ...]:
        return tuple(stream for stream in self.streams if stream.kind == "audio")

    @property
    def subtitle_streams(self) -> tuple[PlannedStream, ...]:
        return tuple(stream for stream in self.streams if stream.kind == "subtitle")

    @property
    def attachment_streams(self) -> tuple[PlannedStream, ...]:
        return tuple(stream for stream in self.streams if stream.kind == "attachment")

    @property
    def copies_attachments(self) -> bool:
        return self.output_container in ATTACHMENT_CAPABLE_CONTAINERS and (
            self.copy_unknown_attachments
            or any(stream.action == "copy" for stream in self.attachment_streams)
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": STREAM_PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "policy_hash": self.policy_hash,
            "output_container": self.output_container,
            "attachments_known": self.attachments_known,
            "copy_unknown_attachments": self.copy_unknown_attachments,
            "streams": [stream.to_payload() for stream in self.streams],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProductionStreamPlan":
        plan_payload = object_dict(payload)
        if int_value(plan_payload.get("schema_version")) != STREAM_PLAN_SCHEMA_VERSION:
            raise StreamPlanIdentityError("The production stream plan uses an unsupported schema version.")
        streams = tuple(_planned_stream_from_payload(object_dict(stream)) for stream in object_list(plan_payload.get("streams")))
        plan = cls(
            plan_id=str(plan_payload.get("plan_id") or ""),
            source_id=str(plan_payload.get("source_id") or ""),
            source_fingerprint=_optional_text(plan_payload.get("source_fingerprint")),
            policy_hash=str(plan_payload.get("policy_hash") or ""),
            output_container=_normalized_container(plan_payload.get("output_container")),
            attachments_known=bool(plan_payload.get("attachments_known")),
            copy_unknown_attachments=bool(plan_payload.get("copy_unknown_attachments")),
            streams=streams,
        )
        expected_plan_id = _stream_plan_id(plan._identity_payload())
        if not plan.plan_id or plan.plan_id != expected_plan_id:
            raise StreamPlanIdentityError("The production stream plan identity does not match its contents.")
        return plan

    def validate_item(self, item: Mapping[str, Any]) -> None:
        policy = object_dict(item.get("resolved_policy"))
        if self.policy_hash != stable_policy_hash(policy):
            raise StreamPlanIdentityError("The production stream plan does not match the resolved policy.")
        if self.source_id != stable_source_id(item):
            raise StreamPlanIdentityError("The production stream plan does not match the source item.")
        source_fingerprint = _optional_text(item.get("source_fingerprint", item.get("fingerprint")))
        if self.source_fingerprint != source_fingerprint:
            raise StreamPlanIdentityError("The production stream plan source fingerprint is stale.")
        output_container = _output_container(item)
        if output_container and self.output_container != output_container:
            raise StreamPlanIdentityError("The production stream plan does not match the output container.")

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": STREAM_PLAN_SCHEMA_VERSION,
            "source_id": self.source_id,
            "source_fingerprint": self.source_fingerprint,
            "policy_hash": self.policy_hash,
            "output_container": self.output_container,
            "attachments_known": self.attachments_known,
            "copy_unknown_attachments": self.copy_unknown_attachments,
            "streams": [stream.to_payload() for stream in self.streams],
        }


def resolve_stream_plan(
        item: Mapping[str, Any],
        *,
        text_subtitle_codecs: set[str] | frozenset[str] = TEXT_SUBTITLE_CODECS,
) -> ProductionStreamPlan:
    item_payload = object_dict(item)
    policy = object_dict(item_payload.get("resolved_policy"))
    audio_policy = object_dict(policy.get("audio"))
    audio_tracks = [object_dict(track) for track in object_list(item_payload.get("audio_summary"))]
    subtitle_tracks = [object_dict(track) for track in object_list(item_payload.get("subtitle_summary"))]
    selection = {
        "audio_tracks": [_pick_audio(audio_tracks)] if audio_tracks else [],
        "subtitle_tracks": _pick_subtitles(
            subtitle_tracks,
            bool(object_dict(policy.get("subtitle")).get("prefer_text", True)),
            text_subtitle_codecs=set(text_subtitle_codecs),
        ),
    }
    streams: list[PlannedStream] = []

    for track in selection["audio_tracks"]:
        source_codec = str(track.get("codec_name") or "unknown").lower()
        codec_argument = _audio_codec(track, audio_policy)
        output_bitrate_text = _opus_bitrate(track, audio_policy) if codec_argument == "libopus" else None
        streams.append(
            PlannedStream(
                kind="audio",
                source_index=int_value(track.get("index")),
                source_codec=source_codec,
                action="transcode" if codec_argument == "libopus" else "copy",
                output_codec="opus" if codec_argument == "libopus" else source_codec,
                codec_argument=codec_argument,
                output_bitrate_bps=(
                    _parse_bitrate_text(output_bitrate_text)
                    if output_bitrate_text is not None
                    else None
                ),
                output_bitrate_text=output_bitrate_text,
                channels=_positive_int(track.get("channels")),
                language=_optional_text(track.get("language")),
                default=bool(track.get("default")),
                source_bitrate_bps=_positive_int(track.get("bit_rate")),
                source_duration_seconds=_positive_float(track.get("duration_seconds")),
                source_size_bytes=_positive_int(track.get("size_bytes")),
            )
        )

    for track in selection["subtitle_tracks"]:
        source_codec = str(track.get("codec_name") or "unknown").lower()
        is_text = source_codec in text_subtitle_codecs
        streams.append(
            PlannedStream(
                kind="subtitle",
                source_index=int_value(track.get("index")),
                source_codec=source_codec,
                action="transcode" if is_text else "copy",
                output_codec="srt" if is_text else source_codec,
                codec_argument="srt" if is_text else "copy",
                language=_optional_text(track.get("language")),
                default=bool(track.get("default")),
                forced=bool(track.get("forced")),
                source_bitrate_bps=_positive_int(track.get("bit_rate")),
                source_duration_seconds=_positive_float(track.get("duration_seconds")),
                source_size_bytes=_positive_int(track.get("size_bytes")),
            )
        )

    output_container = _output_container(item_payload)
    attachments_value = item_payload.get("attachment_summary")
    attachments_known = isinstance(attachments_value, list)
    attachment_tracks = [object_dict(track) for track in object_list(attachments_value)]
    attachment_action: StreamAction = "copy" if output_container in ATTACHMENT_CAPABLE_CONTAINERS else "drop"
    for track in attachment_tracks:
        source_codec = str(track.get("codec_name") or "unknown").lower()
        streams.append(
            PlannedStream(
                kind="attachment",
                source_index=int_value(track.get("index")),
                source_codec=source_codec,
                action=attachment_action,
                output_codec=source_codec if attachment_action == "copy" else None,
                codec_argument="copy" if attachment_action == "copy" else None,
                source_size_bytes=_positive_int(track.get("size_bytes")),
                file_name=_optional_text(track.get("file_name")),
                mime_type=_optional_text(track.get("mime_type")),
            )
        )

    identity_payload = {
        "schema_version": STREAM_PLAN_SCHEMA_VERSION,
        "source_id": stable_source_id(item_payload),
        "source_fingerprint": _optional_text(item_payload.get("source_fingerprint", item_payload.get("fingerprint"))),
        "policy_hash": stable_policy_hash(policy),
        "output_container": output_container,
        "attachments_known": attachments_known,
        "copy_unknown_attachments": (
            not attachments_known and output_container in ATTACHMENT_CAPABLE_CONTAINERS
        ),
        "streams": [stream.to_payload() for stream in streams],
    }
    return ProductionStreamPlan(
        plan_id=_stream_plan_id(identity_payload),
        source_id=str(identity_payload["source_id"]),
        source_fingerprint=_optional_text(identity_payload.get("source_fingerprint")),
        policy_hash=str(identity_payload["policy_hash"]),
        output_container=output_container,
        attachments_known=attachments_known,
        copy_unknown_attachments=bool(identity_payload["copy_unknown_attachments"]),
        streams=tuple(streams),
    )


def _select_streams(item: dict[str, Any], *, text_subtitle_codecs: set[str]) -> dict[str, Any]:
    audio_tracks = [object_dict(track) for track in object_list(item.get("audio_summary"))]
    subtitle_tracks = [object_dict(track) for track in object_list(item.get("subtitle_summary"))]

    selected_audio = _pick_audio(audio_tracks)
    selected_subtitles = _pick_subtitles(
        subtitle_tracks,
        bool(object_dict(object_dict(item.get("resolved_policy")).get("subtitle")).get("prefer_text", True)),
        text_subtitle_codecs=text_subtitle_codecs,
    )

    return {
        "audio_tracks": [selected_audio],
        "subtitle_tracks": selected_subtitles,
    }


def _pick_audio(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    return select_primary_audio_track(audio_tracks)


def _pick_subtitles(
        subtitle_tracks: list[dict[str, Any]], prefer_text: bool, *, text_subtitle_codecs: set[str]
) -> list[dict[str, Any]]:
    english = [track for track in subtitle_tracks if track.get("language") == "eng"]
    if not english:
        fallback = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
        if fallback and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks):
            english = fallback
        else:
            return []

    def sort_key(track: dict[str, Any]) -> tuple[int, int, int, int]:
        codec = str(track.get("codec_name") or "")
        is_text = codec in text_subtitle_codecs
        return (
            0 if (prefer_text and is_text and not track.get("forced")) else 1,
            0 if (not track.get("forced")) else 1,
            0 if track.get("default") else 1,
            int(track["index"]),
        )

    ordered = sorted(english, key=sort_key)
    forced = [track for track in ordered if track.get("forced")]
    full = [track for track in ordered if not track.get("forced")]
    return full[:1] + forced + full[1:]


def _source_has_preservable_subtitles(subtitle_tracks: list[dict[str, Any]]) -> bool:
    if any(track.get("language") == "eng" for track in subtitle_tracks):
        return True
    untagged = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
    return bool(untagged) and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks)


def _audio_codec(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return planned_audio_action(track, audio_policy)


def _opus_bitrate(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return planned_opus_bitrate(track, audio_policy)


def _opus_layout_filter(track: dict[str, Any]) -> str | None:
    channels = int_value(track.get("channels")) or 2
    if channels >= 8:
        return "channelmap=channel_layout=7.1"
    if channels >= 6:
        return "channelmap=channel_layout=5.1"
    return None


def _check(validation: dict[str, Any], passed: bool, message: str) -> None:
    validation["checks"].append({"passed": passed, "message": message})
    validation["passed"] = validation["passed"] and passed


def _parse_bitrate_text(value: str) -> int:
    stripped = value.strip().lower()
    if stripped.endswith("k"):
        return int(float(stripped[:-1]) * 1000)
    if stripped.endswith("m"):
        return int(float(stripped[:-1]) * 1_000_000)
    return int(float(stripped))


def _record_event(connection: DBClient, library_item_id: int, event_type: str,
                  details: dict[str, Any]) -> None:
    connection.execute(
        item_events.insert().values(
            library_item_id=library_item_id,
            created_at=timestamp(),
            event_type=event_type,
            details_json=json.dumps(details, separators=(",", ":")),
        )
    )


def _format_crf(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"


def _planned_stream_from_payload(payload: dict[str, Any]) -> PlannedStream:
    kind = str(payload.get("kind") or "")
    action = str(payload.get("action") or "")
    if kind not in {"audio", "subtitle", "attachment"}:
        raise StreamPlanIdentityError(f"Unsupported production stream kind: {kind or 'missing'}")
    if action not in {"copy", "transcode", "drop"}:
        raise StreamPlanIdentityError(f"Unsupported production stream action: {action or 'missing'}")
    return PlannedStream(
        kind=kind,  # type: ignore[arg-type]
        source_index=int_value(payload.get("source_index")),
        source_codec=str(payload.get("source_codec") or "unknown"),
        action=action,  # type: ignore[arg-type]
        output_codec=_optional_text(payload.get("output_codec")),
        codec_argument=_optional_text(payload.get("codec_argument")),
        output_bitrate_bps=_positive_int(payload.get("output_bitrate_bps")),
        output_bitrate_text=_optional_text(payload.get("output_bitrate_text")),
        channels=_positive_int(payload.get("channels")),
        language=_optional_text(payload.get("language")),
        default=bool(payload.get("default")),
        forced=bool(payload.get("forced")),
        source_bitrate_bps=_positive_int(payload.get("source_bitrate_bps")),
        source_duration_seconds=_positive_float(payload.get("source_duration_seconds")),
        source_size_bytes=_positive_int(payload.get("source_size_bytes")),
        file_name=_optional_text(payload.get("file_name")),
        mime_type=_optional_text(payload.get("mime_type")),
    )


def _stream_plan_id(payload: Mapping[str, Any]) -> str:
    return f"sp1_{stable_json_hash(payload)[:32]}"


def _output_container(item: Mapping[str, Any]) -> str:
    configured = _normalized_container(item.get("output_container"))
    if configured:
        return configured
    staging_path = str(item.get("staging_path") or "").strip()
    if staging_path:
        suffix = Path(staging_path).suffix
        if suffix:
            return _normalized_container(suffix)
    return _normalized_container(item.get("container"))


def _normalized_container(value: object) -> str:
    return str(value or "").strip().lower().removeprefix(".")


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value: object) -> int | None:
    parsed = int_value(value)
    return parsed if parsed > 0 else None


def _positive_float(value: object) -> float | None:
    parsed = float_value(value)
    return parsed if parsed > 0 else None
