"""Sound check for samples whose audio Mediaforce re-encodes.

The check encodes the whole episode's audio exactly as production would, then
compares it with the original: ViSQOL on clips spread through the episode, and
ffmpeg loudness, dialogue, peak and clipping measurements on the whole file.
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.core.type_defs import float_value, object_dict, object_list
from mediaforce.encoding.fingerprint import parse_ebur128_summary
from mediaforce.encoding.streams import opus_encode_arguments

AUDIO_QUALITY_SCHEMA_VERSION = 1
VISQOL_IMAGE = "mediaforce-visqol:38d0b01"
# ViSQOL audio mode's ceiling: what a file scores against itself.
VISQOL_MAX_SCORE = 4.73
DEFAULT_MIN_SOUND_SCORE = 4.3
CLIP_SECONDS = 30.0
CLIP_POSITIONS = (0.2, 0.5, 0.8)
DIALOGUE_BAND_FILTER = "highpass=f=300,lowpass=f=3400"
MAX_DIALOGUE_DROP_DB = 1.5
MAX_LOUDNESS_CHANGE_LU = 2.0
MAX_LOUDNESS_RANGE_CHANGE_LU = 2.0
NEW_PEAK_ALLOWANCE_DB = 0.5
NEW_FLATNESS_ALLOWANCE = 0.5
ENCODE_TIMEOUT_SECONDS = 1800.0
MEASURE_TIMEOUT_SECONDS = 900.0
VISQOL_TIMEOUT_SECONDS = 300.0

_MOS_LQO_RE = re.compile(r"MOS-LQO:\s*([-+]?\d+(?:\.\d+)?)")
_FLAT_FACTOR_RE = re.compile(r"Flat factor:\s*([-+]?\d+(?:\.\d+)?)")

CommandRunner = Callable[..., Any]


class AudioCheckUnavailableError(RuntimeError):
    """A tool the sound check needs could not run; the reason is shown as is."""


@dataclass(frozen=True, slots=True)
class AudioLevels:
    integrated_lufs: float
    loudness_range_lu: float
    true_peak_dbtp: float
    dialogue_lufs: float
    flat_factor: float

    @property
    def dialogue_relative_db(self) -> float:
        return self.dialogue_lufs - self.integrated_lufs

    def to_payload(self) -> dict[str, float]:
        return {
            "integrated_lufs": self.integrated_lufs,
            "loudness_range_lu": self.loudness_range_lu,
            "true_peak_dbtp": self.true_peak_dbtp,
            "dialogue_lufs": self.dialogue_lufs,
            "flat_factor": self.flat_factor,
        }


def min_sound_score(audio_policy: Mapping[str, Any]) -> float:
    return float_value(audio_policy.get("min_sound_score")) or DEFAULT_MIN_SOUND_SCORE


def clip_starts(duration_seconds: float) -> list[float]:
    latest_start = max(0.0, duration_seconds - CLIP_SECONDS)
    return [round(min(latest_start, duration_seconds * position), 3) for position in CLIP_POSITIONS]


def check_sample_audio(
        *,
        source_path: Path,
        audio_plan: Mapping[str, Any] | None,
        duration_seconds: float,
        audio_policy: Mapping[str, Any],
        process_controller: ManagedProcessController | None = None,
        run: CommandRunner = run_command,
        work_root: Path | None = None,
) -> dict[str, Any]:
    if audio_plan is None:
        return _unchanged_result("This sample has no audio Mediaforce can check.")
    if str(audio_plan.get("production_action") or "") != "transcode":
        return _unchanged_result("The original audio is kept as is.")
    floor = min_sound_score(audio_policy)
    with tempfile.TemporaryDirectory(prefix="mediaforce-sound-check-", dir=work_root) as work_dir:
        work_path = Path(work_dir)
        reference_path = work_path / "original.mka"
        candidate_path = work_path / "new.mka"
        starts = clip_starts(duration_seconds)
        try:
            _run_checked(
                run,
                _copy_audio_command(source_path, reference_path, audio_plan),
                process_controller=process_controller,
                timeout=ENCODE_TIMEOUT_SECONDS,
                failure="The original audio could not be read for the sound check",
            )
            _run_checked(
                run,
                _production_audio_command(reference_path, candidate_path, audio_plan),
                process_controller=process_controller,
                timeout=ENCODE_TIMEOUT_SECONDS,
                failure="The new audio could not be made for the sound check",
            )
            reference = measure_audio_levels(reference_path, run=run, process_controller=process_controller)
            candidate = measure_audio_levels(candidate_path, run=run, process_controller=process_controller)
            clip_dirs = _cut_clip_pairs(
                reference_path,
                candidate_path,
                starts=starts,
                work_path=work_path,
                run=run,
                process_controller=process_controller,
            )
            clip_scores = [
                {"start_seconds": start, "score": _score_clip(clip_dir, run=run, process_controller=process_controller)}
                for start, clip_dir in zip(starts, clip_dirs, strict=True)
            ]
        except AudioCheckUnavailableError as exc:
            return {
                "schema_version": AUDIO_QUALITY_SCHEMA_VERSION,
                "status": "unmeasured",
                "reason": str(exc),
                "min_sound_score": floor,
                "max_sound_score": VISQOL_MAX_SCORE,
            }
    sound_score = round(statistics.fmean(float(clip["score"]) for clip in clip_scores), 3)
    checks = audio_quality_checks(
        sound_score=sound_score,
        floor=floor,
        reference=reference,
        candidate=candidate,
    )
    return {
        "schema_version": AUDIO_QUALITY_SCHEMA_VERSION,
        "status": "passed" if all(check["passed"] for check in checks) else "failed",
        "reason": None,
        "sound_score": sound_score,
        "min_sound_score": floor,
        "max_sound_score": VISQOL_MAX_SCORE,
        "clip_scores": clip_scores,
        "checks": checks,
        "original": reference.to_payload(),
        "new": candidate.to_payload(),
        "production_codec": "opus",
        "production_bitrate_text": audio_plan.get("production_bitrate_text"),
        "tool": {"visqol_image": visqol_image()},
    }


def sample_sound_check_summary(sample_result: Mapping[str, Any]) -> dict[str, Any] | None:
    audio_quality = object_dict(sample_result.get("audio_quality"))
    if not audio_quality:
        return None
    return {
        "status": audio_quality.get("status"),
        "sound_score": audio_quality.get("sound_score"),
        "min_sound_score": audio_quality.get("min_sound_score"),
        "failed_checks": [
            object_dict(check).get("key")
            for check in object_list(audio_quality.get("checks"))
            if not object_dict(check).get("passed")
        ],
    }


def sample_audio_review_reason(audio_quality: Mapping[str, Any]) -> str | None:
    status = audio_quality.get("status")
    if status == "failed":
        failed = [
            f"{check.get('label')}: {check.get('detail')}"
            for check in object_list(audio_quality.get("checks"))
            if not object_dict(check).get("passed")
        ]
        return f"The sound check failed ({'; '.join(failed)})."
    if status == "unmeasured":
        return f"The sound check could not run. {audio_quality.get('reason') or ''}".strip()
    return None


def audio_quality_checks(
        *,
        sound_score: float,
        floor: float,
        reference: AudioLevels,
        candidate: AudioLevels,
) -> list[dict[str, Any]]:
    dialogue_change = candidate.dialogue_relative_db - reference.dialogue_relative_db
    loudness_change = candidate.integrated_lufs - reference.integrated_lufs
    range_change = candidate.loudness_range_lu - reference.loudness_range_lu
    new_peak = candidate.true_peak_dbtp > 0.0 and (
            candidate.true_peak_dbtp > reference.true_peak_dbtp + NEW_PEAK_ALLOWANCE_DB
    )
    return [
        _audio_check(
            "sound_score",
            "Sound score",
            sound_score >= floor,
            f"{sound_score:.2f} of {VISQOL_MAX_SCORE:.2f}; needs {floor:.2f}",
        ),
        _audio_check(
            "dialogue",
            "Dialogue",
            dialogue_change >= -MAX_DIALOGUE_DROP_DB,
            f"{_signed(dialogue_change)} dB against the rest of the sound; may drop {MAX_DIALOGUE_DROP_DB:g} dB",
        ),
        _audio_check(
            "loudness",
            "Volume",
            abs(loudness_change) <= MAX_LOUDNESS_CHANGE_LU,
            f"{_signed(loudness_change)} dB overall; may move {MAX_LOUDNESS_CHANGE_LU:g} dB",
        ),
        _audio_check(
            "loudness_range",
            "Loud and quiet parts",
            abs(range_change) <= MAX_LOUDNESS_RANGE_CHANGE_LU,
            f"range {_signed(range_change)} dB; may move {MAX_LOUDNESS_RANGE_CHANGE_LU:g} dB",
        ),
        _audio_check(
            "true_peak",
            "Peaks",
            not new_peak,
            f"loudest peak {candidate.true_peak_dbtp:.1f} dB, original {reference.true_peak_dbtp:.1f} dB",
        ),
        _audio_check(
            "clipping",
            "Clipping",
            candidate.flat_factor <= reference.flat_factor + NEW_FLATNESS_ALLOWANCE,
            "none added" if candidate.flat_factor <= reference.flat_factor else "flattened peaks added",
        ),
    ]


def measure_audio_levels(
        path: Path,
        *,
        run: CommandRunner = run_command,
        process_controller: ManagedProcessController | None = None,
) -> AudioLevels:
    overall = parse_ebur128_summary(_filter_output(path, "ebur128=peak=true:framelog=quiet", run, process_controller))
    dialogue = parse_ebur128_summary(
        _filter_output(path, f"{DIALOGUE_BAND_FILTER},ebur128=framelog=quiet", run, process_controller)
    )
    stats_output = _filter_output(
        path,
        "astats=measure_perchannel=none:measure_overall=Flat_factor",
        run,
        process_controller,
    )
    flat_match = _FLAT_FACTOR_RE.search(stats_output)
    try:
        return AudioLevels(
            integrated_lufs=overall["integrated_loudness_lufs"],
            loudness_range_lu=overall["loudness_range_lu"],
            true_peak_dbtp=overall["true_peak_db"],
            dialogue_lufs=dialogue["integrated_loudness_lufs"],
            flat_factor=float(flat_match.group(1)) if flat_match else 0.0,
        )
    except KeyError as exc:
        raise AudioCheckUnavailableError(f"ffmpeg did not report a {exc.args[0]} reading for {path.name}") from exc


def parse_visqol_score(output: str) -> float | None:
    match = _MOS_LQO_RE.search(output)
    return float(match.group(1)) if match else None


def visqol_image() -> str:
    return os.environ.get("MEDIAFORCE_VISQOL_IMAGE") or VISQOL_IMAGE


def visqol_command(clip_dir: Path, *, container_name: str) -> list[str]:
    return [
        _docker_binary(),
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        f"--name={container_name}",
        "--volume",
        f"{clip_dir}:/clips:ro",
        visqol_image(),
        "--reference_file",
        "/clips/reference.wav",
        "--degraded_file",
        "/clips/new.wav",
    ]


def _cut_clip_pairs(
        reference_path: Path,
        candidate_path: Path,
        *,
        starts: list[float],
        work_path: Path,
        run: CommandRunner,
        process_controller: ManagedProcessController | None,
) -> list[Path]:
    clip_dirs = [work_path / f"clip-{index}" for index in range(len(starts))]
    for clip_dir in clip_dirs:
        clip_dir.mkdir()
    for path, name in ((reference_path, "reference"), (candidate_path, "new")):
        _run_checked(
            run,
            _clip_wavs_command(path, [(start, clip_dir / f"{name}.wav") for start, clip_dir in zip(starts, clip_dirs)]),
            process_controller=process_controller,
            timeout=MEASURE_TIMEOUT_SECONDS,
            failure="The sound check clips could not be cut",
        )
    return clip_dirs


def _score_clip(
        clip_dir: Path,
        *,
        run: CommandRunner,
        process_controller: ManagedProcessController | None,
) -> float:
    container_name = f"mediaforce-visqol-{uuid.uuid4().hex[:12]}"
    try:
        result = run(
            visqol_command(clip_dir, container_name=container_name),
            process_controller=process_controller,
            timeout=VISQOL_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        raise AudioCheckUnavailableError(f"Docker could not start the sound scorer: {exc}") from exc
    except BaseException:
        _remove_container(container_name, run)
        raise
    score = parse_visqol_score(f"{result.stdout}\n{result.stderr}")
    if result.returncode != 0 or score is None:
        _remove_container(container_name, run)
        raise AudioCheckUnavailableError(_visqol_failure(result))
    return round(score, 3)


def _copy_audio_command(source_path: Path, output_path: Path, audio_plan: Mapping[str, Any]) -> list[str]:
    # One read of a possibly remote source; every later pass reads this local copy.
    return [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        f"0:{int(audio_plan['source_stream_index'])}",
        "-vn",
        "-sn",
        "-dn",
        "-c:a",
        "copy",
        str(output_path),
    ]


def _production_audio_command(source_path: Path, output_path: Path, audio_plan: Mapping[str, Any]) -> list[str]:
    return [
        ffmpeg_binary(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        *opus_encode_arguments(
            channels=int(audio_plan.get("source_channels") or 2),
            bitrate_text=str(audio_plan.get("production_bitrate_text") or "128k"),
        ),
        str(output_path),
    ]


def _clip_wavs_command(path: Path, clips: list[tuple[float, Path]]) -> list[str]:
    # Seeking after -i decodes from the start. Seeking before -i is faster but
    # can land seconds off in Mediaforce's own AV1/Opus Matroska output.
    command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path)]
    for start_seconds, output_path in clips:
        # ViSQOL audio mode scores 48 kHz stereo; both sides are downmixed the same way.
        command.extend(
            [
                "-map",
                "0:a:0",
                "-ss",
                f"{start_seconds:.3f}",
                "-t",
                f"{CLIP_SECONDS:.3f}",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )
    return command


def _filter_output(
        path: Path,
        audio_filter: str,
        run: CommandRunner,
        process_controller: ManagedProcessController | None,
) -> str:
    result = _run_checked(
        run,
        [
            ffmpeg_binary(),
            "-hide_banner",
            "-nostats",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            audio_filter,
            "-f",
            "null",
            "-",
        ],
        process_controller=process_controller,
        timeout=MEASURE_TIMEOUT_SECONDS,
        failure=f"The sound of {path.name} could not be measured",
    )
    return f"{result.stdout}\n{result.stderr}"


def _run_checked(
        run: CommandRunner,
        command: list[str],
        *,
        process_controller: ManagedProcessController | None,
        timeout: float,
        failure: str,
) -> Any:
    try:
        result = run(command, process_controller=process_controller, timeout=timeout)
    except OSError as exc:
        raise AudioCheckUnavailableError(f"{failure}: {exc}") from exc
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip().splitlines()
        raise AudioCheckUnavailableError(f"{failure}: {details[-1] if details else f'exit {result.returncode}'}")
    return result


def _visqol_failure(result: Any) -> str:
    output = f"{result.stderr or ''}\n{result.stdout or ''}"
    if "Cannot connect to the Docker daemon" in output or "docker daemon" in output.lower():
        return "Docker isn't running, so the sound score could not be measured."
    if "No such image" in output or "Unable to find image" in output:
        return f"The sound scorer image {visqol_image()} isn't built on this Mac yet."
    lines = output.strip().splitlines()
    return f"The sound scorer failed: {lines[-1] if lines else f'exit {result.returncode}'}"


def _remove_container(container_name: str, run: CommandRunner) -> None:
    try:
        run([_docker_binary(), "rm", "--force", container_name], timeout=30.0)
    except OSError:
        pass


def _docker_binary() -> str:
    # noinspection PyDeprecation
    return os.environ.get("MEDIAFORCE_DOCKER") or shutil.which("docker") or "docker"


def _unchanged_result(reason: str) -> dict[str, Any]:
    return {"schema_version": AUDIO_QUALITY_SCHEMA_VERSION, "status": "unchanged", "reason": reason}


def _audio_check(key: str, label: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"key": key, "label": label, "passed": passed, "detail": detail}


def _signed(value: float) -> str:
    return f"{value:+.1f}"
