# pyright: reportMissingImports=false

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # type: ignore[import-not-found]


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        frontend_dir = Path(self.root) / "frontend"
        package_json = frontend_dir / "package.json"
        package_lock = frontend_dir / "package-lock.json"

        if not package_json.exists() or not package_lock.exists():
            raise RuntimeError(
                "Cannot build the packaged frontend because frontend sources are incomplete."
            )

        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeError(
                "Cannot build the packaged frontend because `npm` is not available on PATH."
            )

        self._run([npm, "ci"], cwd=frontend_dir, env=self._npm_install_env())
        self._run([npm, "run", "build"], cwd=frontend_dir, env=self._build_env())

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("CI", "1")
        return env

    def _npm_install_env(self) -> dict[str, str]:
        env = self._build_env()
        env["npm_config_production"] = "false"
        env.pop("npm_config_omit", None)
        env.pop("NPM_CONFIG_PRODUCTION", None)
        env.pop("NPM_CONFIG_OMIT", None)
        return env

    def _run(self, command: list[str], cwd: Path, env: dict[str, str]) -> None:
        try:
            subprocess.run(command, cwd=cwd, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            joined = " ".join(command)
            raise RuntimeError(
                f"Frontend packaging step failed while running `{joined}` in {cwd}."
            ) from exc
