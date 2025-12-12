import pathlib

import pytest

from mediaforce.services.promote import (
    ProbeSummary,
    promote_encoded_file_atomic,
    rollback_promote,
)


def _fake_probe_factory(source: pathlib.Path, encoded: pathlib.Path, *, encoded_codec: str = "av1"):
    def _probe(path: pathlib.Path) -> ProbeSummary:
        if path == source:
            return ProbeSummary(
                path=path,
                duration_seconds=120.0,
                video_codec="h264",
                width=1920,
                height=1080,
                audio_streams=1,
            )
        if path == encoded:
            return ProbeSummary(
                path=path,
                duration_seconds=120.0,
                video_codec=encoded_codec,
                width=1920,
                height=1080,
                audio_streams=1,
            )
        return ProbeSummary(
            path=path,
            duration_seconds=120.0,
            video_codec=encoded_codec,
            width=1920,
            height=1080,
            audio_streams=1,
        )

    return _probe


def test_promote_atomic_moves_and_rolls_back(tmp_path: pathlib.Path):
    source = tmp_path / "Show.S01E01.x264.mkv"
    transcode_dir = tmp_path / "transcode"
    transcode_dir.mkdir()
    encoded = transcode_dir / "Show.S01E01.AV1.mp4"
    sidecar = tmp_path / "Show.S01E01.x264.srt"

    source.write_bytes(b"source" * 1024)
    encoded.write_bytes(b"encoded" * (1024 * 1024 // 6 + 10))
    sidecar.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")

    probe = _fake_probe_factory(source, encoded)

    result, rollback_state = promote_encoded_file_atomic(
        source_path=source,
        encoded_path=encoded,
        dest_path=tmp_path / encoded.name,
        dry_run=False,
        move_original_to_backup=True,
        rename_sidecars=True,
        verify=True,
        probe=probe,
    )

    assert rollback_state is not None
    assert result.dest_path.exists()
    assert not encoded.exists()
    assert result.backup_source_path is not None
    assert result.backup_source_path.exists()
    assert not source.exists()

    new_sidecar = tmp_path / "Show.S01E01.AV1.srt"
    assert new_sidecar.exists()
    assert not sidecar.exists()

    # Simulate a downstream failure (e.g., DB commit) and rollback.
    rollback_promote(rollback_state)
    assert source.exists()
    assert not result.dest_path.exists()
    assert sidecar.exists()


def test_promote_dry_run_no_changes(tmp_path: pathlib.Path):
    source = tmp_path / "Movie.mkv"
    transcode_dir = tmp_path / "transcode"
    transcode_dir.mkdir()
    encoded = transcode_dir / "Movie.AV1.mp4"
    source.write_bytes(b"source")
    encoded.write_bytes(b"encoded" * (1024 * 1024 // 6 + 10))

    probe = _fake_probe_factory(source, encoded)
    result, rollback_state = promote_encoded_file_atomic(
        source_path=source,
        encoded_path=encoded,
        dry_run=True,
        move_original_to_backup=True,
        verify=True,
        probe=probe,
    )

    assert rollback_state is None
    assert result.dest_path == tmp_path / encoded.name
    assert source.exists()
    assert encoded.exists()


def test_promote_verify_rejects_non_av1(tmp_path: pathlib.Path):
    source = tmp_path / "Show.mkv"
    transcode_dir = tmp_path / "transcode"
    transcode_dir.mkdir()
    encoded = transcode_dir / "Show.AV1.mp4"
    source.write_bytes(b"source" * 1024)
    encoded.write_bytes(b"encoded" * (1024 * 1024 // 6 + 10))

    probe = _fake_probe_factory(source, encoded, encoded_codec="h264")
    with pytest.raises(RuntimeError, match="Verify-before-promote failed"):
        promote_encoded_file_atomic(
            source_path=source,
            encoded_path=encoded,
            dest_path=tmp_path / encoded.name,
            dry_run=True,
            move_original_to_backup=True,
            rename_sidecars=True,
            verify=True,
            probe=probe,
        )
