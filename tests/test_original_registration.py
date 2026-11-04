import json
import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update

from mediaforce.core.db import open_db
from mediaforce.core.db import DBClient, DBRow
from mediaforce.core.db_tables import library_items, source_admissions, source_generations, source_transitions, source_versions, staged_artifacts
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.encoding.put_back import PutBackRefused
from mediaforce.encoding.staging import promote_one_item
from mediaforce.library import original_registration as registration
from mediaforce.library.evidence_queue import EVIDENCE_WORK_TERMINAL_STATUSES, original_check_wait_reasons
from mediaforce.library.evidence_state import load_library_item_evidence_states, rebuild_library_item_evidence_states
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, load_source_evidence, observe_file, reconcile_source_replacement
from mediaforce.library.source_intake import assess_source_input, queue_original_intake
from test_source_evidence import _config, _probe as _media_probe, _put_back
from test_source_intake import _probe, _source


@pytest.fixture
def originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MediaforceConfig, int, Path]:
    config = _config(tmp_path)
    monkeypatch.setattr(registration, "confirmation_directory", lambda: tmp_path / "app-approvals")
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: _probe())
    monkeypatch.setattr("mediaforce.library.source_intake.probe_evidence_source", lambda *_args, **_kwargs: _probe())
    item, path = _source(config)
    return config, item, path


def _confirm(preview: dict[str, Any]) -> None:
    directory = registration.confirmation_directory()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{preview['preview_id']}.json").write_text(json.dumps({
        "preview_id": preview["preview_id"], "manifest_hash": preview["manifest_hash"],
        "canonical_json": preview["canonical_json"], "challenge": preview["challenge"],
        "owner_uid": os.getuid(), "authentication": "deviceOwnerAuthentication", "confirmed_at": "2026-11-03T12:00:00Z",
    }))


def _dismiss_preview(preview: dict[str, Any]) -> None:
    path = registration.confirmation_directory() / f"{preview['preview_id']}.dismissed"
    previous = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({"manifest_hash": preview["manifest_hash"],
                               "request_dismissed": previous.get("request_dismissed", False) or not preview.get("problems"),
                               "problems": previous.get("problems", []) + preview.get("problems", [])}))


def _run_unspent_approval_wait(config: MediaforceConfig, item: int) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from test_source_intake import _work

    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        waiting = load_library_item_evidence_states(connection, item)["media_fingerprint"]
        assert waiting["work_status"] not in EVIDENCE_WORK_TERMINAL_STATUSES and waiting["attempt_count"] == 0
        connection.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item).values(retry_not_before=None))


def _confirm_modified_preview(config: MediaforceConfig, preview: dict[str, Any], body: dict[str, Any]) -> None:
    from mediaforce.core.evidence import stable_json_hash
    from mediaforce.library.source_evidence import json_text

    preview.update(canonical_json=json_text(body), manifest_hash=stable_json_hash(body))
    (config.paths.web_state_dir / "original-previews" / f"{preview['preview_id']}.json").write_text(json.dumps(preview))
    _confirm(preview)


def _owners(connection: DBClient) -> list[DBRow]:
    return list(connection.execute(select(source_admissions).where(source_admissions.c.basis == "owner")).mappings())


def _confirmed_av1_preview(config: MediaforceConfig, item: int, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(video_codec="av1"))
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: _probe(codec="av1"))
    preview = registration.build_original_preview(config, [item], assertion="Independent AV1 acquisition", overrides={item: "Independent AV1 download"})
    _confirm(preview)
    return preview


def _apply_preview(config: MediaforceConfig, preview_id: str, manifest_hash: str, **kwargs: Any) -> dict[str, Any]:
    # noinspection PyProtectedMember
    body = json.loads(registration._read_preview(config, preview_id)["canonical_json"])
    ids = []
    for item in body["items"]:
        ids.extend(registration.apply_original_preview(config, preview_id, manifest_hash, item_id=item["item_id"], **kwargs)["admission_ids"])
    return {"ok": True, "admission_ids": ids}


def test_preview_cannot_apply_without_native_confirmation(originals: tuple[Any, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independently acquired original")
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)
    _confirm(preview)
    first = _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    assert _apply_preview(config, preview["preview_id"], preview["manifest_hash"]) == first
    with open_db(config.paths.db_path) as connection:
        admission, = _owners(connection)
        assert admission["tier"] == "proven"
        assert registration.owner_confirmation_matches(admission)


@pytest.mark.parametrize("change", ["hash", "owner", "challenge", "enumeration", "revision", "bytes"])
def test_confirmation_binds_exact_preview_and_current_file(originals: tuple[Any, int, Path], change: str) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independently acquired original")
    _confirm(preview)
    receipt_path = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    receipt = json.loads(receipt_path.read_text())
    if change == "hash":
        receipt["manifest_hash"] = "other"
    elif change == "owner":
        receipt["owner_uid"] += 1
    elif change == "challenge":
        receipt["challenge"] = "other"
    elif change == "enumeration":
        receipt["canonical_json"] = "{}"
    elif change == "revision":
        with open_db(config.paths.db_path) as connection:
            connection.execute(update(library_items).values(source_revision=library_items.c.source_revision + 1))
    else:
        path.write_bytes(b"altered" * 100)
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_one_file_revision_race_does_not_undo_another_members_approval(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, first, _path = originals
    second, _other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent originals")
    _confirm(preview)
    registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=first)
    def replace(database: DBClient) -> None:
        database.execute(update(library_items).where(library_items.c.id == second).values(source_revision=4))
    with pytest.raises(ValueError):
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=second, before_commit=replace)
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1
        assert load_source_evidence(connection, first).availability == "available"



def test_corrections_append_and_copied_native_reference_cannot_authorize_other_version(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(first)
    first_id = _apply_preview(config, first["preview_id"], first["manifest_hash"])["admission_ids"][0]
    second = registration.build_original_preview(config, [item], assertion="Corrected acquisition details")
    _confirm(second)
    second_id = _apply_preview(config, second["preview_id"], second["manifest_hash"])["admission_ids"][0]
    with open_db(config.paths.db_path) as connection:
        latest = connection.execute(select(source_admissions).where(source_admissions.c.basis == "owner").where(source_admissions.c.admission_id == second_id)).mappings().one()
        assert latest["supersedes_id"] == first_id
        assert len(_owners(connection)) == 2
        assert load_source_evidence(connection, item).availability == "available"
        forged = {**latest, "confirmation_ref": f"mac-app:{first['preview_id']}"}
        assert not registration.owner_confirmation_matches(forged)
        connection.execute(source_admissions.insert().values(**{
            **forged, "admission_id": "forged", "supersedes_id": second_id,
        }))
        assert load_source_evidence(connection, item).availability == "unknown"


@pytest.mark.parametrize("location", ["container", "stream", "codec", "missing-codec"])
def test_codec_exceptions_never_waive_stamps_or_missing_data(originals: tuple[MediaforceConfig, int, Path],
                                                          monkeypatch: pytest.MonkeyPatch, location: str) -> None:
    config, item, _path = originals
    probe = _probe(codec="av1" if location != "missing-codec" else "")
    if location == "container":
        probe["format"]["tags"]["MEDIAFORCE_ENCODED_BY"] = ""
    if location == "stream":
        probe["streams"][0]["tags"]["Mediaforce_Encoded_By"] = "anything"
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: probe)
    with pytest.raises(ValueError):
        registration.build_original_preview(config, [item], assertion="Independent acquisition")
    if location == "codec":
        preview = registration.build_original_preview(config, [item], assertion="Independent AV1 acquisition",
                                                      overrides={item: "Downloaded this AV1 file independently"})
        assert json.loads(preview["canonical_json"])["items"][0]["checks"]["exception_matches"]
    else:
        with pytest.raises(ValueError):
            registration.build_original_preview(config, [item], assertion="Independent acquisition", overrides={item: "Exception"})


