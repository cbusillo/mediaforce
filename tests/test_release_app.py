from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
import plistlib
import subprocess
from unittest.mock import patch
from xml.etree import ElementTree

import pytest

from scripts import release_app
from scripts.release_app import (
    SPARKLE, appcast, feed_offer, newest_published, next_build, release_identity, require_listed_digest, verify_release,
)

PUBLISHED = datetime(2026, 11, 17, 21, 0, tzinfo=UTC)


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "Mediaforce.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleVersion": "7", "CFBundleShortVersionString": "0.2.0", "LSMinimumSystemVersion": "15.0"}))
    archive = tmp_path / "Mediaforce-0.2.0.zip"
    archive.write_bytes(b"notarized archive bytes")
    return app, archive


def build_feed(release: tuple[Path, Path], previous: str | None = None) -> bytes:
    app, archive = release
    return appcast(app, archive, "c2lnbmF0dXJl", "https://example.invalid/Mediaforce-0.2.0.zip",
                   "https://example.invalid/notes", previous, PUBLISHED)


def test_feed_offers_the_built_app_as_sparkle_reads_it(release: tuple[Path, Path]) -> None:
    item = ElementTree.fromstring(build_feed(release)).find("channel/item")

    assert item is not None
    assert item.findtext(f"{{{SPARKLE}}}version") == "7"
    assert item.findtext(f"{{{SPARKLE}}}shortVersionString") == "0.2.0"
    assert item.findtext(f"{{{SPARKLE}}}minimumSystemVersion") == "15.0"
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.get("length") == str(release[1].stat().st_size)
    assert enclosure.get(f"{{{SPARKLE}}}edSignature") == "c2lnbmF0dXJl"
    assert enclosure.get("url") == "https://example.invalid/Mediaforce-0.2.0.zip"


@pytest.mark.parametrize("live_build", ["7", "8"])
def test_feed_refuses_to_replace_one_offering_the_same_or_a_newer_build(
    release: tuple[Path, Path], live_build: str
) -> None:
    live = build_feed(release).decode().replace("<sparkle:version>7<", f"<sparkle:version>{live_build}<")

    with pytest.raises(ValueError, match=f"already offers build {live_build}"):
        build_feed(release, live)


def test_feed_replaces_an_older_one(release: tuple[Path, Path]) -> None:
    older = build_feed(release).decode().replace("<sparkle:version>7<", "<sparkle:version>6<")

    assert newest_published(build_feed(release, older).decode()) == release_app.PublishedBuild(7, "0.2.0")


@pytest.mark.parametrize(("live", "expected"), [(None, 4), (("3", "0.1.0"), 4), (("1", "0.0.9"), 4), (("8", "0.1.9"), 9)])
def test_build_is_one_above_the_live_feed_and_never_reuses_a_hand_numbered_one(
    release: tuple[Path, Path], live: tuple[str, str] | None, expected: int
) -> None:
    previous = None
    if live is not None:
        previous = build_feed(release).decode().replace("<sparkle:version>7<", f"<sparkle:version>{live[0]}<").replace(
            "<sparkle:shortVersionString>0.2.0<", f"<sparkle:shortVersionString>{live[1]}<")

    with patch.object(subprocess, "check_output", return_value=""):
        assert next_build("0.2.0", previous) == expected


@pytest.mark.parametrize("previous", [None, "<rss><channel/></rss>"])
def test_missing_build_history_refuses_existing_releases(previous: str | None) -> None:
    with patch.object(subprocess, "check_output", return_value="v0.1.0"), pytest.raises(ValueError, match="v0.1.0"):
        next_build("0.2.0", previous)


def test_served_feed_must_offer_candidate_build(release: tuple[Path, Path]) -> None:
    feed = build_feed(release).decode()
    release_app.require_served_build(feed, 7)
    with pytest.raises(ValueError, match="8"):
        release_app.require_served_build(feed, 8)


@pytest.mark.parametrize("live_version", ["0.2.0", "0.10.0", "1.0.0"])
def test_build_numbering_refuses_a_version_that_is_not_higher_than_the_published_one(
    release: tuple[Path, Path], live_version: str
) -> None:
    previous = build_feed(release).decode().replace("<sparkle:shortVersionString>0.2.0<",
                                                    f"<sparkle:shortVersionString>{live_version}<")

    with pytest.raises(ValueError, match=f"not higher than the published {live_version}"):
        next_build("0.2.0", previous)


