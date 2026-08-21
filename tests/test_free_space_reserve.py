from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mediaforce.core.config import MediaforceConfig, _validate_free_space_reserve
from mediaforce.core.db import DBClient
from mediaforce.encoding.free_space import (
    FreeSpaceReservePolicy,
    ReservePreflight,
    SpaceRequirement,
    VolumeCapacity,
    check_free_space_reserve,
    encode_reserve_preflight,
    large_job_requires_serialization,
)
from mediaforce.encoding.staging import promote_one_item
from mediaforce.web.runtime import encode_runtime


class _Config:
    def __init__(self, root: Path) -> None:
        self.media = {
            "free_space_reserve": {
                "operating_headroom_gib": 1,
                "staged_output_overhead_percent": 10,
                "large_job_gib": 2,
            },
        }
        self.output_container = "mkv"
        self._root = root

    def source_root_map_for_host(self, _host: dict[str, object] | None = None) -> dict[str, Path]:
        return {"movies": self._root / "movies"}

    def staging_root_for_host(self, _host: dict[str, object] | None = None) -> Path:
        return self._root / "staging"

    def archive_root_for_host(self, _host: dict[str, object] | None = None) -> Path:
        return self._root / "archive"

    @property
    def archive_root(self) -> Path:
        return self._root / "archive"


def _item(source: Path, *, size_bytes: int = 100) -> dict[str, object]:
    return {
        "media_root": "movies",
        "rel_path": "movies/source.mkv",
        "source_path": str(source),
        "size_bytes": size_bytes,
        "resolved_policy": {"video": {"max_encoded_percent": 80}},
    }


def test_encode_reserve_groups_same_volume_once(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)

    result = encode_reserve_preflight(
        cast(MediaforceConfig, config),
        [_item(source)],
        volume_probe=lambda path: VolumeCapacity("shared", path, 10 * 1024 ** 3),
    )

    assert result.allowed
    assert result.required_by_volume == {"shared": 88}


def test_encode_reserve_accounts_for_cross_volume_copy_and_promotion(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)

    def volume_probe(path: Path) -> VolumeCapacity:
        if "staging" in path.parts:
            return VolumeCapacity("staging", path, 10 * 1024 ** 3)
        if "archive" in path.parts:
            return VolumeCapacity("archive", path, 10 * 1024 ** 3)
        return VolumeCapacity("source", path, 10 * 1024 ** 3)

    result = encode_reserve_preflight(cast(MediaforceConfig, config), [_item(source)], volume_probe=volume_probe)

    assert result.allowed
    assert result.required_by_volume == {"staging": 88, "archive": 100, "source": 88}


def test_encode_reserve_uses_approved_target_with_overhead(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)
    item = _item(source)
    item["stream_budget_ledger"] = {"total_target_bytes": 50}

    result = encode_reserve_preflight(
        cast(MediaforceConfig, config),
        [item],
        volume_probe=lambda path: VolumeCapacity("shared", path, 10 * 1024 ** 3),
    )

    assert result.allowed
    assert result.required_by_volume == {"shared": 56}


def test_insufficient_reserve_fails_closed_with_actionable_reason(tmp_path: Path) -> None:
    result = check_free_space_reserve(
        [SpaceRequirement(tmp_path, 2 * 1024 ** 3, "staged output")],
        policy=FreeSpaceReservePolicy(
            operating_headroom_bytes=1024 ** 3,
            staged_output_overhead_percent=10,
            large_job_bytes=1,
        ),
        volume_probe=lambda path: VolumeCapacity("volume", path, 2 * 1024 ** 3),
    )

    assert not result.allowed
    assert result.waiting_reason is not None
    assert "needs 3.0 GiB free" in result.waiting_reason
    assert "staged output" in result.waiting_reason


def test_unmeasurable_reserve_fails_closed(tmp_path: Path) -> None:
    def unavailable(_path: Path) -> VolumeCapacity:
        raise OSError("storage is offline")

    result = check_free_space_reserve(
        [SpaceRequirement(tmp_path, 1, "staged output")],
        policy=FreeSpaceReservePolicy(
            operating_headroom_bytes=1,
            staged_output_overhead_percent=10,
            large_job_bytes=1,
        ),
        volume_probe=unavailable,
    )

    assert not result.allowed
    assert result.waiting_reason is not None
    assert "Mount or repair" in result.waiting_reason


def test_free_space_reserve_configuration_rejects_nonpositive_values() -> None:
    with pytest.raises(ValueError, match="operating_headroom_gib"):
        _validate_free_space_reserve({"media": {"free_space_reserve": {
            "operating_headroom_gib": 0,
            "staged_output_overhead_percent": 10,
            "large_job_gib": 16,
        }}})


def test_large_job_requires_serialization_when_scheduler_has_running_work(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x")

    assert large_job_requires_serialization(
        cast(MediaforceConfig, config),
        [_item(source, size_bytes=2 * 1024 ** 3)],
    )


def test_large_job_serializes_against_existing_running_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _Config(tmp_path)
    manifest_path = tmp_path / "running-manifest.json"
    source = tmp_path / "movies" / "source.mkv"
    staging = tmp_path / "staging" / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x")
    manifest_path.write_text(
        '{"items": [{"size_bytes": 1, "source_path": "'
        + str(source)
        + '", "staging_path": "'
        + str(staging)
        + '", "rel_path": "movies/source.mkv"}]}'
    )

    class _Connection:
        @staticmethod
        def execute(_statement: object) -> SimpleNamespace:
            return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchall=lambda: [{"job_id": "running"}]))

    monkeypatch.setattr(
        encode_runtime,
        "load_encode_job",
        lambda _connection, _job_id: {"job_id": "running", "manifest_path": str(manifest_path), "manifest_indexes": [0]},
    )

    waiting_reason = encode_runtime._large_job_serialization_waiting_reason(
        cast(DBClient, _Connection()),
        cast(MediaforceConfig, config),
        {"job_id": "candidate"},
        [{
            "size_bytes": 2 * 1024 ** 3,
            "source_path": str(source),
            "staging_path": str(staging),
            "rel_path": "movies/source.mkv",
        }],
        manifest_items_cache={},
    )

    assert waiting_reason == "Waiting for the active large encode job to release its free-space reserve."


def test_promotion_reserve_preflight_is_atomic_before_any_move(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    staging = tmp_path / "staging" / "movies" / "source.mkv"
    source.parent.mkdir()
    staging.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    staging.write_bytes(b"staged")

    class _Connection:
        @staticmethod
        def execute(_statement: object) -> SimpleNamespace:
            return SimpleNamespace(mappings=lambda: SimpleNamespace(fetchone=lambda: {
                "validation_json": '{"passed": true}',
                "staging_path": str(staging),
            }))

    with pytest.raises(RuntimeError, match="Waiting for free-space reserve"):
        promote_one_item(
            cast(DBClient, _Connection()),
            cast(MediaforceConfig, config),
            {**_item(source), "library_item_id": 1},
            force=False,
            probe_media=lambda _path: None,
            file_fingerprint=lambda *_args: "unused",
            timestamp=lambda: "unused",
            record_event=lambda *_args: None,
            reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                allowed=False,
                waiting_reason="Waiting for free-space reserve on /archive",
                required_by_volume={},
            ),
        )

    assert source.exists()
    assert staging.exists()
    assert not (tmp_path / "archive" / "movies" / "source.mkv").exists()
