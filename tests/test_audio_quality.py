import subprocess
from pathlib import Path
from typing import Any

import pytest

from mediaforce.core.process_control import ProcessCancelledError
from mediaforce.encoding import audio_quality
from mediaforce.encoding.audio_quality import (
    MAX_DIALOGUE_DROP_DB,
    MAX_LOUDNESS_CHANGE_LU,
    NEW_PEAK_ALLOWANCE_DB,
    AudioLevels,
    audio_quality_checks,
    check_sample_audio,
    clip_starts,
    parse_visqol_score,
    sample_audio_review_reason,
)
from mediaforce.encoding.streams import TRUE_PEAK_LIMITER_FILTER, opus_encode_arguments

ORIGINAL = AudioLevels(
    integrated_lufs=-23.0,
    loudness_range_lu=8.0,
    true_peak_dbtp=-3.0,
    dialogue_lufs=-24.0,
    flat_factor=0.0,
)
TRANSCODE_PLAN = {
    "source_stream_index": 1,
    "source_channels": 6,
    "production_action": "transcode",
    "production_bitrate_text": "256k",
}


def _levels(**changes: float) -> AudioLevels:
    return AudioLevels(**{**ORIGINAL.to_payload(), **changes})


def _failed_keys(candidate: AudioLevels, *, sound_score: float = 4.5, reference: AudioLevels = ORIGINAL) -> list[str]:
    checks = audio_quality_checks(sound_score=sound_score, floor=4.3, reference=reference, candidate=candidate)
    return [check["key"] for check in checks if not check["passed"]]


def _ebur128(integrated: float, loudness_range: float = 8.0, peak: float = -3.0) -> str:
    return (
        "[Parsed_ebur128_0 @ 0x1] Summary:\n\n"
        f"  Integrated loudness:\n    I:         {integrated:.1f} LUFS\n    Threshold: -33.0 LUFS\n\n"
        f"  Loudness range:\n    LRA:        {loudness_range:.1f} LU\n\n"
        f"  True peak:\n    Peak:       {peak:.1f} dBFS\n"
    )


class FakeTools:
    """Answers the sound check's ffmpeg and Docker commands like the real tools."""

    def __init__(self, *, visqol_scores: list[float], visqol_failure: str | None = None) -> None:
        self.visqol_scores = list(visqol_scores)
        self.visqol_failure = visqol_failure
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[1] == "run":
            if self.visqol_failure is not None:
                return subprocess.CompletedProcess(command, 125, "", self.visqol_failure)
            return subprocess.CompletedProcess(command, 0, f"MOS-LQO:\t\t{self.visqol_scores.pop(0)}\n", "")
        if command[1] == "rm":
            return subprocess.CompletedProcess(command, 0, "", "")
        audio_filter = command[command.index("-af") + 1] if "-af" in command and "-f" in command else ""
        is_new = Path(command[command.index("-i") + 1]).name == "new.mka"
        if audio_filter.startswith("highpass"):
            return subprocess.CompletedProcess(command, 0, "", _ebur128(-26.0 if is_new else -24.0))
        if audio_filter.startswith("ebur128"):
            return subprocess.CompletedProcess(command, 0, "", _ebur128(-23.0))
        if audio_filter.startswith("astats"):
            return subprocess.CompletedProcess(command, 0, "", "[Parsed_astats_0 @ 0x1] Flat factor: 0.000000\n")
        return subprocess.CompletedProcess(command, 0, "", "")


def test_checks_pass_when_the_new_sound_matches_the_original() -> None:
    assert _failed_keys(_levels(true_peak_dbtp=-2.8)) == []


def test_sound_score_below_the_floor_fails() -> None:
    assert _failed_keys(ORIGINAL, sound_score=4.29) == ["sound_score"]


def test_dialogue_falling_against_the_rest_of_the_sound_fails() -> None:
    within_limit = _levels(dialogue_lufs=ORIGINAL.dialogue_lufs - MAX_DIALOGUE_DROP_DB + 0.1)
    buried = _levels(dialogue_lufs=ORIGINAL.dialogue_lufs - MAX_DIALOGUE_DROP_DB - 0.1)

    assert _failed_keys(within_limit) == []
    assert _failed_keys(buried) == ["dialogue"]


def test_a_quieter_mix_keeps_dialogue_when_both_drop_together() -> None:
    quieter = _levels(integrated_lufs=-24.0, dialogue_lufs=-25.0)

    assert _failed_keys(quieter) == []


def test_large_volume_change_fails() -> None:
    louder = _levels(
        integrated_lufs=ORIGINAL.integrated_lufs + MAX_LOUDNESS_CHANGE_LU + 0.5,
        dialogue_lufs=ORIGINAL.dialogue_lufs + MAX_LOUDNESS_CHANGE_LU + 0.5,
    )

    assert _failed_keys(louder) == ["loudness"]


def test_only_new_peaks_above_full_scale_fail() -> None:
    hot_original = _levels(true_peak_dbtp=2.0)

    assert _failed_keys(_levels(true_peak_dbtp=0.6)) == ["true_peak"]
    assert _failed_keys(_levels(true_peak_dbtp=1.5), reference=hot_original) == []
    assert _failed_keys(_levels(true_peak_dbtp=2.0 + NEW_PEAK_ALLOWANCE_DB + 0.1), reference=hot_original) == [
        "true_peak"
    ]


def test_added_clipping_fails() -> None:
    assert _failed_keys(_levels(flat_factor=3.0)) == ["clipping"]


def test_clips_spread_through_the_episode_and_stay_inside_it() -> None:
    assert clip_starts(1000.0) == [200.0, 500.0, 800.0]
    assert clip_starts(40.0) == [8.0, 10.0, 10.0]


