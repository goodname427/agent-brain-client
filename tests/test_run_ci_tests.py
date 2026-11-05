"""Regression tests for focused execution and complete failure propagation."""
import os
import sys
import unittest
from unittest.mock import patch

import run_ci_tests as runner


class RunnerContract(unittest.TestCase):
    def test_serial_failure_is_preserved(self):
        with patch.dict(os.environ, CI_TESTS='["test_synthetic"]'), \
                patch.object(sys, 'argv', ['run_ci_tests']), \
                patch.object(runner.subprocess, 'call', return_value=7) as call:
            with self.assertRaises(SystemExit) as result:
                runner.main()
        self.assertEqual(result.exception.code, 7)
        self.assertEqual(call.call_args.args[0][-1], 'test_synthetic')

    def test_parallel_worker_failure_fails_the_complete_run(self):
        def result(modules):
            return (7 if 'test_b' in modules else 0, '', 0.0)
        with patch.dict(os.environ, CI_TESTS='["test_a", "test_b"]'), \
                patch.object(sys, 'argv', ['run_ci_tests', '--workers', '2']), \
                patch.object(runner, 'run_group', side_effect=result) as run:
            with self.assertRaises(SystemExit) as status:
                runner.main()
        self.assertEqual(status.exception.code, 1)
        self.assertEqual(run.call_count, 2)

    def test_parallel_dispatches_every_module_individually(self):
        with patch.dict(os.environ, CI_TESTS='["test_a", "test_b", "test_c", "test_d"]'), \
                patch.object(sys, 'argv', ['run_ci_tests', '--workers', '2']), \
                patch.object(runner, 'run_group', return_value=(0, '', 0.0)) as run:
            with self.assertRaises(SystemExit) as status:
                runner.main()
        self.assertEqual(status.exception.code, 0)
        self.assertEqual(sorted(tuple(call.args[0]) for call in run.call_args_list),
                         [('test_a',), ('test_b',), ('test_c',), ('test_d',)])

    def test_empty_selection_keeps_full_discovery(self):
        with patch.dict(os.environ, CI_TESTS='[]'), \
                patch.object(sys, 'argv', ['run_ci_tests']), \
                patch.object(runner.subprocess, 'call', return_value=0) as call:
            with self.assertRaises(SystemExit) as status:
                runner.main()
        self.assertEqual(status.exception.code, 0)
        self.assertEqual(call.call_args.args[0][-3:], ['discover', '-s', 'tests'])

    def test_invalid_module_arguments_do_not_start_a_process(self):
        with patch.dict(os.environ, CI_TESTS='["test_a;echo injected"]'), \
                patch.object(sys, 'argv', ['run_ci_tests']), \
                patch.object(runner.subprocess, 'call') as call:
            with self.assertRaises(ValueError):
                runner.main()
        call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
