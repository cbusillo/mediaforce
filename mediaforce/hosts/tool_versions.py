from __future__ import annotations

import ctypes
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


LIBRARIES = {
    "svt-av1": ("libSvtAv1Enc", "svt_av1_get_version"),
    "opus": ("libopus", "opus_get_version_string"),
    "libvmaf": ("libvmaf", "vmaf_version"),
}
PROGRAMS = ("ffmpeg", "ffprobe", "ab-av1")
VERSION = r"[0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z.]+)?"
TOOLS_RELATIVE_DIRECTORY = ".local/share/mediaforce/tools"
TOOLS_DIRECTORY = Path.home() / TOOLS_RELATIVE_DIRECTORY


def command_output(arguments: list[str], **environment: str) -> str:
    result = subprocess.run(
        arguments, capture_output=True, encoding="utf-8", errors="replace", timeout=4, env={**os.environ, **environment}, check=True
    )
    return result.stdout + result.stderr


def running_versions(programs: list[str]) -> dict[str, str]:
    versions = {}
    for name, path in zip(PROGRAMS, programs):
        try:
            environment = {"DYLD_PRINT_LIBRARIES": "1"} if name == "ffmpeg" and sys.platform == "darwin" else {}
            output = command_output([path, "--version" if name == "ab-av1" else "-version"], **environment)
            match = re.search(rf"^{name} (?:version )?n?({VERSION})(?:\s|$)", output, re.MULTILINE)
            if match:
                versions[name] = match[1]
            if name == "ffmpeg":
                links = output if sys.platform == "darwin" else command_output(["ldd", shutil.which(path) or path])
                versions.update(linked_versions(links))
        except (OSError, ValueError, subprocess.CalledProcessError):
            continue
    return versions


def linked_versions(links: str) -> dict[str, str]:
    versions = {}
    for library, (basename, symbol) in LIBRARIES.items():
        match = re.search(rf"(/[^\n]*?/{basename}[.]\S+)(?:\s|$)", links)
        if not match:
            continue
        path = Path(match[1].split(" => ")[-1])
        try:
            release = library_release(path, library)
            if release:
                versions[library] = release
                continue
            function = getattr(ctypes.CDLL(str(path)), symbol)
            function.restype = ctypes.c_char_p
            version = re.fullmatch(rf"(?:libopus |v)?({VERSION})", function().decode())
            if version:
                versions[library] = version[1]
        except (OSError, ValueError, IndexError, KeyError, AttributeError):
            continue
    return versions


def library_release(path: Path, library: str) -> str | None:
    keg = path.resolve().parent.parent
    metadata = keg / "sbom.spdx.json"
    if metadata.is_file():
        package = json.loads(metadata.read_text(encoding="utf-8"))["packages"][0]
        if package["name"] == library:
            return package["versionInfo"]
    receipt = keg / "mediaforce-sources.json"
    if keg.parent == TOOLS_DIRECTORY.resolve() and receipt.is_file():
        versions = json.loads(receipt.read_text(encoding="utf-8"))
        if library in versions:
            return versions[library]
    return None


def probe_record() -> str:
    def observation() -> str:
        return json.dumps({"versions": running_versions(list(PROGRAMS)), "pending": (TOOLS_DIRECTORY / "pending").is_file()})

    lock_path = TOOLS_DIRECTORY / "lock"
    if lock_path.exists():
        with lock_path.open(encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return json.dumps({"versions": {}, "updating": True})
            return observation()
    return observation()


if __name__ == "__main__":
    print(probe_record())
