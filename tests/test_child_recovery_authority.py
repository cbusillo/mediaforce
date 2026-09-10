import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from mediaforce.core.config import ConfigPaths, MediaforceConfig
from mediaforce.core.db import DBClient, open_db, reset_engine_cache
from mediaforce.core.db_tables import library_items
from mediaforce.encoding.cadence import analyze_cadence
from mediaforce.web.runtime import folder_actions


def test_recovery_requires_current_approval_and_matching_manifest() -> None:
    config = Mock()
    calibration = {"accepted_sample_job_id": "sample", "accepted_policy_hash": "policy", "sample_item": {
        "resolved_operator_intent": {"request": {"compression_intent": "balanced"}},
    }}
    contract = folder_actions._production_approval_contract(calibration)
    kwargs = dict(load_calibration_state=lambda *_: calibration,
                  review_gate=lambda _: {"can_confirm_full": True}, load_advice_state=lambda *_: {})
    with patch.object(folder_actions, "production_action_blocker", return_value=None), \
            patch.object(folder_actions, "build_quality_risk_contract", return_value={}), \
            patch.object(folder_actions, "_quality_risk_blocking_reason", return_value=None):
        assert folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
            "selection": {"production_approval_contract": contract},
        }, **kwargs) == contract
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
                "selection": {"production_approval_contract": {**contract, "sample_job_id": "old"}},
            }, **kwargs)
        kwargs["review_gate"] = lambda _: {"can_confirm_full": False}
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_approval(config, {"prefix": "tv/Show"}, {
                "selection": {"production_approval_contract": contract},
            }, **kwargs)


@pytest.mark.parametrize("eligible,cleared", [(False, {1}), (True, set())])
def test_recovery_rechecks_policy_and_cadence_without_writes(eligible: bool, cleared: set[int]) -> None:
    decision = SimpleNamespace(item_id=1, eligible=eligible, override_applied=False, hold_reasons=[])
    with patch.object(folder_actions, "project_candidates", return_value=[decision]), \
            patch.object(folder_actions, "cadence_safety_partition", return_value=SimpleNamespace(cleared_item_ids=cleared)) as cadence:
        with pytest.raises(HTTPException):
            folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [{"library_item_id": 1}])
        if eligible:
            assert cadence.call_args.kwargs["synchronize"] is False


def test_recovery_only_reuses_original_in_scope_override() -> None:
    decision = SimpleNamespace(item_id=1, eligible=True, override_applied=True, hold_reasons=[], is_current_season=False)
    item = {"library_item_id": 1, "selection_provenance": {
        "manual_override": True, "override_applied": True, "season_prefix": "tv/Show/Season 1", "is_current_season": False,
    }}
    with patch.object(folder_actions, "project_candidates", return_value=[decision]) as candidates, \
            patch.object(folder_actions, "cadence_safety_partition", return_value=SimpleNamespace(cleared_item_ids={1})):
        evidence = folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert evidence["cadence_cleared"] == [1]
        assert candidates.call_args.kwargs["manual_override_prefixes"] == {"tv/Show/Season 1"}
        item["selection_provenance"]["season_prefix"] = "tv/Other/Season 1"
        folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert candidates.call_args.kwargs["manual_override_prefixes"] == set()


def test_recovery_refuses_new_lifecycle_holds_despite_original_override() -> None:
    decision = SimpleNamespace(item_id=1, eligible=True, override_applied=True,
                               hold_reasons=[SimpleNamespace(code="new_hold")], is_current_season=False)
    item = {"library_item_id": 1, "selection_provenance": {
        "manual_override": True, "override_applied": True, "season_prefix": "tv/Show/Season 1",
        "hold_reasons": [{"code": "old_hold"}], "is_current_season": False,
    }}
    with patch.object(folder_actions, "project_candidates", return_value=[decision]):
        with pytest.raises(HTTPException) as exc:
            folder_actions.child_recovery_candidate_evidence(Mock(), Mock(), {"prefix": "tv/Show"}, [item])
        assert "original lifecycle override" in exc.value.detail


