from pathlib import Path
import subprocess


def test_native_mac_tests_do_not_require_python_setup(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("xcodegen", "xcodebuild"):
        executable = tools / name
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    dirname = tools / "dirname"
    dirname.write_text('#!/bin/sh\nprintf "%s\\n" "${1%/*}"\n')
    dirname.chmod(0o755)
    script = Path(__file__).resolve().parents[1] / "scripts/build-mac-app.sh"

    result = subprocess.run(
        ["/bin/bash", str(script), "test"], cwd=tmp_path,
        env={"PATH": str(tools)}, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
