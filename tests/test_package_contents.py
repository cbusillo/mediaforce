import copy
import io
from importlib import resources
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

from scripts.verify_package_contents import PackageContentsError, verify_package_archives
from mediaforce.core.config import _source_checkout_default_config_path


class PackageContentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source_artifact = self.root / "av1_cold_start_priors_v1.json"
        self.source_artifact.write_bytes(b'{"safe":true}\n')

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_verifier_accepts_one_identical_public_artifact(self) -> None:
        wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": self.source_artifact.read_bytes(),
                "mediaforce/__init__.py": b"",
            }
        )
        sdist = self._sdist(
            {
                "mediaforce-0.1.0/mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce-0.1.0/pyproject.toml": b"[project]\nname='mediaforce'\n",
            }
        )

        summaries = verify_package_archives(
            [wheel, sdist],
            source_artifact=self.source_artifact,
        )

        self.assertEqual(len(summaries), 2)

    def test_verifier_rejects_private_members_and_duplicate_artifacts(self) -> None:
        private_wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": self.source_artifact.read_bytes(),
                "frontend/.idea/workspace.xml": b"private",
            },
            name="private.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([private_wheel], source_artifact=self.source_artifact)

        env_wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce/.env.local": b"TOKEN=private",
            },
            name="env-local.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([env_wheel], source_artifact=self.source_artifact)

        nested_env_wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce/.env.local/secret.txt": b"TOKEN=private",
            },
            name="nested-env-local.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([nested_env_wheel], source_artifact=self.source_artifact)

        sidecar_wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "state/library.sqlite3-wal": b"private",
            },
            name="sqlite-sidecar.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([sidecar_wheel], source_artifact=self.source_artifact)

        private_config = self._sdist(
            {
                "mediaforce-0.1.0/mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce-0.1.0/config/folder-defaults.toml": b'path_prefix = "tv/Private"\n',
            },
            name="private-config.tar.gz",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([private_config], source_artifact=self.source_artifact)

        traversal_wheel = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": self.source_artifact.read_bytes(),
                "../runtime.sqlite3": b"private",
            },
            name="traversal.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([traversal_wheel], source_artifact=self.source_artifact)

        duplicate_sdist = self._sdist(
            {
                "mediaforce-0.1.0/mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce-0.1.0/extra/mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
            },
            name="duplicate.tar.gz",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([duplicate_sdist], source_artifact=self.source_artifact)

        impersonator_sdist = self._sdist(
            {
                "mediaforce-junk/mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
            },
            name="impersonator.tar.gz",
        )
        with self.assertRaisesRegex(PackageContentsError, "forbidden"):
            verify_package_archives([impersonator_sdist], source_artifact=self.source_artifact)

    def test_verifier_rejects_artifact_byte_drift_and_private_tokens(self) -> None:
        drifted = self._wheel(
            {"mediaforce/tuning/data/av1_cold_start_priors_v1.json": b'{"safe":false}\n'},
            name="drifted.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "differ"):
            verify_package_archives([drifted], source_artifact=self.source_artifact)

        private_source = self.root / "private-prior.json"
        private_source.write_bytes(b'{"source_path":"/Users/example/media.mkv"}\n')
        private_prior = self._wheel(
            {"mediaforce/tuning/data/av1_cold_start_priors_v1.json": private_source.read_bytes()},
            name="private-prior.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "privacy contract"):
            verify_package_archives([private_prior], source_artifact=private_source)

        machine_path = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": self.source_artifact.read_bytes(),
                "notes.txt": b"generated from /Users/alice/private/library.sqlite3",
            },
            name="machine-path.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "machine-specific"):
            verify_package_archives([machine_path], source_artifact=self.source_artifact)

        temporary_path = self._wheel(
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "notes.txt": b"generated from /tmp/private-training-export.json",
            },
            name="temporary-path.whl",
        )
        with self.assertRaisesRegex(PackageContentsError, "temporary path"):
            verify_package_archives([temporary_path], source_artifact=self.source_artifact)

    def test_install_safe_defaults_are_public_and_byte_identical(self) -> None:
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
            {
                "mediaforce/tuning/data/av1_cold_start_priors_v1.json": (
                    self.source_artifact.read_bytes()
                ),
                "mediaforce/package_defaults/defaults.toml": default_bytes,
            },
            name="install-safe.whl",
        )
        summaries = verify_package_archives(
            [wheel],
            source_artifact=self.source_artifact,
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
