"""Behavior policy cases use annotated synthetic evidence, no model/session reads."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "adapters/codex"))
from content_contract import MEMORY_GUIDANCE, ONBOARD_COVERAGE_GUIDANCE, installation_follow_up, review_memory_sources


class MemoryBehavior(unittest.TestCase):
    def review_cli(self, sources, rows):
        result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'tools/memory_review.py')],
                                input=json.dumps({'mode': 'onboard', 'sources': sources, 'candidates': rows}),
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_install_handoff_is_platform_independent_and_old_markers_do_not_prove_coverage(self):
        original = {'installed': True, 'model_loaded': False,
                    'installation_complete': True, 'prior_import_complete': True,
                    'onboard_check': {'status': 'complete', 'coverage': 'old-sources'}}
        before = copy.deepcopy(original)
        for platform in ('darwin', 'win32', 'linux'):
            with patch.object(sys, 'platform', platform):
                result = installation_follow_up(original)
            self.assertEqual(result['model_loaded'], 'unknown')
            self.assertEqual(result['onboard_check']['coverage'], 'unknown')
            self.assertEqual(result['onboard_check']['status'], 'coverage-check-required')
            self.assertEqual(result['onboard_check']['next_action'], ONBOARD_COVERAGE_GUIDANCE)
            self.assertFalse(result['onboard_check']['data_transferred'])
            self.assertFalse(result['onboard_check']['source_scan_performed'])
            self.assertTrue(result['onboard_check']['harness_source_inventory_required'])
            self.assertFalse(result['onboard_check']['agentbrain_catalog_is_source_inventory'])
            self.assertIn('predate AgentBrain', result['onboard_check']['next_action'])
            self.assertIn('rebuildable target cache', result['onboard_check']['next_action'])
            self.assertIn('dated direct evidence', result['onboard_check']['next_action'])
        self.assertEqual(original, before)
        self.assertNotIn('onboard_check', installation_follow_up({'installed': False}))

    def test_partial_first_import_and_unchecked_new_harness_remain_coverage_gaps(self):
        sources, rows = self.material()
        partial = self.review_cli(sources, rows[:3])
        self.assertEqual(partial['onboard_check']['status'], 'coverage-incomplete')
        self.assertFalse(partial['accessible_review_complete'])
        sources = sources[:1] + [{'source_id': 'approved-new-harness', 'status': 'not-checked',
                                  'authorized': True, 'expected_candidates': 1,
                                  'reason': 'New authorized synthetic memory source'}]
        missing = self.review_cli(sources, rows)
        self.assertEqual(missing['onboard_check']['status'], 'coverage-incomplete')
        sources[1]['status'] = 'read'
        new = dict(rows[0], id='new-small-fact', source_id='approved-new-harness', utility='none')
        checked = self.review_cli(sources, rows + [new])
        self.assertEqual(checked['onboard_check']['status'], 'provided-sources-reviewed')
        self.assertFalse(checked['onboard_check']['source_discovery_performed'])
        self.assertEqual(checked['onboard_check']['scope'], 'explicit-source-metadata-only')
        self.assertEqual(next(r['decision'] for r in checked['candidates'] if r['id'] == 'new-small-fact'), 'candidate')
        self.assertFalse(checked['account_complete'])
        self.assertFalse(checked['data_transferred'])

    def test_repeated_equivalent_source_review_requires_no_full_reupload(self):
        sources, rows = self.material()
        sources = sources[:1]
        for row in rows:
            if not row['sensitive'] and not row['raw_history'] and row['scope'] == 'personal-shared':
                row['duplicate'] = True
        first = self.review_cli(sources, rows)
        second = self.review_cli(sources, rows)
        self.assertEqual(first, second)
        self.assertEqual(first['onboard_check']['incremental_candidates'], 0)
        self.assertEqual(first['onboard_check']['equivalent_records'], 6)
        self.assertEqual(first['onboard_check']['status'], 'provided-sources-reviewed')
        self.assertFalse(first['data_transferred'])
        self.assertFalse(first['permissions_granted'])

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
