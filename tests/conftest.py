from collections.abc import Iterator
from unittest.mock import patch

import pytest

from mediaforce.library.service_keys import KeyStore


class MemoryKeyStore:
    def __init__(self) -> None:
        self.keys: dict[str, str] = {}

    def read(self, service: str) -> str | None:
        return self.keys.get(service)

    def write(self, service: str, key: str) -> None:
        self.keys[service] = key


@pytest.fixture(autouse=True)
def key_store() -> Iterator[KeyStore]:
    """Every test gets an empty in-memory key store, so none reads or writes the real login Keychain."""
    store = MemoryKeyStore()
    with patch("mediaforce.library.service_keys.system_key_store", return_value=store):
        yield store
