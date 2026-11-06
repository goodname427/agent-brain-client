"""Exact install plan entry, with safe failure and recovery behavior."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import io, json, os, tempfile, unittest, sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src"), str(ROOT / "src")]
import agent_brain_client.interfaces.cli.install_plan as entry


class Management(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for n in ("home", "codex"):
            (self.root / n).mkdir()
        env = patch.dict(
            os.environ,
            {"HOME": str(self.root / "home"), "CODEX_HOME": str(self.root / "codex")},
            clear=True,
        )
        env.start()
        self.addCleanup(env.stop)

    def run_entry(self, args):
        with patch("sys.stdout", new=io.StringIO()) as output:
            code = entry.main(args)
        return code, json.loads(output.getvalue())

    def test_apply_loads_pinned_plan_and_delegates_recovery_before_preimages(self):
        plan = {"schema": 1, "fixture": "synthetic"}
        calls = []
        with (
            patch(
                "agent_brain_client.adapters.harnesses.codex.installation.inspect_plan",
                side_effect=AssertionError("Recovery must run first"),
            ) as inspect,
            patch.object(entry, "pinned", return_value=plan),
            patch(
                "agent_brain_client.adapters.harnesses.codex.installation.apply",
                side_effect=lambda p, d: calls.append(p) or {"installed": True},
            ),
        ):
            code, value = self.run_entry(
                ["apply", "--plan-file", "synthetic-plan", "--plan-sha256", "a" * 64]
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [plan])
        inspect.assert_not_called()

    def test_drift_before_apply_never_runs_mutator(self):
        with (
            patch.object(entry, "pinned", side_effect=ValueError()),
            patch(
                "agent_brain_client.adapters.harnesses.codex.installation.apply"
            ) as apply,
        ):
            code, value = self.run_entry(
                ["apply", "--plan-file", "synthetic-plan", "--plan-sha256", "a" * 64]
            )
        apply.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(value["native_state"], "unconfirmed-review-required")

    def test_partial_apply_error_does_not_claim_rollback_or_unchanged_state(self):
        with (
            patch.object(entry, "pinned", return_value={}),
            patch(
                "agent_brain_client.adapters.harnesses.codex.installation.apply",
                side_effect=OSError(),
            ),
        ):
            code, value = self.run_entry(
                ["apply", "--plan-file", "synthetic-plan", "--plan-sha256", "a" * 64]
            )
        self.assertEqual(code, 2)
        self.assertNotIn("native_state_preserved", value)


if __name__ == "__main__":
    unittest.main()
