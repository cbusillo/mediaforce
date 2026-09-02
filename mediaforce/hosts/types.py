from dataclasses import dataclass, field

DEFAULT_HOST_CAPABILITIES = ("encode_queue",)
DEFAULT_HOST_MEDIA_ACCESS = "mounted"
DEFAULT_WAKE_WAIT_SECONDS = 60
REMOTE_STATUS_RETRY_DELAY_SECONDS = 1.0
REMOTE_SHELL_PATH = "/opt/homebrew/opt/ffmpeg-full/bin:/usr/local/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
FFMPEG_MISSING_ISSUE = "ffmpeg is not installed on the remote PATH."
AB_AV1_MISSING_ISSUE = "ab-av1 is not installed on the remote PATH."
SAMPLE_METRIC_MISSING_ISSUE = "ffmpeg is missing both libvmaf and xpsnr support required for sampled calibration."
SAMPLE_AV1_ENCODER_MISSING_ISSUE = "ffmpeg is missing libsvtav1 support required for sampled calibration."
LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE = "Sampled calibration is not supported on Linux hosts."
SOURCE_ROOT_READ_MISSING_ISSUE = "Source root is not readable on this host."
STAGING_ROOT_WRITE_MISSING_ISSUE = "Staging root is not writable on this host."


@dataclass(slots=True)
class HostStatus:
    key: str
    label: str
    mode: str
    priority: int
    capabilities: list[str]
    available: bool
    message: str
    missing_paths: list[str]
    repo_path: str | None = None
    ffmpeg_path: str | None = None
    platform: str = "unknown"
    videotoolbox_available: bool | None = None
    schedule_timezone: str | None = None
    utc_offset_minutes: int | None = None
    issues: list[str] = field(default_factory=list)
    missing_mounts: list[str] = field(default_factory=list)
    detail: str | None = None
    setup_supported: bool = False
    setup_requires_password: bool = False
    trust_reset_supported: bool = False
    probe_available: bool | None = None


@dataclass(slots=True)
class HostSetupResult:
    ok: bool
    message: str
    detail: str | None = None
    performed_steps: list[str] = field(default_factory=list)
    requires_password: bool = False
    failure_kind: str | None = None


class HostReadinessError(RuntimeError):
    def __init__(self, message: str, *, failure_kind: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


__all__ = [
    "AB_AV1_MISSING_ISSUE",
    "DEFAULT_HOST_CAPABILITIES",
    "DEFAULT_HOST_MEDIA_ACCESS",
    "DEFAULT_WAKE_WAIT_SECONDS",
    "FFMPEG_MISSING_ISSUE",
    "HostSetupResult",
    "HostStatus",
    "HostReadinessError",
    "LINUX_SAMPLE_CALIBRATION_UNSUPPORTED_ISSUE",
    "REMOTE_SHELL_PATH",
    "REMOTE_STATUS_RETRY_DELAY_SECONDS",
    "SAMPLE_AV1_ENCODER_MISSING_ISSUE",
    "SAMPLE_METRIC_MISSING_ISSUE",
    "SOURCE_ROOT_READ_MISSING_ISSUE",
    "STAGING_ROOT_WRITE_MISSING_ISSUE",
]
