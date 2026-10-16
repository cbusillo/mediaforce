"""The owner's sound choice and lifecycle mode stay owner settings after a sample is approved."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mediaforce.core.audio_presets import KEEP_SURROUND, PRESET_AUDIO_KEYS, SPEECH_STEREO
from mediaforce.core.config import (
    FOLDER_POLICY_OVERRIDES_KEY,
    ConfigPaths,
    MediaforceConfig,
    set_runtime_folder_audio_preset,
    update_runtime_folder_policy_values,
)
from mediaforce.core.db import open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.web import app as web_app
from mediaforce.web.runtime.audio_preset_runtime import show_audio_preset_payload
from mediaforce.web.runtime.decision_evidence import CadenceQueuePartition
from mediaforce.web.runtime.folder_actions import queue_folder_encode_action
from mediaforce.web.runtime.folder_tuning_advice import calibration_policy_hash
from mediaforce.web.runtime.production_approval import (
    effective_production_approval,
    production_approval_contract,
    policy_with_owner_settings,
    production_approval_matches,
)

SHOW = "tv/Big Brother (US)"
SEASON = f"{SHOW}/Season 19"
EPISODE = f"{SEASON}/Big.Brother.S19E01.mkv"
BASE_AUDIO: dict[str, Any] = {
    "copy_codecs": ["aac", "opus"],
    "convert_to_opus_codecs": ["ac3", "eac3", "dts"],
    "stereo_opus_bitrate": "128k",
}
APPROVED_VIDEO: dict[str, Any] = {
    "target_vmaf": 93.0,
    "compression_intent_schema_version": 1,
    "compression_intent": "balanced",
    "compression_intent_source": "test",
    "compression_intent_confirmed": True,
}


def _config(tmp_path: Path, *, library_sound: str = "") -> MediaforceConfig:
    """The live config, with the runtime overrides saved so far, as the service loads it."""
    runtime_path = tmp_path / "runtime.json"
    overrides = json.loads(runtime_path.read_text())[FOLDER_POLICY_OVERRIDES_KEY] if runtime_path.exists() else []
    return MediaforceConfig(
        raw={
            "video": {},
            "audio": dict(BASE_AUDIO),
            "subtitle": {},
            "planning": {"series_lifecycle_mode": "auto"},
            "media": {
                "libraries": [
                    {
                        "key": "tv", "path": str(tmp_path / "tv"), "type": "tv", "availability": "production",
                        "policy": {"audio_preset": library_sound},
                    },
                ],
            },
            "overrides": overrides,
        },
        paths=ConfigPaths(
            project_root=tmp_path,
            config_path=tmp_path / "config.toml",
            db_path=tmp_path / "library.sqlite3",
            run_manifest_dir=tmp_path / "runs",
            web_state_dir=tmp_path / "web",
            review_dir=tmp_path / "review",
            runtime_settings_path=runtime_path,
            runtime_reservation_dir=tmp_path / "reservations",
        ),
    )


def _saved_override(config: MediaforceConfig, prefix: str) -> dict[str, Any]:
    overrides = json.loads(config.paths.runtime_settings_path.read_text())[FOLDER_POLICY_OVERRIDES_KEY]
    return next(override for override in overrides if override["path_prefix"] == prefix)


def _approve_sample(config: MediaforceConfig, prefix: str, *, before_the_fix: bool = True) -> dict[str, Any]:
    """A sample approved at this folder and saved as its override.

    Before the fix, approving saved the sample's whole policy; it now saves only what the sample owns.
    """
    policy = config.resolve_policy(prefix)
    policy["video"].update(APPROVED_VIDEO)
    calibration: dict[str, Any] = {
        "mode": "sample",
        "job_id": "sample-job",
        "accepted_sample_job_id": "sample-job",
        "accepted_at": "2026-10-15T00:00:00+00:00",
        "policy": policy,
        "sample_item": {
            "resolved_policy": policy,
            "resolved_operator_intent": {"request": {"compression_intent": "balanced"}},
        },
    }
    calibration["accepted_policy_hash"] = calibration_policy_hash(calibration)
    saved = policy if before_the_fix else policy_with_owner_settings(policy, config, prefix)
    web_app._upsert_override(config.paths.runtime_settings_path, prefix, saved)
    return calibration


def _insert_episode(config: MediaforceConfig) -> int:
    timestamp = datetime.now(tz=UTC).isoformat(timespec="seconds")
    with open_db(config.paths.db_path) as connection:
        result = connection.execute(library_items.insert().values(
            source_path=f"/media/{EPISODE}", rel_path=EPISODE, media_root="tv", parent_dir=SEASON,
            file_name=Path(EPISODE).name, container="mkv", size_bytes=1_000_000_000, mtime_ns=1,
            fingerprint=EPISODE, duration_seconds=2_700.0, video_codec="h264", width=1920, height=1080,
            audio_summary_json="[]", subtitle_summary_json="[]", status="discovered", priority_score=50,
            recommendation="priority_encode", recommendation_reason="fixture", last_scan_id="scan",
            discovered_at=timestamp, last_seen_at=timestamp, updated_at=timestamp,
        ))
    return int(result.inserted_primary_key[0])


def _cadence_clears_every_item(*_args: object, library_item_ids: list[int], **_kwargs: object) -> CadenceQueuePartition:
    return CadenceQueuePartition(
        cleared_item_ids=frozenset(library_item_ids),
        blocked_item_ids=frozenset(),
        analysis_queued_item_ids=frozenset(),
        analysis_failed_item_ids=frozenset(),
        analysis_unavailable_item_ids=frozenset(),
        analysis_unavailable_reason=None,
        evidence_work={},
    )


def _queue(
        config: MediaforceConfig, prefix: str, calibration: dict[str, Any], *, library_sound: str = "",
) -> dict[str, Any]:
    """Queue the folder; returns the manifest made from the policy production would encode with."""
    item_id = _insert_episode(config)
    manifests: list[dict[str, Any]] = []

    def create_manifest(_connection: object, manifest_config: MediaforceConfig, **_kwargs: object) -> tuple[
        dict[str, Any], Path]:
        manifest = {"items": [{
            "library_item_id": item_id,
            "rel_path": EPISODE,
            "resolved_policy": manifest_config.resolve_policy(EPISODE),
        }]}
        manifests.append(manifest)
        return manifest, config.paths.run_manifest_dir / "run.json"

    with patch("mediaforce.web.runtime.folder_actions.create_folder_manifest", side_effect=create_manifest), patch(
        "mediaforce.web.runtime.folder_actions.cadence_queue_partition", side_effect=_cadence_clears_every_item,
    ), patch("mediaforce.web.runtime.folder_actions.load_config", side_effect=lambda _path: _config(config.paths.project_root, library_sound=library_sound)):
        result = queue_folder_encode_action(
            config,
            prefix,
            "",
            False,
            now_iso=lambda: "2026-10-16T00:00:00+00:00",
            load_job_state=lambda *_args: None,
            load_calibration_state=lambda _config, requested: calibration if requested == prefix else None,
            review_gate=lambda _calibration: {"can_confirm_full": True, "message": "Ready"},
            upsert_override=web_app._upsert_override,
            load_active_encode_job_for_prefix_fn=lambda *_args: None,
            clear_terminal_encode_jobs_for_prefix_fn=lambda *_args: None,
            prepare_terminal_encode_job_for_requeue_fn=lambda *_args: None,
            save_encode_job=lambda *_args: None,
            reserve_preflight=lambda *_args: ReservePreflight(True, None, {}),
        )
    reset_engine_cache()
    assert result["ok"], result
    return manifests[0]


def test_a_sound_chosen_after_approval_is_the_sound_production_uses(tmp_path: Path) -> None:
    calibration = _approve_sample(_config(tmp_path), SEASON)
    set_runtime_folder_audio_preset(tmp_path / "runtime.json", SHOW, SPEECH_STEREO, BASE_AUDIO)
    live = _config(tmp_path)
    live_audio = live.resolve_policy(EPISODE)["audio"]
    assert live_audio != calibration["policy"]["audio"]

    manifest = _queue(live, SEASON, calibration)

    produced_policy = manifest["items"][0]["resolved_policy"]
    assert produced_policy["audio"] == live_audio
    assert produced_policy["video"] == calibration["policy"]["video"]
    # Queueing does not bring the sample's older sound back for the next run either.
    assert _config(tmp_path).resolve_policy(EPISODE)["audio"] == live_audio


def test_files_made_with_the_new_sound_still_publish_under_the_approval(tmp_path: Path) -> None:
    calibration = _approve_sample(_config(tmp_path), SEASON)
    set_runtime_folder_audio_preset(tmp_path / "runtime.json", SHOW, SPEECH_STEREO, BASE_AUDIO)
    live = _config(tmp_path)

    manifest = {**_queue(live, SEASON, calibration), "selection": {
        "production_approval_contract": production_approval_contract(calibration),
    }}

    approval = effective_production_approval(
        _config(tmp_path), EPISODE, lambda _config, prefix: calibration if prefix == SEASON else None,
    )
    assert production_approval_matches(approval, manifest, manifest["items"][0])


def test_the_lifecycle_mode_survives_queueing_a_show_approval(tmp_path: Path) -> None:
    calibration = _approve_sample(_config(tmp_path), SHOW)
    update_runtime_folder_policy_values(
        tmp_path / "runtime.json", SHOW, section="planning", values={"series_lifecycle_mode": "off"},
    )
    live = _config(tmp_path)
    planning_before = _saved_override(live, SHOW)["planning"]
    live_planning = live.resolve_policy(SHOW)["planning"]
    assert live_planning != calibration["policy"]["planning"]

    manifest = _queue(live, SHOW, calibration)

    assert manifest["items"][0]["resolved_policy"]["planning"] == live.resolve_policy(EPISODE)["planning"]
    assert _saved_override(live, SHOW)["planning"] == planning_before
    assert _config(tmp_path).resolve_policy(SHOW)["planning"] == live_planning


def test_the_folder_page_says_when_the_sound_changed_since_the_sample(tmp_path: Path) -> None:
    calibration = _approve_sample(_config(tmp_path), SEASON)

    unchanged = show_audio_preset_payload(_config(tmp_path), SEASON, sample_policy=calibration["policy"])
    set_runtime_folder_audio_preset(tmp_path / "runtime.json", SHOW, SPEECH_STEREO, BASE_AUDIO)
    changed = show_audio_preset_payload(_config(tmp_path), SEASON, sample_policy=calibration["policy"])
    no_sample = show_audio_preset_payload(_config(tmp_path), SEASON)

    assert unchanged is not None and not unchanged["sound_changed_since_sample"]
    assert changed is not None and changed["sound_changed_since_sample"]
    assert no_sample is not None and not no_sample["sound_changed_since_sample"]


def test_a_season_keeps_inheriting_the_library_sound_after_approval_and_queueing(tmp_path: Path) -> None:
    calibration = _approve_sample(_config(tmp_path, library_sound=SPEECH_STEREO), SEASON, before_the_fix=False)
    live = _config(tmp_path, library_sound=SPEECH_STEREO)

    manifest = _queue(live, SEASON, calibration, library_sound=SPEECH_STEREO)

    assert manifest["items"][0]["resolved_policy"]["audio"] == live.resolve_policy(EPISODE)["audio"]
    saved_audio = _saved_override(live, SEASON).get("audio", {})
    assert not set(PRESET_AUDIO_KEYS) & set(saved_audio)
    changed = _config(tmp_path, library_sound=KEEP_SURROUND)
    for key in PRESET_AUDIO_KEYS:
        assert changed.resolve_policy(EPISODE)["audio"].get(key) == changed.resolve_policy(SHOW)["audio"].get(key)
