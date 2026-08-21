from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

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
    promotion_reserve_preflight,
    remote_volume_capacity,
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

    @property
    def free_space_reserve(self) -> dict[str, object]:
        return cast(dict[str, object], self.media["free_space_reserve"])

    def source_root_map_for_host(self, _host: dict[str, object] | None = None) -> dict[str, Path]:
        return {"movies": self._root / "movies"}

    def staging_root_for_host(self, host: dict[str, object] | None = None) -> Path:
        if host and host.get("staging_root"):
            return Path(str(host["staging_root"]))
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
        "source_size_bytes": size_bytes,
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


def test_active_reserve_is_aggregated_before_admission(tmp_path: Path) -> None:
    gib = 1024 ** 3

    result = check_free_space_reserve(
        [SpaceRequirement(tmp_path, 9 * gib, "staged output")],
        policy=FreeSpaceReservePolicy(
            operating_headroom_bytes=16 * gib,
            staged_output_overhead_percent=10,
            large_job_bytes=16 * gib,
        ),
        volume_probe=lambda path: VolumeCapacity("shared", path, 40 * gib),
        reserved_by_volume={"shared": 20 * gib},
    )

    assert not result.allowed
    assert result.waiting_reason is not None
    assert "45.0 GiB free" in result.waiting_reason
    assert "20.0 GiB already reserved by active work" in result.waiting_reason


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


def test_partial_probe_failure_is_explicitly_unmeasurable(tmp_path: Path) -> None:
    first_path = tmp_path / "first"
    second_path = tmp_path / "second"

    def partial_probe(path: Path) -> VolumeCapacity:
        if path == first_path:
            return VolumeCapacity("first", path, 10 * 1024 ** 3)
        raise OSError("second volume offline")

    result = check_free_space_reserve(
        [
            SpaceRequirement(first_path, 100, "first copy"),
            SpaceRequirement(second_path, 200, "second copy"),
        ],
        policy=FreeSpaceReservePolicy(
            operating_headroom_bytes=1,
            staged_output_overhead_percent=10,
            large_job_bytes=1,
        ),
        volume_probe=partial_probe,
    )

    assert not result.allowed
    assert not result.measurable
    assert result.required_by_volume == {"first": 100}


def test_free_space_reserve_configuration_rejects_nonpositive_values() -> None:
    with pytest.raises(ValueError, match="operating_headroom_gib"):
        _validate_free_space_reserve({"media": {"free_space_reserve": {
            "operating_headroom_gib": 0,
            "staged_output_overhead_percent": 10,
            "large_job_gib": 16,
        }}})


def test_free_space_reserve_configuration_fills_missing_keys() -> None:
    raw = {"media": {"free_space_reserve": {"large_job_gib": 32}}}

    _validate_free_space_reserve(raw)

    assert raw["media"]["free_space_reserve"] == {
        "operating_headroom_gib": 16,
        "staged_output_overhead_percent": 10,
        "large_job_gib": 32,
    }


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
    source = tmp_path / "movies" / "source.mkv"
    staging = tmp_path / "staging" / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x")

    waiting_reason = encode_runtime._large_job_serialization_waiting_reason(
        cast(MediaforceConfig, config),
        [{
            "source_size_bytes": 2 * 1024 ** 3,
            "source_path": str(source),
            "staging_path": str(staging),
            "rel_path": "movies/source.mkv",
        }],
        encode_runtime._RunningEncodeReserveState(
            has_running_work=True,
            has_large_or_unmeasurable_work=False,
            reserve_unmeasurable=False,
            reserved_by_volume={},
        ),
    )

    assert waiting_reason == "Waiting for the active large encode job to release its free-space reserve."


def test_manifest_source_size_supports_unresolved_target_fallback(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)
    item = _item(source)
    item["resolved_operator_intent"] = {"size_goal": {"status": "missing_target"}}

    result = encode_reserve_preflight(
        cast(MediaforceConfig, config),
        [item],
        volume_probe=lambda path: VolumeCapacity("shared", path, 10 * 1024 ** 3),
    )

    assert result.allowed
    assert result.required_by_volume == {"shared": 88}


def test_incomplete_reserve_inputs_fail_closed_with_plan_repair_reason(tmp_path: Path) -> None:
    config = _Config(tmp_path)

    result = encode_reserve_preflight(
        cast(MediaforceConfig, config),
        [{"rel_path": "movies/source.mkv", "source_size_bytes": 100}],
        volume_probe=lambda path: VolumeCapacity("shared", path, 10 * 1024 ** 3),
    )

    assert not result.allowed
    assert result.waiting_reason is not None
    assert "complete free-space reserve inputs" in result.waiting_reason
    assert "Mount or repair" not in result.waiting_reason


