from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_harness.binaries import ffmpeg_binary
from media_harness.config import HarnessConfig
from media_harness.process_control import ManagedProcessController, run_command
from media_harness.probe import probe_media
from media_harness.quality import QualitySearchError, QualitySearchResult, run_crf_search, select_quality_metric
from media_harness.remote import remote_shell_path_export_line
from media_harness.utils import file_fingerprint, timestamp


TEXT_SUBTITLE_CODECS = {"ass", "mov_text", "srt", "ssa", "subrip", "text", "webvtt"}


@dataclass(slots=True)
class EncodeResult:
    staging_path: Path
    source_size_bytes: int
    staging_size_bytes: int
    chosen_crf: float
    quality_metric: str
    quality_target: float
    quality_score: float
    encode_command: list[str]


def encode_manifest_items(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    manifest_path: Path,
    manifest: dict[str, Any],
    indexes: list[int],
    overwrite: bool,
    process_controller: ManagedProcessController | None = None,
    host: dict[str, Any] | None = None,
) -> list[EncodeResult]:
    results: list[EncodeResult] = []
    for index in indexes:
        if process_controller is not None:
            process_controller.throw_if_cancelled()
        item = manifest["items"][index]
        result = encode_one_item(
            connection,
            config,
            manifest_path,
            manifest,
            index,
            item,
            overwrite=overwrite,
            process_controller=process_controller,
            host=host,
        )
        results.append(result)
    return results


def describe_item_plan(item: dict[str, Any]) -> dict[str, Any]:
    policy = item["resolved_policy"]
    selection = _select_streams(item)
    quality_metric, _ = select_quality_metric(str(policy["video"].get("quality_metric", "auto")))
    selected_audio = selection["audio_tracks"][0]
    audio_codec = _audio_codec(selected_audio, policy["audio"])
    audio_plan = {
        "source_codec": str(selected_audio.get("codec_name") or "unknown"),
        "channels": int(selected_audio.get("channels") or 0),
        "language": selected_audio.get("language") or "und",
        "action": "convert" if audio_codec == "libopus" else "copy",
        "output_codec": "opus" if audio_codec == "libopus" else str(selected_audio.get("codec_name") or "unknown"),
        "output_bitrate": _opus_bitrate(selected_audio, policy["audio"]) if audio_codec == "libopus" else None,
        "source_track_count": len(item.get("audio_summary") or []),
        "kept_track_count": len(selection["audio_tracks"]),
    }
    subtitle_tracks = selection["subtitle_tracks"]
    return {
        "video": {
            "source_codec": item.get("video_codec") or "unknown",
            "output_codec": "av1",
            "quality_metric": quality_metric,
            "target": float(
                policy["video"][
                    "target_vmaf" if quality_metric == "vmaf" else "target_xpsnr"
                ]
            ),
            "min_target": float(
                policy["video"][
                    "min_target_vmaf" if quality_metric == "vmaf" else "min_target_xpsnr"
                ]
            ),
            "max_encoded_percent": float(policy["video"].get("max_encoded_percent", 100)),
            "default_grain": int(policy["video"].get("default_grain", 0)),
        },
        "audio": audio_plan,
        "subtitles": {
            "source_track_count": len(item.get("subtitle_summary") or []),
            "kept_track_count": len(subtitle_tracks),
            "languages": [track.get("language") or "und" for track in subtitle_tracks],
            "codecs": [track.get("codec_name") or "unknown" for track in subtitle_tracks],
        },
    }


def build_svt_params(video_policy: dict[str, Any]) -> list[str]:
    return [
        "tune=0",
        f"film-grain={int(video_policy.get('default_grain', 0))}",
        f"film-grain-denoise={int(video_policy.get('grain_denoise', 0))}",
    ]


def estimate_output_overhead_bytes(item: dict[str, Any]) -> dict[str, int]:
    selection = _select_streams(item)
    duration_seconds = float(item.get("duration_seconds") or 0.0)
    audio_bytes = 0
    for track in selection["audio_tracks"]:
        audio_bytes += _estimate_audio_track_bytes(track, item["resolved_policy"]["audio"], duration_seconds)

    subtitle_bytes = 0
    for track in selection["subtitle_tracks"]:
        subtitle_bytes += _estimate_subtitle_track_bytes(track)

    container_bytes = 256 * 1024
    return {
        "audio_bytes": audio_bytes,
        "subtitle_bytes": subtitle_bytes,
        "container_bytes": container_bytes,
        "total_bytes": audio_bytes + subtitle_bytes + container_bytes,
    }


