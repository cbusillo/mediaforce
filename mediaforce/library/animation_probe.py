import json
import math
import tomllib
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Literal

from mediaforce.core.binaries import ffmpeg_binary, ffprobe_binary
from mediaforce.core.config import DEFAULT_CONFIG_PATH
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.core.type_defs import int_value, object_dict, object_list
from mediaforce.encoding.cadence import analyze_cadence, cadence_manifest_payload
from mediaforce.encoding.helpers import build_svt_params
from mediaforce.encoding.remote_media import RemoteMediaCommands
from mediaforce.encoding.video_filters import build_video_filter
from mediaforce.tuning.size_goals import size_goal_from_policy
from mediaforce.tuning.stream_budget import resolve_stream_budget_ledger, stream_budget_projection_blocker

ANIMATION_PROBE = "animation_picture"
CARTOON_TARGET_MB = 150
MIN_CARTOON_SCORE = 87
CLIP_SECONDS = 2.5
WINDOWS = 10


def animation_look(median_score: float) -> Literal["cartoon", "live_action"]:
    return "cartoon" if median_score >= MIN_CARTOON_SCORE else "live_action"


def animation_moments(duration: float) -> list[float]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The animation has no readable duration.")
    return [duration * (0.12 + 0.08 * index) for index in range(WINDOWS)]


def hdr_picture(stream: dict[str, object]) -> bool:
    side_data = {str(object_dict(entry).get("side_data_type", "")).casefold() for entry in object_list(stream.get("side_data_list"))}
    return (
        stream.get("color_transfer") in ("smpte2084", "arib-std-b67")
        or stream.get("codec_tag_string") in ("dvhe", "dvh1", "dva1", "dvav", "dav1")
        or bool(side_data & {"dovi configuration record", "mastering display metadata", "content light level metadata"})
        or any("hdr10+" in kind or "dolby vision" in kind for kind in side_data)
    )


@dataclass(frozen=True)
class PictureBudget:
    bytes_per_second: float
    lower: float
    upper: float


def _picture_budget(item: dict[str, Any], path: Path, ffprobe: str, run: Callable[[list[str]], str]) -> PictureBudget:
    for selector, tracks in (("a", item["audio_summary"]), ("s", item["subtitle_summary"])):
        if not any(track["size_bytes"] is None for track in tracks):
            continue
        sizes: Counter[int] = Counter()
        packets = run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                selector,
                "-show_entries",
                "packet=stream_index,size",
                "-of",
                "csv=p=0",
                str(path),
            ]
        )
        for line in packets.splitlines():
            fields = line.split(",")
            if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
                sizes[int(fields[0])] += int(fields[1])
        for track in tracks:
            if size := sizes.get(track["index"]):
                track["size_bytes"] = size
    duration = item["duration_seconds"]
    goal = size_goal_from_policy(
        {"size_goal_mode": "normalized", "target_size_mb": CARTOON_TARGET_MB, "target_runtime_minutes": 45}
    ).resolve(duration)
    ledger = resolve_stream_budget_ledger(item, resolved_size_goal=goal)
    if stream_budget_projection_blocker(ledger) or not ledger.remaining_video_bytes or ledger.non_video_bytes is None:
        raise ValueError("The picture allowance for this original could not be measured.")
    return PictureBudget(
        ledger.remaining_video_bytes / duration,
        (ledger.size_goal["sample_lower_bound_bytes"] - ledger.non_video_bytes) / duration,
        (ledger.size_goal["sample_upper_bound_bytes"] - ledger.non_video_bytes) / duration,
    )


@dataclass(frozen=True)
class PictureSample:
    frames: int
    seconds: float
    size: int
    duplicate_times: bool


