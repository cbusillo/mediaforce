from __future__ import annotations

import pathlib

from mediaforce.domain.types import ClassificationResult, MediaInfo, SourceTier
from mediaforce.services.classification import TIER_SETTINGS, adjust_tier_with_vmaf, classify_source


def test_classify_source_manual_override_tier():
    info = MediaInfo(
        path=pathlib.Path("/tmp/video.mkv"),
        video_codec="h264",
        video_width=1920,
        video_height=1080,
        video_bitrate_kbps=5000,
        video_framerate=24.0,
    )

    result = classify_source(info, override_tier="poor")

    assert result.tier == SourceTier.POOR
    assert result.confidence == "high"
    assert result.recommended_settings == TIER_SETTINGS[SourceTier.POOR]
    assert any("Manual override" in reason for reason in result.reasons)


def test_classify_source_vmaf_hint_can_change_tier_more_aggressive():
    # Base score should land in MEDIOCRE; high hint nudges it to GOOD.
    info = MediaInfo(
        path=pathlib.Path("/tmp/video.mkv"),
        video_codec="h264",
        video_width=1920,
        video_height=1080,
        video_bitrate_kbps=8000,
        video_framerate=24.0,
    )

    base = classify_source(info)
    hinted = classify_source(info, vmaf_hint=95.0)

    assert base.tier == SourceTier.MEDIOCRE
    assert hinted.tier == SourceTier.GOOD


def test_classify_source_vmaf_hint_can_change_tier_less_aggressive():
    # Base score should land in GOOD; low hint nudges it to MEDIOCRE.
    info = MediaInfo(
        path=pathlib.Path("/tmp/video.mkv"),
        video_codec="h264",
        video_width=1920,
        video_height=1080,
        video_bitrate_kbps=5000,
        video_framerate=24.0,
    )

    base = classify_source(info)
    hinted = classify_source(info, vmaf_hint=84.0)

    assert base.tier == SourceTier.GOOD
    assert hinted.tier == SourceTier.MEDIOCRE


def test_tier_settings_mapping_is_stable():
    assert TIER_SETTINGS[SourceTier.PRISTINE].crf == 26
    assert TIER_SETTINGS[SourceTier.PRISTINE].preset == 5
    assert TIER_SETTINGS[SourceTier.PRISTINE].film_grain == 0
    assert TIER_SETTINGS[SourceTier.PRISTINE].denoise is None

    assert TIER_SETTINGS[SourceTier.GOOD].crf == 28
    assert TIER_SETTINGS[SourceTier.GOOD].preset == 5
    assert TIER_SETTINGS[SourceTier.GOOD].film_grain == 8
    assert TIER_SETTINGS[SourceTier.GOOD].denoise is None

    assert TIER_SETTINGS[SourceTier.MEDIOCRE].crf == 30
    assert TIER_SETTINGS[SourceTier.MEDIOCRE].preset == 6
    assert TIER_SETTINGS[SourceTier.MEDIOCRE].film_grain == 4
    assert TIER_SETTINGS[SourceTier.MEDIOCRE].denoise == "light"

    assert TIER_SETTINGS[SourceTier.POOR].crf == 32
    assert TIER_SETTINGS[SourceTier.POOR].preset == 6
    assert TIER_SETTINGS[SourceTier.POOR].film_grain == 0
    assert TIER_SETTINGS[SourceTier.POOR].denoise == "heavy"


def test_adjust_tier_with_vmaf_handles_high_and_low_paths():
    original = ClassificationResult(
        tier=SourceTier.GOOD,
        confidence="high",
        reasons=["baseline"],
        recommended_settings=TIER_SETTINGS[SourceTier.GOOD],
    )

    high = adjust_tier_with_vmaf(original, {"median": 95.0})
    assert high.tier == SourceTier.MEDIOCRE
    assert high.recommended_settings == TIER_SETTINGS[SourceTier.MEDIOCRE]

    low = adjust_tier_with_vmaf(original, {"median": 85.0, "min": 80.0})
    assert low.tier == SourceTier.PRISTINE
    assert low.recommended_settings == TIER_SETTINGS[SourceTier.PRISTINE]
    assert any("VMAF low" in reason for reason in low.reasons)