def encode_one_item(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    manifest_path: Path,
    manifest: dict[str, Any],
    index: int,
    item: dict[str, Any],
    *,
    overwrite: bool,
    process_controller: ManagedProcessController | None = None,
    host: dict[str, Any] | None = None,
) -> EncodeResult:
    source_path = Path(item["source_path"])
    staging_path = Path(item["staging_path"])
    staging_path.parent.mkdir(parents=True, exist_ok=True)

    if staging_path.exists() and not overwrite:
        raise FileExistsError(f"Staging file already exists: {staging_path}")

    policy = item["resolved_policy"]
    quality_result = _search_quality(source_path, policy["video"], process_controller=process_controller)
    selection = _select_streams(item)
    ffmpeg_cmd = _build_ffmpeg_command(
        source_path=source_path,
        staging_path=staging_path,
        video_policy=policy["video"],
        audio_policy=policy["audio"],
        subtitle_policy=policy["subtitle"],
        selection=selection,
        quality=quality_result,
    )

    started_at = timestamp()
    _record_event(connection, item["library_item_id"], "encoding_started", {"manifest": str(manifest_path), "item_index": index})
    connection.execute(
        "UPDATE library_items SET status = 'encoding', updated_at = ? WHERE id = ?",
        (started_at, item["library_item_id"]),
    )
    connection.commit()

    temp_output = staging_path.with_name(f"{staging_path.stem}.partial{staging_path.suffix}")
    if temp_output.exists():
        temp_output.unlink()
    if staging_path.exists() and overwrite:
        staging_path.unlink()

    try:
        result = _run_encode_command(
            ffmpeg_cmd=ffmpeg_cmd,
            temp_output=temp_output,
            staging_path=staging_path,
            overwrite=overwrite,
            process_controller=process_controller,
            host=host,
        )
        if result.returncode != 0:
            details = (result.stdout or "").strip()
            if (result.stderr or "").strip():
                details = f"{details}\n{(result.stderr or '').strip()}".strip()
            raise RuntimeError(details or "ffmpeg encode failed")
        _finalize_output_path(temp_output, staging_path)
    except Exception:
        temp_output.unlink(missing_ok=True)
        staging_path.unlink(missing_ok=True)
        raise

    staged_stat = staging_path.stat()
    staged_probe = probe_media(staging_path)
    staged_fingerprint = file_fingerprint(staging_path, staged_stat, staged_probe.duration_seconds)
    now = timestamp()

    connection.execute(
        """
        INSERT INTO staged_artifacts (
            library_item_id, manifest_run_id, manifest_path, item_index, source_fingerprint,
            staging_path, staging_size_bytes, staging_mtime_ns, staging_fingerprint,
            chosen_crf, quality_metric, quality_target, quality_score, encode_command_json,
            audio_summary_json, subtitle_summary_json, staged_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(library_item_id) DO UPDATE SET
            manifest_run_id = excluded.manifest_run_id,
            manifest_path = excluded.manifest_path,
            item_index = excluded.item_index,
            source_fingerprint = excluded.source_fingerprint,
            staging_path = excluded.staging_path,
            staging_size_bytes = excluded.staging_size_bytes,
            staging_mtime_ns = excluded.staging_mtime_ns,
            staging_fingerprint = excluded.staging_fingerprint,
            chosen_crf = excluded.chosen_crf,
            quality_metric = excluded.quality_metric,
            quality_target = excluded.quality_target,
            quality_score = excluded.quality_score,
            encode_command_json = excluded.encode_command_json,
            audio_summary_json = excluded.audio_summary_json,
            subtitle_summary_json = excluded.subtitle_summary_json,
            staged_at = excluded.staged_at,
            updated_at = excluded.updated_at
        """,
        (
            item["library_item_id"],
            manifest["run_id"],
            str(manifest_path),
            index,
            item["source_fingerprint"],
            str(staging_path),
            staged_stat.st_size,
            staged_stat.st_mtime_ns,
            staged_fingerprint,
            quality_result.crf,
            quality_result.metric,
            quality_result.target,
            quality_result.score,
            json.dumps(ffmpeg_cmd, separators=(",", ":")),
            staged_probe.audio_summary_json,
            staged_probe.subtitle_summary_json,
            now,
            now,
        ),
    )
    connection.execute(
        "UPDATE library_items SET status = 'encoded', updated_at = ? WHERE id = ?",
        (now, item["library_item_id"]),
    )
    _record_event(
        connection,
        item["library_item_id"],
        "encoding_completed",
        {
            "staging_path": str(staging_path),
            "chosen_crf": quality_result.crf,
            "quality_metric": quality_result.metric,
            "quality_score": quality_result.score,
        },
    )
    connection.commit()

    return EncodeResult(
        staging_path=staging_path,
        source_size_bytes=int(item["source_size_bytes"]),
        staging_size_bytes=staged_stat.st_size,
        chosen_crf=quality_result.crf,
        quality_metric=quality_result.metric,
        quality_target=quality_result.target,
        quality_score=quality_result.score,
        encode_command=ffmpeg_cmd,
    )


