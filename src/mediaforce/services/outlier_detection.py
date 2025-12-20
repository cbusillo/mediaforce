import pathlib
from typing import Optional

from mediaforce.domain.types import (
    DEFAULT_OUTLIER_THRESHOLDS,
    OutlierResult,
    OutlierThresholds,
    QualityMetrics,
)
from mediaforce.services.media_probe import probe_media


def check_for_outliers(
    source_path: pathlib.Path,
    encoded_path: pathlib.Path,
    metrics: Optional[QualityMetrics] = None,
    thresholds: OutlierThresholds = DEFAULT_OUTLIER_THRESHOLDS,
) -> OutlierResult:
    reasons: list[str] = []
    source_size = source_path.stat().st_size
    encoded_size = encoded_path.stat().st_size
    compression_ratio = encoded_size / source_size
    if compression_ratio < thresholds.min_compression_ratio:
        reasons.append(f"Aggressive compression ({compression_ratio:.1%} of original)")
    elif compression_ratio > thresholds.max_compression_ratio:
        reasons.append(f"Weak compression ({compression_ratio:.1%} of original)")

    encoded_info = probe_media(encoded_path)
    output_bitrate = encoded_info.video_bitrate_kbps if encoded_info else None
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