@pytest.mark.parametrize("version", ["0.4", "0.4.0-3", "v0.4.0", "1.0"])
def test_release_identity_refuses_a_version_that_is_not_semantic(tmp_path: Path, version: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nversion = "{version}"\n[tool.mediaforce.release]\nfeed = "f"\npublic-key = "k"\n')

    with pytest.raises(ValueError, match="MAJOR.MINOR.PATCH"):
        release_identity(pyproject)


def test_release_identity_names_its_tag_and_archive_after_the_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "0.4.1"\n[tool.mediaforce.release]\nfeed = "f"\npublic-key = "k"\n')

    identity = release_identity(pyproject)

    assert (identity["tag"], identity["archive"]) == ("v0.4.1", "Mediaforce-0.4.1.zip")


PROOF_KEY = "cHJvb2Yta2V5"


@pytest.fixture
def published(tmp_path: Path) -> Path:
    folder = tmp_path / "published"
    app = folder / "built/Mediaforce.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleVersion": "7", "CFBundleShortVersionString": "0.2.0", "LSMinimumSystemVersion": "15.0",
        "SUFeedURL": str(folder / "appcast.xml"), "SUPublicEDKey": PROOF_KEY, "MediaforceServiceLabel": "com.mediaforce.web.proof",
    }))
    archive = folder / "Mediaforce-0.2.0.zip"
    archive.write_bytes(b"notarized archive bytes")
    (folder / "SHA256SUMS").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    (folder / "appcast.xml").write_bytes(appcast(app, archive, "c2lnbmF0dXJl", f"https://example.invalid/{archive.name}",
                                                 "https://example.invalid/notes", None, PUBLISHED))
    return folder


def _verify(published: Path, tmp_path: Path, *, proof: bool = True, archive: str = "Mediaforce-0.2.0.zip") -> release_app.VerifiedRelease:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    signed: list[str] = []
    with patch.object(release_app, "require_signature", side_effect=lambda _key, signature, _archive: signed.append(signature)), \
            patch.object(release_app, "notarized_app", return_value=published / "built/Mediaforce.app") as notarized:
        verified = verify_release(str(published / archive), scratch, feed=str(published.resolve() / "appcast.xml"),
                                  public_key=PROOF_KEY, proof=proof)
    assert signed == ["c2lnbmF0dXJl"] and notarized.called
    return verified


