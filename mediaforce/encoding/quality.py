import os
import json
import math
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from mediaforce.encoding.ffmpeg import ab_av1_hwaccel_input_args
from mediaforce.core.process_control import ManagedProcessController, run_command
from mediaforce.core.type_defs import object_dict
from mediaforce.remote import execution_mode_for_host, run_remote_command

RESULT_RE = re.compile(
    r"crf\s+(?P<crf>[0-9.]+)\s+(?P<metric>VMAF|XPSNR)\s+(?P<score>[0-9.]+)",
    re.IGNORECASE,
)
QUALITY_PREDICTION_RE = re.compile(
    r"crf\s+(?P<crf>[0-9.]+)\s+(?P<metric>VMAF|XPSNR)\s+(?P<score>[0-9.]+)"
    r"\s+predicted video stream size\s+(?P<size>[0-9.]+)\s+(?P<unit>[KMGT]?i?B)"
    r"\s+\((?P<percent>[0-9.]+)%\)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class QualitySearchResult:
    crf: float
    metric: str
    target: float
    score: float
    stdout: str
    target_size_trace: dict[str, object] | None = None


@dataclass(slots=True)
class SampleEncodeResult:
    metric: str
    score: float
    predicted_encode_percent: float
    predicted_encode_seconds: float
    predicted_encode_size_bytes: int
    stdout: str
    sampled_clip_size_bytes: int | None = None


@dataclass(slots=True)
class QualitySearchMeasurement:
    crf: float
    metric: str
    score: float
    predicted_encode_percent: float
    predicted_encode_size_bytes: int
    line: str


@dataclass(slots=True)
class QualityFailureAnalysis:
    kind: str
    retry_strategy: str
    auto_retry_allowed: bool
    requested_metric: str
    target_score: float
    min_score: float
    max_encoded_percent: float
    best_candidate: QualitySearchMeasurement | None
    proposed_max_encoded_percent: int | None
    summary: str


class QualitySearchError(RuntimeError):
    pass


class SampleEncodeError(RuntimeError):
    pass


class QualityTempCleanupError(RuntimeError):
    pass


class QualityTempSetupError(RuntimeError):
    pass


REMOTE_QUALITY_TIMEOUT_SECONDS = 2 * 60 * 60
REMOTE_QUALITY_CLEANUP_TIMEOUT_SECONDS = 15
LOCAL_QUALITY_PATH_PREFIX = "/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin"
DEFAULT_LOCAL_QUALITY_TEMP_ROOT_NAME = "mediaforce-quality-temp"
LEGACY_LOCAL_QUALITY_TEMP_ROOT_NAME = "quality-temp"
PREVIOUS_LOCAL_QUALITY_TEMP_ROOT_NAME = "mediaforce-quality"
QUALITY_CLEANUP_NOTE_PREFIX = "Cleanup warning: "
QUALITY_SIZE_NEAR_MISS_PERCENT = 2.0
QUALITY_MAX_AUTO_PERCENT = 25


def parse_quality_search_measurements(stdout: str) -> list[QualitySearchMeasurement]:
    measurements: list[QualitySearchMeasurement] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        match = QUALITY_PREDICTION_RE.search(line)
        if match is None:
            continue
        measurements.append(
            QualitySearchMeasurement(
                crf=float(match.group("crf")),
                metric=match.group("metric").upper(),
                score=float(match.group("score")),
                predicted_encode_percent=float(match.group("percent")),
                predicted_encode_size_bytes=_size_to_bytes(float(match.group("size")), match.group("unit")),
                line=line,
            )
        )
    return measurements


def analyze_quality_policy_failure(
        error_message: str,
        video_policy: dict[str, object],
        *,
        max_encoded_percent_override: float | None = None,
) -> dict[str, object] | None:
    if "failed to find a suitable crf" not in error_message.lower():
        return None
    measurements = parse_quality_search_measurements(error_message)
    if not measurements:
        return None
    metric = _analysis_metric(video_policy, measurements)
    target_score = _float_policy(video_policy, f"target_{metric.lower()}", 0.0)
    min_score = _float_policy(video_policy, f"min_target_{metric.lower()}", target_score)
    max_encoded_percent = (
        max_encoded_percent_override
        if max_encoded_percent_override is not None
        else _float_policy(video_policy, "max_encoded_percent", 100.0)
    )
    same_metric = [measurement for measurement in measurements if measurement.metric.lower() == metric.lower()]
    if not same_metric:
        return None
    candidates_above_floor = [measurement for measurement in same_metric if measurement.score >= min_score]
    if candidates_above_floor:
        best_candidate = min(
            candidates_above_floor,
            key=lambda measurement: (
                measurement.predicted_encode_percent,
                -measurement.score,
                measurement.crf,
            ),
        )
    else:
        best_candidate = max(
            same_metric,
            key=lambda measurement: (
                measurement.score,
                -measurement.predicted_encode_percent,
                -measurement.crf,
            ),
        )
    proposed_cap = None
    auto_retry_allowed = False
    retry_strategy = "needs_operator_approval"
    kind = "quality_floor_too_strict"
    if best_candidate.score >= min_score and best_candidate.predicted_encode_percent > max_encoded_percent:
        kind = "size_cap_too_strict"
        proposed_cap = _proposed_size_cap(max_encoded_percent, best_candidate.predicted_encode_percent)
        auto_retry_allowed = (
            proposed_cap is not None
            and proposed_cap <= QUALITY_MAX_AUTO_PERCENT
            and proposed_cap - max_encoded_percent <= QUALITY_SIZE_NEAR_MISS_PERCENT
        )
        retry_strategy = "auto_adjust_cap" if auto_retry_allowed else "needs_operator_approval"
    elif best_candidate.score < min_score:
        kind = "quality_floor_too_strict"

    analysis = QualityFailureAnalysis(
        kind=kind,
        retry_strategy=retry_strategy,
        auto_retry_allowed=auto_retry_allowed,
        requested_metric=metric.upper(),
        target_score=target_score,
        min_score=min_score,
        max_encoded_percent=max_encoded_percent,
        best_candidate=best_candidate,
        proposed_max_encoded_percent=proposed_cap,
        summary=_quality_failure_summary(
            kind=kind,
            metric=metric.upper(),
            best_candidate=best_candidate,
            max_encoded_percent=max_encoded_percent,
            proposed_cap=proposed_cap,
            auto_retry_allowed=auto_retry_allowed,
        ),
    )
    return _analysis_to_dict(analysis)


def _analysis_metric(video_policy: dict[str, object], measurements: list[QualitySearchMeasurement]) -> str:
    configured = str(video_policy.get("quality_metric") or "").strip().lower()
    if configured in {"vmaf", "xpsnr"}:
        return configured
    return measurements[-1].metric.lower()


def _float_policy(video_policy: dict[str, object], key: str, default: float) -> float:
    value = video_policy.get(key, default)
    if not isinstance(value, (int, float, str)):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _proposed_size_cap(max_encoded_percent: float, measured_percent: float) -> int | None:
    proposed = max(math.floor(max_encoded_percent) + 1, math.ceil(measured_percent + 0.25))
    if proposed <= max_encoded_percent:
        proposed = math.floor(max_encoded_percent) + 1
    return proposed


def _quality_failure_summary(
        *,
        kind: str,
        metric: str,
        best_candidate: QualitySearchMeasurement,
        max_encoded_percent: float,
        proposed_cap: int | None,
        auto_retry_allowed: bool,
) -> str:
    if kind == "size_cap_too_strict":
        retry_copy = (
            f"; retrying with max_encoded_percent {proposed_cap}"
            if auto_retry_allowed and proposed_cap is not None
            else ""
        )
        return (
            f"Best measured candidate was CRF {best_candidate.crf:g} at {metric} {best_candidate.score:.2f} "
            f"and {best_candidate.predicted_encode_percent:.1f}% against the {max_encoded_percent:g}% cap{retry_copy}."
        )
    return (
        f"Best measured candidate was CRF {best_candidate.crf:g} at {metric} {best_candidate.score:.2f} "
        f"and {best_candidate.predicted_encode_percent:.1f}%, below the requested quality floor."
    )


def _analysis_to_dict(analysis: QualityFailureAnalysis) -> dict[str, object]:
    payload = asdict(analysis)
    if analysis.best_candidate is not None:
        payload["best_candidate"] = asdict(analysis.best_candidate)
    return payload


def _size_to_bytes(size: float, unit: str) -> int:
    normalized = unit.strip().lower()
    multipliers = {
        "b": 1,
        "kb": 1000,
        "mb": 1000 ** 2,
        "gb": 1000 ** 3,
        "tb": 1000 ** 4,
        "kib": 1024,
        "mib": 1024 ** 2,
        "gib": 1024 ** 3,
        "tib": 1024 ** 4,
    }
    return int(size * multipliers.get(normalized, 1))


def _format_number(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _host_hwaccel_context(host: dict[str, object] | None) -> tuple[str | None, bool | None]:
    host_payload = object_dict(host)
    platform_name = str(host_payload.get("platform") or "") or None
    videotoolbox_available = (
        bool(host_payload.get("videotoolbox_available"))
        if "videotoolbox_available" in host_payload
        else None
    )
    return platform_name, videotoolbox_available


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
        max_encoded_percent: float,
        svt_params: list[str],
        thorough: bool,
        video_filter: str | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, object] | None = None,
        quality_temp_dir: Path | None = None,
) -> QualitySearchResult:
    metric, _ = select_quality_metric(preferred_metric)
    platform_name, videotoolbox_available = _host_hwaccel_context(host)
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
        _format_number(max_encoded_percent),
    ]
    cmd.extend(
        ab_av1_hwaccel_input_args(
            source_codec,
            platform_name=platform_name,
            videotoolbox_available=videotoolbox_available,
        )
    )
    if thorough:
        cmd.append("--thorough")
    if video_filter:
        cmd.extend(["--vfilter", video_filter])
    for param in svt_params:
        cmd.extend(["--svt", param])

    if metric == "vmaf":
        cmd.extend(["--min-vmaf", str(metric_target)])
    else:
        cmd.extend(["--min-xpsnr", str(metric_target)])
    try:
        scoped_temp_dir = _scoped_quality_temp_dir(quality_temp_dir, host=host)
    except OSError as exc:
        raise QualityTempSetupError(_quality_temp_setup_error(quality_temp_dir, exc, host=host)) from exc
    if scoped_temp_dir is not None:
        cmd.extend(["--temp-dir", str(scoped_temp_dir)])

    try:
        result = _run_quality_command(cmd, process_controller=process_controller, host=host)
    except Exception as exc:
        cleanup_error = _cleanup_scoped_quality_temp_dir(scoped_temp_dir, host=host)
        if cleanup_error is not None:
            _attach_quality_cleanup_detail(exc, cleanup_error)
        raise
    cleanup_error = _cleanup_scoped_quality_temp_dir(scoped_temp_dir, host=host)
    if result.returncode != 0:
        details = result.stdout.strip()
        if result.stderr.strip():
            details = f"{details}\n{result.stderr.strip()}".strip()
        error = QualitySearchError(details)
        if cleanup_error is not None:
            _attach_quality_cleanup_detail(error, cleanup_error)
        raise error
    if cleanup_error is not None:
        raise QualityTempCleanupError(cleanup_error)
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
        video_filter: str | None = None,
        process_controller: ManagedProcessController | None = None,
        host: dict[str, object] | None = None,
        quality_temp_dir: Path | None = None,
) -> SampleEncodeResult:
    metric, _ = select_quality_metric(preferred_metric)
    platform_name, videotoolbox_available = _host_hwaccel_context(host)
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
            platform_name=platform_name,
            videotoolbox_available=videotoolbox_available,
        )
    )
    if video_filter:
        cmd.extend(["--vfilter", video_filter])
    for param in svt_params:
        cmd.extend(["--svt", param])
    if metric == "xpsnr":
        cmd.append("--xpsnr")
    try:
        scoped_temp_dir = _scoped_quality_temp_dir(quality_temp_dir, host=host)
    except OSError as exc:
        raise QualityTempSetupError(_quality_temp_setup_error(quality_temp_dir, exc, host=host)) from exc
    if scoped_temp_dir is not None:
        cmd.extend(["--temp-dir", str(scoped_temp_dir)])

    try:
        result = _run_quality_command(cmd, process_controller=process_controller, host=host)
    except Exception as exc:
        cleanup_error = _cleanup_scoped_quality_temp_dir(scoped_temp_dir, host=host)
        if cleanup_error is not None:
            _attach_quality_cleanup_detail(exc, cleanup_error)
        raise
    cleanup_error = _cleanup_scoped_quality_temp_dir(scoped_temp_dir, host=host)
    if result.returncode != 0:
        details = result.stdout.strip()
        if result.stderr.strip():
            details = f"{details}\n{result.stderr.strip()}".strip()
        error = SampleEncodeError(details)
        if cleanup_error is not None:
            _attach_quality_cleanup_detail(error, cleanup_error)
        raise error
    if cleanup_error is not None:
        raise QualityTempCleanupError(cleanup_error)
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
        sampled_clip_size_bytes=_sampled_clip_size_bytes(payload),
    )


