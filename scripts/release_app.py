"""Release identity and Sparkle feed for a notarized Mediaforce release."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
import plistlib
import tomllib
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
# An XML namespace name, never fetched.
# noinspection HttpUrlsUsage
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
ElementTree.register_namespace("sparkle", SPARKLE)


def release_identity(pyproject: Path = ROOT / "pyproject.toml") -> dict[str, str]:
    """The one committed source for a release: package version, app build, feed and public update key."""
    data = tomllib.loads(pyproject.read_text())
    release = data["tool"]["mediaforce"]["release"]
    version, build = data["project"]["version"], int(release["build"])
    return {"version": version, "build": str(build), "tag": f"v{version}-{build}", "feed": release["feed"],
            "public_key": release["public-key"]}


def newest_build(feed: str) -> int | None:
    builds = [int(element.text or "") for element in ElementTree.fromstring(feed).iter(f"{{{SPARKLE}}}version")]
    return max(builds, default=None)


def appcast(app: Path, archive: Path, signature: str, url: str, notes_url: str, previous: str | None,
            published: datetime) -> bytes:
    """The feed offering this one notarized build. Refuses to replace a feed that already offers it or a newer one."""
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    build = int(info["CFBundleVersion"])
    newest = newest_build(previous) if previous else None
    if newest is not None and newest >= build:
        raise ValueError(f"The live feed already offers build {newest}; this release is build {build}")
    rss = ElementTree.Element("rss", {"version": "2.0"})
    channel = ElementTree.SubElement(rss, "channel")
    ElementTree.SubElement(channel, "title").text = "Mediaforce"
    item = ElementTree.SubElement(channel, "item")
    ElementTree.SubElement(item, "title").text = f"Mediaforce {info['CFBundleShortVersionString']} ({build})"
    ElementTree.SubElement(item, "pubDate").text = format_datetime(published.astimezone(UTC), usegmt=True)
    for name, value in (("version", str(build)), ("shortVersionString", info["CFBundleShortVersionString"]),
                        ("minimumSystemVersion", info["LSMinimumSystemVersion"]),
                        ("fullReleaseNotesLink", notes_url)):
        ElementTree.SubElement(item, f"{{{SPARKLE}}}{name}").text = value
    ElementTree.SubElement(item, "enclosure", {
        "url": url, "length": str(archive.stat().st_size), "type": "application/octet-stream",
        f"{{{SPARKLE}}}edSignature": signature,
    })
    return ElementTree.tostring(rss, encoding="utf-8", xml_declaration=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("identity", help="print the release identity as name=value lines")
    feed = commands.add_parser("appcast", help="write the feed offering one notarized build")
    feed.add_argument("--app", type=Path, required=True)
    feed.add_argument("--archive", type=Path, required=True)
    feed.add_argument("--signature", required=True)
    feed.add_argument("--url", required=True)
    feed.add_argument("--notes-url", required=True)
    feed.add_argument("--previous", type=Path, help="the feed being replaced, when one is published")
    feed.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "identity":
        print("\n".join(f"{name}={value}" for name, value in release_identity().items()))
        return 0
    previous = args.previous.read_text() if args.previous else None
    args.output.write_bytes(appcast(args.app, args.archive, args.signature, args.url, args.notes_url, previous,
                                    datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
