"""Behavior policy cases use annotated synthetic evidence, no model/session reads."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/codex"))
from content_contract import MEMORY_GUIDANCE, review_memory_sources


class MemoryBehavior(unittest.TestCase):
    def material(self):
        # Same source material: a coffee preference, a small task result, a
        # useful mundane fact, a one-off font request, uncertain inference,
        # a duplicate, a company secret, raw transcript and project-only data.
        sources = [
            {
                "source_id": "approved-memory",
                "status": "read",
                "authorized": True,
                "expected_candidates": 9,
                "reason": "Explicit synthetic existing-memory export",
            },
            {
                "source_id": "other-export",
                "status": "not-checked",
                "authorized": False,
                "expected_candidates": None,
                "reason": "No authorized retrieval evidence",
            },
        ]
        rows = []
        for identity, utility, evidence, scope, sensitive, history, duplicate in [
            (
                "coffee",
                "preference",
                "user-stated",
                "personal-shared",
                False,
                False,
                False,
            ),
            (
                "small-progress",
                "continuity",
                "observed-result",
                "personal-shared",
                False,
                False,
                False,
            ),
            (
                "mundane-fact",
                "useful-fact",
                "user-stated",
                "personal-shared",
                False,
                False,
                False,
            ),
            (
                "one-off-font",
                "ephemeral",
                "user-stated",
                "personal-shared",
                False,
                False,
                False,
            ),
            (
                "old-inference",
                "none",
                "inferred",
                "personal-shared",
                False,
                False,
                False,
            ),
            (
                "duplicate",
                "preference",
                "user-stated",
                "personal-shared",
                False,
                False,
                True,
            ),
            (
                "company-secret",
                "continuity",
                "observed-result",
                "personal-shared",
                True,
                False,
                False,
            ),
            (
                "raw-transcript",
                "continuity",
                "user-stated",
                "personal-shared",
                False,
                True,
                False,
            ),
            (
                "project-file",
                "continuity",
                "observed-result",
                "project-only",
                False,
                False,
                False,
            ),
        ]:
            rows.append(
                dict(
                    id=identity,
                    source_id="approved-memory",
                    kind="memory",
                    scope=scope,
                    evidence=evidence,
                    utility=utility,
                    sensitive=sensitive,
                    raw_history=history,
                    duplicate=duplicate,
                    requested=False,
                )
            )
        return sources, rows

    def test_daily_keeps_useful_small_facts_without_major_importance_gate(self):
        sources, rows = self.material()
        report = review_memory_sources(sources, rows)
        choices = {x["id"]: x["decision"] for x in report["candidates"]}
        self.assertEqual(
            {k for k, v in choices.items() if v == "candidate"},
            {"coffee", "small-progress", "mundane-fact"},
        )
        self.assertEqual(choices["one-off-font"], "skipped")
        self.assertEqual(choices["old-inference"], "review")
        self.assertEqual(choices["company-secret"], "excluded")
        self.assertEqual(choices["project-file"], "deferred")
        self.assertFalse(report["account_complete"])

    def test_onboard_keeps_all_authorized_existing_memories_not_only_long_term(self):
        sources, rows = self.material()
        report = review_memory_sources(sources, rows, "onboard")
        selected = {
            x["id"] for x in report["candidates"] if x["decision"] == "candidate"
        }
        self.assertEqual(
            selected,
            {
                "coffee",
                "small-progress",
                "mundane-fact",
                "one-off-font",
                "old-inference",
            },
        )
        self.assertEqual(report["counts"]["duplicate"], 1)
        self.assertEqual(report["counts"]["excluded"], 2)
        self.assertEqual(report["counts"]["deferred"], 1)
        self.assertEqual(report["coverage_gaps"], 1)
        self.assertTrue(report["accessible_review_complete"])
        self.assertFalse(report["data_transferred"])
        self.assertFalse(report["permissions_granted"])

    def test_missing_candidates_unavailable_and_explicit_request_are_visible(self):
        sources, rows = self.material()
        partial = review_memory_sources(sources, rows[:3], "onboard")
        self.assertFalse(partial["accessible_review_complete"])
        self.assertEqual(partial["sources"][0]["reviewed_candidates"], 3)
        requested = copy.deepcopy(rows)
        requested[3]["requested"] = True
        self.assertEqual(
            review_memory_sources(sources, requested)["candidates"][3]["decision"],
            "candidate",
        )
        sources[0]["status"] = "unavailable"
        self.assertTrue(
            all(
                x["decision"] in ("deferred", "excluded")
                for x in review_memory_sources(sources, rows, "onboard")["candidates"]
            )
        )

    def test_actual_startup_and_owned_mapping_include_behavior_policy(self):
        import test_content_outbox as fixtures
        from content_mcp import ContentServer

        fixture = fixtures.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        server = ContentServer(fixture.adapter)
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "synthetic", "version": "1"},
                },
            }
        )
        self.assertIn(MEMORY_GUIDANCE, reply["instructions"])
        self.assertIn(MEMORY_GUIDANCE, (fixture.home / ".codex/AGENTS.md").read_text())

    def test_tool_only_reports_metadata_and_guidance_has_no_three_item_import_cap(self):
        sources, rows = self.material()
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "tools/memory_review.py")],
            input=json.dumps(
                {"mode": "onboard", "sources": sources, "candidates": rows}
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["counts"]["candidate"], 5)
        self.assertIn("migrate all existing memories", MEMORY_GUIDANCE)
        self.assertIn("not an import", MEMORY_GUIDANCE)


if __name__ == "__main__":
    unittest.main()
