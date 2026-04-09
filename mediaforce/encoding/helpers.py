from pathlib import Path
from typing import Any, Callable

from mediaforce.core.config import MediaforceConfig
from mediaforce.core.type_defs import int_value


def _mapped_item_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
) -> Path | None:
    media_root = str(item.get("media_root") or "").strip()
    rel_path = str(item.get("rel_path") or "").strip()
    if not media_root or not rel_path:
        return None
    root = config.source_root_map_for_host(host).get(media_root)
    if root is None:
        return None
    return root.parent / Path(rel_path)


def _controller_item_source_path(item: dict[str, Any]) -> Path:
    source_path = str(item.get("source_path") or "").strip()
    if source_path:
        return Path(source_path)
    raise KeyError("source_path")


def build_svt_params(video_policy: dict[str, Any]) -> list[str]:
    return [
        "tune=0",
        f"film-grain={int(video_policy.get('default_grain', 0))}",
        f"film-grain-denoise={int(video_policy.get('grain_denoise', 0))}",
    ]


def effective_video_preset(
        video_policy: dict[str, Any],
        *,
        width: int | None = None,
        height: int | None = None,
        svt_av1_8k_dimension_threshold: int,
        svt_av1_min_8k_preset: int,
) -> int:
    preset = int(video_policy["preset"])
    if str(video_policy.get("encoder") or "").lower() != "libsvtav1":
        return preset
    largest_dimension = max(int_value(width), int_value(height))
    if largest_dimension < svt_av1_8k_dimension_threshold:
        return preset
    return max(preset, svt_av1_min_8k_preset)


def resolve_item_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
) -> Path:
    if host_media_access_for_host(host) == "stream":
        return _controller_item_source_path(item)
    mapped_path = _mapped_item_source_path(config, item, host=host)
    if mapped_path is not None:
        return mapped_path
    return _controller_item_source_path(item)


def resolve_item_quality_source_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
) -> Path:
    if host_media_access_for_host(host) != "stream":
        return resolve_item_source_path(
            config,
            item,
            host=host,
            host_media_access_for_host=host_media_access_for_host,
        )
    mapped_path = _mapped_item_source_path(config, item, host=host)
    if mapped_path is not None:
        return mapped_path
    return _controller_item_source_path(item)


def resolve_item_staging_path(
        config: MediaforceConfig,
        item: dict[str, Any],
        *,
        host: dict[str, Any] | None = None,
        host_media_access_for_host: Callable[[dict[str, Any] | None], str],
) -> Path:
    if host_media_access_for_host(host) == "stream":
        return Path(str(item["staging_path"]))
    rel_path = str(item.get("rel_path") or "").strip()
    if rel_path:
        return config.staging_root_for_host(host) / Path(rel_path).with_suffix(f".{config.output_container}")
    return Path(str(item["staging_path"]))


def _streaming_output_args_for_path(path: Path) -> list[str]:
    suffix = path.suffix.lower()
    if suffix == ".mkv":
        return ["-f", "matroska"]
    if suffix in {".mp4", ".m4v", ".mov"}:
        return ["-movflags", "+frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    if suffix == ".ts":
        return ["-f", "mpegts"]
    raise RuntimeError(f"Streaming encode output is not supported for {suffix or 'this file type'}.")


def _build_streaming_remote_ffmpeg_command(
        ffmpeg_cmd: list[str],
        *,
        source_path: Path,
        output_path: Path,
        executable_path: str | None = None,
        ffmpeg_command_with_progress: Callable[[list[str]], list[str]],
) -> list[str]:
    remote_ffmpeg_cmd = list(ffmpeg_cmd[:-1])
    remote_ffmpeg_cmd[0] = str(executable_path or Path(remote_ffmpeg_cmd[0]).name)
    try:
        source_index = remote_ffmpeg_cmd.index(str(source_path))
    except ValueError as exc:
        raise RuntimeError("Streaming encode source path was missing from the ffmpeg command.") from exc
    remote_ffmpeg_cmd[source_index] = "pipe:0"
    remote_ffmpeg_cmd = ffmpeg_command_with_progress(remote_ffmpeg_cmd)
    return [*remote_ffmpeg_cmd, *_streaming_output_args_for_path(output_path), "pipe:1"]
