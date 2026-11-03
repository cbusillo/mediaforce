import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update

from mediaforce.core.db import open_db
from mediaforce.core.db import DBClient
from mediaforce.core.db_tables import library_items, source_admissions, source_generations, source_transitions, source_versions, staged_artifacts
from mediaforce.core.config import MediaforceConfig
from mediaforce.core.process_control import ManagedProcessController
from mediaforce.core.utils import content_version_fingerprint, file_fingerprint, timestamp
from mediaforce.encoding.free_space import ReservePreflight
from mediaforce.encoding.put_back import PutBackRefused
from mediaforce.encoding.staging import promote_one_item
from mediaforce.library import original_registration as registration
from mediaforce.library.source_evidence import append_version, ensure_active_source_version, load_source_evidence, observe_file, reconcile_source_replacement
from mediaforce.library.source_intake import assess_source_input, queue_original_intake
from mediaforce.web.routes.originals import register_original_routes
from test_source_evidence import _config, _probe as _media_probe, _put_back
from test_source_intake import _probe, _source
from test_web_route_security import _json_request, _route_endpoint


@pytest.fixture
def originals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MediaforceConfig, int, Path]:
    config = _config(tmp_path)
    monkeypatch.setattr(registration, "confirmation_directory", lambda: tmp_path / "app-approvals")
    monkeypatch.setattr(registration, "probe_evidence_source", lambda *_args, **_kwargs: _probe())
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


