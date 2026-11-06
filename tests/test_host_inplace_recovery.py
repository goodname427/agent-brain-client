"""Install journal fault injection with synthetic homes and portable data I/O."""

import copy
from contextlib import contextmanager
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import brain_host_files as host
import content_install as install
import test_content_install as fixture


class PrefixWrites(unittest.TestCase):
    def test_every_prefix_and_final_truncate_is_recognized(self):
        for before, after in (
            (b"abcdefgh", b"ABC"),
            (b"ab", b"1234567"),
            (b"", b"abc"),
            (b"abc", b""),
            (b"abcd", b"abXY"),
        ):
            for offset in range(len(after) + 1):
                self.assertIsNotNone(
                    install.partial_prefix(
                        before, after, after[:offset] + before[offset:]
                    )
                )
            self.assertIsNotNone(install.partial_prefix(before, after, after))
            self.assertIsNone(install.partial_prefix(before, after, b"foreign edit!"))


class HostInplaceRecovery(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ContentInstall(
            "test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state"
        )
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.config = self.f.config
        self.before = self.config.read_bytes()
        self.identity = self.config.stat().st_ino
        self.receipt = (
            self.f.home / ".config/agent-brain-client/content-install-receipt.json"
        )
        self.plan = self.f.plan()
        self.pin = self.f.digest(self.plan)

        @contextmanager
        def stage(path, raw, mode, expected):
            # Replace only Win32 handle acquisition. Execute the real writer,
            # transaction, write-ahead journal, validation and recovery.
            self.assertEqual(Path(path).parent, self.f.native)
            with Path(path).open("r+b", buffering=0) as file:
                st = os.fstat(file.fileno())
                self.assertEqual(file.read(), expected)

                def commit():
                    host.host_write(file, raw)

                commit.inplace_identity = {"device": st.st_dev, "inode": st.st_ino}
                yield commit

        changed = patch.object(install, "host_stage", side_effect=stage)
        changed.start()
        self.addCleanup(changed.stop)

        def atomic(path, raw, mode, expected):
            with stage(path, raw, mode, expected) as commit:
                commit()

        changed = patch.object(install, "host_atomic", side_effect=atomic)
        changed.start()
        self.addCleanup(changed.stop)

    def apply(self, check=lambda p: None):
        return install.apply(self.plan, self.pin, check)

    def tear(self, file, raw):
        file.seek(0)
        file.write(raw[: max(1, len(raw) // 2)])
        file.flush()
        os.fsync(file.fileno())
        raise SystemExit("Synthetic process stop during data write")

    def interrupt(self):
        with (
            patch.object(host, "host_write", side_effect=self.tear),
            self.assertRaises(SystemExit),
        ):
            self.apply()
        journal = install.private_json(self.receipt)
        self.assertEqual(journal["phase"], "applying")
        self.assertIn(str(self.config), journal["host_inplace"])
        return journal

    def test_partial_write_and_partial_rollback_resume_same_plan(self):
        self.interrupt()
        with (
            patch.object(host, "host_write", side_effect=self.tear),
            self.assertRaises(SystemExit),
        ):
            self.apply()
        journal = install.private_json(self.receipt)
        self.assertIn(str(self.config), journal["host_rollback"])
        self.assertTrue(self.apply()["installed"])
        self.assertEqual(self.config.stat().st_ino, self.identity)
        self.assertTrue(install.inspect(self.f.home)["installed"])

    def test_data_written_before_truncate_is_recovered(self):
        self.assertTrue(self.apply()["installed"])
        self.f.publish("0.2.1")
        self.f.package = Path(self.f.channel.update()["active"]["package"])
        self.plan = self.f.plan(True)
        self.pin = self.f.digest(self.plan)

        def stop_after_data(file, raw):
            file.seek(0)
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
            raise SystemExit("Synthetic interruption before truncate")

        with (
            patch.object(host, "host_write", side_effect=stop_after_data),
            self.assertRaises(SystemExit),
        ):
            self.apply()
        self.assertTrue(self.apply()["installed"])
        self.assertEqual(self.config.stat().st_ino, self.identity)

    def test_write_error_restores_exact_preimage_then_retries(self):
        real = host.host_write
        calls = []

        def fail_once(file, raw):
            calls.append(True)
            if len(calls) == 1:
                file.seek(0)
                file.write(raw[: len(raw) // 2])
                file.flush()
                raise OSError("Synthetic write failure")
            real(file, raw)

        with patch.object(host, "host_write", side_effect=fail_once):
            self.assertEqual(self.apply()["status"], "content-install-rolled-back")
        self.assertEqual(self.config.read_bytes(), self.before)
        self.assertEqual(self.config.stat().st_ino, self.identity)
        self.assertTrue(self.apply()["installed"])

    def test_external_edit_after_partial_write_is_retained(self):
        self.interrupt()
        self.config.write_bytes(b"# synthetic outside edit\n")
        self.assertEqual(
            self.apply()["status"], "content-install-recovery-review-required"
        )
        self.assertEqual(self.config.read_bytes(), b"# synthetic outside edit\n")

    def test_replaced_file_with_identical_partial_bytes_is_retained(self):
        self.interrupt()
        image = self.config.read_bytes()
        other = self.config.with_name("outside.toml")
        other.write_bytes(image)
        os.replace(other, self.config)
        self.assertEqual(
            self.apply()["status"], "content-install-recovery-review-required"
        )
        self.assertEqual(self.config.read_bytes(), image)

    def test_altered_preimage_or_rollback_intent_cannot_authorize_recovery(self):
        journal = self.interrupt()
        altered = copy.deepcopy(journal)
        altered["before"][str(self.config)]["sha"] = "a" * 64
        with self.assertRaises(install.InstallBlocked):
            install.validate_recovery(altered, self.plan)
        altered = copy.deepcopy(journal)
        altered["host_rollback"] = {str(self.config): {"prefix": 10000000, "size": 0}}
        with self.assertRaises(install.InstallBlocked):
            install.validate_recovery(altered, self.plan)


if __name__ == "__main__":
    unittest.main()
