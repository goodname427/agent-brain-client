"""Run unittest selectors with per-case setup/body/teardown/cleanup timings.

This uses the normal unittest runner, including its failure and skip handling.
Phase times are nested under each selected test; manually invoked helper fixtures
are included in the caller's phase. Timings are diagnostic, never a pass gate.
"""

import json
import time
import unittest


class TimingResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self._phases = {}
        self._originals = {}
        for method, phase in (
            ("_callSetUp", "setup"),
            ("_callTestMethod", "test"),
            ("_callTearDown", "teardown"),
            ("doCleanups", "cleanup"),
        ):
            original = getattr(test, method)
            self._originals[method] = original

            def timed(*args, _original=original, _phase=phase, **kwargs):
                start = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self._phases[_phase] = time.perf_counter() - start

            setattr(test, method, timed)

    def stopTest(self, test):
        for method in self._originals:
            delattr(test, method)
        self.stream.writeln("TEST_PHASES " + json.dumps({
            "id": test.id(),
            **{name: round(seconds, 6) for name, seconds in self._phases.items()},
        }, sort_keys=True))
        super().stopTest(test)


class TimingRunner(unittest.TextTestRunner):
    resultclass = TimingResult


if __name__ == "__main__":
    unittest.main(module=None, testRunner=TimingRunner)
