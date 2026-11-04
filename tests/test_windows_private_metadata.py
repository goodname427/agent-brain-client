"""Synthetic Win32 metadata contracts; native concurrency lives in WindowsNative."""

import ctypes as C
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import brain_windows as windows


class PrivateMetadata(unittest.TestCase):
    def setUp(self):
        self.info = dict(attributes=0, links=1)
        self.acl = C.create_string_buffer(8)
        self.api = SimpleNamespace(
            GetSecurityInfo=Mock(side_effect=self.security), GetAce=Mock()
        )
        self.kernel = SimpleNamespace(
            CreateFileW=Mock(return_value=123),
            GetFileInformationByHandle=Mock(side_effect=self.metadata),
            GetFileType=Mock(return_value=1),
            CloseHandle=Mock(),
            LocalFree=Mock(),
        )
        for target, value in (
            ("canonical", lambda p: Path(p)),
            ("libraries", lambda: (self.api, self.kernel)),
            ("user_sid", lambda: "synthetic-owner"),
            ("sid_text", lambda p: "synthetic-owner"),
        ):
            mock = patch.object(windows, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.path = Path("synthetic-private-item")

    def metadata(self, handle, info):
        for name, value in self.info.items():
            setattr(info._obj, name, value)
        return True

    def security(self, handle, kind, flags, owner, group, acl, sacl, descriptor):
        owner._obj.value = 1234
        acl._obj.value = C.addressof(self.acl)
        descriptor._obj.value = 5678
        return 0

    def test_incomplete_path_stat_cannot_reject_a_fully_verified_open_file(self):
        # Windows stat's sharing-violation fallback has no link count. The
        # handle query still proves exactly one link; no zero-count exception.
        with patch.object(
            Path, "stat", return_value=SimpleNamespace(st_mode=0o100600, st_nlink=0)
        ):
            self.assertEqual(windows.private(self.path, owner=True), self.path)
        self.kernel.CreateFileW.assert_called_once_with(
            str(self.path), 0x20080, 7, None, 3, 0x02200000, None
        )
        self.assertEqual(self.api.GetSecurityInfo.call_args.args[0], 123)
        self.kernel.CloseHandle.assert_called_once_with(123)
        self.kernel.LocalFree.assert_called_once()

    def test_unknown_links_hardlinks_reparse_and_type_mismatch_remain_blocked(self):
        for info in (
            dict(links=0),
            dict(links=2),
            dict(attributes=0x400),
            dict(attributes=0x10),
        ):
            with self.subTest(info=info):
                self.info = dict(attributes=0, links=1)
                self.info.update(info)
                with self.assertRaisesRegex(ValueError, "Owned private"):
                    windows.private(self.path)
        self.assertEqual(self.kernel.CloseHandle.call_count, 4)
        self.api.GetSecurityInfo.assert_not_called()

    def test_owner_dacl_and_native_failures_fail_closed_and_close_handle(self):
        with patch.object(windows, "sid_text", return_value="other-owner"):
            with self.assertRaisesRegex(ValueError, "Owned private"):
                windows.private(self.path, owner=True)
        self.api.GetSecurityInfo.side_effect = None
        self.api.GetSecurityInfo.return_value = 5
        with self.assertRaisesRegex(ValueError, "DACL"):
            windows.private(self.path)
        self.kernel.GetFileInformationByHandle.side_effect = None
        self.kernel.GetFileInformationByHandle.return_value = False
        with self.assertRaisesRegex(ValueError, "metadata"):
            windows.private(self.path)
        self.assertEqual(self.kernel.CloseHandle.call_count, 3)

    def test_unopenable_item_and_non_disk_handle_are_rejected(self):
        self.kernel.CreateFileW.return_value = C.c_void_p(-1).value
        with self.assertRaisesRegex(ValueError, "metadata"):
            windows.private(self.path)
        self.kernel.CloseHandle.assert_not_called()
        self.kernel.CreateFileW.return_value = 123
        self.kernel.GetFileType.return_value = 3
        with self.assertRaisesRegex(ValueError, "metadata"):
            windows.private(self.path)
        self.kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
