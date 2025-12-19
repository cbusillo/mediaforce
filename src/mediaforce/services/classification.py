from typing import Any, Optional

from mediaforce.domain.types import ClassificationResult, MediaInfo, SourceTier, TierSettings


TIER_SETTINGS: dict[SourceTier, TierSettings] = {
    SourceTier.PRISTINE: TierSettings(crf=26, preset=5, film_grain=0, denoise=None),
    SourceTier.GOOD: TierSettings(crf=28, preset=5, film_grain=8, denoise=None),
    SourceTier.MEDIOCRE: TierSettings(crf=30, preset=6, film_grain=4, denoise="light"),
    SourceTier.POOR: TierSettings(crf=32, preset=6, film_grain=0, denoise="heavy"),
}


def classify_source(
    info: MediaInfo,
    override_tier: Optional[str] = None,
    vmaf_hint: Optional[float] = None,
) -> ClassificationResult:
    """Classify source quality and recommend encoding settings.

    The classification is based on heuristics about bitrate efficiency, codec
    age, and resolution vs. likely content era.
    """

    reasons: list[str] = []

    if override_tier:
        tier_str = override_tier.lower()
        for tier in SourceTier:
            if tier.value == tier_str:
                reasons.append(f"Manual override from DB: {tier_str}")
                return ClassificationResult(
                    tier=tier,
                    confidence="high",
                    reasons=reasons,
                    recommended_settings=TIER_SETTINGS[tier],
                )

    score = 0

    codec = (info.video_codec or "").lower()
    if codec in ("mpeg2video", "mpeg2"):
        score += 3
        reasons.append("MPEG-2 codec suggests older/legacy source")
    elif codec in ("mpeg4", "msmpeg4", "divx", "xvid"):
        score += 3
        reasons.append("Legacy MPEG-4/DivX codec")
    elif codec == "vc1":
        score += 1
        reasons.append("VC-1 codec (older HD era)")
    elif codec in ("h264", "avc"):
        pass
    elif codec in ("hevc", "h265"):
        score -= 1
        reasons.append("HEVC suggests modern encode")
    elif "av1" in codec:
        score -= 2
        reasons.append("Already AV1 - likely high quality source")

    if info.video_bitrate_kbps and info.video_width and info.video_height:
        pixels = info.video_width * info.video_height
        fps = info.video_framerate or 24
        bpp = (info.video_bitrate_kbps * 1000) / (pixels * fps)

        if bpp > 0.15:
            score += 2
            reasons.append(f"High bpp ({bpp:.3f}) suggests noisy/inefficient source")
        elif bpp > 0.10:
            score += 1
            reasons.append(f"Elevated bpp ({bpp:.3f})")
        elif bpp < 0.02:
            score -= 1
            reasons.append(f"Low bpp ({bpp:.3f}) - already well compressed")

    if info.video_height and info.video_bitrate_kbps:
        if info.video_height >= 1080:
            if info.video_bitrate_kbps > 15000:
                score += 2
                reasons.append(
                    f"Very high bitrate ({info.video_bitrate_kbps}kbps) for {info.resolution_label}"
                )
            elif info.video_bitrate_kbps > 10000:
                score += 1
                reasons.append(
                    f"High bitrate ({info.video_bitrate_kbps}kbps) for {info.resolution_label}"
                )
            elif info.video_bitrate_kbps < 3000:
                score -= 1
                reasons.append(f"Efficient bitrate ({info.video_bitrate_kbps}kbps)")
        elif info.video_height >= 720:
            if info.video_bitrate_kbps > 8000:
                score += 2
                reasons.append(f"Very high bitrate for 720p ({info.video_bitrate_kbps}kbps)")
        elif info.video_height < 720:
            score += 2
            reasons.append(f"Sub-HD resolution ({info.resolution_label}) - possibly upscaled")

    if vmaf_hint is not None:
        if vmaf_hint >= 95:
            score -= 1
            reasons.append(f"VMAF hint high ({vmaf_hint:.1f}) -> more aggressive")
        elif vmaf_hint < 85:
            score += 1
            reasons.append(f"VMAF hint low ({vmaf_hint:.1f}) -> less aggressive")

    if score <= -1:
        tier = SourceTier.PRISTINE
    elif score <= 1:
        tier = SourceTier.GOOD
    elif score <= 3:
        tier = SourceTier.MEDIOCRE
    else:
        tier = SourceTier.POOR

    if len(reasons) >= 3:
        confidence = "high"
    elif len(reasons) >= 2:
        confidence = "medium"
    else:
        confidence = "low"
        reasons.append("Limited metadata available for classification")

    if not reasons:
        reasons.append("Default classification - no strong indicators")

    return ClassificationResult(
        tier=tier,
        confidence=confidence,
        reasons=reasons,
        recommended_settings=TIER_SETTINGS[tier],
    )


def adjust_tier_with_vmaf(
    classification: ClassificationResult,
    vmaf_stats: dict[str, Any],
) -> ClassificationResult:
    """Adjust tier based on VMAF statistics (median/min)."""

    median = vmaf_stats.get("median")
    vmin = vmaf_stats.get("min")
    tier = classification.tier

    def more_aggressive(t: SourceTier) -> SourceTier:
        order = [SourceTier.POOR, SourceTier.MEDIOCRE, SourceTier.GOOD, SourceTier.PRISTINE]
        idx = order.index(t)
        return order[max(0, idx - 1)]

    def less_aggressive(t: SourceTier) -> SourceTier:
        order = [SourceTier.POOR, SourceTier.MEDIOCRE, SourceTier.GOOD, SourceTier.PRISTINE]
        idx = order.index(t)
        return order[min(len(order) - 1, idx + 1)]

    adjusted = tier
    reasons = list(classification.reasons)

    if median is not None and median >= 94:
        adjusted = more_aggressive(adjusted)
        reasons.append(f"VMAF median {median:.1f} -> more aggressive")
    if (median is not None and median < 86) or (vmin is not None and vmin < 82):
        adjusted = less_aggressive(adjusted)
        median_label = f"{median:.1f}" if isinstance(median, (int, float)) else "n/a"
        vmin_label = f"{vmin:.1f}" if isinstance(vmin, (int, float)) else "n/a"
        reasons.append(f"VMAF low (median {median_label}, min {vmin_label}) -> less aggressive")

    return ClassificationResult(
        tier=adjusted,
        confidence=classification.confidence,
        reasons=reasons,
        recommended_settings=TIER_SETTINGS[adjusted],
    )

