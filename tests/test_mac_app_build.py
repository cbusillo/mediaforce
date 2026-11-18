from pathlib import Path
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_native_mac_tests_need_no_python_and_show_the_project_version(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    arguments = tmp_path / "xcodebuild-arguments"
    (tools / "xcodegen").write_text("#!/bin/sh\nexit 0\n")
    (tools / "xcodebuild").write_text(f'#!/bin/sh\nfor argument in "$@"; do echo "$argument"; done > {arguments}\n')
    dirname = tools / "dirname"
    dirname.write_text('#!/bin/sh\nprintf "%s\\n" "${1%/*}"\n')
    for executable in tools.iterdir():
        executable.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(ROOT / "scripts/build-mac-app.sh"), "test"], cwd=tmp_path,
        env={"PATH": str(tools)}, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert f"MARKETING_VERSION={version}" in arguments.read_text().splitlines()