def _rescan(config: MediaforceConfig, item: int, path: Path) -> None:
    stat = path.stat()
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(
            size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns, content_version_fingerprint=content_version_fingerprint(path, stat),
            fingerprint=file_fingerprint(path, stat, 60),
        ))
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        ensure_active_source_version(connection, current)


def _confirmed_publish(originals: tuple[MediaforceConfig, int, Path]) -> tuple[str, str, bytes]:
    config, item, path = originals
    path.write_bytes(b"S" * 400_000)
    _rescan(config, item, path)
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    output_bytes = b"A" * 1500
    staging = config.staging_root / "source.mkv"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(output_bytes)
    with open_db(config.paths.db_path) as connection:
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        original = str(current["active_source_version_id"])
        connection.execute(staged_artifacts.insert().values(
            library_item_id=item, staging_path=str(staging), source_path=str(path),
            validation_json='{"passed":true}', validated_at=timestamp(), updated_at=timestamp(),
        ))
        connection.commit()
        promote_one_item(connection, config, {"library_item_id": item, "source_path": str(path),
            "rel_path": current["rel_path"], "media_root": "tv"}, force=False,
            probe_media=lambda _path: _media_probe(), file_fingerprint=file_fingerprint, timestamp=timestamp,
            record_event=lambda *_args: None, reserve_preflight=lambda *_args, **_kwargs: ReservePreflight(
                allowed=True, waiting_reason=None, required_by_volume={}))
        output = connection.execute(select(library_items.c.active_source_version_id).where(library_items.c.id == item)).scalar_one()
    return original, str(output), output_bytes


