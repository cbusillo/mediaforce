import subprocess
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from mediaforce.library.external_metadata import ProviderHttpError
from mediaforce.library.metadata_sync import metadata_connection_states
from mediaforce.library.service_keys import KEYCHAIN_SERVICE, LIVE_STATE_DIR, SECURITY_ITEM_NOT_FOUND, \
    SERVICE_KEY_ENVIRONMENT, KeychainKeyStore, KeyStore, ServiceKeyNotSaved, keychain_service_for, service_key


class FakeSecurityTool:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.items: dict[tuple[str, str], str] = {}

    def run(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent = kwargs.get("input")
        self.calls.append((command, sent))
        if command[1:] == ["-q", "-i"] and isinstance(sent, str):
            words = sent.split('"')
            self.items[words[1], words[3]] = words[7]
            return subprocess.CompletedProcess(command, 0, "", "")
        item = (command[command.index("-s") + 1], command[command.index("-a") + 1])
        if item in self.items:
            return subprocess.CompletedProcess(command, 0, f"{self.items[item]}\n", "")
        return subprocess.CompletedProcess(command, SECURITY_ITEM_NOT_FOUND, "", "not found")


@pytest.fixture
def security_tool() -> Iterator[FakeSecurityTool]:
    tool = FakeSecurityTool()
    with patch("mediaforce.library.service_keys.subprocess.run", side_effect=tool.run):
        yield tool


def test_a_key_entered_in_settings_wins_over_one_from_the_environment(
        key_store: KeyStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENVIRONMENT["plex"], "from-env")
    key_store.write("plex", "from-keychain")

    assert service_key("plex", {}, key_store) == "from-keychain"


def test_an_existing_environment_key_keeps_working_without_reentry(
        key_store: KeyStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENVIRONMENT["tmdb"], " from-env ")
    monkeypatch.setenv("MEDIAFORCE_PLEX_HOME_TOKEN", "named-by-settings")

    assert service_key("tmdb", {}, key_store) == "from-env"
    assert service_key("plex", {"token_env": "MEDIAFORCE_PLEX_HOME_TOKEN"}, key_store) == "named-by-settings"


def test_keychain_write_keeps_the_key_off_the_command_line(security_tool: FakeSecurityTool) -> None:
    KeychainKeyStore(available=True).write("plex", "plex-secret-123")

    (command, sent), _read_back = security_tool.calls
    assert "plex-secret-123" not in " ".join(command)
    assert sent is not None and '-w "plex-secret-123"' in sent
    assert KeychainKeyStore(available=True).read("plex") == "plex-secret-123"


def test_keychain_without_the_key_reads_as_none(security_tool: FakeSecurityTool) -> None:
    assert KeychainKeyStore(available=True).read("tmdb") is None


def test_keychain_write_that_did_not_stick_is_refused(security_tool: FakeSecurityTool) -> None:
    def ignore_adds(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[1:] == ["-q", "-i"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return security_tool.run(command, **kwargs)

    with patch("mediaforce.library.service_keys.subprocess.run", side_effect=ignore_adds):
        with pytest.raises(ServiceKeyNotSaved):
            KeychainKeyStore(available=True).write("plex", "plex-secret-123")


def _metadata_config(*, plex_address: str = "http://plex.local:32400", tmdb_enabled: bool = True) -> Any:
    return SimpleNamespace(
        metadata={"plex": {"enabled": True, "base_url": plex_address}, "tmdb": {"enabled": tmdb_enabled}},
        paths=SimpleNamespace(db_path=Path("/library.sqlite3")),
    )


def _answering(status_codes: dict[str, int | None]) -> Callable[..., Any]:
    def fetch(_url: str, headers: Mapping[str, str], _timeout: float) -> tuple[dict[str, Any], Mapping[str, str]]:
        service = "plex" if "X-Plex-Token" in headers else "tmdb"
        status = status_codes[service]
        if status == 200:
            return {}, {}
        raise ProviderHttpError("refused", status_code=status)

    return fetch


@pytest.mark.parametrize(
    ("status_codes", "states"),
    [
        ({"plex": 200, "tmdb": 200}, {"plex": "connected", "tmdb": "connected"}),
        ({"plex": 401, "tmdb": 401}, {"plex": "rejected", "tmdb": "rejected"}),
        ({"plex": None, "tmdb": 503}, {"plex": "unreachable", "tmdb": "unreachable"}),
    ],
)
def test_connection_state_says_whether_each_service_accepts_its_key(
        key_store: KeyStore, status_codes: dict[str, int | None], states: dict[str, str],
) -> None:
    key_store.write("plex", "plex-key")
    key_store.write("tmdb", "tmdb-key")

    assert metadata_connection_states(_metadata_config(), key_store, fetch_json_fn=_answering(status_codes)) == states


def test_connection_state_without_a_key_or_a_way_to_check_never_calls_out(
        key_store: KeyStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SERVICE_KEY_ENVIRONMENT["plex"], raising=False)
    key_store.write("tmdb", "tmdb-key")

    def refuse_calls(*_args: Any) -> Any:
        raise AssertionError("no service should be called")

    states = metadata_connection_states(_metadata_config(tmdb_enabled=False), key_store, fetch_json_fn=refuse_calls)

    assert states == {"plex": "missing", "tmdb": "not_checked"}


def test_only_the_installed_apps_own_folder_uses_the_plain_keychain_entries(tmp_path: Path) -> None:
    live = tmp_path / LIVE_STATE_DIR
    proof = tmp_path / "Library" / "Application Support" / "com.mediaforce.web.proof"
    others = [proof, tmp_path / "snapshot" / "state", live / "development"]
    for folder in [live, *others]:
        folder.mkdir(parents=True)

    names = [keychain_service_for(folder, home=tmp_path) for folder in others]

    assert keychain_service_for(live, home=tmp_path) == KEYCHAIN_SERVICE
    assert KEYCHAIN_SERVICE not in names
    assert len(set(names)) == len(others)


def test_servers_on_different_state_folders_never_see_each_others_keys(
        security_tool: FakeSecurityTool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = KeychainKeyStore(available=True, keychain_service=keychain_service_for(tmp_path / LIVE_STATE_DIR, tmp_path))
    snapshot = KeychainKeyStore(available=True, keychain_service=keychain_service_for(tmp_path / "snapshot", tmp_path))
    installed.write("plex", "installed-key")
    snapshot.write("tmdb", "snapshot-key")
    monkeypatch.setenv(SERVICE_KEY_ENVIRONMENT["plex"], "from-snapshot-env")

    assert (installed.read("tmdb"), snapshot.read("plex")) == (None, None)
    assert service_key("plex", {}, snapshot) == "from-snapshot-env"
    assert service_key("plex", {}, installed) == "installed-key"
