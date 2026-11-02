"""Host file writes use synthetic homes and preserve concurrent changes."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_connect import host_stage, host_atomic


@unittest.skipUnless(
    os.name == "posix", "POSIX metadata tests; Windows uses native descriptor tests"
)
class HostConfig(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.path = self.root / "codex/config.toml"
        self.path.parent.mkdir()
        self.path.write_bytes(b'model="synthetic"\n')
        self.platform = patch("codex_connect.require_platform")
        self.platform.start()
        self.addCleanup(self.platform.stop)

    def test_conflicting_content_mode_or_identity_is_preserved(self):
        for change in ("bytes", "mode", "identity"):
            with self.subTest(change=change):
                before = self.path.read_bytes()
                with host_stage(self.path, b"candidate", 0o600, before) as commit:
                    if change == "bytes":
                        self.path.write_bytes(b"external")
                    elif change == "mode":
                        self.path.chmod(
                            0o644
                            if self.path.stat().st_mode & 0o777 != 0o644
                            else 0o600
                        )
                    else:
                        other = self.path.with_name("outside.toml")
                        other.write_bytes(before)
                        os.replace(other, self.path)
                    image = (
                        self.path.read_bytes(),
                        self.path.stat().st_mode,
                        self.path.stat().st_ino,
                    )
                    with self.assertRaisesRegex(ValueError, "concurrently"):
                        commit()
                    self.assertEqual(
                        (
                            self.path.read_bytes(),
                            self.path.stat().st_mode,
                            self.path.stat().st_ino,
                        ),
                        image,
                    )
                self.assertEqual(list(self.path.parent.glob(".client-host-*")), [])

    def test_new_file_concurrent_creator_survives(self):
        self.path.unlink()
        with host_stage(self.path, b"candidate", 0o600, None) as commit:
            self.path.write_bytes(b"outside")
            with self.assertRaises(ValueError):
                commit()
        self.assertEqual(self.path.read_bytes(), b"outside")

    def test_existing_mode_and_parent_are_preserved(self):
        self.path.chmod(0o640)
        parent = self.path.parent.stat()
        host_atomic(self.path, b"candidate", 0o640, self.path.read_bytes())
        self.assertEqual(self.path.read_bytes(), b"candidate")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(
            (
                self.path.parent.stat().st_ino,
                self.path.parent.stat().st_mode,
                self.path.parent.stat().st_uid,
                self.path.parent.stat().st_gid,
            ),
            (parent.st_ino, parent.st_mode, parent.st_uid, parent.st_gid),
        )

    def test_symlinks_hardlinks_and_wrong_preimage_are_rejected(self):
        with self.assertRaises(ValueError):
            host_atomic(self.path, b"x", 0o600, b"wrong")
        link = self.root / "alias"
        os.link(self.path, link)
        with self.assertRaises(ValueError):
            host_atomic(self.path, b"x", 0o600, self.path.read_bytes())
        link.unlink()
        self.path.unlink()
        self.path.symlink_to(link)
        with self.assertRaises(ValueError):
            host_atomic(self.path, b"x", 0o600, None)


class WindowsBoundary(unittest.TestCase):
    def test_existing_open_requests_only_data_io_and_excludes_writers(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from codex_connect import windows_host_file

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name).resolve() / "config.toml"
        path.write_bytes(b"synthetic")
        kernel = SimpleNamespace(CreateFileW=Mock(return_value=123), CloseHandle=Mock())
        descriptor = os.open(path, os.O_RDWR)
        crt = SimpleNamespace(open_osfhandle=Mock(return_value=descriptor))
        with (
            patch("brain_windows.canonical", return_value=path),
            patch("brain_windows.libraries", return_value=(SimpleNamespace(), kernel)),
            patch.dict("sys.modules", msvcrt=crt),
        ):
            with windows_host_file(path) as file:
                self.assertEqual(file.read(), b"synthetic")
        kernel.CreateFileW.assert_called_once_with(
            str(path), 0xC0000000, 1, None, 3, 0x00200000, None
        )
        kernel.CloseHandle.assert_not_called()  # The CRT closes the transferred handle.


if __name__ == "__main__":
    unittest.main()
