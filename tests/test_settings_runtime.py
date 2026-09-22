import tempfile
import unittest
from pathlib import Path
from typing import Any

from mediaforce.web.settings_runtime import SettingsValidationError, build_runtime_settings_payload


class SettingsRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _payload(self, catalog_refresh_hours: object) -> dict[str, Any]:
        return build_runtime_settings_payload(
            libraries=[{"key": "tv", "path": str(self.root / "source" / "tv")}],
            remote_hosts=[],
            transcode_root=str(self.root / "staging"),
            catalog_refresh_hours=catalog_refresh_hours,
            encode_queue_scheduler={"mode": "anytime", "start_hour": 22, "end_hour": 8, "timezone": "local"},
            schedule_profiles=[],
        )

    def test_catalog_refresh_interval_is_persisted(self) -> None:
        payload = self._payload("2.5")

        self.assertEqual(payload["media"]["catalog_refresh_hours"], 2.5)

    def test_catalog_refresh_interval_rejects_out_of_range_and_nonfinite_values(self) -> None:
        for value in ("-1", "8761", "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(SettingsValidationError):
                self._payload(value)


if __name__ == "__main__":
    unittest.main()
