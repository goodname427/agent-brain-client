"""Diagnostic phase reporting must preserve unittest outcomes and cleanups."""
import io
import json
import unittest

from dev.checks.timing import TimingRunner


class TimingContract(unittest.TestCase):
    def test_success_failure_skip_and_cleanup_failure_keep_their_outcomes(self):
        cleaned = []

        class Sample(unittest.TestCase):
            def setUp(self):
                self.addCleanup(lambda: cleaned.append(self._testMethodName))

            def test_ok(self):
                pass

            def test_failure(self):
                self.fail('synthetic failure')

            @unittest.skip('synthetic skip')
            def test_skip(self):
                pass

            def test_cleanup_failure(self):
                self.addCleanup(lambda: 1 / 0)

        stream = io.StringIO()
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Sample)
        result = TimingRunner(stream=stream).run(suite)
        self.assertEqual(result.testsRun, 4)
        self.assertEqual((len(result.failures), len(result.errors), len(result.skipped)), (1, 1, 1))
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(cleaned), 3)
        rows = [json.loads(line.split('TEST_PHASES ', 1)[1])
                for line in stream.getvalue().splitlines() if 'TEST_PHASES ' in line]
        self.assertEqual(len(rows), 4)
        for row in rows:
            if not row['id'].endswith('test_skip'):
                self.assertEqual(set(row) - {'id'}, {'setup', 'test', 'teardown', 'cleanup'})
                self.assertTrue(all(value >= 0 for key, value in row.items() if key != 'id'))