def search_quality_for_source(
    source_path: Path,
    video_policy: dict[str, Any],
    *,
    host: dict[str, Any] | None = None,
) -> QualitySearchResult:
    return _search_quality(source_path, video_policy, host=host)


def validate_manifest_items(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    manifest: dict[str, Any],
    indexes: list[int],
) -> list[dict[str, Any]]:
    results = []
    for index in indexes:
        item = manifest["items"][index]
        result = validate_one_item(connection, config, item)
        results.append(result)
    return results


def validate_one_item(connection: sqlite3.Connection, config: HarnessConfig, item: dict[str, Any]) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM staged_artifacts WHERE library_item_id = ?",
        (item["library_item_id"],),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")

    staging_path = Path(row["staging_path"])
    staged_probe = probe_media(staging_path)
    staged_size_bytes = staging_path.stat().st_size
    source_size_bytes = int(item["source_size_bytes"])
    validation = {
        "passed": True,
        "checks": [],
        "source_size_bytes": source_size_bytes,
        "staged_size_bytes": staged_size_bytes,
        "bytes_saved": source_size_bytes - staged_size_bytes,
        "size_ratio": round(staged_size_bytes / source_size_bytes, 4) if source_size_bytes else None,
    }

    _check(validation, staged_probe.video_codec == "av1", "video codec is AV1")
    _check(validation, staged_probe.audio_track_count == 1, "exactly one audio track remains")
    _check(validation, staged_probe.english_audio_count == staged_probe.audio_track_count, "all audio tracks are tagged English")

    source_has_english_subs = _source_has_preservable_subtitles(item.get("subtitle_summary") or [])
    if source_has_english_subs:
        _check(validation, staged_probe.english_subtitle_count >= 1, "English subtitles were preserved")
        if staged_probe.english_subtitle_count:
            subtitles = json.loads(staged_probe.subtitle_summary_json)
            first_language = subtitles[0].get("language") if subtitles else None
            first_default = subtitles[0].get("default") if subtitles else 0
            first_forced = subtitles[0].get("forced") if subtitles else 0
            _check(validation, first_language == "eng", "first subtitle is English")
            if any(not subtitle.get("forced") for subtitle in subtitles):
                _check(validation, first_default == 1, "first subtitle is default")
            else:
                _check(validation, first_forced == 1, "forced-only subtitle outputs stay flagged forced")

    require_size_reduction = bool(config.validation.get("require_size_reduction", True))
    if require_size_reduction:
        _check(validation, staged_size_bytes < source_size_bytes, "staged file is smaller than source")

    now = timestamp()
    connection.execute(
        "UPDATE staged_artifacts SET validation_json = ?, validated_at = ?, updated_at = ? WHERE library_item_id = ?",
        (json.dumps(validation, separators=(",", ":")), now, now, item["library_item_id"]),
    )
    if validation["passed"]:
        connection.execute(
            "UPDATE library_items SET status = 'validated', updated_at = ? WHERE id = ?",
            (now, item["library_item_id"]),
        )
    _record_event(connection, item["library_item_id"], "validation_completed", validation)
    connection.commit()
    return validation


def promote_manifest_items(
    connection: sqlite3.Connection,
    config: HarnessConfig,
    manifest: dict[str, Any],
    indexes: list[int],
    force: bool,
) -> list[Path]:
    promoted_paths = []
    for index in indexes:
        item = manifest["items"][index]
        promoted_paths.append(promote_one_item(connection, config, item, force=force))
    return promoted_paths


