import math
from fractions import Fraction
from pathlib import Path
from statistics import mean
from typing import Literal

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.core.type_defs import object_dict, object_list
from mediaforce.encoding.remote_media import RemoteMediaCommands

ANIMATION_PROBE = "animation_bitrate"
CUTOFF_KBPS = 1504
MIN_FLAT_FRACTION = 0.50
CLIP_SECONDS = 2.5
WINDOWS = 10
ENCODE_SCALE = "scale=w='min(iw,1920)':h='min(ih,1080)':force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
FLAT_FILTER = (
    "trim=end_frame=1,scale=w='min(iw,640)':h=-2,crop=iw*0.9:ih*0.9,format=yuv444p,median=radius=1:planes=1,split[a][b];"
    "[a]dilation=threshold0=255[hi];[b]erosion=threshold0=255[lo];[hi][lo]blend=all_mode=difference,"
    "lut=y='if(lte(val,3),255,0)',signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-"
)


def animation_look(kbps: float, flat_fraction: float) -> Literal["cartoon", "live_action"]:
    return "cartoon" if kbps <= CUTOFF_KBPS and flat_fraction >= MIN_FLAT_FRACTION else "live_action"


def animation_moments(duration: float) -> list[float]:
    seconds = min(CLIP_SECONDS, duration / WINDOWS)
    if not math.isfinite(duration) or seconds <= 0:
        raise ValueError("The animation has no readable duration.")
    return [min(duration - seconds, duration * (0.12 + 0.08 * index)) for index in range(WINDOWS)]


def hdr_picture(stream: dict[str, object]) -> bool:
    side_data = {str(object_dict(entry).get("side_data_type", "")).casefold() for entry in object_list(stream.get("side_data_list"))}
    return (stream.get("color_transfer") in ("smpte2084", "arib-std-b67")
            or stream.get("codec_tag_string") in ("dvhe", "dvh1", "dva1", "dvav", "dav1")
            or bool(side_data & {"dovi configuration record", "mastering display metadata", "content light level metadata"})
            or any("hdr10+" in kind or "dolby vision" in kind for kind in side_data))


def _video_kbps(output: str, seconds: float) -> float:
    lines = output.splitlines()
    time_base = next((Fraction(line.split(": ", 1)[1]) for line in lines if line.startswith("#tb 0: ")), None)
    packets = [line.split(",") for line in lines if line.startswith("0,")]
    if not packets or not time_base:
        raise ValueError("The animation sample contains no picture.")
    elapsed = float(sum(int(packet[3]) for packet in packets) * time_base)
    if elapsed <= 0 or elapsed + float(time_base) < seconds:
        raise ValueError("The animation sample did not finish.")
    return sum(int(packet[4]) for packet in packets) * 8 / elapsed / 1000


def probe_animation(path: Path, duration: float, *, video_stream: dict[str, object],
                    process_controller: ManagedProcessController | None, command_runner: RemoteMediaCommands | None) -> dict[str, object]:
    if hdr_picture(video_stream):
        return {"hdr": True}
    starts = animation_moments(duration)
    seconds = min(CLIP_SECONDS, duration / WINDOWS)
    binary = "ffmpeg" if command_runner else ffmpeg_binary()

    def sample(at_seconds: float, options: list[str]) -> str:
        command = ["nice", "-n", "19", binary, "-nostdin", "-v", "error", "-ss", f"{at_seconds:.6f}", "-i", str(path),
                   "-map", "0:V:0", "-an", "-sn", "-dn", *options, "-"]
        if command_runner:
            result = command_runner.run(command, timeout_seconds=600, process_controller=process_controller)
            result.check_returncode()
        else:
            result = run_command(command, timeout=600, check=True, capture_output=True, text=True, process_controller=process_controller)
        return result.stdout

    rates = []
    fractions = []
    for start in starts:
        encoded = sample(start, ["-t", str(seconds), "-vf", ENCODE_SCALE, "-c:v", "libsvtav1", "-preset", "6",
                                 "-crf", "36", "-svtav1-params", "tune=0:film-grain=0:lp=2", "-pix_fmt", "yuv420p10le", "-f", "framecrc"])
        rates.append(_video_kbps(encoded, seconds))
        frame = sample(start + seconds / 2, ["-vf", FLAT_FILTER, "-frames:v", "1", "-f", "null"])
        fraction = next((float(line.split("=")[1]) / 255 for line in frame.splitlines() if line.startswith("lavfi.signalstats.YAVG=")), None)
        if fraction is None or not 0 <= fraction <= 1:
            raise ValueError("The animation frame could not be measured.")
        fractions.append(fraction)
    return {"kbps": mean(sorted(rates)[2:-2]), "flat_fraction": mean(fractions)}
