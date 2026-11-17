"""Release identity, Sparkle feed and controller install for a notarized Mediaforce release."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from urllib.request import urlopen
from xml.etree import ElementTree

from mediaforce.ops.controller_deploy import APPLICATIONS_APP, DEFAULT_STARTUP_TIMEOUT, install_release
from mediaforce.ops.login_item import _verify_signed_bundle
from mediaforce.ops.runtime_install import SERVICE_LABEL, bundle_service, retained_deploy_stage


ROOT = Path(__file__).resolve().parents[1]
RELEASES = "https://github.com/cbusillo/mediaforce/releases/download"
TAG = re.compile(r"v(?P<version>[^/]+)-(?P<build>\d+)")
ARCHIVE = re.compile(r"Mediaforce-(?P<version>[^/]+)-(?P<build>\d+)\.zip")
DOWNLOAD_TIMEOUT_SECONDS = 300
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


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    app: Path
    identity: dict[str, str]
    evidence: dict[str, str]


def archive_location(release: str) -> str:
    tag = TAG.fullmatch(release)
    return f"{RELEASES}/{release}/Mediaforce-{tag['version']}-{tag['build']}.zip" if tag else release


def fetch(location: str, folder: Path) -> Path:
    target = folder / location.rsplit("/", 1)[-1]
    # noinspection HttpUrlsUsage
    if location.startswith(("https://", "http://")):
        with urlopen(location, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            target.write_bytes(response.read())
    else:
        shutil.copyfile(Path(location).expanduser(), target)
    return target


def require_listed_digest(archive: Path, sums: Path) -> str:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    listed = [line.split(maxsplit=1) for line in sums.read_text().splitlines() if line.strip()]
    if [digest, archive.name] not in [[value, name.removeprefix("*")] for value, name in listed]:
        raise ValueError(f"{archive.name} does not match its SHA256SUMS")
    return digest


def feed_signature(feed: str, build: str, archive: Path) -> str:
    for item in ElementTree.fromstring(feed).iter("item"):
        enclosure = item.find("enclosure")
        if item.findtext(f"{{{SPARKLE}}}version") != build or enclosure is None:
            continue
        if enclosure.get("url", "").rsplit("/", 1)[-1] != archive.name or enclosure.get("length") != str(archive.stat().st_size):
            raise ValueError(f"the feed's build {build} is not {archive.name}")
        return enclosure.get(f"{{{SPARKLE}}}edSignature", "")
    raise ValueError(f"the feed does not offer build {build}; install the release it offers")


def require_signature(public_key: str, signature: str, archive: Path) -> None:
    if subprocess.run(["swift", str(ROOT / "scripts/sparkle-verify.swift"), public_key, signature, str(archive)]).returncode:
        raise ValueError(f"{archive.name} is not signed with the update key")


def notarized_app(archive: Path, folder: Path) -> Path:
    subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(folder)], check=True)
    app = folder / "Mediaforce.app"
    _verify_signed_bundle(app)
    subprocess.run(["/usr/bin/xcrun", "stapler", "validate", str(app)], check=True, capture_output=True)
    assessment = subprocess.run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=4", str(app)],
                                capture_output=True, text=True)
    if assessment.returncode or "source=Notarized Developer ID" not in assessment.stdout + assessment.stderr:
        raise ValueError(f"Gatekeeper does not accept {app} as notarized: {assessment.stderr.strip()}")
    return app


def require_identity(app: Path, expected: dict[str, str]) -> None:
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    found = {key: str(info.get(key, "")) for key in expected}
    if found != expected:
        raise ValueError(f"{app} is {found}, not the release {expected}")


def verify_release(release: str, folder: Path, *, feed: str, public_key: str, proof: bool) -> VerifiedRelease:
    location = archive_location(release)
    named = ARCHIVE.fullmatch(location.rsplit("/", 1)[-1])
    if named is None or not public_key:
        raise ValueError(f"{location} is not a release archive, or no Sparkle public key is configured")
    downloads, extracted = folder / "download", folder / "app"
    downloads.mkdir()
    extracted.mkdir()
    archive = fetch(location, downloads)
    digest = require_listed_digest(archive, fetch(location.rsplit("/", 1)[0] + "/SHA256SUMS", downloads))
    feed_text = fetch(feed, downloads).read_text()
    require_signature(public_key, feed_signature(feed_text, named["build"], archive), archive)
    app = notarized_app(archive, extracted)
    if (bundle_service(app)[0] != SERVICE_LABEL) != proof:
        raise ValueError("a proof feed and key are for proof builds only, and a proof build never installs as the live app")
    identity = {"CFBundleShortVersionString": named["version"], "CFBundleVersion": named["build"], "SUFeedURL": feed,
                "SUPublicEDKey": public_key}
    require_identity(app, identity)
    return VerifiedRelease(app, identity, {"release": release, "archive": location, "sha256": digest, **identity})


def install(args: argparse.Namespace) -> dict[str, object]:
    identity = release_identity()
    proof = args.feed is not None or args.public_key is not None or args.install is not None
    feed, public_key = args.feed or identity["feed"], args.public_key or identity["public_key"]
    stage = args.stage.expanduser().absolute()
    destination = args.install.expanduser().absolute() if args.install else APPLICATIONS_APP
    with tempfile.TemporaryDirectory(prefix="mediaforce-release-") as scratch:
        release = verify_release(args.release, Path(scratch), feed=feed, public_key=public_key, proof=proof)
        install_release(release.app, stage, Path.home(), evidence=release.evidence, install=destination,
                        startup_timeout=args.startup_timeout, stopped=args.stopped)
    require_identity(destination, release.identity)
    rollback = {"stage": str(stage), "retained": retained_deploy_stage(stage.parent.parent) == stage,
                **{name: (stage / name).is_file() for name in ("before.sqlite", "launch-item.before.plist")}}
    if not all(rollback.values()):
        raise RuntimeError(f"the release runs, but its saved rollback is incomplete: {rollback}")
    return {"installed": str(destination), **release.identity, "rollback": rollback}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("identity", help="print the release identity as name=value lines")
    installing = commands.add_parser("install", help="install a verified release through the staged controller deploy")
    installing.add_argument("--release", required=True, help="tag (v<version>-<build>), archive URL or local archive")
    installing.add_argument("--stage", type=Path, required=True, help="fresh folder under the database's deployments/")
    installing.add_argument("--stopped", action="store_true", help="install while the service is stopped, after verifying idle work records")
    installing.add_argument("--startup-timeout", type=int, default=DEFAULT_STARTUP_TIMEOUT,
                            help="seconds allowed for startup and migrations")
    for option in ("--feed", "--public-key"):
        installing.add_argument(option, help="proof builds only: the build's own feed and Sparkle key")
    installing.add_argument("--install", type=Path, help="proof builds only: where the proof app goes")
    feed = commands.add_parser("appcast", help="write the feed offering one notarized build")
    feed.add_argument("--app", type=Path, required=True)
    feed.add_argument("--archive", type=Path, required=True)
    feed.add_argument("--signature", required=True)
    feed.add_argument("--url", required=True)
    feed.add_argument("--notes-url", required=True)
    feed.add_argument("--previous", type=Path, help="the feed being replaced, when one is published")
    feed.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "install":
        if args.startup_timeout <= 0:
            parser.error("--startup-timeout must be positive")
        print(json.dumps(install(args), indent=2))
        return 0
    if args.command == "identity":
        print("\n".join(f"{name}={value}" for name, value in release_identity().items()))
        return 0
    previous = args.previous.read_text() if args.previous else None
    args.output.write_bytes(appcast(args.app, args.archive, args.signature, args.url, args.notes_url, previous,
                                    datetime.now(UTC)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
