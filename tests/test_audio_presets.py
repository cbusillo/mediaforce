import json
from pathlib import Path
from typing import Any

import pytest

from mediaforce.core.audio_presets import KEEP_SURROUND, SPEECH_STEREO, SPEECH_STEREO_BITRATE, audio_preset_values
from mediaforce.core.config import (
    FOLDER_POLICY_OVERRIDES_KEY,
    ConfigPaths,
    MediaforceConfig,
    set_runtime_folder_audio_preset,
)
from mediaforce.encoding.commands import build_ffmpeg_command
from mediaforce.encoding.quality import QualitySearchResult
from mediaforce.encoding.streams import (
    DOWNMIX_TO_STEREO_FILTER,
    TRUE_PEAK_LIMITER_FILTER,
    ProductionStreamPlan,
    opus_audio_filter,
    opus_encode_arguments,
    resolve_stream_plan,
)
from mediaforce.tuning.quality_risk import compile_allow_listed_transform_fragment
from mediaforce.web.runtime.audio_preset_runtime import show_audio_preset_payload
from mediaforce.web.settings_runtime import SettingsValidationError, normalize_submitted_library_policy

BASE_AUDIO: dict[str, Any] = {
    "keep_languages": ["eng"],
    "copy_codecs": ["aac", "opus"],
    "convert_to_opus_codecs": ["ac3", "eac3", "dts"],
    "stereo_opus_bitrate": "128k",
    "surround_5_1_opus_bitrate": "256k",
    "surround_7_1_opus_bitrate": "320k",
}
SHOW = "tv/Big Brother (US)"


def _config(tmp_path: Path, *, overrides: list[dict[str, Any]] | None = None, library_preset: str = "") -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "video": {},
            "audio": dict(BASE_AUDIO),
            "subtitle": {},
            "planning": {},
            "media": {
                "libraries": [
                    {
                        "key": "tv",
                        "path": str(tmp_path / "tv"),
                        "type": "tv",
                        "policy": {"audio_preset": library_preset},
                    }
                ],
            },
            "overrides": overrides or [],
        },
        paths=ConfigPaths(
            project_root=tmp_path,
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs",
            web_state_dir=tmp_path / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=tmp_path / "runtime.json",
            runtime_reservation_dir=tmp_path / "reservations",
        ),
    )


def _saved_overrides(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text())[FOLDER_POLICY_OVERRIDES_KEY]


def _item(policy: dict[str, Any], *, codec: str = "eac3", channels: int = 6) -> dict[str, Any]:
    return {
        "rel_path": f"{SHOW}/Season 10/Episode.mkv",
        "fingerprint": "fixture",
        "resolved_policy": policy,
        "audio_summary": [{"index": 1, "codec_name": codec, "channels": channels, "language": "eng", "default": 1}],
        "subtitle_summary": [],
    }


def test_speech_preset_downmixes_and_reencodes_aac() -> None:
    values, removed = audio_preset_values(SPEECH_STEREO, BASE_AUDIO)

    assert values["downmix_to_stereo"] is True
    assert values["stereo_opus_bitrate"] == SPEECH_STEREO_BITRATE
    assert "aac" not in values["copy_codecs"]
    assert "aac" in values["convert_to_opus_codecs"]
    assert removed == ()


def test_film_preset_restores_the_configured_audio() -> None:
    values, removed = audio_preset_values(KEEP_SURROUND, BASE_AUDIO)

    assert values["copy_codecs"] == BASE_AUDIO["copy_codecs"]
    assert values["convert_to_opus_codecs"] == BASE_AUDIO["convert_to_opus_codecs"]
    assert values["stereo_opus_bitrate"] == BASE_AUDIO["stereo_opus_bitrate"]
    assert removed == ("downmix_to_stereo",)


def test_unknown_preset_is_refused() -> None:
    with pytest.raises(ValueError):
        audio_preset_values("louder", BASE_AUDIO)


