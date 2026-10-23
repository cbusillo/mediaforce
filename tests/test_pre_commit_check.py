import os
import subprocess
from pathlib import Path


def test_local_gate_does_not_start_complete_test_suites(tmp_path: Path) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("uv", "npm"):
        executable = binaries / name
        executable.write_text(
            "#!/bin/sh\n"
            "for argument do\n"
            '  case "$argument" in tests/*.py|src/*.test.*) exit 0;; esac\n'
            "done\n"
            'case " $* " in *" pytest "*|*" frontend test "*|*" frontend run smoke:web "*) exit 73;; esac\n'
        )
        executable.chmod(0o755)

    gate = Path(__file__).resolve().parents[1] / "scripts/pre-commit-check.sh"
    result = subprocess.run(
        ["bash", str(gate)],
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stdout + result.stderr
