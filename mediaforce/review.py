import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.core.db import DBClient
from mediaforce.encoding.ffmpeg import ffmpeg_hwaccel_input_args
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.remote import copy_remote_file_to_local, execution_mode_for_host, run_remote_command
from mediaforce.reviewing.helpers import auto_timestamps as _auto_timestamps_impl, \
    complexity_timestamps as _complexity_timestamps_impl, default_timestamps as _default_timestamps_impl, \
    format_crf as _format_crf_impl, planned_audio_action as _planned_audio_action_impl, \
    planned_opus_bitrate as _planned_opus_bitrate_impl, scene_change_timestamps as _scene_change_timestamps_impl, \
    slug_seconds as _slug_seconds_impl
from mediaforce.reviewing.clips import encode_preview_clips as encode_preview_clips_impl, \
    encode_preview_clips_remote as _encode_preview_clips_remote_impl, \
    generate_compare_clips as generate_compare_clips_impl, \
    generate_compare_clips_for_pair as generate_compare_clips_for_pair_impl, \
    generate_compare_clips_from_previews as generate_compare_clips_from_previews_impl, \
    render_source_review_clips as render_source_review_clips_impl
from mediaforce.reviewing.assets import render_audio_spectrogram as _render_audio_spectrogram_impl, \
    render_audio_spectrogram_compare as render_audio_spectrogram_compare_impl, \
    render_encoded_audio_clip as _render_encoded_audio_clip_impl, \
    render_review_contact_sheet as render_review_contact_sheet_impl, \
    stack_review_images as _stack_review_images_impl
from mediaforce.reviewing.renderers import render_compare_clip as _render_compare_clip_impl, \
    render_compare_clip_from_preview as _render_compare_clip_from_preview_impl, \
    render_encoded_preview_clip as _render_encoded_preview_clip_impl, \
    render_encoded_preview_clip_remote as _render_encoded_preview_clip_remote_impl, \
    render_source_review_clip as _render_source_review_clip_impl

PTS_TIME_RE = re.compile(r"pts_time:(?P<pts>[0-9]+(?:\.[0-9]+)?)")
REMOTE_PREVIEW_TIMEOUT_SECONDS = 2 * 60 * 60


@dataclass(slots=True)
class CompareClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float


@dataclass(slots=True)
class EncodedPreviewClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float
    size_bytes: int


@dataclass(slots=True)
class BrowserReviewClip:
    output_path: Path
    timestamp_seconds: float
    duration_seconds: float
    size_bytes: int


