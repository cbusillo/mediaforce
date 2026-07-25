from pathlib import Path
from typing import Any, Callable

from mediaforce.core.type_defs import object_dict
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.encoding.streams import ProductionStreamPlan
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.encoding.helpers import build_svt_params


def build_ffmpeg_command(
        *,
        source_path: Path,
        staging_path: Path,
        source_codec: str | None,
        video_policy: dict[str, Any],
        preset: int,
        audio_policy: dict[str, Any],
        subtitle_policy: dict[str, Any],
        selection: ProductionStreamPlan | dict[str, Any],
        quality: QualitySearchResult,
        text_subtitle_codecs: set[str],
        ffmpeg_binary: Callable[[], str],
        ffmpeg_hwaccel_input_args: Callable[..., list[str]],
        audio_codec: Callable[[dict[str, Any], dict[str, Any]], str],
        opus_layout_filter: Callable[[dict[str, Any]], str | None],
        opus_bitrate: Callable[[dict[str, Any], dict[str, Any]], str],
        format_crf: Callable[[float], str],
        host: dict[str, Any] | None = None,
        width: int | None = None,
        height: int | None = None,
        detected_crop: str | None = None,
        cadence_decision: dict[str, Any] | None = None,
        cadence_evidence: dict[str, Any] | None = None,
        cadence_source_fingerprint: str | None = None,
) -> list[str]:
    _ = subtitle_policy
    host_payload = object_dict(host)
    platform_name = str(host_payload.get("platform") or "") or None
    videotoolbox_available = (
        bool(host_payload.get("videotoolbox_available"))
        if "videotoolbox_available" in host_payload
        else None
    )
    mediaforce_tags = {
        "mediaforce_encoded_by": "mediaforce",
        "mediaforce_quality_metric": quality.metric,
        "mediaforce_quality_target": _format_metadata_number(quality.target),
        "mediaforce_quality_score": _format_metadata_number(quality.score),
        "mediaforce_chosen_crf": _format_metadata_number(quality.crf),
    }
    cadence_payload = object_dict(cadence_decision)
    if cadence_payload:
        mediaforce_tags.update(
            {
                "mediaforce_cadence_class": str(cadence_payload.get("classification") or "unknown"),
                "mediaforce_cadence_transform": str(cadence_payload.get("transform") or "none"),
                "mediaforce_cadence_evidence_id": str(cadence_payload.get("evidence_id") or ""),
            }
        )
    cmd = [
        ffmpeg_binary(),
        "-y",
        *ffmpeg_hwaccel_input_args(
            source_codec,
            platform_name=platform_name,
            videotoolbox_available=videotoolbox_available,
        ),
        "-i",
        str(source_path),
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-map",
        "0:v:0",
    ]
    for key, value in mediaforce_tags.items():
        cmd.extend(["-metadata", f"{key}={value}"])

    if isinstance(selection, ProductionStreamPlan):
        for audio in selection.audio_streams:
            cmd.extend(["-map", f"0:{audio.source_index}"])
        for subtitle in selection.subtitle_streams:
            cmd.extend(["-map", f"0:{subtitle.source_index}"])
        if selection.copies_attachments:
            cmd.extend(["-map", "0:t?"])
    else:
        for audio in selection["audio_tracks"]:
            cmd.extend(["-map", f"0:{audio['index']}"])
        for subtitle in selection["subtitle_tracks"]:
            cmd.extend(["-map", f"0:{subtitle['index']}"])

    cmd.extend(
        [
            "-c:v",
            str(video_policy["encoder"]),
            "-pix_fmt",
            str(video_policy["pixel_format"]),
            "-preset",
            str(preset),
            "-crf",
            format_crf(quality.crf),
            "-svtav1-params",
            ":".join(build_svt_params(video_policy)),
        ]
    )
    video_filter = build_video_filter(
        video_policy,
        width=width,
        height=height,
        detected_crop=detected_crop,
        cadence_decision=cadence_decision,
        cadence_evidence=cadence_evidence,
        cadence_source_fingerprint=cadence_source_fingerprint,
    )
    if video_filter:
        cmd.extend(["-vf", video_filter])

    if isinstance(selection, ProductionStreamPlan):
        for output_index, audio in enumerate(selection.audio_streams):
            codec = str(audio.codec_argument or "copy")
            cmd.extend([f"-c:a:{output_index}", codec])
            cmd.extend([f"-metadata:s:a:{output_index}", "language=eng"])
            cmd.extend([f"-disposition:a:{output_index}", "default"])
            if codec == "libopus":
                layout_filter = opus_layout_filter({"channels": audio.channels})
                if layout_filter:
                    cmd.extend([f"-af:a:{output_index}", layout_filter])
                    cmd.extend([f"-mapping_family:a:{output_index}", "1"])
                cmd.extend([f"-b:a:{output_index}", str(audio.output_bitrate_text)])
        if not selection.audio_streams:
            cmd.extend(["-an"])

        for output_index, subtitle in enumerate(selection.subtitle_streams):
            cmd.extend([f"-c:s:{output_index}", str(subtitle.codec_argument or "copy")])
            cmd.extend([f"-metadata:s:s:{output_index}", "language=eng"])
            disposition = "default" if output_index == 0 and not subtitle.forced else "0"
            if subtitle.forced:
                disposition = "forced"
            cmd.extend([f"-disposition:s:{output_index}", disposition])
        if selection.copies_attachments:
            cmd.extend(["-c:t", "copy"])
        has_subtitles = bool(selection.subtitle_streams)
    else:
        for output_index, audio in enumerate(selection["audio_tracks"]):
            codec = audio_codec(audio, audio_policy)
            cmd.extend([f"-c:a:{output_index}", codec])
            cmd.extend([f"-metadata:s:a:{output_index}", "language=eng"])
            cmd.extend([f"-disposition:a:{output_index}", "default"])
            if codec == "libopus":
                layout_filter = opus_layout_filter(audio)
                if layout_filter:
                    cmd.extend([f"-af:a:{output_index}", layout_filter])
                    cmd.extend([f"-mapping_family:a:{output_index}", "1"])
                cmd.extend([f"-b:a:{output_index}", opus_bitrate(audio, audio_policy)])

        for output_index, subtitle in enumerate(selection["subtitle_tracks"]):
            codec = "srt" if subtitle["codec_name"] in text_subtitle_codecs else "copy"
            cmd.extend([f"-c:s:{output_index}", codec])
            cmd.extend([f"-metadata:s:s:{output_index}", "language=eng"])
            disposition = "default" if output_index == 0 and not subtitle.get("forced") else "0"
            if subtitle.get("forced"):
                disposition = "forced"
            cmd.extend([f"-disposition:s:{output_index}", disposition])
        has_subtitles = bool(selection["subtitle_tracks"])

    if not has_subtitles:
        cmd.extend(["-sn"])

    return cmd + [str(staging_path)]


def _format_metadata_number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")
