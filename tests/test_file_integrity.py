import os
from pathlib import Path
import platform
import select
import tempfile
import unittest
from unittest.mock import patch

from mediaforce.core.file_integrity import (
    FileIntegrityError,
    MacOSFileIntegrityGuard,
    assert_macos_file_integrity_capability,
    probe_macos_file_integrity,
)


KQUEUE_AVAILABLE = platform.system() == "Darwin" and hasattr(select, "kqueue")


class FileIntegrityCapabilityTests(unittest.TestCase):
    def test_non_macos_capability_fails_closed(self) -> None:
        with (
            patch("mediaforce.core.file_integrity.platform.system", return_value="Linux"),
            self.assertRaisesRegex(
                FileIntegrityError,
                "macOS file-integrity monitoring is unavailable",
            ),
        ):
            assert_macos_file_integrity_capability()

    @unittest.skipUnless(KQUEUE_AVAILABLE, "requires macOS kqueue")
    def test_macos_capability_is_available(self) -> None:
        assert_macos_file_integrity_capability()


@unittest.skipUnless(KQUEUE_AVAILABLE, "requires macOS kqueue")
class MacOSFileIntegrityGuardTests(unittest.TestCase):
    def test_probe_detects_same_filesystem_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probe_macos_file_integrity(root)
            probe_directories = tuple(root.glob(".mediaforce-integrity-*"))
            self.assertEqual(len(probe_directories), 1)
            self.assertEqual(
                (probe_directories[0] / "probe").read_bytes(),
                b"Xlean",
            )

    def test_probe_mutation_preserves_path_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_pwrite = os.pwrite
            replacement_bytes = b"outside-probe-replacement"
            moved_probe: Path | None = None

            def pwrite_with_path_substitution(
                    descriptor: int,
                    payload: bytes,
                    offset: int,
            ) -> int:
                nonlocal moved_probe
                probe_directory = next(root.glob(".mediaforce-integrity-*"))
                probe_path = probe_directory / "probe"
                moved_probe = probe_directory / "original-probe"
                probe_path.rename(moved_probe)
                probe_path.write_bytes(replacement_bytes)
                return original_pwrite(descriptor, payload, offset)

            with patch(
                "mediaforce.core.file_integrity.os.pwrite",
                side_effect=pwrite_with_path_substitution,
            ):
                probe_macos_file_integrity(root)

            assert moved_probe is not None
            self.assertEqual(
                moved_probe.with_name("probe").read_bytes(),
                replacement_bytes,
            )
            self.assertEqual(moved_probe.read_bytes(), b"Xlean")

    def test_probe_never_deletes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "mediaforce.core.file_integrity.os.unlink",
                    side_effect=AssertionError("integrity probe must not unlink"),
                ),
                patch(
                    "mediaforce.core.file_integrity.os.rmdir",
                    side_effect=AssertionError("integrity probe must not rmdir"),
                ),
            ):
                probe_macos_file_integrity(Path(directory))

    def test_guard_detects_transient_write_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "guarded"
            original_bytes = b"registered-source"
            path.write_bytes(original_bytes)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            guard = MacOSFileIntegrityGuard(
                path=path,
                descriptor=descriptor,
                require_single_link=True,
            )
            try:
                mutation_descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
                try:
                    os.write(mutation_descriptor, b"transient-source")
                    os.ftruncate(mutation_descriptor, len(b"transient-source"))
                    os.fsync(mutation_descriptor)
                    os.lseek(mutation_descriptor, 0, os.SEEK_SET)
                    os.write(mutation_descriptor, original_bytes)
                    os.ftruncate(mutation_descriptor, len(original_bytes))
                    os.fsync(mutation_descriptor)
                finally:
                    os.close(mutation_descriptor)
                with self.assertRaisesRegex(
                    FileIntegrityError,
                    "guarded file or path changed",
                ):
                    guard.assert_quiet(timeout_seconds=0.1)
            finally:
                guard.close()
                os.close(descriptor)

    def test_guard_detects_hardlink_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guarded"
            link_path = root / "alias"
            path.write_bytes(b"registered-source")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            guard = MacOSFileIntegrityGuard(
                path=path,
                descriptor=descriptor,
                require_single_link=True,
            )
            try:
                os.link(path, link_path)
                link_path.unlink()
                with self.assertRaisesRegex(
                    FileIntegrityError,
                    "guarded file or path changed",
                ):
                    guard.assert_quiet(timeout_seconds=0.1)
            finally:
                guard.close()
                os.close(descriptor)

    def test_guard_detects_parent_swap_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent"
            parent.mkdir()
            path = parent / "guarded"
            path.write_bytes(b"registered-source")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            guard = MacOSFileIntegrityGuard(
                path=path,
                descriptor=descriptor,
                require_single_link=True,
            )
            moved_parent = root / "moved-parent"
            try:
                parent.rename(moved_parent)
                parent.mkdir()
                replacement = parent / path.name
                replacement.write_bytes(b"replacement-source")
                replacement.unlink()
                parent.rmdir()
                moved_parent.rename(parent)
                with self.assertRaisesRegex(
                    FileIntegrityError,
                    "guarded file or path changed",
                ):
                    guard.assert_quiet(timeout_seconds=0.1)
            finally:
                guard.close()
                os.close(descriptor)

    def test_guard_rejects_existing_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "guarded"
            link_path = root / "alias"
            path.write_bytes(b"registered-source")
            os.link(path, link_path)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaisesRegex(
                    FileIntegrityError,
                    "guarded file or path changed",
                ):
                    MacOSFileIntegrityGuard(
                        path=path,
                        descriptor=descriptor,
                        require_single_link=True,
                    )
            finally:
                os.close(descriptor)