def promote_one_item(connection: sqlite3.Connection, config: HarnessConfig, item: dict[str, Any], *, force: bool) -> Path:
    stage_row = connection.execute(
        "SELECT * FROM staged_artifacts WHERE library_item_id = ?",
        (item["library_item_id"],),
    ).fetchone()
    if stage_row is None:
        raise FileNotFoundError(f"No staged artifact found for item {item['library_item_id']}")
    validation = json.loads(stage_row["validation_json"] or "{}")
    if not force and not validation.get("passed"):
        raise RuntimeError(f"Item {item['library_item_id']} must be validated before promotion")

    source_path = Path(item["source_path"])
    staging_path = Path(stage_row["staging_path"])
    destination_path = source_path.with_suffix(f".{config.output_container}")
    archive_path = config.archive_root / Path(item["rel_path"])
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    if destination_path.exists() and destination_path != source_path:
        raise FileExistsError(f"Destination already exists: {destination_path}")

    if source_path.exists():
        if archive_path.exists():
            archive_path.unlink()
        shutil.move(str(source_path), str(archive_path))
    shutil.move(str(staging_path), str(destination_path))

    promoted_stat = destination_path.stat()
    promoted_probe = probe_media(destination_path)
    promoted_fingerprint = file_fingerprint(destination_path, promoted_stat, promoted_probe.duration_seconds)
    now = timestamp()
    rel_path = str(destination_path.relative_to(config.source_root_map[item["media_root"]].parent))
    parent_dir = str(destination_path.parent.relative_to(config.source_root_map[item["media_root"]].parent))

    connection.execute(
        """
        UPDATE library_items
        SET source_path = ?, rel_path = ?, parent_dir = ?, file_name = ?, container = ?,
            size_bytes = ?, mtime_ns = ?, fingerprint = ?, duration_seconds = ?, video_codec = ?,
            video_bitrate = ?, width = ?, height = ?, pix_fmt = ?, audio_track_count = ?,
            subtitle_track_count = ?, english_audio_count = ?, english_subtitle_count = ?,
            default_audio_language = ?, default_subtitle_language = ?, audio_summary_json = ?,
            subtitle_summary_json = ?, status = 'promoted', updated_at = ?, last_seen_at = ?
        WHERE id = ?
        """,
        (
            str(destination_path),
            rel_path,
            parent_dir,
            destination_path.name,
            destination_path.suffix.lower(),
            promoted_stat.st_size,
            promoted_stat.st_mtime_ns,
            promoted_fingerprint,
            promoted_probe.duration_seconds,
            promoted_probe.video_codec,
            promoted_probe.video_bitrate,
            promoted_probe.width,
            promoted_probe.height,
            promoted_probe.pix_fmt,
            promoted_probe.audio_track_count,
            promoted_probe.subtitle_track_count,
            promoted_probe.english_audio_count,
            promoted_probe.english_subtitle_count,
            promoted_probe.default_audio_language,
            promoted_probe.default_subtitle_language,
            promoted_probe.audio_summary_json,
            promoted_probe.subtitle_summary_json,
            now,
            now,
            item["library_item_id"],
        ),
    )
    connection.execute(
        """
        UPDATE staged_artifacts
        SET promoted_at = ?, promoted_path = ?, archived_source_path = ?, updated_at = ?
        WHERE library_item_id = ?
        """,
        (now, str(destination_path), str(archive_path), now, item["library_item_id"]),
    )
    _record_event(
        connection,
        item["library_item_id"],
        "promotion_completed",
        {
            "promoted_path": str(destination_path),
            "archived_source_path": str(archive_path),
        },
    )
    return destination_path


def _search_quality(
    source_path: Path,
    video_policy: dict[str, Any],
    *,
    process_controller: ManagedProcessController | None = None,
    host: dict[str, Any] | None = None,
) -> QualitySearchResult:
    metric_name, default_target = select_quality_metric(str(video_policy.get("quality_metric", "auto")))
    metric_target = float(video_policy.get(f"target_{metric_name.lower()}", default_target))
    min_target = float(video_policy.get(f"min_target_{metric_name.lower()}", metric_target))
    relax_step = float(video_policy.get(f"target_relax_step_{metric_name.lower()}", 1.0 if metric_name == "xpsnr" else 0.5))
    svt_params = build_svt_params(video_policy)
    attempted_target = metric_target
    last_error: Exception | None = None

    while attempted_target >= min_target:
        try:
            return run_crf_search(
                source_path,
                preferred_metric=metric_name,
                metric_target=attempted_target,
                preset=int(video_policy["preset"]),
                pixel_format=str(video_policy["pixel_format"]),
                sample_every=str(video_policy["sample_every"]),
                sample_duration=str(video_policy["sample_duration"]),
                min_crf=int(video_policy["min_crf"]),
                max_crf=int(video_policy["max_crf"]),
                max_encoded_percent=int(video_policy["max_encoded_percent"]),
                svt_params=svt_params,
                thorough=bool(video_policy.get("thorough", False)),
                process_controller=process_controller,
                host=host,
            )
        except QualitySearchError as exc:
            last_error = exc
            attempted_target = round(attempted_target - relax_step, 3)

    if last_error is not None:
        raise last_error
    raise RuntimeError("Quality search did not run")


