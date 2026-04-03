from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from mediaforce.core.db_tables import staged_artifacts


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
        process_controller: Any = None,
        execution_mode_for_host: Callable[[dict[str, Any] | None], str],
        encode_preview_clips_remote_fn: Callable[..., list[Any]],
        render_encoded_preview_clip: Callable[..., None],
        slug_seconds: Callable[[float], str],
        encoded_preview_clip_factory: Callable[..., Any],
) -> list[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    host_mode = execution_mode_for_host(host)
    if host_mode == "ssh":
        return encode_preview_clips_remote_fn(
            host=host or {},
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
        )

    encoded: list[Any] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"encoded-{clip_number:02d}-{slug_seconds(clip_time)}.mp4"
        render_encoded_preview_clip(
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
        )
        encoded.append(
            encoded_preview_clip_factory(
                output_path=output_path,
                timestamp_seconds=clip_time,
                duration_seconds=duration_seconds,
                size_bytes=output_path.stat().st_size,
            )
        )
    return encoded


def encode_preview_clips_remote(
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
        remote_preview_timeout_seconds: int,
        uuid_factory: Callable[[], Any],
        run_remote_command: Callable[..., Any],
        render_encoded_preview_clip_remote: Callable[..., None],
        copy_remote_file_to_local: Callable[..., None],
        slug_seconds: Callable[[float], str],
        encoded_preview_clip_factory: Callable[..., Any],
) -> list[Any]:
    remote_root = Path("/tmp") / f"mediaforce-preview-{uuid_factory().hex[:12]}"
    run_remote_command(host, ["mkdir", "-p", str(remote_root)], 30)
    encoded: list[Any] = []
    try:
        for clip_number, clip_time in enumerate(timestamps, start=1):
            file_name = f"encoded-{clip_number:02d}-{slug_seconds(clip_time)}.mp4"
            output_path = output_dir / file_name
            remote_output_path = remote_root / file_name
            render_encoded_preview_clip_remote(
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
            )
            copy_remote_file_to_local(host, remote_output_path, output_path, remote_preview_timeout_seconds)
            encoded.append(
                encoded_preview_clip_factory(
                    output_path=output_path,
                    timestamp_seconds=clip_time,
                    duration_seconds=duration_seconds,
                    size_bytes=output_path.stat().st_size,
                )
            )
    finally:
        try:
            run_remote_command(host, ["rm", "-rf", str(remote_root)], 30)
        except Exception:
            pass
    return encoded


def generate_compare_clips(
        connection: Any,
        manifest: dict[str, Any],
        indexes: list[int],
        *,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float] | None,
        play: bool,
        process_controller: Any = None,
        auto_timestamps: Callable[..., list[float]],
        generate_compare_clips_for_pair_fn: Callable[..., list[Any]],
        subprocess_run: Callable[[list[str]], Any],
) -> list[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Any] = []
    for index in indexes:
        item = manifest["items"][index]
        stage_row = connection.execute(
            select(staged_artifacts.c.staging_path).where(staged_artifacts.c.library_item_id == item["library_item_id"])
        ).mappings().fetchone()
        if stage_row is None:
            raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")

        source_path = Path(item["source_path"])
        staged_path = Path(stage_row["staging_path"])
        clip_times = timestamps or auto_timestamps(
            source_path=source_path,
            total_duration=float(item.get("duration_seconds") or 0),
            clip_duration=duration_seconds,
            process_controller=process_controller,
        )
        item_dir = output_dir / f"item-{index:02d}"
        item_dir.mkdir(parents=True, exist_ok=True)

        generated.extend(
            generate_compare_clips_for_pair_fn(
                source_path=source_path,
                staged_path=staged_path,
                source_codec=str(item.get("video_codec") or ""),
                output_dir=item_dir,
                duration_seconds=duration_seconds,
                timestamps=clip_times,
                process_controller=process_controller,
            )
        )

    if play and generated:
        subprocess_run(["ffplay", "-autoexit", str(generated[0].output_path)])

    return generated


def generate_compare_clips_for_pair(
        *,
        source_path: Path,
        staged_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        duration_seconds: float,
        timestamps: list[float],
        process_controller: Any = None,
        render_compare_clip: Callable[..., None],
        slug_seconds: Callable[[float], str],
        compare_clip_factory: Callable[..., Any],
) -> list[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Any] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"compare-{clip_number:02d}-{slug_seconds(clip_time)}.mkv"
        render_compare_clip(
            source_path,
            staged_path,
            output_path,
            clip_time,
            duration_seconds,
            source_codec=source_codec,
            process_controller=process_controller,
        )
        generated.append(compare_clip_factory(output_path=output_path, timestamp_seconds=clip_time, duration_seconds=duration_seconds))
    return generated


def generate_compare_clips_from_previews(
        *,
        source_path: Path,
        source_codec: str | None = None,
        previews: list[Any],
        output_dir: Path,
        process_controller: Any = None,
        render_compare_clip_from_preview: Callable[..., None],
        slug_seconds: Callable[[float], str],
        compare_clip_factory: Callable[..., Any],
) -> list[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Any] = []
    for clip_number, preview in enumerate(previews, start=1):
        output_path = output_dir / f"compare-{clip_number:02d}-{slug_seconds(preview.timestamp_seconds)}.mkv"
        render_compare_clip_from_preview(
            source_path=source_path,
            source_codec=source_codec,
            preview_path=preview.output_path,
            output_path=output_path,
            clip_time=preview.timestamp_seconds,
            duration_seconds=preview.duration_seconds,
            process_controller=process_controller,
        )
        generated.append(
            compare_clip_factory(
                output_path=output_path,
                timestamp_seconds=preview.timestamp_seconds,
                duration_seconds=preview.duration_seconds,
            )
        )
    return generated


def render_source_review_clips(
        *,
        source_path: Path,
        source_codec: str | None = None,
        output_dir: Path,
        timestamps: list[float],
        duration_seconds: float,
        process_controller: Any = None,
        render_source_review_clip: Callable[..., None],
        slug_seconds: Callable[[float], str],
        browser_review_clip_factory: Callable[..., Any],
) -> list[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Any] = []
    for clip_number, clip_time in enumerate(timestamps, start=1):
        output_path = output_dir / f"source-{clip_number:02d}-{slug_seconds(clip_time)}.mp4"
        render_source_review_clip(
            source_path=source_path,
            source_codec=source_codec,
            output_path=output_path,
            clip_time=clip_time,
            duration_seconds=duration_seconds,
            process_controller=process_controller,
        )
        rendered.append(
            browser_review_clip_factory(
                output_path=output_path,
                timestamp_seconds=clip_time,
                duration_seconds=duration_seconds,
                size_bytes=output_path.stat().st_size,
            )
        )
    return rendered