@dataclass(frozen=True)
class AnimationCheck:
    run: Callable[[list[str]], str]
    ffmpeg: str
    ffprobe: str
    scratch: Path
    video: dict[str, Any]
    filters: str | None
    budget: PictureBudget

    def transcode(self, *args: str) -> None:
        self.run([self.ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-threads", "2", *args])

    def sample(self, clip: Path) -> PictureSample:
        rows = json.loads(
            self.run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "V:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts_time,duration_time,size",
                    "-of",
                    "json",
                    str(clip),
                ]
            )
        ).get("packets")
        if not rows:
            raise ValueError("The animation sample contains no picture.")
        times = [float(row.get("pts_time", "nan")) for row in rows]
        end = max(t + float(row.get("duration_time", "nan")) for t, row in zip(times, rows, strict=True))
        seconds = end - min(times)
        size = sum(int(row.get("size", 0)) for row in rows)
        if not math.isfinite(seconds) or seconds <= 0 or size <= 0:
            raise ValueError("The animation sample has no measurable picture.")
        return PictureSample(len(rows), seconds, size, len(times) != len(set(times)))

    def encode_at_budget(self, reference: Path, picture: Path, sample: PictureSample) -> bool:
        rate = max(1000, round(self.budget.bytes_per_second * 8))
        target = self.budget.bytes_per_second * sample.seconds
        lo, hi = 1, int(self.video["target_search_max_crf"])
        best = math.inf
        tried = set()
        mode = "-b:v"
        while lo <= hi:
            value = rate if mode == "-b:v" else (lo + hi) // 2
            candidate = self.scratch / "candidate.mkv"
            self.transcode(
                "-i",
                str(reference),
                "-map",
                "0:V:0",
                "-c:v",
                self.video["encoder"],
                "-pix_fmt",
                self.video["pixel_format"],
                "-preset",
                str(self.video["preset"]),
                "-svtav1-params",
                ":".join([*build_svt_params(self.video), "lp=2"]),
                mode,
                str(value),
                "-fps_mode",
                "passthrough",
                str(candidate),
            )
            actual = self.sample(candidate)
            if actual.frames != sample.frames or abs(actual.seconds - sample.seconds) > 0.003:
                raise ValueError("The compressed picture did not match the reference timing.")
            ratio = actual.size / target
            if self.budget.lower <= actual.size / sample.seconds <= self.budget.upper and abs(ratio - 1) < best:
                self.run(["mv", str(candidate), str(picture)])
                best = abs(ratio - 1)
                if best <= 0.045:
                    return True
            if mode == "-b:v":
                tried.add(rate)
                rate = max(1000, round(rate / ratio))
                if rate in tried or len(tried) >= 7:
                    mode = "-crf"
            elif ratio > 1:
                lo = value + 1
            else:
                hi = value - 1
        return math.isfinite(best)

    def score(self, picture: Path, reference: Path, frames: int, offset: int = 0) -> float:
        align = "settb=1/24000,setpts=N*1001"
        log = self.scratch / "score.json"
        self.transcode(
            "-i",
            str(picture),
            "-threads",
            "2",
            "-i",
            str(reference),
            "-filter_complex_threads",
            "2",
            "-lavfi",
            f"[0:v]trim=start_frame={offset}:end_frame={offset + frames},{align}[d];[1:v]{align}[r];[d][r]libvmaf=log_fmt=json:log_path={log}:n_threads=3:shortest=1:repeatlast=0",
            "-an",
            "-f",
            "null",
            "-",
        )
        metrics = json.loads(self.run(["cat", str(log)]))
        value = float(metrics.get("pooled_metrics", {}).get("vmaf", {}).get("mean", "nan"))
        if len(metrics.get("frames", [])) != frames or not 0 <= value <= 100:
            raise ValueError("The animation sample could not be compared frame by frame.")
        return value

    def reference(self, path: Path, start: float, length: float, output: Path) -> PictureSample:
        self.transcode(
            "-ss",
            f"{start:.6f}",
            "-i",
            str(path),
            "-t",
            str(length),
            "-map",
            "0:V:0",
            "-map_chapters",
            "-1",
            *(["-vf", self.filters] if self.filters else []),
            "-fps_mode",
            "passthrough",
            "-c:v",
            "ffv1",
            "-threads",
            "2",
            str(output),
        )
        sample = self.sample(output)
        if abs(sample.seconds - length) > sample.seconds / sample.frames + 0.05:
            raise ValueError("The animation sample did not finish.")
        if sample.duplicate_times:
            normalized = self.scratch / "aligned.mkv"
            self.transcode(
                "-i",
                str(output),
                "-vf",
                "settb=AVTB,setpts=N/FRAME_RATE/TB",
                "-fps_mode",
                "passthrough",
                "-c:v",
                "ffv1",
                "-threads",
                "2",
                str(normalized),
            )
            aligned = self.sample(normalized)
            if (
                aligned.frames != sample.frames
                or aligned.duplicate_times
                or abs(aligned.seconds - length) > aligned.seconds / aligned.frames + 0.05
            ):
                raise ValueError("The animation frames could not be matched.")
            self.run(["mv", str(normalized), str(output)])
            return aligned
        return sample

    def moment(self, path: Path, start: float, seconds: float) -> float:
        reference, picture = self.scratch / "reference.mkv", self.scratch / "picture.mkv"
        sample = self.reference(path, start, seconds, reference)
        if self.encode_at_budget(reference, picture, sample):
            return self.score(picture, reference, sample.frames)
        prefix, context = self.scratch / "prefix.mkv", self.scratch / "context.mkv"
        lead = min(8.0, start)
        preceding = self.reference(path, start - lead, lead, prefix)
        self.transcode(
            "-i",
            str(prefix),
            "-threads",
            "2",
            "-i",
            str(reference),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "ffv1",
            "-threads",
            "2",
            str(context),
        )
        combined = self.sample(context)
        if combined.frames != preceding.frames + sample.frames:
            raise ValueError("The animation frames could not be matched.")
        if self.encode_at_budget(context, picture, combined):
            return self.score(picture, reference, sample.frames, preceding.frames)
        raise ValueError("The sample could not be encoded within the cartoon picture allowance.")


