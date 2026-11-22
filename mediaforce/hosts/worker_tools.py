from __future__ import annotations

from collections.abc import Callable
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import signal

from mediaforce.hosts import tool_versions
from mediaforce.hosts.tool_versions import ToolUpdateError


def run_command(command: list[str], *, cwd: Path | None = None, include_stderr: bool = False) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as error:
        if cwd is not None and not cwd.is_dir():
            raise ToolUpdateError("The folder for preparing the new media tools is missing.") from error
        raise ToolUpdateError(f"This computer needs {Path(command[0]).name} to prepare the media tools.") from error
    if result.returncode:
        detail = result.stderr or result.stdout
        print(detail[-2000:], file=sys.stderr)
        if Path(command[0]).name == "brew":
            raise ToolUpdateError(homebrew_failure(command[1:], detail))
        raise ToolUpdateError(f"{Path(command[0]).name} could not finish preparing the media tools.")
    return (result.stdout + (result.stderr if include_stderr else "")).strip()


def homebrew_failure(arguments: list[str], detail: str) -> str:
    action = arguments[0]
    formulae = ", ".join(argument for argument in arguments[1:] if not argument.startswith("-"))
    step = {
        "list": "check which tools are pinned",
        "--repository": "find its installation",
        "--prefix": "check the separate tool folder",
        "--cache": f"find the download for {formulae}",
        "info": f"check the versions of {formulae}",
        "install": f"install {formulae}",
    }.get(action, f"finish the {action} step")
    reason = f"Homebrew could not {step}."
    if "Command Line Tools (CLT) does not support macOS" in detail:
        reason += " The installed Command Line Tools do not support this version of macOS."
    return reason


def install_mac(
    prefix: Path, target: dict[str, str], sources: dict[str, dict[str, str]], needed: set[str], run: Callable = run_command
) -> None:
    formulae = {"ffmpeg-full" if key == "ffmpeg" else key: target[key] for key in needed}
    pins = set(run(["brew", "list", "--pinned"]).split()) & (formulae.keys() | ({"ffmpeg"} if "ffmpeg" in needed else set()))
    if pins:
        names = ", ".join(sorted(pins))
        raise ToolUpdateError(f"Homebrew has pinned {names}. To allow the update, unpin {names} on this computer and choose Retry connection.")
    brew_root = run(["brew", "--repository"])
    private = prefix / "homebrew"
    run(["git", "clone", "--quiet", "--local", brew_root, str(private)])
    brew = str(private / "bin/brew")
    if Path(run([brew, "--prefix"])).resolve() != private.resolve():
        raise ToolUpdateError("Homebrew could not prepare a separate copy of the media tools.")
    os.environ.update(
        HOMEBREW_NO_INSTALL_CLEANUP="1", HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_INSTALL_FROM_API="1", HOMEBREW_NO_ANALYTICS="1"
    )
    core = private / "Library/Taps/homebrew/homebrew-core"
    core.mkdir(parents=True)
    repository = "https://github.com/Homebrew/homebrew-core.git"
    run(["git", "init", "--quiet", str(core)])
    git = ["git", "-C", str(core)]
    run([*git, "remote", "add", "origin", repository])
    base = sources["ffmpeg" if "ffmpeg" in needed else "ab-av1"]["revision"]
    run([*git, "fetch", "--depth=1", "origin", base])
    run([*git, "checkout", "--detach", "FETCH_HEAD"])
    try:
        info = json.loads(run([brew, "info", "--json=v2", *formulae]))
        available = {row["name"]: row["versions"]["stable"] for row in info["formulae"]}
        if available.keys() != formulae.keys():
            raise ValueError
    except (ValueError, KeyError, TypeError) as error:
        raise ToolUpdateError("Homebrew could not report the versions of the requested media tools.") from error
    for formula, version in available.items():
        if version != formulae[formula]:
            name = "ffmpeg" if formula == "ffmpeg-full" else formula
            (core / "Formula" / formula[0] / f"{formula}.rb").write_text(sources[name]["recipe"], encoding="utf-8")
    install_options = []
    if needed == {"ab-av1"} and ".bottle." in Path(run([brew, "--cache", "ab-av1"])).name:
        install_options.append("--ignore-dependencies")
    run([brew, "install", *install_options, *formulae])
    binaries = prefix / "bin"
    binaries.mkdir(exist_ok=True)
    for name in tool_versions.PROGRAMS:
        if ("ffmpeg" if name == "ffprobe" else name) not in needed:
            continue
        formula = "ab-av1" if name == "ab-av1" else "ffmpeg-full"
        (binaries / name).symlink_to(private / "opt" / formula / "bin" / name)