def test_show_choice_reaches_saved_seasons_and_leaves_other_shows_alone(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    season_snapshot = {"path_prefix": f"{SHOW}/Season 10", "audio": dict(BASE_AUDIO), "video": {"preset": 6}}
    other_show = {"path_prefix": "tv/Suits", "audio": dict(BASE_AUDIO)}
    runtime.write_text(json.dumps({FOLDER_POLICY_OVERRIDES_KEY: [season_snapshot, other_show]}))

    set_runtime_folder_audio_preset(runtime, SHOW, SPEECH_STEREO, BASE_AUDIO)
    config = _config(tmp_path, overrides=_saved_overrides(runtime))

    for prefix in (SHOW, f"{SHOW}/Season 10", f"{SHOW}/Season 11"):
        assert config.resolve_policy(prefix)["audio"]["downmix_to_stereo"] is True
    assert config.resolve_policy(f"{SHOW}/Season 10")["video"] == {"preset": 6}
    assert "downmix_to_stereo" not in config.resolve_policy("tv/Suits/Season 1")["audio"]


def test_switching_back_to_film_removes_the_downmix(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({FOLDER_POLICY_OVERRIDES_KEY: [{"path_prefix": f"{SHOW}/Season 10", "audio": dict(BASE_AUDIO)}]}))

    set_runtime_folder_audio_preset(runtime, SHOW, SPEECH_STEREO, BASE_AUDIO)
    set_runtime_folder_audio_preset(runtime, SHOW, KEEP_SURROUND, BASE_AUDIO)
    audio = _config(tmp_path, overrides=_saved_overrides(runtime)).resolve_policy(f"{SHOW}/Season 10")["audio"]

    assert "downmix_to_stereo" not in audio
    assert {key: audio[key] for key in BASE_AUDIO} == BASE_AUDIO
    assert audio["preset"] == KEEP_SURROUND


def test_library_default_applies_only_when_chosen(tmp_path: Path) -> None:
    without_default = _config(tmp_path).resolve_policy(f"{SHOW}/Season 10")
    with_default = _config(tmp_path, library_preset=SPEECH_STEREO).resolve_policy(f"{SHOW}/Season 10")

    assert without_default["audio"] == BASE_AUDIO
    assert with_default["audio"]["downmix_to_stereo"] is True


def test_show_choice_outranks_the_library_default(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({FOLDER_POLICY_OVERRIDES_KEY: []}))
    set_runtime_folder_audio_preset(runtime, SHOW, KEEP_SURROUND, BASE_AUDIO)
    config = _config(tmp_path, overrides=_saved_overrides(runtime), library_preset=SPEECH_STEREO)

    payload = show_audio_preset_payload(config, f"{SHOW}/Season 10")

    assert payload is not None
    assert payload["current"] == KEEP_SURROUND
    assert payload["source"] == "show"
    assert payload["library_default"] == SPEECH_STEREO
    assert "downmix_to_stereo" not in config.resolve_policy(f"{SHOW}/Season 10")["audio"]


def test_payload_reports_the_library_default_and_skips_other_libraries(tmp_path: Path) -> None:
    config = _config(tmp_path, library_preset=SPEECH_STEREO)

    payload = show_audio_preset_payload(config, SHOW)

    assert payload is not None
    assert (payload["show_prefix"], payload["current"], payload["source"]) == (SHOW, SPEECH_STEREO, "library")
    assert show_audio_preset_payload(config, "movies/Heat") is None


def test_downmix_plan_encodes_stereo_at_the_stereo_bitrate() -> None:
    policy = {"audio": {**BASE_AUDIO, **audio_preset_values(SPEECH_STEREO, BASE_AUDIO)[0]}, "subtitle": {}}

    plan = resolve_stream_plan(_item(policy))
    audio = plan.audio_streams[0]

    assert (audio.action, audio.channels, audio.output_channels) == ("transcode", 6, 2)
    assert audio.output_bitrate_text == SPEECH_STEREO_BITRATE
    assert ProductionStreamPlan.from_payload(plan.to_payload()).audio_streams[0].output_channels == 2


def test_speech_preset_reencodes_aac_sources() -> None:
    policy = {"audio": {**BASE_AUDIO, **audio_preset_values(SPEECH_STEREO, BASE_AUDIO)[0]}, "subtitle": {}}

    audio = resolve_stream_plan(_item(policy, codec="aac", channels=2)).audio_streams[0]

    assert (audio.action, audio.output_codec, audio.output_bitrate_text) == ("transcode", "opus", SPEECH_STEREO_BITRATE)


def test_plans_without_a_downmix_keep_their_identity() -> None:
    plan = resolve_stream_plan(_item({"audio": dict(BASE_AUDIO), "subtitle": {}}))

    assert "output_channels" not in plan.to_payload()["streams"][0]
    assert ProductionStreamPlan.from_payload(plan.to_payload()).plan_id == plan.plan_id


def test_opus_arguments_downmix_without_a_surround_mapping() -> None:
    downmix = opus_encode_arguments(channels=6, bitrate_text="96k", output_channels=2, stream_index=0)
    surround = opus_encode_arguments(channels=6, bitrate_text="256k", stream_index=0)

    assert downmix == [
        "-c:a:0", "libopus", "-af:a:0", opus_audio_filter(DOWNMIX_TO_STEREO_FILTER), "-b:a:0", "96k",
    ]
    assert surround[surround.index("-mapping_family:a:0") + 1] == "1"
    assert surround[surround.index("-af:a:0") + 1].endswith(TRUE_PEAK_LIMITER_FILTER)


def test_production_command_downmixes_the_planned_stream() -> None:
    policy = {"video": {}, "audio": {**BASE_AUDIO, **audio_preset_values(SPEECH_STEREO, BASE_AUDIO)[0]}, "subtitle": {}}
    plan = resolve_stream_plan(_item(policy))

    command = build_ffmpeg_command(
        source_path=Path("/library/episode.mkv"),
        staging_path=Path("/staging/episode.mkv"),
        source_codec="h264",
        video_policy={"encoder": "libsvtav1", "pixel_format": "yuv420p10le", "preset": 6},
        preset=6,
        audio_policy=policy["audio"],
        subtitle_policy={},
        selection=plan,
        quality=QualitySearchResult(30.0, "VMAF", 90.0, 90.0, "ok"),
        text_subtitle_codecs=set(),
        ffmpeg_binary=lambda: "ffmpeg",
        ffmpeg_hwaccel_input_args=lambda *_args, **_kwargs: [],
        audio_codec=lambda *_args: "copy",
        opus_bitrate=lambda *_args: "128k",
        format_crf=lambda value: f"{value:g}",
    )

    assert command[command.index("-af:a:0") + 1] == opus_audio_filter(DOWNMIX_TO_STEREO_FILTER)
    assert command[command.index("-b:a:0") + 1] == SPEECH_STEREO_BITRATE
    assert "-mapping_family:a:0" not in command


def test_proposals_cannot_change_the_owner_sound_choice() -> None:
    owner_policy = {"audio": {**BASE_AUDIO, **audio_preset_values(SPEECH_STEREO, BASE_AUDIO)[0]}}

    _, _, flipped = compile_allow_listed_transform_fragment(owner_policy, {"audio": {"downmix_to_stereo": False}})
    _, _, introduced = compile_allow_listed_transform_fragment({"audio": dict(BASE_AUDIO)}, {"audio": {"preset": SPEECH_STEREO}})
    _, _, unchanged = compile_allow_listed_transform_fragment(owner_policy, {"audio": {"downmix_to_stereo": True}})

    assert flipped == ["audio.downmix_to_stereo"]
    assert introduced == ["audio.preset"]
    assert unchanged == []


def test_library_setting_accepts_only_known_sound_choices() -> None:
    assert normalize_submitted_library_policy("tv", {"audio_preset": SPEECH_STEREO})["audio_preset"] == SPEECH_STEREO
    assert normalize_submitted_library_policy("tv", {})["audio_preset"] == ""
    with pytest.raises(SettingsValidationError):
        normalize_submitted_library_policy("tv", {"audio_preset": "louder"})

