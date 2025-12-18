from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Optional

from mediaforce.domain.types import QualityMetrics
from mediaforce.services.media_probe import probe_media


@dataclass
class OutlierThresholds:
    """Thresholds for flagging encodes as outliers requiring review."""

    # Quality thresholds (below these = outlier)
    # Aligned with is_acceptable: VMAF >= 85 is acceptable
    min_vmaf: float = 85.0
    min_ssim: float = 0.92
    min_psnr: float = 32.0

    # Compression ratio thresholds (output/source)
    min_compression_ratio: float = 0.15  # Too aggressive (<15% of original)
    max_compression_ratio: float = 0.75  # Too weak (>75% of original)

    # Bitrate thresholds by resolution (kbps)
    # If output is below min or above max for resolution, flag it
    min_bitrate_1080p: int = 800
    max_bitrate_1080p: int = 6000
    min_bitrate_720p: int = 500
    max_bitrate_720p: int = 4000
    min_bitrate_480p: int = 300
    max_bitrate_480p: int = 2000


# Default thresholds
DEFAULT_OUTLIER_THRESHOLDS = OutlierThresholds()


@dataclass
class OutlierResult:
    """Result of outlier check with reasons."""

    is_outlier: bool
    reasons: list[str]
    metrics: Optional[QualityMetrics] = None
    compression_ratio: Optional[float] = None
    output_bitrate_kbps: Optional[int] = None


def check_for_outliers(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    metrics: Optional[QualityMetrics] = None,
    thresholds: OutlierThresholds = DEFAULT_OUTLIER_THRESHOLDS,
) -> OutlierResult:
    """Check if an encode is an outlier requiring review."""
    reasons: list[str] = []

    # Get file sizes
    source_size = source_path.stat().st_size
    encoded_size = encoded_path.stat().st_size
    compression_ratio = encoded_size / source_size

    # Check compression ratio
    if compression_ratio < thresholds.min_compression_ratio:
        reasons.append(f"Aggressive compression ({compression_ratio:.1%} of original)")
    elif compression_ratio > thresholds.max_compression_ratio:
        reasons.append(f"Weak compression ({compression_ratio:.1%} of original)")

    # Get encoded file info
    encoded_info = probe_media(encoded_path)
    output_bitrate = encoded_info.video_bitrate_kbps if encoded_info else None

    # Check bitrate for resolution
    if encoded_info and output_bitrate:
        height = encoded_info.video_height or 0
        if height >= 1080:
            if output_bitrate < thresholds.min_bitrate_1080p:
                reasons.append(f"Low bitrate for 1080p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_1080p:
                reasons.append(f"High bitrate for 1080p ({output_bitrate} kbps)")
        elif height >= 720:
            if output_bitrate < thresholds.min_bitrate_720p:
                reasons.append(f"Low bitrate for 720p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_720p:
                reasons.append(f"High bitrate for 720p ({output_bitrate} kbps)")
        elif height >= 480:
            if output_bitrate < thresholds.min_bitrate_480p:
                reasons.append(f"Low bitrate for 480p ({output_bitrate} kbps)")
            elif output_bitrate > thresholds.max_bitrate_480p:
                reasons.append(f"High bitrate for 480p ({output_bitrate} kbps)")

    # Check quality metrics
    if metrics:
        if metrics.vmaf is not None and metrics.vmaf < thresholds.min_vmaf:
            reasons.append(f"Low VMAF ({metrics.vmaf:.1f} < {thresholds.min_vmaf})")
        if metrics.ssim is not None and metrics.ssim < thresholds.min_ssim:
            reasons.append(f"Low SSIM ({metrics.ssim:.3f} < {thresholds.min_ssim})")
        if metrics.psnr is not None and metrics.psnr < thresholds.min_psnr:
            reasons.append(f"Low PSNR ({metrics.psnr:.1f} < {thresholds.min_psnr})")

    return OutlierResult(
        is_outlier=len(reasons) > 0,
        reasons=reasons,
        metrics=metrics,
        compression_ratio=compression_ratio,
        output_bitrate_kbps=output_bitrate,
    )
