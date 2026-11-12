"""Native-source safety and Windows long-path I/O, using synthetic files only."""
import os
from pathlib import Path
import tempfile
import unittest

from agent_brain_client.adapters.platforms.source_files import (
    canonical, open_native_source, native_source_stat,
)


class NativeSources(unittest.TestCase):
    def test_regular_file_and_hardlink_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / 'message.json'
            path.write_bytes(b'synthetic')
            with open_native_source(path)[0] as stream:
                self.assertEqual(stream.read(), b'synthetic')
                self.assertEqual(os.fstat(stream.fileno()).st_ino, native_source_stat(path).st_ino)
            os.link(path, Path(root) / 'alias.json')
            with self.assertRaises(ValueError):
                open_native_source(path)

    @unittest.skipUnless(os.name == 'nt', 'Windows native names')
    def test_ambiguous_windows_names_and_device_paths_are_rejected(self):
        for raw in (r'C:\synthetic\NUL', r'C:\synthetic\message.json:stream',
                    r'C:\synthetic\trailing.\message.json',
                    r'\\?\C:\synthetic\message.json', r'\\host\share\message.json'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                canonical(raw)

    @unittest.skipUnless(os.name == 'nt', 'Windows native long-path regression')
    def test_long_path_without_global_long_paths_policy(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / ('synthetic-' * 9) / ('nested-' * 12) / ('turn-' * 15) / 'message.json'
            self.assertGreater(len(str(path)), 260)
            extended = Path('\\\\?\\' + str(path))
            extended.parent.mkdir(parents=True)
            extended.write_bytes(b'future synthetic assistant')
            self.assertEqual(canonical(path), path)
            with open_native_source(path)[0] as stream:
                self.assertEqual(stream.read(), b'future synthetic assistant')
            self.assertEqual(native_source_stat(path).st_size, 26)
            # Cleanup explicitly through long-path I/O, independent of Python's
            # TemporaryDirectory implementation and the host policy setting.
            extended.unlink()
            for parent in [extended.parent, extended.parent.parent, extended.parent.parent.parent]:
                parent.rmdir()
