from __future__ import annotations

import json
import pathlib

from sqlmodel import SQLModel, Session, create_engine, select

from mediaforce.db import (
    MediaItem,
    ProfileChoiceFeedback,
    ProfileEvaluation,
    ProfileSettingsSource,
    RetrainingCandidate,
    VmafSample,
)
from mediaforce.services.quality_feedback import flag_profile_choice
from mediaforce.services.quality_loop import VmafPlanItem, run_profile_quality_loop


def make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_quality_loop_persists_weighted_samples_and_reasoning(tmp_path: pathlib.Path):
    session = make_session()

    item = MediaItem(path=str(tmp_path / "Episode.mkv"), status="pending")
    session.add(item)
    session.commit()
    session.refresh(item)
    assert item.id is not None

    src = ProfileSettingsSource(
        name="remote-default",
        source_type="remote",
        payload=json.dumps({"thresholds": {"min": 82.0, "median": 92.0}}, ensure_ascii=False),
        is_active=True,
    )
    session.add(src)
    session.commit()
    session.refresh(src)

    def fake_measure(sample: VmafPlanItem):
        return 80.0 if sample.kind == "motion" else 83.0

    result = run_profile_quality_loop(
        session,
        media_id=item.id,
        source_path=pathlib.Path(item.path),
        duration_seconds=120.0,
        initial_profile="good",
        settings_source=src,
        sample_length=8.0,
        motion_aware=False,
        measure_vmaf=fake_measure,
        target_height=1080,
        target_height_reason="global",
    )

    assert result.evaluation_id
    assert result.initial_profile == "good"
    assert result.selected_profile == "pristine"  # bumped to less aggressive due to low VMAF
    assert result.status == "done"
    assert result.decision == "bump"

    ev = session.get(ProfileEvaluation, result.evaluation_id)
    assert ev is not None
    assert ev.reason_json

    payload = json.loads(ev.reason_json)
    assert payload["initial_profile"] == "good"
    assert payload["selected_profile"] == "pristine"
    assert payload["target_height"] == 1080
    assert payload["target_height_reason"] == "global"

    samples = session.exec(select(VmafSample).where(VmafSample.evaluation_id == ev.id)).all()
    assert len(samples) == 3
    assert any(s.sample_kind == "motion" and (s.weight or 0) > 1.0 for s in samples)


def test_quality_loop_can_bump_more_aggressive_on_high_weighted(tmp_path: pathlib.Path):
    session = make_session()

    item = MediaItem(path=str(tmp_path / "Episode2.mkv"), status="pending")
    session.add(item)
    session.commit()
    session.refresh(item)
    assert item.id is not None

    def fake_measure(_: VmafPlanItem):
        return 96.0

    result = run_profile_quality_loop(
        session,
        media_id=item.id,
        source_path=pathlib.Path(item.path),
        duration_seconds=120.0,
        initial_profile="good",
        settings_source=None,
        sample_length=8.0,
        motion_aware=False,
        measure_vmaf=fake_measure,
    )

    assert result.selected_profile == "mediocre"
    assert result.decision == "bump"


def test_flag_profile_choice_creates_feedback_and_candidate(tmp_path: pathlib.Path):
    session = make_session()
    item = MediaItem(path=str(tmp_path / "Episode3.mkv"), status="pending")
    session.add(item)
    session.commit()
    session.refresh(item)
    assert item.id is not None

    ev = ProfileEvaluation(media_id=item.id, selected_profile="good")
    session.add(ev)
    session.commit()
    session.refresh(ev)
    assert ev.id is not None

    feedback, candidate = flag_profile_choice(
        session,
        evaluation_id=ev.id,
        decision="bad",
        reason="looks over-smoothed",
    )

    assert feedback.id is not None
    assert candidate is not None
    assert candidate.evaluation_id == ev.id

    feedback_rows = session.exec(
        select(ProfileChoiceFeedback).where(ProfileChoiceFeedback.evaluation_id == ev.id)
    ).all()
    assert len(feedback_rows) == 1

    candidate_rows = session.exec(
        select(RetrainingCandidate).where(RetrainingCandidate.evaluation_id == ev.id)
    ).all()
    assert len(candidate_rows) == 1
