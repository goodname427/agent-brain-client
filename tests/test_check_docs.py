"""Documentation gate rejects broken navigation using only temporary fixtures."""
from pathlib import Path
import tempfile
import unittest

import dev.checks.docs as check_docs


class DocumentationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs/installation').mkdir(parents=True)
        (self.root / 'README.md').write_text(
            '# Entry\n\n[Install](docs/installation/mac.md#状态与使用)\n', encoding='utf-8')
        (self.root / 'docs/installation/mac.md').write_text(
            '# Install\n\n## 状态与使用\n\n[Entry](../../README.md)\n', encoding='utf-8')

    def write(self, name, text):
        (self.root / name).write_text(text, encoding='utf-8')

    def test_valid_links_unicode_fragment_and_explicit_anchor(self):
        self.write('docs/section.md', '# Section\n\n<a id="旧章节"></a>\n\n'
                   '[Continue](installation/mac.md#状态与使用)\n')
        self.write('README.md', '# Entry\n\n[Section](docs/section.md#旧章节)\n'
                   '[Encoded](docs/installation/mac.md#%E7%8A%B6%E6%80%81%E4%B8%8E%E4%BD%BF%E7%94%A8)\n')
        self.assertEqual(check_docs.check(self.root)['local_links'], 4)

    def test_missing_file_fails(self):
        self.write('README.md', '[Missing](docs/missing.md)\n')
        with self.assertRaisesRegex(ValueError, 'missing relative target'):
            check_docs.check(self.root)

    def test_missing_anchor_fails(self):
        self.write('README.md', '[Missing](docs/installation/mac.md#missing)\n')
        with self.assertRaisesRegex(ValueError, 'missing anchor'):
            check_docs.check(self.root)

    def test_removed_anchor_breaks_incoming_link(self):
        self.write('README.md', '[Section](docs/section.md#旧章节)\n')
        self.write('docs/section.md', '# Section\n\n[Continue](installation/mac.md)\n')
        with self.assertRaisesRegex(ValueError, 'missing anchor'):
            check_docs.check(self.root)

    def test_reference_definition_is_checked(self):
        self.write('README.md', '[Install][current]\n\n[current]: docs/missing.md\n')
        with self.assertRaisesRegex(ValueError, 'missing relative target'):
            check_docs.check(self.root)

    def test_repository_escape_fails(self):
        self.write('README.md', '[Outside](../private.md)\n')
        with self.assertRaisesRegex(ValueError, 'escapes repository'):
            check_docs.check(self.root)

    def test_examples_are_ignored_and_duplicate_headings_work(self):
        self.write('README.md', '# Entry\n\n## Same\n\n## Same\n\n[Again](#same-1)\n'
                   '```md\n[Example](missing.md)\n```\n`[Example](missing.md)`\n'
                   '[External](https://example.invalid/unavailable)\n')
        self.assertEqual(check_docs.check(self.root)['local_links'], 2)


if __name__ == '__main__':
    unittest.main()