def test_remote_encode_uses_host_staging_but_controller_promotion_volumes(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)
    remote_staging_root = tmp_path / "remote-fast" / "staging"
    remote_staging_root.mkdir(parents=True)
    remote_paths: list[Path] = []
    local_paths: list[Path] = []

    def remote_probe(_host: dict[str, object], path: Path) -> VolumeCapacity:
        remote_paths.append(path)
        return VolumeCapacity("remote-staging", path, 10 * 1024 ** 3)

    def local_probe(path: Path) -> VolumeCapacity:
        local_paths.append(path)
        if "archive" in path.parts:
            return VolumeCapacity("archive", path, 10 * 1024 ** 3)
        if path.is_relative_to(remote_staging_root):
            return VolumeCapacity("controller-staging", path, 10 * 1024 ** 3)
        return VolumeCapacity("source", path, 10 * 1024 ** 3)

    host = {"mode": "ssh", "media_access": "mounted", "staging_root": str(remote_staging_root)}
    with (
        patch("mediaforce.encoding.free_space.remote_volume_capacity", side_effect=remote_probe),
        patch("mediaforce.encoding.free_space.local_volume_capacity", side_effect=local_probe),
    ):
        result = encode_reserve_preflight(cast(MediaforceConfig, config), [_item(source)], host=host)

    assert result.allowed
    assert remote_paths == [remote_staging_root / "movies"]
    assert any(path.is_relative_to(config.archive_root) for path in local_paths)
    assert all("archive" not in path.parts for path in remote_paths)
    assert result.required_by_volume["remote-staging"] == 88
    assert result.required_by_volume["controller-staging"] == 88


def test_remote_encode_requires_controller_visible_staging_root(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "source.mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 100)
    missing_staging_root = tmp_path / "missing-mounted-staging"
    host = {"mode": "ssh", "media_access": "mounted", "staging_root": str(missing_staging_root)}

    with patch(
        "mediaforce.encoding.free_space.remote_volume_capacity",
        return_value=VolumeCapacity("remote-staging", missing_staging_root, 10 * 1024 ** 3),
    ):
        result = encode_reserve_preflight(cast(MediaforceConfig, config), [_item(source)], host=host)

    assert not result.allowed
    assert not result.measurable
    assert result.waiting_reason is not None
    assert "controller-visible staging root is unavailable" in result.waiting_reason


def test_shared_custom_probe_requires_stable_cache_key(tmp_path: Path) -> None:
    config = _Config(tmp_path)

    result = encode_reserve_preflight(
        cast(MediaforceConfig, config),
        [_item(tmp_path / "movies" / "source.mkv")],
        volume_probe=lambda path: VolumeCapacity("shared", path, 10 * 1024 ** 3),
        capacity_cache={},
    )

    assert not result.allowed
    assert not result.measurable
    assert result.waiting_reason is not None
    assert "stable volume_probe_key" in result.waiting_reason


def test_remote_volume_capacity_walks_to_existing_parent() -> None:
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/disk9 1000 100 900 10% /remote\n",
        stderr="",
    )
    requested = Path("/remote/new/subtree/output.mkv")

    with patch("mediaforce.encoding.free_space.run_remote_command", return_value=completed) as run_command:
        capacity = remote_volume_capacity({"host": "worker"}, requested)

    command = run_command.call_args.args[1]
    assert command[:2] == ["sh", "-lc"]
    assert "while [ ! -e" in command[2]
    assert command[-1] == str(requested)
    assert capacity.free_bytes == 900 * 1024


def test_promotion_reserve_allows_already_archived_source(tmp_path: Path) -> None:
    config = _Config(tmp_path)
    source = tmp_path / "movies" / "missing-source.mkv"
    staging = tmp_path / "staging" / "movies" / "source.mkv"
    destination = source.with_suffix(".mkv")
    archive = tmp_path / "archive" / "movies" / "source.mkv"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"staged")

    def volume_probe(path: Path) -> VolumeCapacity:
        if "staging" in path.parts:
            return VolumeCapacity("staging", path, 10 * 1024 ** 3)
        if "archive" in path.parts:
            return VolumeCapacity("archive", path, 10 * 1024 ** 3)
        return VolumeCapacity("source", path, 10 * 1024 ** 3)

    result = promotion_reserve_preflight(
        cast(MediaforceConfig, config),
        source_path=source,
        staging_path=staging,
        destination_path=destination,
        archive_path=archive,
        volume_probe=volume_probe,
    )

    assert result.allowed
    assert result.required_by_volume == {"source": len(b"staged")}


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
            active_reserve_waiting_reason=lambda *_args: None,
        )

    assert source.exists()
    assert staging.exists()
    assert not (tmp_path / "archive" / "movies" / "source.mkv").exists()
