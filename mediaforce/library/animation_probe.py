import math
from collections.abc import Callable
import tempfile
import uuid
from statistics import quantiles
from pathlib import Path
from typing import Literal

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.encoding.remote_media import RemoteMediaCommands
from mediaforce.remote import copy_remote_file_to_local
from mediaforce.encoding.ffmpeg import ffmpeg_hwaccel_input_args
from mediaforce.reviewing.helpers import default_timestamps, format_crf, spaced_complexity_timestamps
from mediaforce.reviewing.renderers import preview_render_command, browser_preview_proxy_command, source_review_clip_command

ANIMATION_PROBE = "animation_bitrate"
CUTOFF_KBPS = 758
REVIEW_MARGIN_KBPS = 40
CLIP_SECONDS = 10


def animation_look(kbps: float, choice: str | None = None) -> Literal["cartoon", "live_action", "unmeasured"]:
    if kbps < CUTOFF_KBPS - REVIEW_MARGIN_KBPS:
        return "cartoon"
    if kbps > CUTOFF_KBPS + REVIEW_MARGIN_KBPS:
        return "live_action"
    if choice == "cartoon":
        return "cartoon"
    if choice == "live_action":
        return "live_action"
    return "unmeasured"


def animation_moments(path: Path, duration: float, run: Callable[[list[str]], str], probe_binary: str) -> list[float]:
    defaults = default_timestamps(duration, CLIP_SECONDS)
    if duration < CLIP_SECONDS * 12:
        return defaults
    intervals = ",".join(f"{duration * (.06 + .83 * index / 23):.3f}%+{CLIP_SECONDS + 2}" for index in range(24))
    packets = run(["nice", "-n", "19", probe_binary, "-v", "error", "-select_streams", "V:0", "-read_intervals", intervals,
                   "-show_entries", "packet=pts_time,size", "-of", "csv=p=0", str(path)])
    moments = spaced_complexity_timestamps(packets, duration, CLIP_SECONDS)
    return moments if len(moments) == 3 else defaults


def probe_animation(path: Path, duration: float, *, review_dir: Path, process_controller: ManagedProcessController | None,
                    command_runner: RemoteMediaCommands | None) -> dict[str, object]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The animation has no readable duration.")
    seconds = min(duration, CLIP_SECONDS)
    name = f"animation-{uuid.uuid4().hex}"
    binary = (lambda: "ffmpeg") if command_runner else ffmpeg_binary

    def run(command: list[str]) -> str:
        if command_runner:
            result = command_runner.run(command, timeout_seconds=600, process_controller=process_controller)
            result.check_returncode()
        else:
            result = run_command(command, timeout=600, check=True, capture_output=True, text=True, process_controller=process_controller)
        return result.stdout

    starts = animation_moments(path, duration, run, "ffprobe" if command_runner else ffprobe_binary())
    review_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=review_dir) as scratch:
        local = Path(scratch)
        work = Path("/tmp") if command_runner else local
        clips = [work / f"{name}-{index}.mkv" for index in range(len(starts))]
        original = work / f"{name}-original.mp4"
        sample = work / f"{name}-sample.mp4"
        try:
            rates = []
            for start, encoded in zip(starts, clips, strict=True):
                command = preview_render_command(source_path=path, source_codec=None, output_path=encoded,
                    clip_time=start, duration_seconds=seconds, encoder="libsvtav1", pixel_format="yuv420p10le", preset=4, crf=48,
                    svt_params=["tune=0", "film-grain=8", "film-grain-denoise=0", "lp=2"], audio_plan=None, video_filter=None,
                    ffmpeg_binary=binary, ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args, format_crf=format_crf)
                progress = dict(line.split("=", 1) for line in run(["nice", "-n", "19", *command]).splitlines() if "=" in line)
                size = int(progress.get("total_size", "0"))
                if progress.get("progress") != "end" or size <= 0 or int(progress.get("frame", "0")) <= 0:
                    raise ValueError("The animation sample did not finish.")
                rates.append(size * 8 / seconds / 1000)
            kbps = quantiles(rates, n=10, method="inclusive")[-1] if len(rates) > 1 else rates[0]
            hardest = rates.index(max(rates))
            encoded, compare_at = clips[hardest], starts[hardest]
            if animation_look(kbps) != "unmeasured":
                return {"kbps": kbps}
            run(["nice", "-n", "19", *source_review_clip_command(source_path=path, source_codec=None, output_path=original,
                clip_time=compare_at, duration_seconds=seconds, audio_plan=None, ffmpeg_binary=binary, ffmpeg_hwaccel_input_args=ffmpeg_hwaccel_input_args)])
            run(["nice", "-n", "19", *browser_preview_proxy_command(source_path=encoded, output_path=sample, duration_seconds=seconds,
                                                                    include_audio=False, ffmpeg_binary=binary)])
            comparison = local / name
            comparison.mkdir()
            moment: dict[str, object] = {"at_seconds": compare_at}
            for side, clip in (("original", original), ("sample", sample)):
                target = comparison / f"{side}.mp4"
                if command_runner:
                    copy_remote_file_to_local(command_runner.host, clip, target, 120)
                else:
                    clip.rename(target)
                moment[side] = {"url": f"/review-media/{name}/{target.name}", "moment_bytes": target.stat().st_size}
            comparison.rename(review_dir / name)
            return {"kbps": kbps, "moment": moment}
        finally:
            if command_runner:
                command_runner.run(["rm", "-f", *(str(clip) for clip in clips), str(original), str(sample)], timeout_seconds=30, process_controller=None)