def render_review_contact_sheet(
        *,
        source_clip_path: Path,
        preview_clip_path: Path,
        output_path: Path,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return render_review_contact_sheet_impl(
        source_clip_path=source_clip_path,
        preview_clip_path=preview_clip_path,
        output_path=output_path,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        run_command=run_command,
    )


def render_audio_spectrogram_compare(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        audio_track: dict[str, Any],
        audio_policy: dict[str, Any],
        process_controller: ManagedProcessController | None = None,
) -> dict[str, Any] | None:
    return render_audio_spectrogram_compare_impl(
        source_path=source_path,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        audio_track=audio_track,
        audio_policy=audio_policy,
        process_controller=process_controller,
        planned_audio_action=_planned_audio_action,
        planned_opus_bitrate=_planned_opus_bitrate,
        render_audio_spectrogram=_render_audio_spectrogram,
        render_encoded_audio_clip=_render_encoded_audio_clip,
        stack_review_images=_stack_review_images,
    )


def generate_compare_clips(
        connection: DBClient,
        manifest: dict[str, Any],
        indexes: list[int],
        *,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float] | None,
        play: bool,
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    return generate_compare_clips_impl(
        connection,
        manifest,
        indexes,
        output_dir=output_dir,
        duration_seconds=duration_seconds,
        timestamps=timestamps,
        play=play,
        process_controller=process_controller,
        auto_timestamps=_auto_timestamps,
        generate_compare_clips_for_pair=generate_compare_clips_for_pair,
        subprocess_run=subprocess.run,
    )


def recommend_review_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    return _auto_timestamps(source_path, total_duration, clip_duration, process_controller=process_controller)


def encode_preview_clips(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        host: dict[str, Any] | None = None,
        process_controller: ManagedProcessController | None = None,
) -> list[EncodedPreviewClip]:
    return encode_preview_clips_impl(
        source_path=source_path,
        source_codec=source_codec,
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        host=host,
        process_controller=process_controller,
        execution_mode_for_host=execution_mode_for_host,
        encode_preview_clips_remote=_encode_preview_clips_remote,
        render_encoded_preview_clip=_render_encoded_preview_clip,
        slug_seconds=_slug_seconds,
        encoded_preview_clip_factory=EncodedPreviewClip,
    )


def _encode_preview_clips_remote(
        *,
        host: dict[str, Any],
        source_path: Path,
        source_codec: str | None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
) -> list[EncodedPreviewClip]:
    return _encode_preview_clips_remote_impl(
        host=host,
        source_path=source_path,
        source_codec=source_codec,
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        remote_preview_timeout_seconds=REMOTE_PREVIEW_TIMEOUT_SECONDS,
        uuid_factory=uuid.uuid4,
        run_remote_command=run_remote_command,
        render_encoded_preview_clip_remote=_render_encoded_preview_clip_remote,
        copy_remote_file_to_local=copy_remote_file_to_local,
        slug_seconds=_slug_seconds,
        encoded_preview_clip_factory=EncodedPreviewClip,
    )


def generate_compare_clips_for_pair(
        *,
        source_path: Path,
        staged_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float],
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    return generate_compare_clips_for_pair_impl(
        source_path=source_path,
        staged_path=staged_path,
        source_codec=source_codec,
        output_dir=output_dir,
        duration_seconds=duration_seconds,
        timestamps=timestamps,
        process_controller=process_controller,
        render_compare_clip=_render_compare_clip,
        slug_seconds=_slug_seconds,
        compare_clip_factory=CompareClip,
    )


def generate_compare_clips_from_previews(
        *,
        source_path: Path,
        source_codec: str | None = None,
        previews: list[EncodedPreviewClip],
        output_dir: Path,
        process_controller: ManagedProcessController | None = None,
) -> list[CompareClip]:
    return generate_compare_clips_from_previews_impl(
        source_path=source_path,
        source_codec=source_codec,
        previews=previews,
        output_dir=output_dir,
        process_controller=process_controller,
        render_compare_clip_from_preview=_render_compare_clip_from_preview,
        slug_seconds=_slug_seconds,
        compare_clip_factory=CompareClip,
    )


def render_source_review_clips(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> list[BrowserReviewClip]:
    return render_source_review_clips_impl(
        source_path=source_path,
        source_codec=source_codec,
        output_dir=output_dir,
        timestamps=timestamps,
        duration_seconds=duration_seconds,
        process_controller=process_controller,
        render_source_review_clip=_render_source_review_clip,
        slug_seconds=_slug_seconds,
        browser_review_clip_factory=BrowserReviewClip,
    )


def _render_compare_clip(
        source_path: Path,
        staged_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        *,
        source_codec: str | None = None,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_compare_clip_impl(
        source_path,
        staged_path,
        output_path,
        clip_time,
        duration_seconds,
        source_codec=source_codec,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        run_command=run_command,
    )


def _render_encoded_preview_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_encoded_preview_clip_impl(
        source_path=source_path,
        source_codec=source_codec,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        format_crf=_format_crf,
        run_command=run_command,
    )


def _render_encoded_preview_clip_remote(
        *,
        host: dict[str, Any],
        source_path: Path,
        source_codec: str | None = None,
        remote_output_path: Path,
        clip_time: float,
        duration_seconds: float,
        encoder: str,
        pixel_format: str,
        preset: int,
        crf: float,
        svt_params: list[str],
) -> None:
    return _render_encoded_preview_clip_remote_impl(
        host=host,
        source_path=source_path,
        source_codec=source_codec,
        remote_output_path=remote_output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        encoder=encoder,
        pixel_format=pixel_format,
        preset=preset,
        crf=crf,
        svt_params=svt_params,
        remote_preview_timeout_seconds=REMOTE_PREVIEW_TIMEOUT_SECONDS,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        format_crf=_format_crf,
        run_remote_command=run_remote_command,
    )


def _render_compare_clip_from_preview(
        *,
        source_path: Path,
        source_codec: str | None = None,
        preview_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_compare_clip_from_preview_impl(
        source_path=source_path,
        source_codec=source_codec,
        preview_path=preview_path,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        run_command=run_command,
    )


def _render_source_review_clip(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_source_review_clip_impl(
        source_path=source_path,
        source_codec=source_codec,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args,
        run_command=run_command,
    )


def _render_audio_spectrogram(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_audio_spectrogram_impl(
        source_path=source_path,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        run_command=run_command,
    )


def _render_encoded_audio_clip(
        *,
        source_path: Path,
        output_path: Path,
        clip_time: float,
        duration_seconds: float,
        bitrate: str,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _render_encoded_audio_clip_impl(
        source_path=source_path,
        output_path=output_path,
        clip_time=clip_time,
        duration_seconds=duration_seconds,
        bitrate=bitrate,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        run_command=run_command,
    )


def _stack_review_images(
        *,
        top_path: Path,
        bottom_path: Path,
        output_path: Path,
        process_controller: ManagedProcessController | None = None,
) -> None:
    return _stack_review_images_impl(
        top_path=top_path,
        bottom_path=bottom_path,
        output_path=output_path,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        run_command=run_command,
    )


def _planned_audio_action(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return _planned_audio_action_impl(audio_track, audio_policy)


def _planned_opus_bitrate(audio_track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    return _planned_opus_bitrate_impl(audio_track, audio_policy)


def _default_timestamps(total_duration: float, clip_duration: float) -> list[float]:
    return _default_timestamps_impl(total_duration, clip_duration)


def _auto_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    return _auto_timestamps_impl(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
        complexity_timestamps=_complexity_timestamps,
        scene_change_timestamps=_scene_change_timestamps,
        default_timestamps=_default_timestamps,
    )


def _complexity_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    return _complexity_timestamps_impl(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
        ffprobe_binary=ffprobe_binary,
        run_command=run_command,
    )


def _scene_change_timestamps(
        source_path: Path,
        total_duration: float,
        clip_duration: float,
        *,
        process_controller: ManagedProcessController | None = None,
) -> list[float]:
    return _scene_change_timestamps_impl(
        source_path,
        total_duration,
        clip_duration,
        process_controller=process_controller,
        ffmpeg_binary=ffmpeg_binary,
        run_command=run_command,
        pts_time_re=PTS_TIME_RE,
    )


def _slug_seconds(value: float) -> str:
    return _slug_seconds_impl(value)


def _format_crf(value: float) -> str:
    return _format_crf_impl(value)
