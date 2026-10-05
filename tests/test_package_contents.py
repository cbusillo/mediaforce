import io
from importlib import resources
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import zipfile

from mediaforce.core import config as config_module
from mediaforce.core.config import _source_checkout_default_config_path
from scripts.verify_package_contents import PACKAGED_DEFAULTS_PATH, PackageContentsError, verify_package_archives


class PackageContentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        home = patch.object(Path, "home", return_value=Path("/srv/test-operator"))
        home.start()
        self.addCleanup(home.stop)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_verifier_accepts_wheel_and_sdist_without_runtime_state(self) -> None:
        wheel = self._wheel({"mediaforce/__init__.py": b"", "README.md": b"safe"})
        sdist = self._sdist({"mediaforce-0.1.0/README.md": b"safe"})

        summaries = verify_package_archives([wheel, sdist])

        self.assertEqual([summary["member_count"] for summary in summaries], [2, 1])

    def test_verifier_rejects_private_members_and_paths(self) -> None:
        cases = (
            ("frontend/.idea/workspace.xml", b"private", "forbidden"),
            ("mediaforce/.env.local", b"TOKEN=private", "forbidden"),
            ("state/library.sqlite3-wal", b"private", "forbidden"),
            ("../runtime.sqlite3", b"private", "forbidden"),
            ("notes.txt", b"generated from /Users/alice/private/library.sqlite3", "machine-specific"),
            ("notes.txt", b"generated from /tmp/private-training-export.json", "temporary path"),
        )
        for index, (member, payload, message) in enumerate(cases):
            with self.subTest(member=member):
                archive = self._wheel({member: payload}, name=f"forbidden-{index}.whl")
                with self.assertRaisesRegex(PackageContentsError, message):
                    verify_package_archives([archive])

        private_config = self._sdist(
            {
                "mediaforce-0.1.0/config/folder-defaults.toml": (
                    b'path_prefix = "tv/Private"\n'
                ),
            },
            name="private-config.tar.gz",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([private_config])

    def test_install_safe_resource_has_no_library_roots_or_config_includes(self) -> None:
        default_bytes = resources.files("mediaforce.package_defaults").joinpath("defaults.toml").read_bytes()
        defaults = tomllib.loads(default_bytes.decode("utf-8"))

        self.assertEqual(defaults["config"]["include_files"], [])
        self.assertEqual(defaults["media"]["source_roots"], {})
        self.assertNotIn("/Volumes/", default_bytes.decode("utf-8"))
        self.assertNotIn('path_prefix = "tv/', default_bytes.decode("utf-8"))

    def test_verifier_requires_exact_defaults_bytes_in_each_archive_format(self) -> None:
        expected_bytes = b"[config]\ninclude_files=[]\n[audio]\nstereo_opus_bitrate='128k'\n"
        expected_path = self.root / "expected-defaults.toml"
        expected_path.write_bytes(expected_bytes)
        for archive_format in ("wheel", "sdist"):
            for state in ("valid", "missing", "changed", "duplicate"):
                with self.subTest(archive_format=archive_format, state=state):
                    member = str(PACKAGED_DEFAULTS_PATH)
                    if archive_format == "sdist":
                        member = "mediaforce-1.2.3/" + member
                    entries = [] if state == "missing" else [(member, expected_bytes)]
                    if state == "changed":
                        entries = [(member, expected_bytes.replace(b"128k", b"256k"))]
                    elif state == "duplicate":
                        entries.append((member, expected_bytes))
                    if archive_format == "wheel":
                        archive = self._wheel(entries)
                    else:
                        archive = self._sdist(entries)
                    if state == "valid":
                        summaries = verify_package_archives([archive], source_defaults=expected_path)
                        self.assertEqual(summaries[0]["packaged_defaults"], member)
                    else:
                        with self.assertRaises(PackageContentsError):
                            verify_package_archives([archive], source_defaults=expected_path)

    def test_verifier_rejects_the_pinned_home_directory(self) -> None:
        archive = self._wheel({"notes.txt": b"private home /srv/test-operator/library"})
        with self.assertRaises(PackageContentsError):
            verify_package_archives([archive])

    def test_default_config_selection_prefers_a_complete_source_checkout(self) -> None:
        project = self.root / "checkout"
        source = project / "config/defaults.toml"
        source.parent.mkdir(parents=True)
        source.write_text("[config]\ninclude_files=[]\n")
        (project / "pyproject.toml").touch()
        (project / "hatch_build.py").touch()
        with patch.object(config_module, "_SOURCE_PROJECT_ROOT", project), patch.object(
            config_module.resources, "files", return_value=self.root / "packaged"
        ) as packaged:
            self.assertEqual(config_module._default_config_path(), source)
        packaged.assert_not_called()

    def test_default_config_selection_uses_packaged_defaults_without_checkout_markers(self) -> None:
        project = self.root / "installed"
        unrelated_config = project / "config/defaults.toml"
        unrelated_config.parent.mkdir(parents=True)
        unrelated_config.write_text("[config]\ninclude_files=[]\n")
        packaged = self.root / "packaged"
        with patch.object(config_module, "_SOURCE_PROJECT_ROOT", project), patch.object(
            config_module.resources, "files", return_value=packaged
        ):
            self.assertEqual(config_module._default_config_path(), packaged / "defaults.toml")

    def test_default_config_selection_preserves_fallback_when_resource_is_unavailable(self) -> None:
        project = self.root / "installed"
        fallback = self.root / "fallback/defaults.toml"
        with patch.object(config_module, "_SOURCE_PROJECT_ROOT", project), patch.object(
            config_module, "_SOURCE_DEFAULT_CONFIG_PATH", fallback
        ), patch.object(config_module.resources, "files", side_effect=ModuleNotFoundError):
            self.assertEqual(config_module._default_config_path(), fallback)

    def test_source_checkout_defaults_require_repository_markers(self) -> None:
        project_root = self.root / "checkout"
        config_path = project_root / "config" / "defaults.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("[config]\ninclude_files=[]\n")

        self.assertIsNone(_source_checkout_default_config_path(project_root))
        (project_root / "pyproject.toml").write_text("[project]\nname='mediaforce'\n")
        (project_root / "hatch_build.py").write_text("")
        self.assertEqual(_source_checkout_default_config_path(project_root), config_path)

    def _wheel(self, files: dict[str, bytes] | list[tuple[str, bytes]], *, name: str = "mediaforce.whl") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            entries = files.items() if isinstance(files, dict) else files
            for member, payload in entries:
                archive.writestr(member, payload)
        return path

    def _sdist(self, files: dict[str, bytes] | list[tuple[str, bytes]], *, name: str = "mediaforce.tar.gz") -> Path:
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            entries = files.items() if isinstance(files, dict) else files
            for member, payload in entries:
                info = tarfile.TarInfo(member)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return path


if __name__ == "__main__":
    unittest.main()