def test_external_original_and_output_copies_reattach_by_full_digest(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    original, output, output_bytes = _confirmed_publish(originals)
    for content, expected, role in ((b"S" * 400_000, original, "input"), (output_bytes, output, "output")):
        path.write_bytes(content)
        _rescan(config, item, path)
        with open_db(config.paths.db_path) as connection:
            assert queue_original_intake(connection, config) > 0
        assess_source_input(config, item, ManagedProcessController(), lambda: None)
        with open_db(config.paths.db_path) as connection:
            active = connection.execute(select(library_items.c.active_source_version_id).where(library_items.c.id == item)).scalar_one()
            assert active == expected
            assert connection.execute(select(source_versions.c.role).where(source_versions.c.version_id == active)).scalar_one() == role
            assert len(_owners(connection)) == 1
            evidence = load_source_evidence(connection, item)
            assert evidence.original_version_id == original
            assert evidence.source_path is not None
            assert Path(evidence.source_path).read_bytes() == b"S" * 400_000


def test_original_moved_back_externally_is_read_from_its_reattached_path(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    original, _output, _bytes = _confirmed_publish(originals)
    with open_db(config.paths.db_path) as connection:
        archived_path = load_source_evidence(connection, item).source_path
        assert archived_path is not None
        archive = Path(archived_path)
    archive.replace(path)
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        evidence = load_source_evidence(connection, item)
        assert evidence.original_version_id == original
        assert evidence.source_path is not None
        assert evidence.source_path == str(path.resolve())
        assert path.read_bytes() == b"S" * 400_000


@pytest.mark.parametrize("symlink", [False, True])
def test_reattached_original_cannot_read_a_path_in_an_unfinished_move(originals: tuple[MediaforceConfig, int, Path], symlink: bool) -> None:
    from mediaforce.encoding.source_publication import prepare_archive_removal

    config, item, path = originals
    _confirmed_publish(originals)
    with open_db(config.paths.db_path) as connection:
        archived_path = load_source_evidence(connection, item).source_path
        assert archived_path is not None
    if symlink:
        path.unlink()
        path.symlink_to(archived_path)
    else:
        path.write_bytes(Path(archived_path).read_bytes())
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        assert prepare_archive_removal(connection, Path(archived_path))
        evidence = load_source_evidence(connection, item)
        assert evidence.availability == ("unavailable" if symlink else "available")
        assert evidence.source_path == (None if symlink else str(path.resolve()))


def test_equal_samples_with_changed_full_digest_do_not_reattach(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    original, _output, _bytes = _confirmed_publish(originals)
    content = bytearray(b"S" * 400_000)
    content[100_000] = ord("X")
    path.write_bytes(content)
    _rescan(config, item, path)
    with open_db(config.paths.db_path) as connection:
        retained = connection.execute(select(source_versions).where(source_versions.c.version_id == original)).mappings().one()
        assert observe_file(path)["sample_fingerprint"] == retained["sample_fingerprint"]
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(library_items.c.active_source_version_id)).scalar_one() != original
        assert load_source_evidence(connection, item).original_version_id is None


def test_hash_read_failure_and_catalog_removal_preserve_history(originals: tuple[MediaforceConfig, int, Path],
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, path = originals
    _confirmed_publish(originals)
    path.write_bytes(b"S" * 400_000)
    _rescan(config, item, path)
    with open_db(config.paths.db_path) as connection:
        before = connection.execute(select(library_items.c.active_source_version_id)).scalar_one()
    with monkeypatch.context() as context:
        context.setattr("mediaforce.library.source_intake.observe_file", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")))
        with pytest.raises(OSError):
            assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(library_items.c.active_source_version_id)).scalar_one() == before
        connection.execute(library_items.delete().where(library_items.c.id == item))
        from test_source_evidence import _insert_item

        reused = _insert_item(connection, path)
        assert reused == item
        current = connection.execute(select(library_items).where(library_items.c.id == reused)).mappings().one()
        reconciled = reconcile_source_replacement(connection, current, observe_file(path, full_digest=True))
        assert connection.execute(select(source_versions.c.role).where(source_versions.c.version_id == reconciled)).scalar_one() == "unknown"
        assert load_source_evidence(connection, reused).original_version_id is None
        assert len(_owners(connection)) == 1


def test_put_back_refuses_changed_original_despite_equal_samples(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    _original, _output, output_bytes = _confirmed_publish(originals)
    with open_db(config.paths.db_path) as connection:
        archive = Path(connection.execute(select(staged_artifacts.c.archived_source_path)).scalar_one())
        content = bytearray(archive.read_bytes())
        content[100_000] = ord("X")
        archive.write_bytes(content)
        with pytest.raises(PutBackRefused) as refused:
            _put_back(connection, item)
        assert refused.value.code == "backup_changed"
        assert path.read_bytes() == output_bytes
        assert archive.read_bytes() == content


def test_same_digest_in_different_retained_generations_stays_unknown(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    original, _output, _bytes = _confirmed_publish(originals)
    path.write_bytes(b"S" * 400_000)
    _rescan(config, item, path)
    identity = observe_file(path, full_digest=True)
    with open_db(config.paths.db_path) as connection:
        connection.execute(source_generations.insert().values(generation_id="another-acquisition", created_at=timestamp()))
        other = append_version(connection, generation_id="another-acquisition", role="input", identity=identity,
                               profile={"video_codec": "h264"}, recorded_at=timestamp())
        connection.execute(source_transitions.insert().values(
            operation_id="another-replacement", library_item_id=item, before_version_id=original,
            after_version_id=other, expected_revision=0, kind="replacement", outcome="committed", evidence_json="{}", recorded_at=timestamp(),
        ))
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        active = reconcile_source_replacement(connection, current, identity)
        assert active not in (original, other)
        assert load_source_evidence(connection, item).original_version_id is None


def _reuse_catalog_id(connection: DBClient, item_id: int) -> None:
    row = dict(connection.execute(select(library_items).where(library_items.c.id == item_id)).mappings().one())
    connection.execute(library_items.delete().where(library_items.c.id == item_id))
    connection.execute(library_items.insert().values(**{**row, "active_source_version_id": None, "source_revision": 0}))


def test_reused_catalog_id_cannot_take_a_preview_for_the_deleted_item(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        _reuse_catalog_id(connection, item)
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    assert path.exists()
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


@pytest.mark.parametrize("corruption", ["json", "items", "challenge"])
def test_dismissed_or_unreadable_preview_does_not_break_other_requests(originals: tuple[MediaforceConfig, int, Path], corruption: str) -> None:
    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    second = registration.build_original_preview(config, [item], assertion="Updated acquisition details")
    directory = registration.confirmation_directory()
    directory.mkdir(parents=True)
    _dismiss_preview(first)
    assert [preview["preview_id"] for preview in registration.list_original_previews(config)] == [second["preview_id"]]
    if corruption == "json":
        damaged = "{"
    else:
        from mediaforce.core.evidence import stable_json_hash
        from mediaforce.library.source_evidence import json_text

        body = json.loads(second["canonical_json"])
        if corruption == "items":
            body["items"] = [{"item_id": []}]
        else:
            body["challenge"] = None
            second["challenge"] = None
        second["canonical_json"] = json_text(body)
        second["manifest_hash"] = stable_json_hash(body)
        damaged = json.dumps(second)
    (config.paths.web_state_dir / "original-previews" / f"{second['preview_id']}.json").write_text(damaged)
    assert registration.list_original_previews(config) == []


def test_symlink_retarget_before_commit_cannot_approve_the_new_library_file(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    target = path.with_name("original-target")
    path.rename(target)
    path.symlink_to(target)
    _rescan(config, item, path)
    replacement = path.with_name("new-target")
    replacement.write_bytes(b"unapproved")
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)

    def retarget(_connection: DBClient) -> None:
        path.unlink()
        path.symlink_to(replacement)

    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=retarget)
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_replaced_unencoded_file_still_gets_automatic_probable_intake(originals: tuple[MediaforceConfig, int, Path],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, path = originals
    with open_db(config.paths.db_path) as connection:
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        ensure_active_source_version(connection, current)
    path.write_bytes(b"upgraded-independent-download" * 500)
    _rescan(config, item, path)
    with open_db(config.paths.db_path) as connection:
        assert queue_original_intake(connection, config) > 0
    monkeypatch.setattr("mediaforce.library.source_intake.probe_evidence_source", lambda *_args, **_kwargs: _probe())
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        admission = connection.execute(select(source_admissions)).mappings().one()
        assert admission["basis"] == "readable-source"
        assert admission["tier"] == "probable"


def test_owner_confirmation_and_correction_keep_existing_observed_measurements(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from test_source_intake import _measure_source_traits
    from mediaforce.library.source_intake import commit_readable_source

    config, item, path = originals
    _measure_source_traits(config)
    with open_db(config.paths.db_path) as connection:
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        commit_readable_source(connection, current, observe_file(path, full_digest=True), _probe())
        before = load_source_evidence(connection, item)
        assert all(trait.tier == "probable" for trait in before.traits.values())
    for reason in ("Independent acquisition", "Corrected acquisition details"):
        preview = registration.build_original_preview(config, [item], assertion=reason)
        _confirm(preview)
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
        with open_db(config.paths.db_path) as connection:
            after = load_source_evidence(connection, item)
            assert after.original_version_id == before.original_version_id
            assert {kind: trait.measurement_ids for kind, trait in after.traits.items()} == {
                kind: trait.measurement_ids for kind, trait in before.traits.items()}
            assert all(trait.tier == "probable" for trait in after.traits.values())


def test_cancelled_background_recheck_never_commits_an_admission(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.process_control import ProcessCancelledError

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    def cancel() -> None:
        raise ProcessCancelledError("Stopped")
    with pytest.raises(ProcessCancelledError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"], check_cancelled=cancel)
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_native_confirmation_fixture_uses_the_backend_receipt_schema(originals: tuple[MediaforceConfig, int, Path],
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = json.loads((Path(__file__).parent / "fixtures/original-confirmation.json").read_text())
    monkeypatch.setattr(registration.os, "getuid", lambda: fixture["owner_uid"])
    directory = registration.confirmation_directory()
    directory.mkdir(parents=True)
    (directory / f"{fixture['preview_id']}.json").write_text(json.dumps(fixture))
    assert registration._confirmation(fixture["preview_id"]) == fixture


def test_owner_approved_format_exception_can_queue_missing_measurements(originals: tuple[MediaforceConfig, int, Path],
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, _path = originals
    preview = _confirmed_av1_preview(config, item, monkeypatch)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    from test_source_intake import _work

    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions.c.basis).where(source_admissions.c.basis == "owner")).scalar_one() == "owner"


def test_known_output_digest_cannot_be_waived_by_a_format_exception(originals: tuple[MediaforceConfig, int, Path],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, path = originals
    _original, _output, _bytes = _confirmed_publish(originals)
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: _probe(codec="av1"))
    with pytest.raises(ValueError, match="already compressed"):
        registration.build_original_preview(config, [item], assertion="Claimed independent download",
                                            overrides={item: "Claimed AV1 exception"})


def test_concurrent_retries_reconcile_the_same_result(originals: tuple[MediaforceConfig, int, Path],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    original_observe = registration.source_evidence.observe_file
    both_reading = Barrier(2, timeout=10)
    def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_observe(*args, **kwargs)
        both_reading.wait()
        return result
    monkeypatch.setattr("mediaforce.library.original_registration.source_evidence.observe_file", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_apply_preview, config, preview["preview_id"], preview["manifest_hash"])
        second = executor.submit(_apply_preview, config, preview["preview_id"], preview["manifest_hash"])
        assert first.result(timeout=20) == second.result(timeout=20)
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1


def test_put_back_cancellation_stops_the_content_check_before_any_move(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.process_control import ProcessCancelledError
    from mediaforce.encoding.put_back import put_original_back

    config, item, path = originals
    _original, _output, output = _confirmed_publish(originals)
    def cancel() -> None:
        raise ProcessCancelledError("Stopped")
    with open_db(config.paths.db_path) as connection:
        archive = Path(connection.execute(select(staged_artifacts.c.archived_source_path)).scalar_one())
        original = archive.read_bytes()
        with pytest.raises(ProcessCancelledError):
            put_original_back(connection, item, probe_media=lambda _path: _media_probe(),
                file_fingerprint=file_fingerprint, content_fingerprint=content_version_fingerprint,
                timestamp=timestamp, record_event=lambda *_args: None, check_cancelled=cancel)
        assert path.read_bytes() == output
        assert archive.read_bytes() == original
        assert connection.execute(select(source_transitions).where(source_transitions.c.kind == "put-back")).first() is None


def test_exhausted_checks_use_existing_explicit_retry_without_reconfirming(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue
    from test_source_intake import _work

    config, item, _path = originals
    preview = _confirmed_av1_preview(config, item, monkeypatch)
    receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    saved = receipt.read_bytes()
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with monkeypatch.context() as unavailable:
        def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise OSError("The file check could not read its input")
        unavailable.setattr("mediaforce.library.original_registration.source_evidence.observe_file", fail)
        assert _work(config, max_attempts=1) > 0
    for _ in range(2):
        registration.queue_confirmed_originals(config)
    with open_db(config.paths.db_path) as connection:
        work = connection.execute(select(library_item_evidence_state).where(
            library_item_evidence_state.c.library_item_id == item)).mappings().one()
        assert work["work_status"] == "failed" and work["attempt_count"] == 1
        assert not _owners(connection)
        start_evidence_work(connection, config, "tv/source.mkv", evidence_kinds=["media_fingerprint"])
        resume_evidence_queue(connection)
    assert _work(config) > 0
    registration.queue_confirmed_originals(config)
    assert _work(config) > 0
    assert receipt.read_bytes() == saved
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).original_version_id is not None
        assert len(_owners(connection)) == 1


def test_idle_controller_discovers_a_saved_av1_confirmation(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from test_source_intake import _work
    from mediaforce.library.evidence_worker import default_evidence_worker_deps
    from mediaforce.web.runtime.operator_work import BoundedEvidenceRunner

    config, item, _path = originals
    preview = _confirmed_av1_preview(config, item, monkeypatch)
    deps = default_evidence_worker_deps()
    deps.load_config = lambda _path: config
    deps.evidence_host_rows = lambda _config: []
    runner = BoundedEvidenceRunner(config.paths.config_path, deps=deps)
    runner.prepare_background_work()
    assert _work(config) > 0
    assert not registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1


def _record_owner_measurements(config: MediaforceConfig, item: int, path: Path) -> None:
    from test_source_intake import _summary
    from mediaforce.library.source_evidence import record_current_measurement

    with open_db(config.paths.db_path) as connection:
        current = connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one()
        observed = observe_file(path, full_digest=True)
        for kind in ("cadence_analysis", "media_fingerprint"):
            record_current_measurement(connection, current, kind=kind, summary=_summary(kind),
                                       provenance={"before": observed, "after": observed}, binding="observed")
        assert all(trait.tier == "probable" for trait in load_source_evidence(connection, item).traits.values())


def _add_output(connection: DBClient, identity: dict[str, Any], *, codec: str = "av1") -> None:
    from uuid import uuid4

    generation = uuid4().hex
    connection.execute(source_generations.insert().values(generation_id=generation, created_at=timestamp()))
    append_version(connection, generation_id=generation, role="output", identity=identity,
                   profile={"video_codec": codec}, recorded_at=timestamp())


@pytest.mark.parametrize("change", ["output", "probe_description"])
def test_unrelated_snapshot_changes_do_not_invalidate_an_original(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    if change == "output":
        other = path.with_name("unrelated-output")
        other.write_bytes(b"another compressed file")
        def complete(database: DBClient) -> None:
            _add_output(database, observe_file(other, full_digest=True))
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=complete)
    else:
        probe = _probe()
        probe["format"]["format_long_name"] = "More descriptive container information"
        monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: probe)
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).availability == "available"


@pytest.mark.parametrize("digest", [True, False])
def test_new_matching_or_unresolved_output_still_vetoes_under_the_commit_lock(originals: tuple[MediaforceConfig, int, Path], digest: bool) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    identity = observe_file(path, full_digest=digest)
    def complete(database: DBClient) -> None:
        _add_output(database, identity, codec="h264")
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=complete)
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_one_stale_member_fails_once_while_other_files_continue(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import item_events
    from test_source_intake import _work

    config, first, path = originals
    second, _other = _source(config, "second")
    third, _third = _source(config, "third")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent acquisitions")
    _confirm(preview)
    path.write_bytes(b"changed download" * 200)
    _rescan(config, first, path)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, second).availability == "available"
        assert queue_original_intake(connection, config) > 0
    for _ in range(8):
        if not _work(config):
            break
    registration.queue_confirmed_originals(config)
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, third).availability == "available"
        assert load_source_evidence(connection, first).availability == "available"
        failures = connection.execute(select(item_events).where(item_events.c.event_type == "original_confirmation_failed")).mappings().all()
        assert len(failures) == 1
        assert failures[0]["library_item_id"] == first
    waiting = registration.list_original_previews(config)
    assert len(waiting) == 1 and waiting[0]["problems"]
    corrected = registration.build_original_preview(config, [first], assertion="Confirmed this replacement independently")
    _confirm(corrected)
    _apply_preview(config, corrected["preview_id"], corrected["manifest_hash"])
    assert registration.list_original_previews(config) == []


def test_assessed_owner_rows_do_not_starve_later_automatic_intake(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state

    config, first, path = originals
    second, other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent acquisitions")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    _record_owner_measurements(config, first, path)
    _record_owner_measurements(config, second, other)
    later, _later = _source(config, "later")
    with open_db(config.paths.db_path) as connection:
        assert queue_original_intake(connection, config, limit=2) > 0
        assert connection.execute(select(library_item_evidence_state.c.work_status).where(
            library_item_evidence_state.c.library_item_id == later)).scalar_one() == "queued"


def test_owner_rule_problem_clears_when_rules_recover(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES

    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    _record_owner_measurements(config, item, path)
    with monkeypatch.context() as changed:
        changed.setitem(HISTORICAL_OUTPUT_PROFILES, "version", "fixture changed rule")
        with open_db(config.paths.db_path) as connection:
            assert queue_original_intake(connection, config) == 0
        assert registration.list_original_previews(config)[0]["problems"]
    assert registration.list_original_previews(config) == []


def test_missing_receipt_then_restore_never_supersedes_the_owner(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    _record_owner_measurements(config, item, path)
    receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    saved = receipt.read_bytes()
    receipt.unlink()
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1
        assert load_source_evidence(connection, item).availability == "unknown"
    assert registration.list_original_previews(config)[0]["problems"]
    receipt.write_bytes(saved)
    assert registration.list_original_previews(config) == []
    with open_db(config.paths.db_path) as connection:
        assert all(trait.tier == "probable" for trait in load_source_evidence(connection, item).traits.values())


def test_metadata_only_change_keeps_owner_version_and_measurements(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    _record_owner_measurements(config, item, path)
    with open_db(config.paths.db_path) as connection:
        before = load_source_evidence(connection, item)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        after = load_source_evidence(connection, item)
        assert after.original_version_id == before.original_version_id
        assert {kind: value.measurement_ids for kind, value in after.traits.items()} == {
            kind: value.measurement_ids for kind, value in before.traits.items()}
        assert len(_owners(connection)) == 1


@pytest.mark.parametrize("codec", ["h264", "av1"])
def test_never_encoded_original_reattaches_after_same_sample_replacement(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch, codec: str) -> None:
    config, item, path = originals
    path.write_bytes(b"S" * 400_000)
    _rescan(config, item, path)
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: _probe(codec=codec))
    monkeypatch.setattr("mediaforce.library.source_intake.probe_evidence_source", lambda *_args, **_kwargs: _probe(codec=codec))
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition",
        overrides={item: "Independent AV1 acquisition"} if codec == "av1" else None)
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        original = connection.execute(select(library_items.c.active_source_version_id)).scalar_one()
    changed = bytearray(path.read_bytes())
    changed[100_000] = ord("R")
    path.write_bytes(changed)
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    path.write_bytes(b"S" * 400_000)
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(library_items.c.active_source_version_id)).scalar_one() == original
        assert load_source_evidence(connection, item).availability == "available"


def test_bulk_members_take_separate_digest_windows(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.library.evidence_hosts import ControllerDigestWindow, ControllerDigestWindowClosed

    config, first, path = originals
    second, _other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent acquisitions")
    _confirm(preview)
    reads = []
    observe = registration.source_evidence.observe_file
    def read(candidate: Path, **kwargs: Any) -> dict[str, Any]:
        reads.append(candidate)
        return observe(candidate, **kwargs)
    monkeypatch.setattr("mediaforce.library.original_registration.source_evidence.observe_file", read)
    def guard() -> Any:
        return ControllerDigestWindow(admitted=not reads, finish_current_file=True).guard(ManagedProcessController())
    registration.apply_confirmed_originals(config, first, ManagedProcessController(), lambda: None, guard)
    with pytest.raises(ControllerDigestWindowClosed):
        registration.apply_confirmed_originals(config, second, ManagedProcessController(), lambda: None, guard)
    assert reads == [path.resolve()]
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1


def test_preview_build_race_cannot_freeze_a_null_lifetime_token(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, _path = originals
    prepare = registration._preview_item
    def reuse(database: DBClient, item_id: int, override: str | None) -> dict[str, Any]:
        _reuse_catalog_id(database, item_id)
        return prepare(database, item_id, override)
    monkeypatch.setattr(registration, "_preview_item", reuse)
    with pytest.raises(ValueError):
        registration.build_original_preview(config, [item], assertion="Independent acquisition")
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)



def test_matching_observed_av1_measurements_are_used_without_another_analysis(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.core.db_tables import source_measurements
    from test_source_intake import _measure_source_traits, _work

    config, item, _path = originals
    _measure_source_traits(config)
    monkeypatch.setattr("mediaforce.library.original_registration.probe.probe_evidence_source", lambda *_args, **_kwargs: _probe(codec="av1"))
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition", overrides={item: "Independent AV1 acquisition"})
    _confirm(preview)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        before = connection.execute(select(func.count()).select_from(source_measurements)).scalar_one()
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert all(trait.tier == "probable" for trait in load_source_evidence(connection, item).traits.values())
        assert connection.execute(select(func.count()).select_from(source_measurements)).scalar_one() == before


def test_incomplete_saved_checks_cannot_be_applied_even_with_a_native_record(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    body = json.loads(preview["canonical_json"])
    body["items"][0]["checks"]["raw_probe"] = {}
    _confirm_modified_preview(config, preview, body)
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


@pytest.mark.parametrize("field", ["generation", "supersedes", "version"])
def test_native_list_cannot_reassign_an_unlisted_items_original_history(originals: tuple[MediaforceConfig, int, Path], field: str) -> None:
    config, first, _path = originals
    second, _other = _source(config, "second")
    approved = registration.build_original_preview(config, [second], assertion="Independent acquisition")
    _confirm(approved)
    prior = _apply_preview(config, approved["preview_id"], approved["manifest_hash"])["admission_ids"][0]
    with open_db(config.paths.db_path) as connection:
        original = connection.execute(select(library_items.c.active_source_version_id).where(library_items.c.id == second)).scalar_one()
        generation = connection.execute(select(source_versions.c.generation_id).where(source_versions.c.version_id == original)).scalar_one()
    preview = registration.build_original_preview(config, [first], assertion="Independent acquisition")
    body = json.loads(preview["canonical_json"])
    selected = body["items"][0]
    if field == "generation":
        selected.update(generation_id=generation, supersedes_id=prior)
    elif field == "supersedes":
        selected["supersedes_id"] = prior
    else:
        selected["version_id"] = original
    _confirm_modified_preview(config, preview, body)
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, second).original_version_id == original
        assert len(_owners(connection)) == 1


def test_native_draft_cannot_import_another_files_observed_measurements(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import source_measurements
    from mediaforce.library.source_evidence import record_current_measurement
    from test_source_intake import _summary

    config, first, path = originals
    second, other = _source(config, "second")
    data = bytearray(b"S" * 400_000)
    path.write_bytes(data)
    data[100_000] = ord("R")
    other.write_bytes(data)
    _rescan(config, first, path)
    _rescan(config, second, other)
    with open_db(config.paths.db_path) as connection:
        current = connection.execute(select(library_items).where(library_items.c.id == second)).mappings().one()
        observed = observe_file(other, full_digest=True)
        receipt = record_current_measurement(connection, current, kind="media_fingerprint", summary=_summary("media_fingerprint"),
                                            provenance={"before": observed, "after": observed}, binding="observed")
        assert connection.execute(select(source_measurements.c.measurement_id)).scalar_one() == receipt
    assert observe_file(path)["sample_fingerprint"] == observe_file(other)["sample_fingerprint"]
    assert observe_file(path, full_digest=True)["sha256"] != observe_file(other, full_digest=True)["sha256"]
    preview = registration.build_original_preview(config, [first], assertion="Independent acquisition")
    body = json.loads(preview["canonical_json"])
    body["items"][0]["measurement_ids"] = [receipt]
    _confirm_modified_preview(config, preview, body)
    with pytest.raises(ValueError):
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_saved_probe_description_cannot_change_the_original_profile(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    body = json.loads(preview["canonical_json"])
    with open_db(config.paths.db_path) as connection:
        original = connection.execute(select(source_versions).where(source_versions.c.version_id == body["items"][0]["active_version_id"])).mappings().one()
        width = json.loads(original["profile_json"]).get("width")
    body["items"][0]["checks"]["raw_probe"]["streams"][0]["width"] = 123
    _confirm_modified_preview(config, preview, body)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).profile.get("width") == width


@pytest.mark.parametrize("cleanup", [False, True])
def test_published_approval_never_asks_for_a_new_original_list_after_cleanup(originals: tuple[MediaforceConfig, int, Path], cleanup: bool) -> None:
    from mediaforce.encoding.source_publication import commit_archive_removal, prepare_archive_removal

    config, item, _path = originals
    stale = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(stale)
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(
            source_revision=library_items.c.source_revision + 1))
    assert not registration.queue_original_approval(config, stale["preview_id"], stale["manifest_hash"])
    assert registration.list_original_previews(config)[0]["problems"]
    _confirmed_publish(originals)
    assert registration.list_original_previews(config) == []
    with open_db(config.paths.db_path) as connection:
        archived_path = load_source_evidence(connection, item).source_path
        assert archived_path is not None
        operations = prepare_archive_removal(connection, Path(archived_path))
        assert operations
        if cleanup:
            Path(archived_path).unlink()
            commit_archive_removal(connection, operations)
    assert registration.list_original_previews(config) == []


def test_automatic_intake_cannot_stale_the_same_bytes_original_list(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    assert [row["preview_id"] for row in registration.list_original_previews(config)] == [preview["preview_id"]]
    _confirm(preview)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).availability == "available"


def test_overlapping_confirmations_apply_in_time_order_and_show_the_later_conflict(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext

    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    second = registration.build_original_preview(config, [item], assertion="Corrected acquisition details")
    _confirm(first)
    _confirm(second)
    receipt_path = registration.confirmation_directory() / f"{second['preview_id']}.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["confirmed_at"] = "2026-11-03T13:00:00Z"
    receipt_path.write_text(json.dumps(receipt))
    monkeypatch.setattr(registration, "_stored_previews", lambda _config: [second, first])
    registration.apply_confirmed_originals(config, item, ManagedProcessController(), lambda: None, nullcontext)
    waiting = registration.list_original_previews(config)
    assert len(waiting) == 1
    assert waiting[0]["preview_id"] == second["preview_id"] and waiting[0]["problems"]
    with open_db(config.paths.db_path) as connection:
        owner, = _owners(connection)
        assert owner["confirmation_ref"] == f"mac-app:{first['preview_id']}"


def test_queue_conflict_for_one_member_does_not_skip_its_sibling(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.library import evidence_queue
    from mediaforce.core.db_tables import library_item_evidence_state

    config, first, _path = originals
    second, _other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent acquisitions")
    _confirm(preview)
    queue = evidence_queue.queue_decision_evidence_work
    def conflicting(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs["library_item_ids"] == [first]:
            raise evidence_queue.EvidenceQueueConflict("Already owned by another batch")
        return queue(*args, **kwargs)
    monkeypatch.setattr(evidence_queue, "queue_decision_evidence_work", conflicting)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(library_item_evidence_state.c.work_status).where(
            library_item_evidence_state.c.library_item_id == second)).scalar_one() == "queued"


def test_original_preview_refuses_a_library_that_cannot_run_its_checks(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    config.raw["media"]["source_roots"] = {}
    with pytest.raises(ValueError):
        registration.build_original_preview(config, [item], assertion="Independent acquisition")


@pytest.mark.parametrize("stage", ["before", "during"])
def test_unavailable_source_waits_and_recovers_with_the_same_approval(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    from mediaforce.core.db_tables import item_events, library_item_evidence_state
    from mediaforce.library import evidence_worker
    from test_source_intake import _work

    config, item, _path = originals
    sibling, _other = _source(config, "sibling")
    preview = registration.build_original_preview(config, [item, sibling], assertion="Independent acquisitions")
    _confirm(preview)
    receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    saved = receipt.read_bytes()
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    fingerprint = evidence_worker._source_fingerprint
    calls = 0
    def unavailable(*args: Any) -> Any:
        nonlocal calls
        if args[1].library_item_id != item:
            return fingerprint(*args)
        calls += 1
        return ("source_unavailable", None) if stage == "before" or calls > 1 else fingerprint(*args)
    def failed_analysis(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise OSError("The source temporarily stopped responding")
    with monkeypatch.context() as offline:
        offline.setattr(evidence_worker, "_source_fingerprint", unavailable)
        assert _work(config, failed_analysis) > 0
        with open_db(config.paths.db_path) as connection:
            connection.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item).values(
                retry_not_before="2999-01-01T00:00:00+00:00"))
        assert _work(config) > 0
        with open_db(config.paths.db_path) as connection:
            assert connection.execute(select(source_admissions.c.admission_id).where(
                source_admissions.c.confirmation_ref == f"mac-app:{preview['preview_id']}",
                source_admissions.c.version_id == library_items.c.active_source_version_id,
                library_items.c.id == sibling)).first()
    with open_db(config.paths.db_path) as connection:
        work = load_library_item_evidence_states(connection, item)["media_fingerprint"]
        assert work["work_status"] not in EVIDENCE_WORK_TERMINAL_STATUSES and work["attempt_count"] == 0
        assert work["last_error"] in original_check_wait_reasons(connection)
        assert not connection.execute(select(item_events).where(item_events.c.event_type == "original_confirmation_failed")).first()
        connection.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item).values(retry_not_before=None))
    assert _work(config) > 0
    assert receipt.read_bytes() == saved
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).availability == "available"
        assert len(_owners(connection)) == 2


def test_recovered_same_revision_correction_clears_the_old_failure(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES

    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(first)
    with monkeypatch.context() as rules:
        rules.setitem(HISTORICAL_OUTPUT_PROFILES, "version", "fixture changed rule")
        registration.apply_confirmed_originals(config, item, ManagedProcessController(), lambda: None, nullcontext)
    assert registration.list_original_previews(config)[0]["problems"]
    second = registration.build_original_preview(config, [item], assertion="Recovered original-file checks")
    assert json.loads(second["canonical_json"])["items"][0]["source_revision"] == json.loads(first["canonical_json"])["items"][0]["source_revision"]
    _confirm(second)
    _apply_preview(config, second["preview_id"], second["manifest_hash"])
    assert registration.list_original_previews(config) == []


def test_conflicting_result_blocks_only_its_member_before_any_hash(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    from uuid import uuid4
    from mediaforce.core.evidence import stable_json_hash
    from test_source_intake import _work

    config, first, path = originals
    second, _other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent acquisitions")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        generation = uuid4().hex
        connection.execute(source_generations.insert().values(generation_id=generation, created_at=timestamp()))
        unrelated = append_version(connection, generation_id=generation, role="input", identity=observe_file(path, full_digest=True), profile={}, recorded_at=timestamp())
        connection.execute(source_admissions.insert().values(
            admission_id=stable_json_hash({"preview": preview["preview_id"], "item": first}), generation_id=generation,
            version_id=unrelated, basis="owner", tier="proven", checks_json="{}", confirmation_ref=f"mac-app:{preview['preview_id']}", recorded_at=timestamp()))
    assert registration.list_original_previews(config)[0]["problems"]
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with monkeypatch.context() as no_hash:
        def forbidden(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise AssertionError("A conflicting result must be refused before reading content")
        no_hash.setattr("mediaforce.library.original_registration.source_evidence.observe_file", forbidden)
        registration.apply_confirmed_originals(config, first, ManagedProcessController(), lambda: None, nullcontext)
        with pytest.raises(ValueError):
            registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=first)
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, second).availability == "available"
        active = connection.execute(select(library_items.c.active_source_version_id).where(library_items.c.id == first)).scalar_one()
        assert active != unrelated


def test_bad_receipt_date_does_not_stop_another_files_confirmation(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from contextlib import nullcontext

    config, first, _path = originals
    second, _other = _source(config, "second")
    damaged = registration.build_original_preview(config, [first], assertion="Independent acquisition")
    valid = registration.build_original_preview(config, [second], assertion="Independent acquisition")
    _confirm(damaged)
    _confirm(valid)
    path = registration.confirmation_directory() / f"{damaged['preview_id']}.json"
    receipt = json.loads(path.read_text())
    receipt["confirmed_at"] = "2026-11-03T12:00:00"
    path.write_text(json.dumps(receipt))
    registration.apply_confirmed_originals(config, second, ManagedProcessController(), lambda: None, nullcontext)
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1
        assert load_source_evidence(connection, second).availability == "available"
    assert registration.list_original_previews(config)[0]["problems"]


@pytest.mark.parametrize("cancelled", [False, True])
def test_saved_confirmation_never_restarts_a_paused_or_cancelled_batch(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch, cancelled: bool) -> None:
    from mediaforce.library.evidence_queue import cancel_evidence_queue, load_evidence_queue_state, pause_evidence_queue, resume_evidence_queue
    from mediaforce.library.evidence_worker import default_evidence_worker_deps
    from mediaforce.web.runtime.operator_work import BoundedEvidenceRunner
    from test_source_intake import _work

    config, item, _path = originals
    preview = _confirmed_av1_preview(config, item, monkeypatch)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    deps = default_evidence_worker_deps()
    deps.load_config = lambda _path: config
    deps.evidence_host_rows = lambda _config: []
    runner = BoundedEvidenceRunner(config.paths.config_path, deps=deps)
    with open_db(config.paths.db_path) as connection:
        if cancelled:
            cancel_evidence_queue(connection)
        else:
            pause_evidence_queue(connection)
    runner.prepare_background_work()
    assert _work(config) == 0
    with open_db(config.paths.db_path) as connection:
        state = load_evidence_queue_state(connection)
        assert state["cancel_requested"] if cancelled else state["is_paused"]
        assert not _owners(connection)
        if cancelled:
            waiting = registration.list_original_previews(config)
            assert len(waiting) == 1 and waiting[0]["waiting"]
            assert waiting[0]["canonical_json"] == preview["canonical_json"]
            return
        resume_evidence_queue(connection)
    runner.prepare_background_work()
    assert _work(config) > 0
    assert not registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])


def test_transient_catalog_absence_does_not_invalidate_the_saved_approval(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import item_events
    from test_source_intake import _work

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(missing_since=timestamp()))
    assert not registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert not connection.execute(select(item_events).where(item_events.c.event_type == "original_confirmation_failed")).first()
        connection.execute(update(library_items).where(library_items.c.id == item).values(missing_since=None))
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert len(_owners(connection)) == 1


@pytest.mark.parametrize("dismissed", [False, True])
def test_restored_approval_resumes_checks_after_wait_or_dismissal(originals: tuple[MediaforceConfig, int, Path], dismissed: bool) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue
    from test_source_intake import _work

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    saved = receipt.read_bytes()
    receipt.unlink()
    _run_unspent_approval_wait(config, item)
    if dismissed:
        _dismiss_preview(registration.list_original_previews(config)[0])
        assert _work(config) > 0
        with open_db(config.paths.db_path) as connection:
            work = connection.execute(select(library_item_evidence_state)).mappings().one()
            assert work["work_status"] == "completed" and work["last_error"] is None
            assert queue_original_intake(connection, config) == 0
        assert _work(config) == 0
        assert registration.list_original_previews(config) == []
    receipt.write_bytes(saved)
    if dismissed:
        with open_db(config.paths.db_path) as connection:
            rebuild_library_item_evidence_states(connection)
            start_evidence_work(connection, config, "tv/source.mkv")
            resume_evidence_queue(connection)
    assert _work(config) > 0
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        evidence = load_source_evidence(connection, item)
        assert evidence.availability == "available" and all(trait.tier == "probable" for trait in evidence.traits.values())
        assert len(_owners(connection)) == 1
    assert registration.list_original_previews(config) == []


@pytest.mark.parametrize("persisted", [False, True])
def test_pending_physical_path_blocks_approval_and_reads_across_version_aliases(originals: tuple[MediaforceConfig, int, Path], persisted: bool) -> None:
    from mediaforce.core.evidence import stable_json_hash
    from mediaforce.encoding.source_publication import prepare_archive_removal
    from mediaforce.library.source_evidence import json_text

    config, item, path = originals
    _confirmed_publish(originals)
    with open_db(config.paths.db_path) as connection:
        archived_path = load_source_evidence(connection, item).source_path
        assert archived_path is not None
    path.unlink()
    path.symlink_to(archived_path)
    _rescan(config, item, path)
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    member = json.loads(preview["canonical_json"])["items"][0]
    with open_db(config.paths.db_path) as connection:
        assert prepare_archive_removal(connection, Path(archived_path))
        if persisted:
            append_version(connection, generation_id=member["generation_id"], role="input", identity=member["identity"],
                           profile={"video_codec": "h264"}, recorded_at=timestamp(), version_id=member["version_id"])
            connection.execute(source_admissions.insert().values(
                admission_id=stable_json_hash({"preview": preview["preview_id"], "item": item}), generation_id=member["generation_id"],
                version_id=member["version_id"], tier="proven", basis="owner", supersedes_id=member["supersedes_id"],
                confirmation_ref=f"mac-app:{preview['preview_id']}", recorded_at=timestamp(),
                checks_json=json_text({"source_checks": member["checks"], "preview_hash": preview["manifest_hash"], "measurement_ids": member["measurement_ids"]})))
            connection.execute(update(library_items).where(library_items.c.id == item).values(active_source_version_id=member["version_id"]))
    if not persisted:
        with pytest.raises(registration.OriginalApprovalWaiting):
            registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=item)
    with open_db(config.paths.db_path) as connection:
        evidence = load_source_evidence(connection, item)
        assert evidence.source_path is None and evidence.availability != "available"


@pytest.mark.parametrize("admission", ["unassessed", "readable", "owner"])
@pytest.mark.parametrize("stage", ["before", "during", "final"])
def test_scan_confirmed_absence_finishes_and_same_bytes_resume(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch,
                                                             admission: str, stage: str) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library import evidence_worker
    from test_source_intake import _work, _summary

    config, item, path = originals
    if admission == "readable":
        assess_source_input(config, item, ManagedProcessController(), lambda: None)
    elif admission == "owner":
        preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
        _confirm(preview)
        _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        version = connection.execute(select(library_items.c.active_source_version_id)).scalar_one()
        owner_ids = [row["admission_id"] for row in _owners(connection)]
    old_inode = path.stat().st_ino
    removed = path.with_suffix(".absent")

    def scan_absence() -> None:
        path.rename(removed)
        with open_db(config.paths.db_path) as database:
            database.execute(update(library_items).where(library_items.c.id == item).values(missing_since=timestamp()))

    def analyze(_path: Path, kind: str, **_kwargs: Any) -> dict[str, Any]:
        if stage == "during":
            scan_absence()
            raise OSError("The source disappeared during analysis")
        return _summary(kind)

    with open_db(config.paths.db_path) as connection:
        from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue, EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
        rebuild_library_item_evidence_states(connection)
        start_evidence_work(connection, config, "tv/source.mkv", evidence_kinds=["media_fingerprint"])
        resume_evidence_queue(connection)
        connection.execute(update(library_item_evidence_state).values(work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE))
    if stage == "before":
        scan_absence()
    fingerprint = evidence_worker._source_fingerprint
    calls = 0

    def disappear_at_final_check(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            scan_absence()
        return fingerprint(*args)

    with monkeypatch.context() as patch_final:
        if stage == "final":
            patch_final.setattr(evidence_worker, "_source_fingerprint", disappear_at_final_check)
        assert _work(config, analyze) > 0
    with open_db(config.paths.db_path) as connection:
        work = load_library_item_evidence_states(connection, item)["media_fingerprint"]
        assert work["work_status"] == "completed" and work["last_error"] and work["attempt_count"] == 0
        assert queue_original_intake(connection, config) == 0
    _work(config)
    assert _work(config) == 0
    path.write_bytes(removed.read_bytes())
    assert path.stat().st_ino != old_inode
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(missing_since=None))
    _rescan(config, item, path)
    with open_db(config.paths.db_path) as connection:
        assert queue_original_intake(connection, config) > 0
    assert _work(config) > 0
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        evidence = load_source_evidence(connection, item)
        assert all(trait.tier == "probable" for trait in evidence.traits.values())
        if admission != "unassessed":
            assert evidence.original_version_id == version
        assert [row["admission_id"] for row in _owners(connection)] == owner_ids
        assert not connection.execute(select(library_item_evidence_state.c.last_error).where(
            library_item_evidence_state.c.last_error.is_not(None))).first()
        assert queue_original_intake(connection, config) == 0


def test_waiting_owner_restoration_does_not_starve_next_discovery(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue, EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
    from test_source_intake import _work

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    (registration.confirmation_directory() / f"{preview['preview_id']}.json").unlink()
    sibling, _other = _source(config, "sibling")
    with open_db(config.paths.db_path) as connection:
        rebuild_library_item_evidence_states(connection)
        start_evidence_work(connection, config, "tv/source.mkv")
        resume_evidence_queue(connection)
        connection.execute(update(library_item_evidence_state).values(
            work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE, work_status="completed", last_error="File was missing"))
        assert queue_original_intake(connection, config, limit=1) > 0
    assert _work(config) > 0
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert queue_original_intake(connection, config, limit=1) > 0
        assert connection.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == sibling)).first()
        for work in connection.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item)).mappings():
            assert work["work_status"] not in EVIDENCE_WORK_TERMINAL_STATUSES and work["attempt_count"] == 0


def test_owner_rule_change_settles_without_requeue_or_retry_instruction(originals: tuple[MediaforceConfig, int, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue, EVIDENCE_WORK_REASON_ORIGINAL_SOURCE
    from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES
    from test_source_intake import _work

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        rebuild_library_item_evidence_states(connection)
        start_evidence_work(connection, config, "tv/source.mkv")
        resume_evidence_queue(connection)
        connection.execute(update(library_item_evidence_state).values(work_reason=EVIDENCE_WORK_REASON_ORIGINAL_SOURCE))
    monkeypatch.setitem(HISTORICAL_OUTPUT_PROFILES, "version", "changed test rule")
    assert _work(config) > 0
    assert _work(config) > 0
    assert _work(config) == 0
    with open_db(config.paths.db_path) as connection:
        assert {trait.reason for trait in load_source_evidence(connection, item).traits.values()} == {"original_rule_changed"}
        assert queue_original_intake(connection, config) == 0
        assert not connection.execute(select(library_item_evidence_state).where(library_item_evidence_state.c.last_error.is_not(None))).first()
    problems = registration.list_original_previews(config)
    assert len(problems) == 1 and problems[0]["problems"]
    assert all("retry" not in problem["reason"].lower() for problem in problems[0]["problems"])


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_unavailable_preview_member_stays_visible(originals: tuple[MediaforceConfig, int, Path], changed: bool, cancelled: bool) -> None:
    from mediaforce.library.evidence_queue import cancel_evidence_queue

    config, item, _path = originals
    sibling, _other = _source(config, "sibling")
    preview = registration.build_original_preview(config, [item, sibling], assertion="Independent acquisitions")
    if cancelled:
        _confirm(preview)
        assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        if cancelled:
            cancel_evidence_queue(connection)
        connection.execute(update(library_items).where(library_items.c.id == item).values(
            **({"source_revision": library_items.c.source_revision + 1} if changed else {"missing_since": timestamp()})))
    listed = registration.list_original_previews(config)
    assert len(listed) == 1 and listed[0]["canonical_json"] == preview["canonical_json"]
    assert listed[0]["problems" if changed else "waiting"]


@pytest.mark.parametrize("stage", ["preview", "commit"])
def test_pending_move_is_checked_before_preview_and_under_commit_lock(originals: tuple[MediaforceConfig, int, Path], stage: str) -> None:
    from mediaforce.encoding.source_publication import prepare_archive_removal

    config, item, path = originals
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    if stage == "preview":
        with open_db(config.paths.db_path) as connection:
            assert prepare_archive_removal(connection, path)
        with pytest.raises(ValueError):
            registration.build_original_preview(config, [item], assertion="Independent acquisition")
    else:
        preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
        _confirm(preview)
        def start_move(database: DBClient) -> None:
            assert prepare_archive_removal(database, path)
        with pytest.raises(registration.OriginalApprovalWaiting):
            _apply_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=start_move)
    with open_db(config.paths.db_path) as connection:
        assert not _owners(connection)


def test_pending_move_resolves_stored_and_requested_path_aliases(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import source_moves
    from mediaforce.encoding.source_publication import path_in_pending_move, prepare_archive_removal

    config, item, path = originals
    assess_source_input(config, item, ManagedProcessController(), lambda: None)
    alias = path.parent.with_name("source-alias")
    alias.symlink_to(path.parent, target_is_directory=True)
    alias_path = alias / path.name
    with open_db(config.paths.db_path) as connection:
        assert prepare_archive_removal(connection, path)
        connection.execute(update(source_moves).where(source_moves.c.from_path == str(path.resolve())).values(from_path=str(alias_path)))
        assert path_in_pending_move(connection, str(path))
        assert path_in_pending_move(connection, str(alias_path))
        evidence = load_source_evidence(connection, item)
        assert evidence.availability != "available" and evidence.waiting_reason


@pytest.mark.parametrize("unchecked_request", [False, True])
def test_dismissal_never_hides_another_members_later_problem(originals: tuple[MediaforceConfig, int, Path], unchecked_request: bool,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from mediaforce.library.evidence_queue import start_evidence_work, resume_evidence_queue
    from mediaforce.library.source_evidence import HISTORICAL_OUTPUT_PROFILES
    from test_source_intake import _work

    config, item, _path = originals
    sibling, _other = _source(config, "sibling")
    preview = registration.build_original_preview(config, [item, sibling], assertion="Independent acquisitions")
    if unchecked_request:
        registration.confirmation_directory().mkdir(parents=True)
        _dismiss_preview(preview)
    _confirm(preview)
    assert registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=item)
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == sibling).values(source_revision=library_items.c.source_revision + 1))
    registration.queue_original_approval(config, preview["preview_id"], preview["manifest_hash"])
    shown = registration.list_original_previews(config)
    assert len(shown) == 1 and {problem["item_id"] for problem in shown[0]["problems"]} == {sibling}
    with open_db(config.paths.db_path) as connection:
        from mediaforce.core.db_tables import item_events
        recorded = connection.execute(select(item_events.c.details_json).where(
            item_events.c.library_item_id == sibling, item_events.c.event_type == "original_confirmation_failed")).scalar_one()
        assert shown[0]["problems"][0]["reason"] == json.loads(recorded)["reason"]
    _dismiss_preview(shown[0])
    assert registration.list_original_previews(config) == []
    receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
    saved = receipt.read_bytes()
    receipt.unlink()
    shown = registration.list_original_previews(config)
    assert len(shown) == 1 and {problem["item_id"] for problem in shown[0]["problems"]} == {item}
    _run_unspent_approval_wait(config, item)
    _dismiss_preview(shown[0])
    for _ in range(4):
        _work(config)
    with open_db(config.paths.db_path) as connection:
        completed = load_library_item_evidence_states(connection, item)["media_fingerprint"]
        assert completed["work_status"] == "completed" and completed["last_error"] is None
    assert registration.list_original_previews(config) == []
    receipt.write_bytes(saved)
    with open_db(config.paths.db_path) as connection:
        rebuild_library_item_evidence_states(connection)
        start_evidence_work(connection, config, "tv/source.mkv")
        resume_evidence_queue(connection)
    assert _work(config) > 0
    assert _work(config) > 0
    with open_db(config.paths.db_path) as connection:
        assert all(trait.tier == "probable" for trait in load_source_evidence(connection, item).traits.values())
        assert len(_owners(connection)) == 1
    monkeypatch.setitem(HISTORICAL_OUTPUT_PROFILES, "version", "changed test rules")
    changed = registration.list_original_previews(config)
    assert len(changed) == 1 and {problem["item_id"] for problem in changed[0]["problems"]} == {item}


def test_dismissal_matches_member_and_reason_independently_of_path_metadata(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    registration.confirmation_directory().mkdir(parents=True)
    preview["problems"] = [{"item_id": item, "rel_path": "old/name", "reason": "The saved approval needs restoring"}]
    _dismiss_preview(preview)
    assert registration.original_request_dismissed(preview["preview_id"], preview["manifest_hash"],
                                                  [{**preview["problems"][0], "rel_path": "new/name"}])
    assert not registration.original_request_dismissed(preview["preview_id"], preview["manifest_hash"],
                                                      [{"item_id": item, "reason": "A different problem"}])


def test_native_dismissal_fixture_uses_the_backend_marker_schema(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    fixture = json.loads((Path(__file__).parent / "fixtures" / "original-dismissal.json").read_text())
    directory = registration.confirmation_directory()
    directory.mkdir(parents=True)
    (directory / f"{preview['preview_id']}.dismissed").write_text(json.dumps(fixture))
    assert registration.original_request_dismissed(preview["preview_id"], fixture["manifest_hash"], fixture["problems"])
    assert not registration.original_request_dismissed(preview["preview_id"], preview["manifest_hash"], fixture["problems"])


def test_unchecked_request_dismissal_preserves_prior_problem_acknowledgements(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    sibling, _other = _source(config, "sibling")
    preview = registration.build_original_preview(config, [item, sibling], assertion="Independent acquisitions")
    _confirm(preview)
    registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], item_id=item)
    (registration.confirmation_directory() / f"{preview['preview_id']}.json").unlink()
    shown = registration.list_original_previews(config)
    assert len(shown) == 1 and {problem["item_id"] for problem in shown[0]["problems"]} == {item}
    _dismiss_preview(shown[0])
    unchecked = registration.list_original_previews(config)
    assert len(unchecked) == 1 and not unchecked[0].get("problems")
    _dismiss_preview(unchecked[0])
    assert registration.list_original_previews(config) == []
    with open_db(config.paths.db_path) as connection:
        assert load_source_evidence(connection, item).waiting_reason is None
        connection.execute(update(library_items).where(library_items.c.id == sibling).values(source_revision=library_items.c.source_revision + 1))
    changed = registration.list_original_previews(config)
    assert len(changed) == 1 and {problem["item_id"] for problem in changed[0]["problems"]} == {sibling}


@pytest.mark.parametrize("change", ["confirmation", "move"])
def test_library_reuse_observes_native_confirmation_and_original_move(originals: tuple[MediaforceConfig, int, Path], change: str) -> None:
    from mediaforce.core.db_tables import series_metadata, source_moves
    from mediaforce.library.calculation_reuse import LibraryCalculations
    from mediaforce.library.content_kinds import season_kind_report

    config, item, _path = originals
    with open_db(config.paths.db_path) as connection:
        connection.execute(library_items.update().where(library_items.c.id == item).values(rel_path="tv/Show/Season 1/episode.mkv"))
        connection.execute(series_metadata.insert().values(series_prefix="tv/Show", tmdb_genres_json='["Drama"]', updated_at="now"))
    preview = registration.build_original_preview(config, [item], assertion="Independent originals")
    _confirm(preview)
    _apply_preview(config, preview["preview_id"], preview["manifest_hash"])
    calculations = LibraryCalculations()
    with open_db(config.paths.db_path) as connection:
        def report() -> dict[str, Any]:
            with calculations.request():
                return season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _: False)

        initial = report()
        assert report() == initial
        if change == "confirmation":
            receipt = registration.confirmation_directory() / f"{preview['preview_id']}.json"
            saved = receipt.read_bytes()
            receipt.unlink()
        else:
            original = connection.execute(select(source_admissions.c.version_id).where(source_admissions.c.basis == "owner")).scalar_one()
            connection.execute(source_transitions.insert().values(operation_id="move", before_version_id=original,
                after_version_id=original, expected_revision=0, kind="move", outcome="prepared", evidence_json="{}", recorded_at="now"))
            connection.execute(source_moves.insert().values(operation_id="move", ordinal=0, version_id=original,
                from_path="before.mkv", to_path="after.mkv", phase="move", checks_json="{}"))
        changed = report()
        assert changed != initial
        with LibraryCalculations().request():
            assert season_kind_report(connection, library_types={"tv": "tv"}, folder_approved=lambda _: False) == changed
        if change == "confirmation":
            receipt.write_bytes(saved)
        else:
            connection.execute(source_transitions.update().where(source_transitions.c.operation_id == "move").values(outcome="committed"))
        assert report() == initial
