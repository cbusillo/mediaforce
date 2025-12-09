"""Mediaforce package initializer.

This file re-exports the existing core API so legacy imports like
`from mediaforce import AppSettings` keep working after the move to a
package-based layout.
"""

from .core import *  # noqa: F401,F403

__all__ = [name for name in globals() if not name.startswith("_")]
