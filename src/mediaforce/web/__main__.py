"""Module entrypoint to launch the Mediaforce web UI.

Allows running via `python -m mediaforce.web` or `uv run mediaforce.web`.
"""

from .app import main


if __name__ == "__main__":  # pragma: no cover - thin shim
    main()
