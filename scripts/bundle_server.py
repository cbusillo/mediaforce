from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request

from scripts.verify_package_contents import verify_package_archives


ROOT = Path(__file__).resolve().parents[1]
MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def run(*arguments: str | Path) -> str:
    return subprocess.run([str(arg) for arg in arguments], check=True, text=True, capture_output=True, cwd=ROOT).stdout


def native_files(root: Path) -> list[Path]:
    result = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open("rb") as stream:
            if stream.read(4) in MACHO_MAGICS:
                result.append(path)
    return result


def verify_native_target(path: Path, architectures: set[str], minimum_os: str) -> None:
    actual = set(run("lipo", "-archs", path).split())
    if not architectures.issubset(actual):
        raise ValueError(f"Missing architectures {architectures - actual}: {path}")
    commands = run("otool", "-l", path)
    targets = re.findall(r"\bminos\s+(\d+(?:\.\d+)+)", commands)
    targets.extend(re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version (\d+(?:\.\d+)+)", commands))
    def version(value: str) -> tuple[int, ...]:
        return tuple(map(int, value.split("."))) + (0,) * (3 - len(value.split(".")))

    if not targets or any(version(target) > version(minimum_os) for target in targets):
        raise ValueError(f"Native code requires a newer macOS than {minimum_os}: {path}: {targets}")
    linked = run("otool", "-L", path)
    libraries = [line.strip().split(" (", 1)[0] for line in linked.splitlines() if line.startswith("\t")]
    if any(not library.startswith(("/System/Library/", "/usr/lib/", "@rpath/", "@loader_path/", "@executable_path/")) for library in libraries):
        raise ValueError(f"Native dependency is not relocatable: {path}: {libraries}")
    rpaths = re.findall(r"cmd LC_RPATH\s+cmdsize \d+\s+path (.*?) \(offset", commands)
    if any(not value.startswith(("@loader_path/", "@executable_path/")) for value in rpaths):
        raise ValueError(f"Native search path is not relocatable: {path}: {rpaths}")


def prepare_framework(destination: Path, cache: Path) -> None:
    manifest = tomllib.loads((ROOT / "vendor/python-runtime.toml").read_text())
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"{manifest['sha256']}.tar.gz"
    if not archive.exists():
        with urllib.request.urlopen(manifest["url"], timeout=60) as response, archive.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError(f"Embedded Python digest mismatch: {archive}")
    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        with tarfile.open(archive) as source:
            source.extractall(scratch, filter="data")
        shutil.copytree(Path(scratch) / manifest["framework_source_path"], destination, symlinks=True)


def stage_server(app: Path, cache: Path, identity: str) -> None:
    contents = app / "Contents"
    with (contents / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    minimum_os = info["LSMinimumSystemVersion"]
    architectures = set(run("lipo", "-archs", contents / "MacOS" / info["CFBundleExecutable"]).split())
    if not architectures or not architectures.issubset({"arm64", "x86_64"}):
        raise ValueError(f"Unsupported app architectures: {architectures}")
    framework = contents / "Frameworks/Python.framework"
    prepare_framework(framework, cache)
    server = contents / "Resources/server"
    server.mkdir(parents=True)
    manifest = tomllib.loads((ROOT / "vendor/python-runtime.toml").read_text())
    abi = manifest["python_abi"]
    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        build = Path(scratch)
        run("uv", "build", "--wheel", "--out-dir", build)
        wheel = next(build.glob("*.whl"))
        verify_package_archives([wheel], source_defaults=ROOT / "mediaforce/package_defaults/defaults.toml")
        run("uv", "pip", "install", "--no-deps", "--no-compile-bytecode", "--target", server / "app", wheel)
        requirements = build / "requirements.txt"
        run("uv", "export", "--locked", "--no-default-groups", "--no-emit-project", "--no-header",
            "--no-annotate", "--output-file", requirements)
        for architecture in sorted(architectures):
            platform = "aarch64-apple-darwin" if architecture == "arm64" else "x86_64-apple-darwin"
            run("uv", "pip", "install", "--target", server / architecture, "--requirements", requirements,
                "--require-hashes", "--only-binary", ":all:", "--python-version", abi, "--python-platform", platform,
                "--no-compile-bytecode", "--link-mode", "copy")
    for directory in server.rglob("bin"):
        if directory.is_dir():
            shutil.rmtree(directory)
    launcher = contents / "MacOS/mediaforce-server"
    run("xcrun", "clang", *[arg for arch in sorted(architectures) for arg in ("-arch", arch)],
        f"-mmacosx-version-min={minimum_os}", f'-DPYTHON_ABI="{abi}"', "-F", contents / "Frameworks", "-framework", "Python",
        "-I", framework / "Headers", "-Wl,-rpath,@executable_path/../Frameworks", ROOT / "macos/server-launcher.c",
        "-o", launcher)
    for path in framework.rglob("*.o"):
        path.unlink()
    for path in native_files(contents):
        required = {path.relative_to(server).parts[0]} if path.is_relative_to(server) and path.relative_to(server).parts[0] in architectures else architectures
        verify_native_target(path, required, minimum_os)
        run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, path)
    run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, framework)
    run("codesign", "--force", "--options", "runtime", "--timestamp", "--sign", identity, app)
    run("codesign", "--verify", "--deep", "--strict", app)
    verify_package_archives([app], source_defaults=ROOT / "mediaforce/package_defaults/defaults.toml")
    print(json.dumps({"app": str(app), "architectures": sorted(architectures), "minimum_os": minimum_os}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage and sign the embedded server in a built Mediaforce app.")
    parser.add_argument("app", type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--identity", default="Developer ID Application")
    args = parser.parse_args()
    stage_server(args.app.resolve(strict=True), args.cache, args.identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
