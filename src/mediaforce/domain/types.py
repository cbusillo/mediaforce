import pathlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SourceTier(Enum):
    """Source quality classification."""

    PRISTINE = "pristine"  # Modern streaming, Blu-ray
    GOOD = "good"  # Most HD TV
    MEDIOCRE = "mediocre"  # Older HD, moderate grain
    POOR = "poor"  # Upscaled SD, heavy noise


@dataclass
class TierSettings:
    """Encoding settings for a source tier."""

    crf: int
    preset: int
    film_grain: int  # 0 = disabled, 4-8 = typical range
    denoise: Optional[str]  # None, "light", "medium", "heavy"


@dataclass
class MediaInfo:
    """Parsed media file information."""

    path: pathlib.Path
    duration_seconds: Optional[float] = None
    video_codec: Optional[str] = None
    video_width: Optional[int] = None
    video_height: Optional[int] = None
    video_bitrate_kbps: Optional[int] = None
    video_bit_depth: Optional[int] = None
    video_framerate: Optional[float] = None
    video_field_order: Optional[str] = None
    interlace_detected: Optional[bool] = None
    interlace_tff_ratio: Optional[float] = None
    audio_tracks: list[dict] = field(default_factory=list)
    subtitle_tracks: list[dict] = field(default_factory=list)
    container_bitrate_kbps: Optional[int] = None
    is_hdr: Optional[bool] = None
    hdr_format: Optional[str] = None

    @property
    def resolution_label(self) -> str:
        if self.video_height is None:
            return "unknown"
        if self.video_height >= 2160:
            return "4K"
        if self.video_height >= 1080:
            return "1080p"
        if self.video_height >= 720:
            return "720p"
        if self.video_height >= 480:
            return "480p"
        return f"{self.video_height}p"

    @property
    def is_already_av1(self) -> bool:
        codec = self.video_codec or ""
        return "av1" in codec.lower()

    @property
    def is_interlaced(self) -> bool:
        if self.interlace_detected is not None:
            return self.interlace_detected
        if not self.video_field_order:
            return False
        return self.video_field_order.lower() not in ("progressive", "unknown", "")


@dataclass
class ClassificationResult:
    """Result of source quality classification."""

    tier: SourceTier
    confidence: str  # "high", "medium", "low"
    reasons: list[str]
    recommended_settings: TierSettings


@dataclass
class QualityMetrics:
    """Quality metrics from SSIM/PSNR/VMAF comparison."""

    ssim: Optional[float] = None
    psnr: Optional[float] = None
    vmaf: Optional[float] = None
    sample_duration_sec: Optional[float] = None
    sample_start_sec: Optional[float] = None

    @property
    def is_acceptable(self) -> bool:
        if self.vmaf is not None:
            return self.vmaf >= 85
        if self.ssim is not None:
            return self.ssim >= 0.92
        return True

    @property
    def quality_grade(self) -> str:
        if self.vmaf is not None:
            if self.vmaf >= 95:
                return "A+"
            if self.vmaf >= 90:
                return "A"
            if self.vmaf >= 85:
                return "B"
            if self.vmaf >= 80:
                return "C"
            return "D"
        if self.ssim is not None:
            if self.ssim >= 0.98:
                return "A+"
            if self.ssim >= 0.96:
                return "A"
            if self.ssim >= 0.94:
                return "B"
            if self.ssim >= 0.92:
                return "C"
            return "D"
        if self.psnr is not None:
            if self.psnr >= 45:
                return "A+"
            if self.psnr >= 40:
                return "A"
            if self.psnr >= 35:
                return "B"
            if self.psnr >= 30:
                return "C"
            return "D"
        return "?"


@dataclass
class OutlierThresholds:
    min_vmaf: float = 85.0
    min_ssim: float = 0.92
    min_psnr: float = 32.0
    min_compression_ratio: float = 0.15
    max_compression_ratio: float = 0.75
    min_bitrate_1080p: int = 800
    max_bitrate_1080p: int = 6000
    min_bitrate_720p: int = 500
    max_bitrate_720p: int = 4000
    min_bitrate_480p: int = 300
    max_bitrate_480p: int = 2000


DEFAULT_OUTLIER_THRESHOLDS = OutlierThresholds()


@dataclass
class OutlierResult:
    """Outlier check result used by review and worker reporting."""

    is_outlier: bool
    reasons: list[str]
    metrics: Optional[QualityMetrics] = None
    compression_ratio: Optional[float] = None
    output_bitrate_kbps: Optional[int] = None
