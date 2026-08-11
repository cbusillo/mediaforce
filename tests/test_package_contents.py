import copy
import io
from importlib import resources
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

from mediaforce.core.config import _source_checkout_default_config_path
from scripts.verify_package_contents import PackageContentsError, verify_package_archives


class PackageContentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

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

    def test_install_safe_defaults_are_byte_identical(self) -> None:
        source_defaults_resource = resources.files("mediaforce.package_defaults").joinpath(
            "defaults.toml"
        )
        default_bytes = source_defaults_resource.read_bytes()
        defaults = tomllib.loads(default_bytes.decode("utf-8"))
        source_tree_defaults = tomllib.loads(Path("config/defaults.toml").read_text())
        flattened = default_bytes.decode("utf-8")

        self.assertEqual(defaults["config"]["include_files"], [])
        self.assertEqual(defaults["media"]["source_roots"], {})
        self.assertNotIn("/Volumes/", flattened)
        self.assertNotIn("path_prefix = \"tv/", flattened)

        normalized_source = copy.deepcopy(source_tree_defaults)
        normalized_source["config"]["include_files"] = []
        normalized_source["media"]["staging_root"] = defaults["media"]["staging_root"]
        normalized_source["media"]["archive_root"] = defaults["media"]["archive_root"]
        normalized_source["media"]["source_roots"] = {}
        self.assertEqual(normalized_source, defaults)

        wheel = self._wheel(
            {"mediaforce/package_defaults/defaults.toml": default_bytes},
            name="install-safe.whl",
        )
        summaries = verify_package_archives(
            [wheel],
            source_defaults=Path(str(source_defaults_resource)),
        )

        self.assertEqual(
            summaries[0]["packaged_defaults"],
            "mediaforce/package_defaults/defaults.toml",
        )

    def test_source_checkout_defaults_require_repository_markers(self) -> None:
        project_root = self.root / "checkout"
        config_path = project_root / "config" / "defaults.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("[config]\ninclude_files=[]\n")

        self.assertIsNone(_source_checkout_default_config_path(project_root))
        (project_root / "pyproject.toml").write_text("[project]\nname='mediaforce'\n")
        (project_root / "hatch_build.py").write_text("")
        self.assertEqual(_source_checkout_default_config_path(project_root), config_path)

    def _wheel(self, files: dict[str, bytes], *, name: str = "mediaforce.whl") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for member, payload in files.items():
                archive.writestr(member, payload)
        return path

    def _sdist(self, files: dict[str, bytes], *, name: str = "mediaforce.tar.gz") -> Path:
        path = self.root / name
        with tarfile.open(path, "w:gz") as archive:
            for member, payload in files.items():
                info = tarfile.TarInfo(member)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return path


if __name__ == "__main__":
    unittest.main()
