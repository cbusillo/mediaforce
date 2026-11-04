from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

from mediaforce.core.config import load_config
from mediaforce.ops.login_item import run_login_item_command


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the server inside Mediaforce.app.")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--credentials", type=Path)
    parser.add_argument("--service", choices=("install", "enable", "restart", "disable", "uninstall", "status", "logs"))
    args, server_args = parser.parse_known_args()
    config_path = args.config.expanduser().resolve(strict=True)
    config = load_config(config_path)
    bundle = Path(sys.executable).resolve().parents[2]
    if args.service:
        return run_login_item_command(
            args.service, project_root=bundle, config_path=config_path,
            credentials_path=args.credentials,
        )
    credentials = args.credentials or config.paths.db_path.parent / ".env"
    if args.credentials is not None and not credentials.is_absolute():
        parser.error("--credentials must be an absolute path")
    if args.credentials is not None:
        with credentials.open("rb"):
            pass
    os.environ["MEDIAFORCE_ENV_FILE"] = str(credentials)
    from mediaforce.web.app import main as web_main

    web_main(["--config", str(config_path), "--no-reload", *server_args])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
