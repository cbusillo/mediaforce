from __future__ import annotations

import os
import pathlib


def load_dotenv_if_present() -> None:
    """Load a local .env file when running from a repo checkout.

    This is intentionally best-effort (no hard dependency).
    """

    env_file = os.getenv("MEDIAFORCE_ENV_FILE") or ".env"
    try:
        path = pathlib.Path(env_file)
    except Exception:
        return

    if not path.exists():
        return

    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except Exception:
        return

    load_dotenv(dotenv_path=path, override=False)