def install_linux(prefix: Path, sources: dict[str, dict[str, str]], needed: set[str]) -> None:
    configure = run_command(["ffmpeg", "-buildconf"], include_stderr=True) if shutil.which("ffmpeg") else ""
    features = re.findall(r"^\s*(--enable-[a-z0-9-]+)\s*$", configure, re.MULTILINE)
    os.environ.update(
        PKG_CONFIG_PATH=str(prefix / "lib/pkgconfig"),
        LDFLAGS=f"-Wl,-rpath,{prefix}/lib -L{prefix}/lib",
        CFLAGS=f"-I{prefix}/include",
    )
    with tempfile.TemporaryDirectory() as work:
        for name in ("opus", "svt-av1", "libvmaf", "ffmpeg", "ab-av1"):
            if name not in needed:
                continue
            source = sources[name]
            archive = Path(work) / "source"
            run_command(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--silent",
                    "--show-error",
                    "--output",
                    str(archive),
                    source["url"],
                ]
            )
            with archive.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != source["sha256"]:
                raise ToolUpdateError(f"The downloaded {name} source did not match this Mac.")
            tree = Path(work) / name
            tree.mkdir()
            run_command(["tar", "-xf", str(archive), "--strip-components=1", "-C", str(tree)])
            commands = linux_build_commands(name, prefix, features)
            for command in commands:
                run_command(command, cwd=tree)


def linux_build_commands(name: str, prefix: Path, features: list[str]) -> list[list[str]]:
    destination = f"--prefix={prefix}"
    if name == "svt-av1":
        return [
            [
                "cmake",
                "-S",
                ".",
                "-B",
                "build",
                f"-DCMAKE_INSTALL_PREFIX={prefix}",
                "-DCMAKE_INSTALL_LIBDIR=lib",
                "-DCMAKE_BUILD_TYPE=Release",
                "-DBUILD_SHARED_LIBS=ON",
                "-DBUILD_APPS=OFF",
            ],
            ["cmake", "--build", "build", "-j2"],
            ["cmake", "--install", "build"],
        ]
    if name == "libvmaf":
        return [
            ["meson", "setup", "build", "libvmaf", destination, "--libdir=lib"],
            ["meson", "compile", "-C", "build", "-j2"],
            ["meson", "install", "-C", "build"],
        ]
    if name == "ab-av1":
        return [["cargo", "install", "--locked", "--path", ".", "--root", str(prefix)]]
    options = [destination]
    if name == "ffmpeg":
        options += sorted(set(features) | {"--enable-libsvtav1", "--enable-libopus", "--enable-libvmaf"})
        options += [f"--extra-ldflags=-Wl,-rpath,{prefix}/lib"]
    return [["./configure", *options], ["make", "-j2"], ["make", "install"]]


def select_tools(current: Path, target: Path | None) -> None:
    if target is None:
        current.unlink(missing_ok=True)
        return
    link = current.with_name(".next")
    link.unlink(missing_ok=True)
    link.symlink_to(target)
    link.replace(current)


def restore_previous(current: Path, journal: Path) -> None:
    record = json.loads(journal.read_text(encoding="utf-8"))
    previous = record["previous"]
    select_tools(current, Path(previous) if previous else None)
    journal.unlink()
    if tool_versions.running_versions(list(tool_versions.PROGRAMS)) != record["versions"]:
        raise ToolUpdateError("The previous tools were selected again. Their versions changed and will be checked again.")


def activate(current: Path, candidate: Path, previous_versions: dict[str, str], verify: Callable[[], None]) -> None:
    previous = current.resolve() if current.is_symlink() and current.exists() else None
    journal = current.with_name("pending")
    staging = journal.with_suffix(".tmp")
    staging.write_text(
        json.dumps({"previous": str(previous) if previous else None, "versions": previous_versions}), encoding="utf-8"
    )
    staging.replace(journal)
    try:
        select_tools(current, candidate)
        verify()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        try:
            restore_previous(current, journal)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as restore_error:
            raise ToolUpdateError("The new tools failed their check. Mediaforce could not confirm the previous tools.") from restore_error
        reason = str(error) if isinstance(error, ToolUpdateError) else "The new tools failed their check."
        raise ToolUpdateError(f"Restored the previous media tools. {reason}") from error
    select_tools(current.with_name("previous"), previous)
    journal.unlink()


def retire_unused_builds(root: Path) -> None:
    kept = set()
    for link in (root / "current", root / "previous"):
        if not link.is_symlink():
            continue
        bundle = link.resolve()
        kept.add(bundle)
        for name in tool_versions.PROGRAMS:
            binary = (bundle / "bin" / name).resolve()
            if root in binary.parents:
                kept.add(root / binary.relative_to(root).parts[0])
    for marker in root.glob("ffmpeg-*/.mediaforce-build"):
        bundle = marker.parent
        if not bundle.is_symlink() and bundle not in kept:
            shutil.rmtree(bundle)