def test_preview_cannot_apply_without_native_confirmation(originals: tuple[Any, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independently acquired original")
    with pytest.raises(ValueError):
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None
    _confirm(preview)
    first = registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
    assert registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"]) == first
    with open_db(config.paths.db_path) as connection:
        admission = connection.execute(select(source_admissions)).mappings().one()
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
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None


def test_bulk_revision_race_rolls_back_every_admission(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, first, _path = originals
    second, _other = _source(config, "second")
    preview = registration.build_original_preview(config, [first, second], assertion="Independent originals")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        versions = set(connection.execute(select(source_versions.c.version_id)).scalars())

    def replace(connection: DBClient) -> None:
        connection.execute(update(library_items).where(library_items.c.id == second).values(source_revision=4))

    with pytest.raises(ValueError):
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=replace)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None
        assert set(connection.execute(select(source_versions.c.version_id)).scalars()) == versions


def test_corrections_append_and_copied_native_reference_cannot_authorize_other_version(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(first)
    first_id = registration.apply_original_preview(config, first["preview_id"], first["manifest_hash"])["admission_ids"][0]
    second = registration.build_original_preview(config, [item], assertion="Corrected acquisition details")
    _confirm(second)
    second_id = registration.apply_original_preview(config, second["preview_id"], second["manifest_hash"])["admission_ids"][0]
    with open_db(config.paths.db_path) as connection:
        latest = connection.execute(select(source_admissions).where(source_admissions.c.admission_id == second_id)).mappings().one()
        assert latest["supersedes_id"] == first_id
        assert len(connection.execute(select(source_admissions)).mappings().all()) == 2
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
    monkeypatch.setattr(registration, "probe_evidence_source", lambda *_args, **_kwargs: probe)
    with pytest.raises(ValueError):
        registration.build_original_preview(config, [item], assertion="Independent acquisition")
    if location == "codec":
        preview = registration.build_original_preview(config, [item], assertion="Independent AV1 acquisition",
                                                      overrides={item: "Downloaded this AV1 file independently"})
        assert json.loads(preview["canonical_json"])["items"][0]["checks"]["exception_matches"]
    else:
        with pytest.raises(ValueError):
            registration.build_original_preview(config, [item], assertion="Independent acquisition", overrides={item: "Exception"})


def test_receipt_is_required_and_browser_origin_and_extra_fields_are_rejected(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    body = {"preview_id": preview["preview_id"], "manifest_hash": preview["manifest_hash"]}
    app = FastAPI()
    register_original_routes(app, config)
    endpoint = _route_endpoint(app, "/api/app/originals/apply", "POST")

    def post(payload: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None) -> int:
        request = _json_request(payload)
        request.scope["client"] = ("127.0.0.1", 12345)
        request.scope["headers"] += headers or []
        return asyncio.run(endpoint(request)).status_code

    assert post(body, [(b"x-mediaforce-app", b"true")]) == 409
    _confirm(preview)
    assert post(body, [(b"origin", b"http://localhost")]) == 409
    assert post({**body, "actor": "owner", "session": "trusted"}) == 409
    assert post(body) == 202
    from test_source_intake import _work

    assert _work(config) > 0
    assert post(body) == 200
    assert registration.list_original_previews(config) == []


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
    registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
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
            assert connection.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 1
            assert load_source_evidence(connection, item).original_version_id == original


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
        assert connection.execute(select(func.count()).select_from(source_admissions)).scalar_one() == 1


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


def test_reused_catalog_id_cannot_take_a_preview_for_the_deleted_item(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        old = dict(connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one())
        connection.execute(library_items.delete().where(library_items.c.id == item))
        connection.execute(library_items.insert().values(**{**old, "active_source_version_id": None, "source_revision": 0}))
    with pytest.raises(ValueError):
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
    assert path.exists()
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None


def test_dismissed_or_unreadable_preview_does_not_break_other_requests(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    first = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    second = registration.build_original_preview(config, [item], assertion="Updated acquisition details")
    directory = registration.confirmation_directory()
    directory.mkdir(parents=True)
    (directory / f"{first['preview_id']}.dismissed").write_text(first["manifest_hash"])
    assert [preview["preview_id"] for preview in registration.list_original_previews(config)] == [second["preview_id"]]
    (config.paths.web_state_dir / "original-previews" / f"{second['preview_id']}.json").write_text("{")
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
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], before_commit=retarget)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None


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
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
        with open_db(config.paths.db_path) as connection:
            after = load_source_evidence(connection, item)
            assert after.original_version_id == before.original_version_id
            assert {kind: trait.measurement_ids for kind, trait in after.traits.items()} == {
                kind: trait.measurement_ids for kind, trait in before.traits.items()}
            assert all(trait.tier == "probable" for trait in after.traits.values())


def test_stale_preview_no_longer_requests_owner_attention(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    registration.build_original_preview(config, [item], assertion="Independent acquisition")
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(source_revision=50))
    assert registration.list_original_previews(config) == []


def test_cancelled_background_recheck_never_commits_an_admission(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.process_control import ProcessCancelledError

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    def cancel() -> None:
        raise ProcessCancelledError("Stopped")
    with pytest.raises(ProcessCancelledError):
        registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"], check_cancelled=cancel)
    with open_db(config.paths.db_path) as connection:
        assert connection.execute(select(source_admissions)).first() is None


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
    with open_db(config.paths.db_path) as connection:
        connection.execute(update(library_items).where(library_items.c.id == item).values(video_codec="av1"))
    monkeypatch.setattr(registration, "probe_evidence_source", lambda *_args, **_kwargs: _probe(codec="av1"))
    preview = registration.build_original_preview(config, [item], assertion="Independent AV1 acquisition",
                                                  overrides={item: "Independent AV1 download"})
    _confirm(preview)
    registration.apply_original_preview(config, preview["preview_id"], preview["manifest_hash"])
    with open_db(config.paths.db_path) as connection:
        assert queue_original_intake(connection, config) > 0


def test_known_output_digest_cannot_be_waived_by_a_format_exception(originals: tuple[MediaforceConfig, int, Path],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    config, item, path = originals
    _original, _output, _bytes = _confirmed_publish(originals)
    monkeypatch.setattr(registration, "probe_evidence_source", lambda *_args, **_kwargs: _probe(codec="av1"))
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
    original_observe = registration.observe_file
    both_reading = Barrier(2, timeout=10)
    def read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_observe(*args, **kwargs)
        both_reading.wait()
        return result
    monkeypatch.setattr(registration, "observe_file", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registration.apply_original_preview, config, preview["preview_id"], preview["manifest_hash"])
        second = executor.submit(registration.apply_original_preview, config, preview["preview_id"], preview["manifest_hash"])
        assert first.result(timeout=20) == second.result(timeout=20)
    with open_db(config.paths.db_path) as connection:
        assert len(connection.execute(select(source_admissions)).all()) == 1


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


def test_a_fresh_confirmation_can_retry_older_failed_evidence_work(originals: tuple[MediaforceConfig, int, Path]) -> None:
    from mediaforce.core.db_tables import library_item_evidence_state
    from mediaforce.library.evidence_state import sync_library_item_evidence_state

    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    with open_db(config.paths.db_path) as connection:
        current = dict(connection.execute(select(library_items).where(library_items.c.id == item)).mappings().one())
        sync_library_item_evidence_state(connection, current, "media_fingerprint")
        connection.execute(update(library_item_evidence_state).where(library_item_evidence_state.c.library_item_id == item).values(
            work_status="failed", last_attempt_at="2025-01-01T00:00:00+00:00", updated_at=timestamp(), last_error="Earlier check failed"))
    assert registration.request_original_apply(config, preview["preview_id"], preview["manifest_hash"])["pending"]
    from test_source_intake import _work

    assert _work(config) > 0
    assert not registration.request_original_apply(config, preview["preview_id"], preview["manifest_hash"])["pending"]


def test_saved_confirmation_requeues_after_a_lost_request_and_stops_asking_owner(originals: tuple[MediaforceConfig, int, Path]) -> None:
    config, item, _path = originals
    preview = registration.build_original_preview(config, [item], assertion="Independent acquisition")
    _confirm(preview)
    assert registration.list_original_previews(config, unconfirmed_only=True) == []
    assert len(registration.list_original_previews(config)) == 1
    from test_source_intake import _work
    from mediaforce.library.evidence_worker import _discover_originals, default_evidence_worker_deps

    _discover_originals(config, default_evidence_worker_deps())
    assert _work(config) > 0
    assert not registration.request_original_apply(config, preview["preview_id"], preview["manifest_hash"])["pending"]
