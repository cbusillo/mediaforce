import logging
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "Mediaforce"
SECURITY_TOOL = "/usr/bin/security"
SECURITY_TIMEOUT_SECONDS = 10.0
SECURITY_ITEM_NOT_FOUND = 44
SERVICE_KEY_ENVIRONMENT = {"plex": "MEDIAFORCE_PLEX_TOKEN", "tmdb": "MEDIAFORCE_TMDB_TOKEN"}
SERVICE_KEY_LABELS = {"plex": "Mediaforce Plex key", "tmdb": "Mediaforce TMDB key"}
SERVICE_KEY_PATTERN = re.compile(r"[A-Za-z0-9._~+/=-]{1,4096}")


class ServiceKeyNotSaved(RuntimeError):
    pass


class KeyStore(Protocol):
    def read(self, service: str) -> str | None: ...

    def write(self, service: str, key: str) -> None: ...


class KeychainKeyStore:
    """The login Keychain through Apple's `security` tool.

    An item the tool adds trusts only that tool, so the server reads it back on every start without a permission
    dialog, and an app update never changes who may read it. The key goes to the tool on standard input, never on
    its command line. Off macOS there is no Keychain, so keys come from the environment alone.
    """

    def __init__(self, *, available: bool) -> None:
        self._available = available

    def read(self, service: str) -> str | None:
        if not self._available:
            return None
        try:
            result = subprocess.run(
                [SECURITY_TOOL, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", service, "-w"],
                capture_output=True,
                text=True,
                timeout=SECURITY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            _logger.warning("Could not read the %s key from the Keychain: %s", service, type(exc).__name__)
            return None
        if result.returncode != 0:
            if result.returncode != SECURITY_ITEM_NOT_FOUND:
                _logger.warning("Could not read the %s key from the Keychain (exit %s)", service, result.returncode)
            return None
        return result.stdout.strip() or None

    def write(self, service: str, key: str) -> None:
        if not self._available:
            raise ServiceKeyNotSaved("This computer has no Keychain.")
        command = (
            f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{service}" '
            f'-l "{SERVICE_KEY_LABELS[service]}" -w "{key}"\n'
        )
        try:
            subprocess.run(
                [SECURITY_TOOL, "-q", "-i"],
                input=command,
                capture_output=True,
                text=True,
                timeout=SECURITY_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceKeyNotSaved(type(exc).__name__) from None
        if self.read(service) != key:
            raise ServiceKeyNotSaved("The Keychain does not hold the key that was sent.")


def system_key_store() -> KeyStore:
    return KeychainKeyStore(available=sys.platform == "darwin")


def is_valid_service_key(key: str) -> bool:
    return SERVICE_KEY_PATTERN.fullmatch(key) is not None


def service_key(service: str, settings: Mapping[str, Any], store: KeyStore) -> str:
    """The key entered in Settings, else the one an earlier install set in the environment or its `.env` file."""
    return store.read(service) or environment_service_key(service, settings)


def environment_service_key(service: str, settings: Mapping[str, Any]) -> str:
    name = str(settings.get("token_env") or SERVICE_KEY_ENVIRONMENT[service]).strip()
    return str(os.environ.get(name) or "").strip()
