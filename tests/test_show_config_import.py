from __future__ import annotations

import json

from sqlmodel import Session, SQLModel, create_engine

from mediaforce.db import ShowOverride
from mediaforce.services.show_overrides import import_show_config_json


def _make_session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_import_show_config_dry_run_does_not_write(tmp_path):
    config_path = tmp_path / "show_config.json"
    config_path.write_text(json.dumps({"ShowA": {"tier": "good", "max_height": 1080}}))

    session = _make_session()
    try:
        result = import_show_config_json(session, config_path=config_path, dry_run=True)
        assert result.created == 1
        assert result.updated == 0
        assert result.skipped == 0
        assert session.get(ShowOverride, "ShowA") is None
    finally:
        session.close()


def test_import_show_config_apply_writes_override(tmp_path):
    config_path = tmp_path / "show_config.json"
    config_path.write_text(json.dumps({"ShowA": {"tier": "good", "max_height": 1080}}))

    session = _make_session()
    try:
        result = import_show_config_json(session, config_path=config_path, dry_run=False)
        assert result.created == 1
        row = session.get(ShowOverride, "ShowA")
        assert row is not None
        assert row.default_tier == "good"
        assert row.max_height == 1080
        assert row.updated_at
    finally:
        session.close()


def test_import_show_config_does_not_overwrite_existing(tmp_path):
    config_path = tmp_path / "show_config.json"
    config_path.write_text(json.dumps({"ShowA": {"tier": "good"}}))

    session = _make_session()
    try:
        session.add(ShowOverride(show_name="ShowA", default_tier="poor"))
        session.commit()

        result = import_show_config_json(session, config_path=config_path, dry_run=False)
        assert result.created == 0
        assert result.updated == 0
        assert result.skipped == 1

        row = session.get(ShowOverride, "ShowA")
        assert row is not None
        assert row.default_tier == "poor"
    finally:
        session.close()