def _build_ffmpeg_command(
    *,
    source_path: Path,
    staging_path: Path,
    video_policy: dict[str, Any],
    audio_policy: dict[str, Any],
    subtitle_policy: dict[str, Any],
    selection: dict[str, Any],
    quality: QualitySearchResult,
) -> list[str]:
    cmd = [
        ffmpeg_binary(),
        "-y",
        "-i",
        str(source_path),
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-map",
        "0:v:0",
    ]

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
            str(video_policy["preset"]),
            "-crf",
            _format_crf(quality.crf),
            "-svtav1-params",
            f"tune=0:film-grain={int(video_policy.get('default_grain', 0))}:film-grain-denoise={int(video_policy.get('grain_denoise', 0))}",
        ]
    )

    for output_index, audio in enumerate(selection["audio_tracks"]):
        codec = _audio_codec(audio, audio_policy)
        cmd.extend([f"-c:a:{output_index}", codec])
        cmd.extend([f"-metadata:s:a:{output_index}", "language=eng"])
        cmd.extend([f"-disposition:a:{output_index}", "default"])
        if codec == "libopus":
            layout_filter = _opus_layout_filter(audio)
            if layout_filter:
                cmd.extend([f"-af:a:{output_index}", layout_filter])
                cmd.extend([f"-mapping_family:a:{output_index}", "1"])
            cmd.extend([f"-b:a:{output_index}", _opus_bitrate(audio, audio_policy)])

    for output_index, subtitle in enumerate(selection["subtitle_tracks"]):
        codec = "srt" if subtitle["codec_name"] in TEXT_SUBTITLE_CODECS else "copy"
        cmd.extend([f"-c:s:{output_index}", codec])
        cmd.extend([f"-metadata:s:s:{output_index}", "language=eng"])
        disposition = "default" if output_index == 0 and not subtitle.get("forced") else "0"
        if subtitle.get("forced"):
            disposition = "forced"
        cmd.extend([f"-disposition:s:{output_index}", disposition])

    if not selection["subtitle_tracks"]:
        cmd.extend(["-sn"])

    return cmd + [str(staging_path)]


def _finalize_output_path(temp_output: Path, staging_path: Path) -> None:
    if temp_output.exists():
        temp_output.replace(staging_path)
        return
    if staging_path.exists():
        return
    raise FileNotFoundError(f"Encoded output missing after ffmpeg completed: {temp_output}")


def _run_encode_command(
    *,
    ffmpeg_cmd: list[str],
    temp_output: Path,
    staging_path: Path,
    overwrite: bool,
    process_controller: ManagedProcessController | None,
    host: dict[str, Any] | None,
) -> subprocess.CompletedProcess[str]:
    host_mode = str((host or {}).get("mode") or "local")
    if host_mode != "ssh":
        return run_command(
            ffmpeg_cmd[:-1] + [str(temp_output)],
            process_controller=process_controller,
            capture_output=True,
            text=True,
        )

    ssh_host = str((host or {}).get("key") or (host or {}).get("host") or "").strip()
    if not ssh_host:
        raise RuntimeError("Remote encode host is missing an SSH target.")

    remote_ffmpeg_cmd = list(ffmpeg_cmd[:-1]) + [str(temp_output)]
    remote_ffmpeg_cmd[0] = Path(remote_ffmpeg_cmd[0]).name
    remote_script_parts = [
        remote_shell_path_export_line(),
        f"mkdir -p {shlex.quote(str(staging_path.parent))}",
        f"rm -f {shlex.quote(str(temp_output))}",
    ]
    if overwrite:
        remote_script_parts.append(f"rm -f {shlex.quote(str(staging_path))}")
    remote_script_parts.extend(
        [
            shlex.join(remote_ffmpeg_cmd),
            f"mv -f {shlex.quote(str(temp_output))} {shlex.quote(str(staging_path))}",
        ]
    )
    ssh_cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        ssh_host,
        "sh",
        "-lc",
        " && ".join(remote_script_parts),
    ]
    return run_command(ssh_cmd, process_controller=process_controller, capture_output=True, text=True)


