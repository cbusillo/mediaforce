from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import plistlib
from xml.etree import ElementTree

import pytest

from scripts.release_app import SPARKLE, appcast, newest_build

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
