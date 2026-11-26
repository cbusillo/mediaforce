import math
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from statistics import median
from typing import Any

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.encoding.commands import planned_stream_codecs, planned_stream_maps
from mediaforce.encoding.helpers import build_svt_params
from mediaforce.encoding.remote_media import RemoteMediaCommands
from mediaforce.encoding.streams import ProductionStreamPlan
from mediaforce.remote import copy_remote_file_to_local
from mediaforce.reviewing.renderers import browser_preview_proxy_command
from mediaforce.tuning.size_goals import DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT
from mediaforce.tuning.target_defaults import REFERENCE_RUNTIME_SECONDS

ANIMATION_PROBE = "animation_bitrate"
CARTOON_TARGET_MB = 150
CLIP_SECONDS = 10
REVIEW_MARGIN = 1.0
CALIBRATION_FRAME = "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"


def animation_moments(duration: float, fingerprint: dict[str, Any]) -> list[float]:
    ranges = [entry for entry in fingerprint.get("analysis", {}).get("ranges", [])
              if entry.get("status") == "measured" and entry.get("measurements")]
    selected: list[float] = []
    for metric in ("ydif_p90", "edge_density_p90", "high_texture_frame_fraction"):
        for entry in sorted(ranges, key=lambda row: row["measurements"].get(metric, 0), reverse=True):
            start = min(float(entry["start_seconds"]), max(0.0, duration - CLIP_SECONDS))
            if all(abs(start - other) >= CLIP_SECONDS for other in selected):
                selected.append(start)
                break
    if len(selected) < min(3, max(1, int(duration // CLIP_SECONDS))):
        raise ValueError("Too few separate scenes have measured motion and detail for a picture test.")
    return sorted(selected)


def picture_look(score: float, floor: float) -> str:
    if not math.isfinite(score) or not 0 <= score <= 100 or not math.isfinite(floor):
        raise ValueError("The picture test returned an unreadable score.")
    if score < floor - REVIEW_MARGIN:
        return "live_action"
    if score > floor + REVIEW_MARGIN:
        return "cartoon"
    return "unmeasured"


def _progress(output: str) -> dict[str, str]:
    progress = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if progress.get("progress") != "end":
        raise ValueError("The animation sample did not finish.")
    return progress


def _picture_sample(run: Callable[[list[str]], str], ffmpeg: list[str], reference: Path, encoded: Path,
                    video_policy: dict[str, Any], target_bytes: float, reserved: float, seconds: float, frames: int) -> tuple[float, float]:
    budget = target_bytes - reserved
    if budget <= 0:
        raise ValueError("The retained sound and other streams already use the cartoon size.")
    rate = budget * 8 / seconds
    tolerance = video_policy.get("sample_projection_tolerance_percent", DEFAULT_SAMPLE_PROJECTION_TOLERANCE_PERCENT) / 100
    for _ in range(4):
        result = _progress(run([*ffmpeg, "-i", str(reference), "-map", "0:v:0", "-an", "-c:v", "libsvtav1", "-preset", str(video_policy["preset"]),
            "-pix_fmt", "yuv420p10le", "-svtav1-params", ":".join([*build_svt_params(video_policy), "lp=2"]), "-b:v", str(max(1, round(rate))),
            "-fps_mode", "passthrough", str(encoded)]))
        size = int(result["total_size"])
        if size <= 0 or int(result["frame"]) != frames:
            raise ValueError("The test picture did not cover the original scene.")
        if abs(size - budget) <= target_bytes * tolerance:
            score = float(run(["ab-av1", "vmaf", "--reference", str(reference), "--distorted", str(encoded), "--vmaf", "n_threads=2"]).strip())
            return score, size * 8 / seconds
        rate *= budget / size
    raise ValueError("The encoder could not make this scene at the cartoon size.")


def probe_animation(path: Path, duration: float, *, review_dir: Path, process_controller: ManagedProcessController | None,
                    command_runner: RemoteMediaCommands | None, fingerprint: dict[str, Any], plan: ProductionStreamPlan,
                    video_policy: dict[str, Any]) -> dict[str, Any]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The animation has no readable duration.")
    seconds = min(duration, CLIP_SECONDS)
    starts = animation_moments(duration, fingerprint)
    name = f"animation-{uuid.uuid4().hex}"
    binary = (lambda: "ffmpeg") if command_runner else ffmpeg_binary
    ffmpeg = [binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1"]

    def run(command: list[str]) -> str:
        command = ["nice", "-n", "19", *command]
        if command_runner:
            result = command_runner.run(command, timeout_seconds=1800, process_controller=process_controller)
            result.check_returncode()
        else:
            result = run_command(command, timeout=1800, check=True, capture_output=True, text=True, process_controller=process_controller)
        return result.stdout

    review_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=review_dir) as scratch:
        local = Path(scratch)
        work = Path("/tmp") if command_runner else local
        nonvideo = work / f"{name}-streams.mkv"
        references = [work / f"{name}-{index}-ref.mkv" for index in range(len(starts))]
        clips = [work / f"{name}-{index}.mkv" for index in range(len(starts))]
        proxies = [work / f"{name}-{side}.mp4" for side in ("original", "sample")]
        try:
            maps = planned_stream_maps(plan)
            reserved = 0.0
            if plan.audio_streams or plan.subtitle_streams:
                measured = _progress(run([*ffmpeg, "-i", str(path), "-vn", "-map_chapters", "-1", *maps, *planned_stream_codecs(plan), str(nonvideo)]))
                reserved = int(measured["total_size"]) / duration
                if reserved <= 0:
                    raise ValueError("The size of the retained sound and other streams could not be measured.")
            elif plan.attachment_streams:
                reserved = sum(stream.source_size_bytes or 0 for stream in plan.attachment_streams) / duration
            target = CARTOON_TARGET_MB * 1_000_000 / REFERENCE_RUNTIME_SECONDS
            scenes = []
            for start, reference, encoded in zip(starts, references, clips, strict=True):
                ref = _progress(run([*ffmpeg, "-ss", str(start), "-i", str(path), "-t", str(seconds), "-map", "0:v:0", "-vf", CALIBRATION_FRAME,
                                     "-fps_mode", "passthrough", "-c:v", "ffv1", "-threads", "2", str(reference)]))
                frames = int(ref["frame"])
                elapsed = int(ref["out_time_us"]) / 1_000_000
                if frames <= 0 or abs(elapsed - seconds) > 0.25:
                    raise ValueError("The original scene did not cover the test duration.")
                score, rate = _picture_sample(run, ffmpeg, reference, encoded, video_policy, target * seconds, reserved * seconds, seconds, frames)
                scenes.append({"at_seconds": start, "score": score, "kbps": rate / 1000, "mb_per_45": (rate / 8 + reserved) * REFERENCE_RUNTIME_SECONDS / 1_000_000})
            worst = min(range(len(scenes)), key=lambda index: scenes[index]["score"])
            floor = float(video_policy["min_target_vmaf"])
            look = picture_look(scenes[worst]["score"], floor)
            result = {"kbps": median(row["kbps"] for row in scenes), "picture_look": look, "scenes": scenes,
                      "floor": floor, "reserved_mb_per_45": reserved * REFERENCE_RUNTIME_SECONDS / 1_000_000}
            if look == "unmeasured":
                comparison = local / name
                comparison.mkdir()
                moment: dict[str, Any] = {"at_seconds": starts[worst]}
                for side, source, proxy in zip(("original", "sample"), (references[worst], clips[worst]), proxies, strict=True):
                    run(browser_preview_proxy_command(source_path=source, output_path=proxy, duration_seconds=seconds, include_audio=False, ffmpeg_binary=binary))
                    target_path = comparison / f"{side}.mp4"
                    if command_runner:
                        copy_remote_file_to_local(command_runner.host, proxy, target_path, 120)
                    else:
                        proxy.rename(target_path)
                    moment[side] = {"url": f"/review-media/{name}/{target_path.name}", "moment_bytes": target_path.stat().st_size}
                comparison.rename(review_dir / name)
                result["moment"] = moment
            return result
        finally:
            if command_runner:
                command_runner.run(["rm", "-f", *map(str, [nonvideo, *references, *clips, *proxies])], timeout_seconds=30, process_controller=None)