def _select_streams(item: dict[str, Any]) -> dict[str, Any]:
    audio_tracks = item["audio_summary"]
    subtitle_tracks = item["subtitle_summary"]

    selected_audio = _pick_audio(audio_tracks)
    selected_subtitles = _pick_subtitles(subtitle_tracks, bool(item["resolved_policy"]["subtitle"].get("prefer_text", True)))

    return {
        "audio_tracks": [selected_audio],
        "subtitle_tracks": selected_subtitles,
    }


def _pick_audio(audio_tracks: list[dict[str, Any]]) -> dict[str, Any]:
    english = [track for track in audio_tracks if track.get("language") == "eng"]
    candidates = english or [track for track in audio_tracks if track.get("language") in {None, "und"}] or audio_tracks
    if not candidates:
        raise ValueError("No audio tracks available")
    return sorted(candidates, key=lambda track: (-int(track.get("default") or 0), -(int(track.get("channels") or 0)), int(track["index"])))[0]


def _pick_subtitles(subtitle_tracks: list[dict[str, Any]], prefer_text: bool) -> list[dict[str, Any]]:
    english = [track for track in subtitle_tracks if track.get("language") == "eng"]
    if not english:
        fallback = [track for track in subtitle_tracks if track.get("language") in {None, "und"}]
        if fallback and not any(track.get("language") not in {None, "und", "eng"} for track in subtitle_tracks):
            english = fallback
        else:
            return []

    def sort_key(track: dict[str, Any]) -> tuple[int, int, int, int]:
        codec = str(track.get("codec_name") or "")
        is_text = codec in TEXT_SUBTITLE_CODECS
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
    codec = str(track.get("codec_name") or "").lower()
    if codec in {str(name).lower() for name in audio_policy.get("copy_codecs", [])}:
        return "copy"
    if codec in {str(name).lower() for name in audio_policy.get("convert_to_opus_codecs", [])}:
        return "libopus"
    return "copy"


def _opus_bitrate(track: dict[str, Any], audio_policy: dict[str, Any]) -> str:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return str(audio_policy["surround_7_1_opus_bitrate"])
    if channels >= 6:
        return str(audio_policy["surround_5_1_opus_bitrate"])
    return str(audio_policy["stereo_opus_bitrate"])


def _opus_layout_filter(track: dict[str, Any]) -> str | None:
    channels = int(track.get("channels") or 2)
    if channels >= 8:
        return "channelmap=channel_layout=7.1"
    if channels >= 6:
        return "channelmap=channel_layout=5.1"
    return None


def _check(validation: dict[str, Any], passed: bool, message: str) -> None:
    validation["checks"].append({"passed": passed, "message": message})
    validation["passed"] = validation["passed"] and passed


def _estimate_audio_track_bytes(track: dict[str, Any], audio_policy: dict[str, Any], duration_seconds: float) -> int:
    codec = _audio_codec(track, audio_policy)
    if codec == "libopus":
        bitrate_text = _opus_bitrate(track, audio_policy)
        bitrate_bps = _parse_bitrate_text(bitrate_text)
    else:
        bitrate_bps = int(track.get("bit_rate") or 0)
        if bitrate_bps <= 0:
            channels = int(track.get("channels") or 2)
            bitrate_bps = 640_000 if channels >= 6 else 192_000
    return int((bitrate_bps / 8) * duration_seconds)


def _estimate_subtitle_track_bytes(track: dict[str, Any]) -> int:
    bit_rate = int(track.get("bit_rate") or 0)
    if bit_rate > 0:
        duration_seconds = float(track.get("duration_seconds") or 0.0)
        if duration_seconds > 0:
            return int((bit_rate / 8) * duration_seconds)
    codec = str(track.get("codec_name") or "").lower()
    if codec in TEXT_SUBTITLE_CODECS:
        return 128 * 1024
    return 4 * 1024 * 1024


def _parse_bitrate_text(value: str) -> int:
    stripped = value.strip().lower()
    if stripped.endswith("k"):
        return int(float(stripped[:-1]) * 1000)
    if stripped.endswith("m"):
        return int(float(stripped[:-1]) * 1_000_000)
    return int(float(stripped))


def _record_event(connection: sqlite3.Connection, library_item_id: int, event_type: str, details: dict[str, Any]) -> None:
    connection.execute(
        "INSERT INTO item_events(library_item_id, created_at, event_type, details_json) VALUES (?, ?, ?, ?)",
        (library_item_id, timestamp(), event_type, json.dumps(details, separators=(",", ":"))),
    )


def _format_crf(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}"