def test_parse_visqol_score_reads_mos_lqo() -> None:
    output = "ViSQOL conformance version: 333\nAudio mode, support vector regression model\nMOS-LQO:\t\t4.5635\n"

    assert parse_visqol_score(output) == 4.5635
    assert parse_visqol_score("no score") is None


def test_opus_arguments_limit_peaks_and_keep_surround_layout() -> None:
    stereo = opus_encode_arguments(channels=2, bitrate_text="96k")
    surround = opus_encode_arguments(channels=6, bitrate_text="256k")

    assert stereo[stereo.index("-af") + 1] == TRUE_PEAK_LIMITER_FILTER
    assert "-mapping_family" not in stereo
    assert surround[surround.index("-af") + 1].endswith(TRUE_PEAK_LIMITER_FILTER)
    assert surround[surround.index("-mapping_family") + 1] == "1"


def test_sample_check_scores_new_audio_against_the_original(tmp_path: Path) -> None:
    tools = FakeTools(visqol_scores=[4.6, 4.4, 4.5])

    result = check_sample_audio(
        source_path=Path("/library/episode.mkv"),
        audio_plan=TRANSCODE_PLAN,
        duration_seconds=2600.0,
        audio_policy={"min_sound_score": 4.3},
        run=tools,
        work_root=tmp_path,
    )

    assert result["status"] == "failed"
    assert result["sound_score"] == pytest.approx(4.5)
    assert [clip["start_seconds"] for clip in result["clip_scores"]] == clip_starts(2600.0)
    assert [check["key"] for check in result["checks"] if not check["passed"]] == ["dialogue"]
    assert result["original"]["dialogue_lufs"] == -24.0
    assert result["new"]["dialogue_lufs"] == -26.0
    copy_command, encode_command = tools.commands[0], tools.commands[1]
    assert copy_command[copy_command.index("-map") + 1] == "0:1"
    assert copy_command[copy_command.index("-c:a") + 1] == "copy"
    assert encode_command[encode_command.index("-c:a"):-1] == opus_encode_arguments(channels=6, bitrate_text="256k")
    assert list(tmp_path.iterdir()) == []


def test_sample_check_cuts_clips_by_decoding_instead_of_seeking_the_container(tmp_path: Path) -> None:
    tools = FakeTools(visqol_scores=[4.6, 4.6, 4.6])

    check_sample_audio(
        source_path=Path("/library/episode.mkv"),
        audio_plan=TRANSCODE_PLAN,
        duration_seconds=2600.0,
        audio_policy={},
        run=tools,
        work_root=tmp_path,
    )

    clip_commands = [command for command in tools.commands if "pcm_s16le" in command]
    assert len(clip_commands) == 2
    for command in clip_commands:
        assert command.index("-i") < command.index("-ss")


def test_kept_audio_needs_no_check() -> None:
    tools = FakeTools(visqol_scores=[])

    result = check_sample_audio(
        source_path=Path("/library/episode.mkv"),
        audio_plan={**TRANSCODE_PLAN, "production_action": "copy"},
        duration_seconds=2600.0,
        audio_policy={},
        run=tools,
    )

    assert result["status"] == "unchanged"
    assert tools.commands == []


def test_stopped_docker_leaves_the_sound_unmeasured_with_a_plain_reason(tmp_path: Path) -> None:
    tools = FakeTools(
        visqol_scores=[],
        visqol_failure="docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
    )

    result = check_sample_audio(
        source_path=Path("/library/episode.mkv"),
        audio_plan=TRANSCODE_PLAN,
        duration_seconds=2600.0,
        audio_policy={},
        run=tools,
        work_root=tmp_path,
    )

    assert result["status"] == "unmeasured"
    assert result["reason"] == "Docker isn't running, so the sound score could not be measured."
    assert "sound_score" not in result
    assert any(command[1] == "rm" for command in tools.commands)


def test_cancelling_during_scoring_removes_the_container(tmp_path: Path) -> None:
    tools = FakeTools(visqol_scores=[])
    removed: list[str] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == "run":
            raise ProcessCancelledError("Operation was cancelled.")
        if command[1] == "rm":
            removed.append(command[-1])
        return tools(command, **kwargs)

    with pytest.raises(ProcessCancelledError):
        check_sample_audio(
            source_path=Path("/library/episode.mkv"),
            audio_plan=TRANSCODE_PLAN,
            duration_seconds=2600.0,
            audio_policy={},
            run=run,
            work_root=tmp_path,
        )

    assert len(removed) == 1
    assert removed[0].startswith("mediaforce-visqol-")


def test_review_reason_names_each_failed_check() -> None:
    failed = {
        "status": "failed",
        "checks": [
            {"key": "sound_score", "label": "Sound score", "passed": False, "detail": "4.10 of 4.73; needs 4.30"},
            {"key": "dialogue", "label": "Dialogue", "passed": True, "detail": "+0.0 dB"},
        ],
    }

    assert sample_audio_review_reason(failed) == "The sound check failed (Sound score: 4.10 of 4.73; needs 4.30)."
    assert sample_audio_review_reason({"status": "unmeasured", "reason": "Docker isn't running."}) == (
        "The sound check could not run. Docker isn't running."
    )
    assert sample_audio_review_reason({"status": "passed"}) is None
    assert sample_audio_review_reason({}) is None


def test_visqol_image_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEDIAFORCE_VISQOL_IMAGE", "local/visqol:test")

    command = audio_quality.visqol_command(Path("/clips"), container_name="mediaforce-visqol-test")

    assert "local/visqol:test" in command
    assert "--network=none" in command