def _sampled_clip_size_bytes(payload: dict[str, object]) -> int | None:
    for key in ("sampled_clip_size", "sampled_clip_size_bytes", "encoded_size", "encoded_size_bytes"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            parsed = int(float(value)) if isinstance(value, int | float | str) and str(value).strip() else 0
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    return None


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
        return run_command(cmd, process_controller=process_controller, env=_local_quality_environment())
    temp_dir = _quality_temp_dir_arg(cmd)
    if temp_dir is not None:
        mkdir_result = run_remote_command(object_dict(host), ["mkdir", "-p", temp_dir], REMOTE_QUALITY_TIMEOUT_SECONDS)
        if mkdir_result.returncode != 0:
            details = mkdir_result.stdout.strip()
            if mkdir_result.stderr.strip():
                details = f"{details}\n{mkdir_result.stderr.strip()}".strip()
            raise QualityTempSetupError(details or f"Failed to prepare remote temp dir: {temp_dir}")
    return run_remote_command(object_dict(host), cmd, REMOTE_QUALITY_TIMEOUT_SECONDS)


def _scoped_quality_temp_dir(quality_temp_dir: Path | None, *, host: dict[str, object] | None) -> Path | None:
    if quality_temp_dir is None:
        return None
    host_mode = execution_mode_for_host(host)
    if host_mode != "ssh":
        quality_temp_dir.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=".mediaforce-ab-av1-", dir=quality_temp_dir))
    return quality_temp_dir / f".mediaforce-ab-av1-{uuid.uuid4().hex}"


