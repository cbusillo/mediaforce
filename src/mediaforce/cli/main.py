"""Thin CLI shim to keep the entrypoint stable while we refactor internals."""

from mediaforce.core import main as _core_main


def main() -> int:  # pragma: no cover - delegates immediately
    return _core_main()


if __name__ == "__main__":
    import sys

    sys.exit(main())