def test_local_archive_verifies_with_its_bare_filename(published: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(published)
    assert _verify(Path("."), tmp_path).identity["CFBundleVersion"] == "7"


def test_tag_names_its_archive_on_github_releases() -> None:
    location = release_app.archive_location("v0.2.0")
    assert location.startswith(release_app.RELEASES) and location.endswith("/v0.2.0/Mediaforce-0.2.0.zip")
    assert release_app.archive_location("/tmp/Mediaforce-0.2.0.zip") == "/tmp/Mediaforce-0.2.0.zip"


@pytest.mark.parametrize("release", ["v0.1.0-3", "Mediaforce-0.1.0-3.zip"])
def test_install_refuses_the_retired_numbered_tag_form(tmp_path: Path, release: str) -> None:
    with patch.object(release_app, "fetch") as fetch, pytest.raises(ValueError, match="is not a release archive"):
        verify_release(release, tmp_path, feed="feed", public_key=PROOF_KEY, proof=True)
    fetch.assert_not_called()


@pytest.mark.parametrize("listed", ["other bytes", "renamed"])
def test_archive_must_match_its_published_digest(published: Path, listed: str) -> None:
    archive, sums = published / "Mediaforce-0.2.0.zip", published / "SHA256SUMS"
    assert require_listed_digest(archive, sums) == hashlib.sha256(archive.read_bytes()).hexdigest()
    if listed == "other bytes":
        archive.write_bytes(b"tampered")
    else:
        sums.write_text(sums.read_text().replace(archive.name, "Mediaforce-0.1.0.zip"))
    with pytest.raises(ValueError, match="SHA256SUMS"):
        require_listed_digest(archive, sums)


@pytest.mark.parametrize("problem", ["another archive", "another length"])
def test_build_and_signature_come_only_from_the_feed_item_for_this_archive(published: Path, problem: str) -> None:
    feed, archive = (published / "appcast.xml").read_text(), published / "Mediaforce-0.2.0.zip"
    assert feed_offer(feed, archive) == release_app.FeedOffer("7", "c2lnbmF0dXJl")
    if problem == "another archive":
        other = archive.rename(published / "Mediaforce-0.3.0.zip")
        with pytest.raises(ValueError, match="does not offer Mediaforce-0.3.0.zip"):
            feed_offer(feed, other)
    else:
        archive.write_bytes(b"longer archive bytes than the feed names")
        with pytest.raises(ValueError, match="Mediaforce-0.2.0.zip is not this archive"):
            feed_offer(feed, archive)


def test_verified_release_carries_its_identity(published: Path, tmp_path: Path) -> None:
    verified = _verify(published, tmp_path)
    assert verified.identity == {"CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "7",
                                 "SUFeedURL": str(published / "appcast.xml"), "SUPublicEDKey": PROOF_KEY}
    assert verified.evidence["sha256"] == hashlib.sha256((published / "Mediaforce-0.2.0.zip").read_bytes()).hexdigest()


def test_release_refuses_a_digest_mismatch_before_opening_the_archive(published: Path, tmp_path: Path) -> None:
    (published / "SHA256SUMS").write_text("0" * 64 + "  Mediaforce-0.2.0.zip\n")
    with patch.object(release_app, "notarized_app") as notarized, pytest.raises(ValueError, match="SHA256SUMS"):
        verify_release(str(published / "Mediaforce-0.2.0.zip"), tmp_path, feed=str(published / "appcast.xml"),
                       public_key=PROOF_KEY, proof=True)
    notarized.assert_not_called()


@pytest.mark.parametrize("problem", ["live identity", "named version", "feed build"])
def test_release_refuses_an_app_that_is_not_the_named_release(published: Path, tmp_path: Path, problem: str) -> None:
    archive, feed = "Mediaforce-0.2.0.zip", published / "appcast.xml"
    if problem == "named version":
        archive = "Mediaforce-0.3.0.zip"
        (published / "Mediaforce-0.2.0.zip").rename(published / archive)
        sums = published / "SHA256SUMS"
        sums.write_text(sums.read_text().replace("0.2.0.zip", "0.3.0.zip"))
        feed.write_text(feed.read_text().replace("0.2.0.zip", "0.3.0.zip"))
    elif problem == "feed build":
        feed.write_text(feed.read_text().replace("<sparkle:version>7<", "<sparkle:version>8<"))
    with pytest.raises(ValueError, match="proof builds only" if problem == "live identity" else "not the release"):
        _verify(published, tmp_path, proof=problem != "live identity", archive=archive)


def test_live_install_refuses_a_release_other_than_the_one_the_checkout_names(published: Path, tmp_path: Path) -> None:
    with patch.object(release_app, "fetch") as fetch, pytest.raises(ValueError, match="not the release this checkout names"):
        verify_release(str(published / "Mediaforce-0.2.0.zip"), tmp_path, feed=str(published / "appcast.xml"),
                       public_key=PROOF_KEY, proof=False, committed="0.3.0")
    fetch.assert_not_called()


@pytest.mark.parametrize("saved", ["clone runtime", "bundled app"])
def test_retained_rollback_has_a_usable_app_when_the_old_launch_item_needs_one(tmp_path: Path, saved: str) -> None:
    stage = tmp_path / "support/deployments/stage"
    stage.mkdir(parents=True)
    program = "/clone/.venv/bin/mediaforce-web" if saved == "clone runtime" else "/Applications/Mediaforce.app/Contents/MacOS/mediaforce-server"
    (stage / "launch-item.before.plist").write_bytes(plistlib.dumps({"ProgramArguments": [program]}))
    with patch.object(release_app, "usable_saved_app", return_value=False):
        assert release_app.retained_rollback(stage)["app"] is (saved == "clone runtime")