def _cleanup_scoped_quality_temp_dir(scoped_temp_dir: Path | None, *, host: dict[str, object] | None) -> str | None:
    if scoped_temp_dir is None:
        return None
    host_mode = execution_mode_for_host(host)
    if host_mode != "ssh":
        try:
            shutil.rmtree(scoped_temp_dir)
        except FileNotFoundError:
            return None
        except OSError as exc:
            return f"Failed to remove local quality temp dir {scoped_temp_dir}: {exc}"
        return None
    try:
        result = run_remote_command(
            object_dict(host),
            ["rm", "-rf", str(scoped_temp_dir)],
            REMOTE_QUALITY_CLEANUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Successful quality runs should not fail just because best-effort SSH
        # cleanup took too long; periodic sweeps will catch leftovers later.
        return None
    except Exception as exc:
        return f"Failed to remove remote quality temp dir {scoped_temp_dir}: {exc}"
    if result.returncode == 0:
        return None
    details = result.stdout.strip()
    if result.stderr.strip():
        details = f"{details}\n{result.stderr.strip()}".strip()
    return details or f"Failed to remove remote quality temp dir {scoped_temp_dir}"


def _attach_quality_cleanup_detail(exc: Exception, cleanup_error: str) -> None:
    cleaned = cleanup_error.strip()
    if not cleaned:
        return
    setattr(exc, "quality_cleanup_error", cleaned)
    if hasattr(exc, "add_note"):
        exc.add_note(f"{QUALITY_CLEANUP_NOTE_PREFIX}{cleaned}")


def quality_cleanup_error(exc: BaseException) -> str | None:
    value = getattr(exc, "quality_cleanup_error", None)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def quality_error_message(exc: BaseException) -> str:
    message = str(exc).strip()
    cleanup_error = quality_cleanup_error(exc)
    if cleanup_error is None:
        return message
    cleanup_line = f"{QUALITY_CLEANUP_NOTE_PREFIX}{cleanup_error}"
    if not message:
        return cleanup_line
    return f"{message}\n{cleanup_line}"


def default_local_quality_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / DEFAULT_LOCAL_QUALITY_TEMP_ROOT_NAME


def legacy_local_quality_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / LEGACY_LOCAL_QUALITY_TEMP_ROOT_NAME


def previous_local_quality_temp_root() -> Path:
    return Path(tempfile.gettempdir()) / PREVIOUS_LOCAL_QUALITY_TEMP_ROOT_NAME


def resolve_local_quality_temp_root(preferred_root: Path, *fallback_roots: Path) -> Path:
    for candidate in _quality_temp_root_candidates(preferred_root, *fallback_roots):
        if _quality_temp_root_is_writable(candidate):
            return candidate
    raise OSError(f"No writable quality temp root available for {preferred_root}")


def _quality_temp_root_candidates(preferred_root: Path, *fallback_roots: Path) -> list[Path]:
    candidates: list[Path] = []

    def add_candidate(path: Path | None) -> None:
        if path is None:
            return
        if any(existing == path for existing in candidates):
            return
        candidates.append(path)

    add_candidate(preferred_root)
    for fallback_root in fallback_roots:
        add_candidate(fallback_root)
    try:
        add_candidate(default_local_quality_temp_root())
    except OSError:
        pass
    return candidates


def _quality_temp_root_is_writable(root: Path) -> bool:
    probe_dir = root / f".mediaforce-quality-probe-{uuid.uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_dir.mkdir()
        probe_dir.rmdir()
    except OSError:
        return False
    return True


def _quality_temp_setup_error(
        quality_temp_dir: Path | None,
        exc: OSError,
        *,
        host: dict[str, object] | None,
) -> str:
    base_dir = str(quality_temp_dir) if quality_temp_dir is not None else "default temp dir"
    host_mode = execution_mode_for_host(host)
    location = "remote" if host_mode == "ssh" else "local"
    return f"Failed to prepare {location} quality temp dir under {base_dir}: {exc}"


def _quality_temp_dir_arg(cmd: list[str]) -> str | None:
    try:
        index = cmd.index("--temp-dir")
    except ValueError:
        return None
    if index + 1 >= len(cmd):
        return None
    value = str(cmd[index + 1]).strip()
    return value or None


def _local_quality_environment() -> dict[str, str]:
    env = dict(os.environ)
    current_path = env.get("PATH", "")
    override_dirs = _mediaforce_binary_override_dirs(env)
    prefixes = [*override_dirs, LOCAL_QUALITY_PATH_PREFIX]
    prefix = ":".join(prefixes)
    env["PATH"] = f"{prefix}:{current_path}" if current_path else prefix
    return env


def _mediaforce_binary_override_dirs(env: dict[str, str]) -> list[str]:
    directories: list[str] = []
    for key in ("MEDIAFORCE_FFMPEG", "MEDIAFORCE_FFPROBE"):
        raw_value = env.get(key, "").strip()
        if not raw_value:
            continue
        override_path = Path(raw_value).expanduser()
        directory = override_path.parent
        if not directory.is_absolute():
            continue

        directory_str = str(directory)
        if directory_str not in directories:
            directories.append(directory_str)
    return directories


@lru_cache(maxsize=1)
def has_libvmaf() -> bool:
    result = subprocess.run(
        ["sh", "-lc", "ffmpeg -hide_banner -filters"],
        check=True,
        capture_output=True,
        text=True,
        env=_local_quality_environment(),
    )
    return "libvmaf" in result.stdout
