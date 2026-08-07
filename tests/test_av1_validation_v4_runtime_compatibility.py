from __future__ import annotations

import unittest

from mediaforce.tuning.av1_validation_v4_runtime_compatibility import (
    AV1ValidationV4RuntimeCompatibilityError,
    av1_validation_v4_runtime_compatibility_id,
    av1_validation_v4_runtime_compatibility_payload,
)


class AV1ValidationV4RuntimeCompatibilityTests(unittest.TestCase):
    def test_identity_is_deterministic_and_public_safe(self) -> None:
        payload = self._payload()
        runtime_id = self._runtime_id()
        self.assertEqual(
            runtime_id,
            "av1vruntime4_b93875c7d906cb76967d56a32088d99e",
        )
        self.assertEqual(runtime_id, self._runtime_id())
        self.assertEqual(payload["scope"], "host_toolchain_config")
        self.assertNotIn("hostname", payload["platform"])
        self.assertNotIn("username", payload["platform"])

    def test_identity_changes_with_config_toolchain_or_platform(self) -> None:
        baseline = self._runtime_id()
        changed_config = self._runtime_id(
            effective_config_sha256="sha256:" + "9" * 64
        )
        changed_platform = self._runtime_id(architecture="x86_64")
        changed_toolchain = self._runtime_id(
            toolchain={
                **self._toolchain(),
                "ffmpeg": {
                    "version": "ffmpeg version changed",
                    "binary_sha256": "sha256:" + "8" * 64,
                },
            }
        )
        self.assertEqual(len({baseline, changed_config, changed_platform, changed_toolchain}), 4)

    def test_uri_text_is_not_treated_as_a_windows_path(self) -> None:
        runtime_id = self._runtime_id(
            toolchain={
                **self._toolchain(),
                "ffmpeg": {
                    "version": "ffmpeg https://ffmpeg.org",
                    "binary_sha256": "sha256:" + "2" * 64,
                },
            }
        )
        self.assertTrue(runtime_id.startswith("av1vruntime4_"))

    def test_invalid_or_private_inputs_fail_closed(self) -> None:
        cases = (
            lambda: self._runtime_id(effective_config_sha256="wrong"),
            lambda: self._runtime_id(toolchain={"ffmpeg": self._toolchain()["ffmpeg"]}),
            lambda: self._runtime_id(operating_system_version="/Volumes/private"),
            lambda: self._runtime_id(operating_system_version="C:\\Users\\private"),
            lambda: self._runtime_id(
                operating_system_version="tool --prefix=C:\\Users\\private"
            ),
            lambda: self._runtime_id(
                operating_system_version="tool --prefix=/usr/local/Cellar/ffmpeg"
            ),
            lambda: self._runtime_id(
                operating_system_version="tool=\\\\buildsrv01\\team\\ffmpeg"
            ),
            lambda: self._runtime_id(operating_system_version="smb://server/share"),
            lambda: self._runtime_id(
                toolchain={
                    **self._toolchain(),
                    "ffmpeg": {
                        "version": "ffmpeg --prefix=/opt/homebrew/Cellar/ffmpeg",
                        "binary_sha256": "sha256:" + "2" * 64,
                    },
                }
            ),
            lambda: self._runtime_id(python_version=""),
        )
        for operation in cases:
            with self.subTest(operation=operation):
                with self.assertRaises(AV1ValidationV4RuntimeCompatibilityError):
                    operation()

    def _payload(self) -> dict[str, object]:
        return av1_validation_v4_runtime_compatibility_payload(
            effective_config_sha256="sha256:" + "1" * 64,
            toolchain=self._toolchain(),
            operating_system="macOS",
            operating_system_version="27.0 build 26A5388g",
            architecture="arm64",
            python_version="3.13.7",
        )

    def _runtime_id(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "effective_config_sha256": "sha256:" + "1" * 64,
            "toolchain": self._toolchain(),
            "operating_system": "macOS",
            "operating_system_version": "27.0 build 26A5388g",
            "architecture": "arm64",
            "python_version": "3.13.7",
        }
        values.update(overrides)
        return av1_validation_v4_runtime_compatibility_id(**values)

    def _toolchain(self) -> dict[str, dict[str, str]]:
        return {
            "ffmpeg": {
                "version": "ffmpeg version test",
                "binary_sha256": "sha256:" + "2" * 64,
            },
            "ffprobe": {
                "version": "ffprobe version test",
                "binary_sha256": "sha256:" + "3" * 64,
            },
            "ab_av1": {
                "version": "ab-av1 test",
                "binary_sha256": "sha256:" + "4" * 64,
            },
        }


if __name__ == "__main__":
    unittest.main()
