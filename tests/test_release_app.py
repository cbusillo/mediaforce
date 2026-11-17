from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path
import plistlib
from unittest.mock import patch
from xml.etree import ElementTree

import pytest

from scripts import release_app
from scripts.release_app import SPARKLE, appcast, feed_signature, newest_build, require_listed_digest, verify_release

PUBLISHED = datetime(2026, 11, 17, 21, 0, tzinfo=UTC)


@pytest.fixture
def release(tmp_path: Path) -> tuple[Path, Path]:
    app = tmp_path / "Mediaforce.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleVersion": "7", "CFBundleShortVersionString": "0.2.0", "LSMinimumSystemVersion": "15.0"}))
    archive = tmp_path / "Mediaforce-0.2.0-7.zip"
    archive.write_bytes(b"notarized archive bytes")
    return app, archive


def build_feed(release: tuple[Path, Path], previous: str | None = None) -> bytes:
    app, archive = release
    return appcast(app, archive, "c2lnbmF0dXJl", "https://example.invalid/Mediaforce-0.2.0-7.zip",
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
    assert enclosure.get("url") == "https://example.invalid/Mediaforce-0.2.0-7.zip"


@pytest.mark.parametrize("live_build", ["7", "8"])
def test_feed_refuses_to_replace_one_offering_the_same_or_a_newer_build(
    release: tuple[Path, Path], live_build: str
) -> None:
    live = build_feed(release).decode().replace("<sparkle:version>7<", f"<sparkle:version>{live_build}<")

    with pytest.raises(ValueError, match=f"already offers build {live_build}"):
        build_feed(release, live)


def test_feed_replaces_an_older_one(release: tuple[Path, Path]) -> None:
    older = build_feed(release).decode().replace("<sparkle:version>7<", "<sparkle:version>6<")

    assert newest_build(build_feed(release, older).decode()) == 7


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
    archive = folder / "Mediaforce-0.2.0-7.zip"
    archive.write_bytes(b"notarized archive bytes")
    (folder / "SHA256SUMS").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
    (folder / "appcast.xml").write_bytes(appcast(app, archive, "c2lnbmF0dXJl", f"https://example.invalid/{archive.name}",
                                                 "https://example.invalid/notes", None, PUBLISHED))
    return folder


def _verify(published: Path, tmp_path: Path, *, proof: bool = True, archive: str = "Mediaforce-0.2.0-7.zip") -> release_app.VerifiedRelease:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    signed: list[str] = []
    with patch.object(release_app, "require_signature", side_effect=lambda _key, signature, _archive: signed.append(signature)), \
            patch.object(release_app, "notarized_app", return_value=published / "built/Mediaforce.app") as notarized:
        verified = verify_release(str(published / archive), scratch, feed=str(published / "appcast.xml"),
                                  public_key=PROOF_KEY, proof=proof)
    assert signed == ["c2lnbmF0dXJl"] and notarized.called
    return verified


def test_tag_names_its_archive_on_github_releases() -> None:
    location = release_app.archive_location("v0.2.0-7")
    assert location.startswith(release_app.RELEASES) and location.endswith("/v0.2.0-7/Mediaforce-0.2.0-7.zip")
    assert release_app.archive_location("/tmp/Mediaforce-0.2.0-7.zip") == "/tmp/Mediaforce-0.2.0-7.zip"


@pytest.mark.parametrize("listed", ["other bytes", "renamed"])
def test_archive_must_match_its_published_digest(published: Path, listed: str) -> None:
    archive, sums = published / "Mediaforce-0.2.0-7.zip", published / "SHA256SUMS"
    assert require_listed_digest(archive, sums) == hashlib.sha256(archive.read_bytes()).hexdigest()
    if listed == "other bytes":
        archive.write_bytes(b"tampered")
    else:
        sums.write_text(sums.read_text().replace(archive.name, "Mediaforce-0.2.0-6.zip"))
    with pytest.raises(ValueError, match="SHA256SUMS"):
        require_listed_digest(archive, sums)


@pytest.mark.parametrize("problem", ["another build", "another length"])
def test_signature_comes_only_from_the_feed_item_for_this_archive(published: Path, problem: str) -> None:
    feed, archive = (published / "appcast.xml").read_text(), published / "Mediaforce-0.2.0-7.zip"
    assert feed_signature(feed, "7", archive) == "c2lnbmF0dXJl"
    if problem == "another build":
        with pytest.raises(ValueError, match="does not offer build 8"):
            feed_signature(feed, "8", archive)
    else:
        archive.write_bytes(b"longer archive bytes than the feed names")
        with pytest.raises(ValueError, match="is not Mediaforce-0.2.0-7.zip"):
            feed_signature(feed, "7", archive)


def test_verified_release_carries_its_identity(published: Path, tmp_path: Path) -> None:
    verified = _verify(published, tmp_path)
    assert verified.identity == {"CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "7",
                                 "SUFeedURL": str(published / "appcast.xml"), "SUPublicEDKey": PROOF_KEY}
    assert verified.evidence["sha256"] == hashlib.sha256((published / "Mediaforce-0.2.0-7.zip").read_bytes()).hexdigest()


def test_release_refuses_a_digest_mismatch_before_opening_the_archive(published: Path, tmp_path: Path) -> None:
    (published / "SHA256SUMS").write_text("0" * 64 + "  Mediaforce-0.2.0-7.zip\n")
    with patch.object(release_app, "notarized_app") as notarized, pytest.raises(ValueError, match="SHA256SUMS"):
        verify_release(str(published / "Mediaforce-0.2.0-7.zip"), tmp_path, feed=str(published / "appcast.xml"),
                       public_key=PROOF_KEY, proof=True)
    notarized.assert_not_called()


@pytest.mark.parametrize("problem", ["live identity", "named build"])
def test_release_refuses_an_app_that_is_not_the_named_release(published: Path, tmp_path: Path, problem: str) -> None:
    if problem == "named build":
        (published / "Mediaforce-0.2.0-7.zip").rename(published / "Mediaforce-0.2.0-8.zip")
        sums = published / "SHA256SUMS"
        sums.write_text(sums.read_text().replace("-7.zip", "-8.zip"))
        feed = published / "appcast.xml"
        feed.write_text(feed.read_text().replace("-7.zip", "-8.zip").replace(">7<", ">8<"))
    with pytest.raises(ValueError):
        _verify(published, tmp_path, proof=problem != "live identity",
                archive="Mediaforce-0.2.0-8.zip" if problem == "named build" else "Mediaforce-0.2.0-7.zip")