def test_real_candidate_and_cadence_authorities_are_read_only_on_success_and_blocker() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        config = _authority_config(root)
        try:
            with open_db(config.paths.db_path) as connection:
                item_id = _insert_authority_item(connection, cadence_summary_json=_cleared_cadence())
                connection.commit()
                before_success = _database_snapshot(connection)

                evidence = folder_actions.child_recovery_candidate_evidence(
                    connection, config, {"prefix": "other/Collection"}, [{"library_item_id": item_id}],
                )

                assert evidence["cadence_cleared"] == [item_id]
                assert evidence["items"][str(item_id)]["eligible"] is True
                assert _database_snapshot(connection) == before_success

                connection.execute(
                    library_items.update().where(library_items.c.id == item_id).values(cadence_summary_json=None)
                )
                connection.commit()
                before_blocker = _database_snapshot(connection)

                with pytest.raises(HTTPException) as raised:
                    folder_actions.child_recovery_candidate_evidence(
                        connection, config, {"prefix": "other/Collection"}, [{"library_item_id": item_id}],
                    )

                assert raised.value.status_code == 409
                assert "cadence evidence" in str(raised.value.detail).lower()
                assert _database_snapshot(connection) == before_blocker
        finally:
            reset_engine_cache()


def _authority_config(root: Path) -> MediaforceConfig:
    return MediaforceConfig(
        raw={
            "media": {
                "libraries": [{
                    "key": "other", "label": "Other", "path": str(root / "source" / "other"),
                    "type": "other", "availability": "production", "default_profile": "other_conservative",
                    "policy": {"grouping": "folder"},
                }],
                "output_container": "mkv", "staging_root": str(root / "staging"),
                "archive_root": str(root / "archive"),
            },
            "video": {}, "audio": {}, "subtitle": {}, "planning": {}, "validation": {},
            "overrides": [], "remote_hosts": [],
        },
        paths=ConfigPaths(
            project_root=root, config_path=root / "config.toml", db_path=root / "library.sqlite3",
            run_manifest_dir=root / "runs", web_state_dir=root / "web", review_dir=root / "review",
            runtime_settings_path=root / "runtime.json", runtime_reservation_dir=root / "reservations",
        ),
    )


def _insert_authority_item(connection: DBClient, *, cadence_summary_json: str | None) -> int:
    result = connection.execute(library_items.insert().values(
        source_path="/fixture/other/Collection/Episode.mkv", rel_path="other/Collection/Episode.mkv",
        media_root="other", parent_dir="other/Collection", file_name="Episode.mkv", container="mkv",
        size_bytes=1024, mtime_ns=1_700_000_000_000_000_000, fingerprint="authority-purity-fixture",
        duration_seconds=3600.0, video_codec="h264", width=1920, height=1080, audio_track_count=1,
        english_audio_count=1, audio_summary_json="[]", subtitle_summary_json="[]",
        cadence_summary_json=cadence_summary_json, status="discovered", priority_score=50,
        recommendation="priority_encode", recommendation_reason="Authority purity fixture.",
        last_scan_id="authority-purity", discovered_at="2026-09-09T12:00:00+00:00",
        last_seen_at="2026-09-09T12:00:00+00:00", updated_at="2026-09-09T12:00:00+00:00",
    ))
    return int(result.inserted_primary_key[0])


def _cleared_cadence() -> str:
    return json.dumps(analyze_cadence(
        Path("unused.mkv"),
        video_stream={
            "field_order": "progressive", "avg_frame_rate": "24/1",
            "r_frame_rate": "24/1", "time_base": "1/1000",
        },
        duration_seconds=3600.0,
    ), sort_keys=True, separators=(",", ":"))


def _database_snapshot(connection: DBClient) -> dict[str, list[tuple[Any, ...]]]:
    names = [str(row[0]) for row in connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).all()]
    return {
        name: [tuple(row) for row in connection.exec_driver_sql(f'SELECT * FROM "{name}" ORDER BY rowid').all()]
        for name in names
    }
