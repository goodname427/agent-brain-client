"""Synthetic Win32 metadata contracts; native concurrency lives in WindowsNative."""

import ctypes as C
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_brain_client.adapters.platforms.windows.native as windows
import agent_brain_client.adapters.platforms.filesystem as storage


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
            self.assertEqual(storage.windows_private(self.path, owner=True), self.path)
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
                    storage.windows_private(self.path)
        self.assertEqual(self.kernel.CloseHandle.call_count, 4)
        self.api.GetSecurityInfo.assert_not_called()

    def test_owner_dacl_and_native_failures_fail_closed_and_close_handle(self):
        with patch.object(windows, "sid_text", return_value="other-owner"):
            with self.assertRaisesRegex(ValueError, "Owned private"):
                storage.windows_private(self.path, owner=True)
        self.api.GetSecurityInfo.side_effect = None
        self.api.GetSecurityInfo.return_value = 5
        with self.assertRaisesRegex(ValueError, "DACL"):
            storage.windows_private(self.path)
        self.kernel.GetFileInformationByHandle.side_effect = None
        self.kernel.GetFileInformationByHandle.return_value = False
        with self.assertRaisesRegex(ValueError, "metadata"):
            storage.windows_private(self.path)
        self.assertEqual(self.kernel.CloseHandle.call_count, 3)

    def test_unopenable_item_and_non_disk_handle_are_rejected(self):
        self.kernel.CreateFileW.return_value = C.c_void_p(-1).value
        with self.assertRaisesRegex(ValueError, "metadata"):
            storage.windows_private(self.path)
        self.kernel.CloseHandle.assert_not_called()
        self.kernel.CreateFileW.return_value = 123
        self.kernel.GetFileType.return_value = 3
        with self.assertRaisesRegex(ValueError, "metadata"):
            storage.windows_private(self.path)
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_cache_metadata_does_not_require_security_descriptor_access(self):
        self.api.GetSecurityInfo.side_effect = AssertionError('Cache ACL gate removed')
        with (
            patch.object(storage, 'require_platform'),
            patch.object(storage, 'os', SimpleNamespace(name='nt')),
        ):
            self.assertEqual(storage.cache_mode(self.path), self.path)
            self.kernel.CreateFileW.assert_called_once_with(
                str(self.path), 0x80, 7, None, 3, 0x02200000, None
            )
            self.info['links'] = 2
            with self.assertRaisesRegex(ValueError, 'Owned private'):
                storage.cache_mode(self.path)
        self.api.GetSecurityInfo.assert_not_called()

    def test_legacy_new_file_postcheck_revalidates_owned_handle_and_never_closes_it(
        self,
    ):
        legacy = ValueError("Owned private Windows item required")
        with (
            patch.object(storage, "require_platform"),
            patch.object(storage, "os", SimpleNamespace(name="nt")),
            patch.object(windows, "protect_descriptor", side_effect=legacy),
            patch.dict(
                "sys.modules", {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: 123)}
            ),
        ):
            storage.file_mode(5, 0o600)
            self.info["links"] = 2
            with self.assertRaisesRegex(ValueError, "Owned private"):
                storage.file_mode(5, 0o600)
        self.assertEqual(self.kernel.GetFileInformationByHandle.call_count, 2)
        self.kernel.CloseHandle.assert_not_called()

    def test_legacy_dacl_assignment_failure_is_never_treated_as_metadata_fallback(self):
        with (
            patch.object(storage, "require_platform"),
            patch.object(storage, "os", SimpleNamespace(name="nt")),
            patch.object(
                windows,
                "protect_descriptor",
                side_effect=ValueError("Private Windows DACL unavailable"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "DACL unavailable"):
                storage.file_mode(5, 0o600)
        self.kernel.GetFileInformationByHandle.assert_not_called()

    def test_lock_creation_installs_owner_and_dacl_atomically_without_rewriting_existing_files(
        self,
    ):
        def descriptor(text, revision, result, length):
            self.assertIn("O:synthetic-ownerD:P", text)
            result._obj.value = 5678
            return True

        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW = Mock(
            side_effect=descriptor
        )
        native = SimpleNamespace(open_osfhandle=Mock(return_value=5))
        with (
            patch.dict("sys.modules", {"msvcrt": native}),
            patch.object(storage, "os", SimpleNamespace(O_RDWR=2, O_BINARY=0x8000)),
        ):
            self.assertEqual(storage._windows_open_lock(self.path), 5)
        args = self.kernel.CreateFileW.call_args.args
        self.assertEqual(args[:3], (str(self.path), 0xC0000000, 3))
        self.assertEqual(args[4:], (4, 0x00200080, None))
        self.assertEqual(args[3]._obj.descriptor, 5678)
        self.assertFalse(args[3]._obj.inherit)
        self.kernel.CloseHandle.assert_not_called()
        self.kernel.LocalFree.assert_called_once()

    def test_rejected_lock_metadata_closes_the_open_file(self):
        handle = Mock()
        handle.fileno.return_value = 5
        with (
            patch.object(storage, "require_platform"),
            patch.object(storage, "private_directory"),
            patch.object(storage, "_windows_open_lock", return_value=5),
            patch.object(
                storage,
                "_windows_private_handle",
                side_effect=ValueError("unsafe owner"),
            ),
            patch.object(
                storage,
                "os",
                SimpleNamespace(name="nt", fdopen=Mock(return_value=handle)),
            ),
            patch.dict(
                "sys.modules", {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: 123)}
            ),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe owner"):
                with storage.locked(self.path):
                    self.fail("Unsafe lock was yielded")
        handle.close.assert_called_once()

    def test_cache_lock_creation_uses_native_inheritance_without_sid_gate(self):
        native = SimpleNamespace(open_osfhandle=Mock(return_value=5))
        with (
            patch.dict('sys.modules', {'msvcrt':native}),
            patch.object(storage, 'os', SimpleNamespace(O_RDWR=2, O_BINARY=0x8000)),
            patch.object(windows, 'user_sid', side_effect=AssertionError('No cache SID list')),
        ):
            self.assertEqual(storage._windows_open_lock(self.path, cache=True), 5)
        self.assertIsNone(self.kernel.CreateFileW.call_args.args[3])
        self.kernel.LocalFree.assert_not_called()


if __name__ == "__main__":
    unittest.main()