def self_test(quality: list[str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        sample = str(Path(directory) / "test.mkv")
        run_command(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=320x180:r=24:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=2",
                "-c:v",
                "libsvtav1",
                "-preset",
                "10",
                "-c:a",
                "libopus",
                sample,
            ]
        )
        report = run_command(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "json", sample])
        try:
            codecs = {stream["codec_name"] for stream in json.loads(report)["streams"]}
        except (ValueError, TypeError, KeyError) as error:
            raise ToolUpdateError("The computer could not read the test file’s picture and sound details.") from error
        if codecs != {"av1", "opus"}:
            raise ToolUpdateError("The test file failed its picture or sound check.")
        run_command(["ffmpeg", "-v", "error", "-i", sample, "-vf", "idet", "-f", "null", "-"])
        run_command(["ffmpeg", *quality])
        run_command(["ffmpeg", *["xpsnr" if arg == "libvmaf" else arg for arg in quality]])
        if sys.platform == "darwin" and "videotoolbox" not in run_command(["ffmpeg", "-hwaccels"]).lower():
            raise ToolUpdateError("The Mac could not use its video hardware.")
        run_command(
            ["ab-av1", "sample-encode", "-i", sample, "--crf", "32", "--preset", "10", "--temp-dir", directory], cwd=Path(directory)
        )


def ensure_idle() -> None:
    processes = run_command(["ps", "-A", "-o", "comm="])
    if any(Path(line.strip()).name in tool_versions.PROGRAMS for line in processes.splitlines()):
        raise BlockingIOError("A media program is still running on this computer.")


def update(target: dict[str, str], sources: dict[str, dict[str, str]], quality: list[str], repair_video: bool = False) -> None:
    root = tool_versions.TOOLS_DIRECTORY.resolve()
    root.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    with (root / "lock").open("a", encoding="utf-8") as lock:
        deadline = time.monotonic() + 8
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BlockingIOError("Another tool check is still running.") from None
                time.sleep(0.15)
        ensure_idle()
        pending = root / "pending"
        if pending.exists():
            restore_previous(current, pending)
            raise ToolUpdateError("The update was interrupted. The previous media tools were restored.")
        retire_unused_builds(root)
        before = tool_versions.running_versions(list(tool_versions.PROGRAMS))
        if before == target and not repair_video:
            self_test(quality)
            return
        prefix = Path(tempfile.mkdtemp(prefix=f"ffmpeg-{target['ffmpeg']}-", dir=root))
        (prefix / ".mediaforce-build").touch()
        try:
            video = {"ffmpeg", "svt-av1", "opus", "libvmaf"}
            needed = video.copy() if repair_video or any(before.get(name) != target[name] for name in (*video, "ffprobe")) else set()
            if before.get("ab-av1") != target["ab-av1"]:
                needed.add("ab-av1")
            binaries = prefix / "bin"
            binaries.mkdir()
            for name in tool_versions.PROGRAMS:
                if ("ffmpeg" if name == "ffprobe" else name) not in needed:
                    selected = Path(shutil.which(name) or name).resolve(strict=True)
                    if root in selected.parents:
                        (binaries / name).symlink_to(selected)
            if sys.platform == "darwin":
                install_mac(prefix, target, sources, needed)
            else:
                install_linux(prefix, sources, needed)
                (prefix / "mediaforce-sources.json").write_text(json.dumps(target), encoding="utf-8")
            candidate = [str(binaries / name) if (binaries / name).exists() else name for name in tool_versions.PROGRAMS]
            if tool_versions.running_versions(candidate) != target:
                raise ToolUpdateError("The new media tools do not match this Mac. The previous tools are still selected.")
            while True:
                try:
                    ensure_idle()
                    break
                except BlockingIOError:
                    time.sleep(5)

            def verify() -> None:
                if tool_versions.running_versions(list(tool_versions.PROGRAMS)) != target:
                    raise ToolUpdateError("The selected tools reported unexpected versions.")
                self_test(quality)

            activate(current, prefix, before, verify)
            retire_unused_builds(root)
        finally:
            if current.resolve() != prefix and not pending.exists():
                shutil.rmtree(prefix)


def report_update_error(message: str) -> int:
    print(f"{ToolUpdateError.prefix}{message}")
    return 65


if __name__ == "__main__":
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        update(*[json.loads(argument) for argument in sys.argv[1:]])
    except BlockingIOError:
        sys.exit(75)
    except ToolUpdateError as failure:
        sys.exit(report_update_error(str(failure)))
    except OSError as failure:
        if failure.errno == errno.ENOSPC:
            sys.exit(report_update_error("This computer ran out of space while preparing the new tools."))
        if isinstance(failure, PermissionError):
            sys.exit(report_update_error("This computer did not allow Mediaforce to prepare or select its tools."))
        raise