def probe_animation(
    path: Path,
    item: dict[str, Any],
    *,
    video_stream: dict[str, object],
    process_controller: ManagedProcessController | None,
    command_runner: RemoteMediaCommands | None,
) -> dict[str, object]:
    if hdr_picture(video_stream):
        return {"hdr": True}
    duration = item["duration_seconds"]
    starts = animation_moments(duration)
    with DEFAULT_CONFIG_PATH.open("rb") as defaults:
        video = tomllib.load(defaults)["video"]
    ffprobe = "ffprobe" if command_runner else ffprobe_binary()

    def run(command: list[str]) -> str:
        if command_runner:
            result = command_runner.run(command, timeout_seconds=600, process_controller=process_controller)
            result.check_returncode()
        else:
            result = run_command(
                command, timeout=600, check=True, capture_output=True, text=True, process_controller=process_controller
            )
        return result.stdout

    budget = _picture_budget(item, path, ffprobe, run)
    cadence = analyze_cadence(
        path,
        video_stream=video_stream,
        duration_seconds=duration,
        process_controller=process_controller,
        command_runner=command_runner,
    )
    evidence, decision = cadence_manifest_payload(cadence, source_id=str(path), source_fingerprint=item["source_fingerprint"])
    if decision.get("status") != "resolved":
        cadence = analyze_cadence(
            path,
            video_stream=video_stream,
            duration_seconds=duration,
            max_frames=4000,
            range_count=10,
            timeout_seconds=300,
            process_controller=process_controller,
            command_runner=command_runner,
        )
        evidence, decision = cadence_manifest_payload(cadence, source_id=str(path), source_fingerprint=item["source_fingerprint"])
    filters = build_video_filter(
        video,
        width=int_value(video_stream.get("width")),
        height=int_value(video_stream.get("height")),
        cadence_evidence=evidence,
        cadence_decision=decision if evidence else None,
        cadence_source_fingerprint=item["source_fingerprint"],
    )
    scratch = Path("/tmp") / f"mediaforce-animation-{uuid.uuid4().hex}"
    check = AnimationCheck(run, "ffmpeg" if command_runner else ffmpeg_binary(), ffprobe, scratch, video, filters, budget)
    run(["mkdir", "-m", "700", str(scratch)])
    try:
        return {"median_score": median(check.moment(path, start, min(CLIP_SECONDS, duration / WINDOWS)) for start in starts)}
    finally:
        cleanup = ["rm", "-rf", str(scratch)]
        if command_runner:
            command_runner.run(cleanup, timeout_seconds=30, process_controller=None).check_returncode()
        else:
            run_command(cleanup, timeout=30, check=True, process_controller=None)
