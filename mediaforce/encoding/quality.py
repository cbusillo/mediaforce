import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mediaforce.core.binaries import ffmpeg_binary
from mediaforce.encoding.ffmpeg import ab_av1_hwaccel_input_args
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.core.type_defs import object_dict
from mediaforce.remote import execution_mode_for_host, run_remote_command

RESULT_RE = re.compile(
    r"crf\s+(?P<crf>[0-9.]+)\s+(?P<metric>VMAF|XPSNR)\s+(?P<score>[0-9.]+)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class QualitySearchResult:
    crf: float
    metric: str
    target: float
    score: float
    stdout: str


@dataclass(slots=True)
class SampleEncodeResult:
    metric: str
    score: float
    predicted_encode_percent: float
    predicted_encode_seconds: float
    predicted_encode_size_bytes: int
    stdout: str


class QualitySearchError(RuntimeError):
    pass


class SampleEncodeError(RuntimeError):
    pass


REMOTE_QUALITY_TIMEOUT_SECONDS = 2 * 60 * 60


def select_quality_metric(preferred: str) -> tuple[str, float]:
    preferred_value = preferred.lower()
    if preferred_value == "vmaf" and has_libvmaf():
        return "vmaf", 95.0
    if preferred_value == "auto":
        if has_libvmaf():
            return "vmaf", 95.0
        return "xpsnr", 41.0
    return preferred_value, 41.0


def run_crf_search(
        source_path: Path,
        *,
        source_codec: str | None = None,
        preferred_metric: str,
        metric_target: float,
        preset: int,
        pixel_format: str,
        sample_every: str,
        sample_duration: str,
        min_crf: int,
        max_crf: int,
        max_encoded_percent: int,
        svt_params: list[str],
        thorough: bool,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, object] | None = None,
) -> QualitySearchResult:
    metric, _ = select_quality_metric(preferred_metric)
    cmd = [
        "ab-av1",
        "crf-search",
        "-i",
        str(source_path),
        "--encoder",
        "libsvtav1",
        "--preset",
        str(preset),
        "--pix-format",
        pixel_format,
        "--sample-every",
        sample_every,
        "--sample-duration",
        sample_duration,
        "--min-crf",
        str(min_crf),
        "--max-crf",
        str(max_crf),
        "--max-encoded-percent",
        str(max_encoded_percent),
    ]
    cmd.extend(
        ab_av1_hwaccel_input_args(
            source_codec,
            platform_name=str((host or {}).get("platform") or "") or None,
            videotoolbox_available=bool((host or {}).get("videotoolbox_available"))
            if "videotoolbox_available" in (host or {})
            else None,
        )
    )
    if thorough:
        cmd.append("--thorough")
    for param in svt_params:
        cmd.extend(["--svt", param])

    if metric == "vmaf":
        cmd.extend(["--min-vmaf", str(metric_target)])
    else:
        cmd.extend(["--min-xpsnr", str(metric_target)])

    result = _run_quality_command(cmd, process_controller=process_controller, host=host)
    if result.returncode != 0:
        details = result.stdout.strip()
        if result.stderr.strip():
            details = f"{details}\n{result.stderr.strip()}".strip()
        raise QualitySearchError(details)
    parsed = parse_quality_result(result.stdout)
    if parsed.metric.lower() != metric:
        parsed.metric = metric.upper()
    parsed.target = metric_target
    return parsed


def parse_quality_result(stdout: str) -> QualitySearchResult:
    matches = RESULT_RE.findall(stdout)
    if not matches:
        raise ValueError(f"Could not parse ab-av1 output:\n{stdout}")
    crf_text, metric, score_text = matches[-1]
    return QualitySearchResult(
        crf=float(crf_text),
        metric=metric.upper(),
        target=0.0,
        score=float(score_text),
        stdout=stdout,
    )


def run_sample_encode(
        source_path: Path,
        *,
        source_codec: str | None = None,
        preferred_metric: str,
        crf: float,
        preset: int,
        pixel_format: str,
        sample_every: str,
        sample_duration: str,
        svt_params: list[str],
        process_controller: ManagedProcessController | None = None,
        host: dict[str, object] | None = None,
) -> SampleEncodeResult:
    metric, _ = select_quality_metric(preferred_metric)
    cmd = [
        "ab-av1",
        "sample-encode",
        "-i",
        str(source_path),
        "--encoder",
        "libsvtav1",
        "--preset",
        str(preset),
        "--pix-format",
        pixel_format,
        "--sample-every",
        sample_every,
        "--sample-duration",
        sample_duration,
        "--crf",
        _format_crf(crf),
        "--stdout-format",
        "json",
    ]
    cmd.extend(
        ab_av1_hwaccel_input_args(
            source_codec,
            platform_name=str((host or {}).get("platform") or "") or None,
            videotoolbox_available=bool((host or {}).get("videotoolbox_available"))
            if "videotoolbox_available" in (host or {})
            else None,
        )
    )
    for param in svt_params:
        cmd.extend(["--svt", param])
    if metric == "xpsnr":
        cmd.append("--xpsnr")

    result = _run_quality_command(cmd, process_controller=process_controller, host=host)
    if result.returncode != 0:
        details = result.stdout.strip()
        if result.stderr.strip():
            details = f"{details}\n{result.stderr.strip()}".strip()
        raise SampleEncodeError(details)
    return parse_sample_encode_result(result.stdout, metric)


def parse_sample_encode_result(stdout: str, metric: str) -> SampleEncodeResult:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse ab-av1 sample-encode output:\n{stdout}") from exc

    metric_key = metric.lower()
    score = payload.get(metric_key)
    if score is None:
        raise ValueError(f"ab-av1 sample-encode output missing {metric_key} score:\n{stdout}")

    return SampleEncodeResult(
        metric=metric.upper(),
        score=float(score),
        predicted_encode_percent=float(payload["predicted_encode_percent"]),
        predicted_encode_seconds=float(payload["predicted_encode_seconds"]),
        predicted_encode_size_bytes=int(payload["predicted_encode_size"]),
        stdout=stdout,
    )


def _format_crf(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _run_quality_command(
        cmd: list[str],
        *,
        process_controller: ManagedProcessController | None,
        host: dict[str, object] | None,
) -> subprocess.CompletedProcess[str]:
    host_mode = execution_mode_for_host(host)
    if host_mode != "ssh":
        return run_command(cmd, process_controller=process_controller)
    return run_remote_command(object_dict(host), cmd, REMOTE_QUALITY_TIMEOUT_SECONDS)


@lru_cache(maxsize=1)
def has_libvmaf() -> bool:
    result = subprocess.run(
        [ffmpeg_binary(), "-hide_banner", "-filters"],
        check=True,
        capture_output=True,
        text=True,
    )
    return "libvmaf" in result.stdout
