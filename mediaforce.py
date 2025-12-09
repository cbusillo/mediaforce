"""Compatibility shim; routes to package entrypoint."""

import pathlib
import sys

# Allow running from a checkout without installation
sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from mediaforce.core import main  # type: ignore


if __name__ == "__main__":
    import sys

    sys.exit(main())
