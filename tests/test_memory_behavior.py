from agent_brain_client.application.memory import guidance
from agent_brain_client.application.memory.guidance import HOST_GUIDANCE
"""Behavior policy cases use annotated synthetic evidence, no model/session reads."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_brain_client.application.memory.guidance import MEMORY_GUIDANCE, ONBOARDING_GUIDANCE
from agent_brain_client.application.installation.follow_up import installation_follow_up
from agent_brain_client.domain.sources import review_memory_sources
import agent_brain_client.interfaces.mcp.content as content_mcp

ONBOARD_PATH = "memory_onboard"
MEMORY_PATH = "docs/reference/memory-behavior.md"
MAINTENANCE_PATH = "docs/installation/maintenance.md"
OLD_SKILLS = ("agent-brain-onboard", "agent-brain-memory", "agent-brain-maintain")


class MemoryBehavior(unittest.TestCase):
    def test_consumer_instructions_delegate_behavior_and_describe_complete_target_schema(self):
        class Adapter:
            policy = {'kinds': ['memory', 'rule', 'skill']}
            def methods(self):
                return set(content_mcp.contract.FIELDS)
            def refresh(self):
                return {'status': 'synthetic-only'}

        server = content_mcp.ContentServer(Adapter(), client_version='0.2.12')
        initialized = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                     'params': {'protocolVersion': '2025-11-25', 'capabilities': {},
                                                'clientInfo': {'name': 'synthetic-test', 'version': '1'}}})
        instructions = initialized['instructions']
        for path in (ONBOARD_PATH, MEMORY_PATH, MAINTENANCE_PATH):
            self.assertIn(path, instructions)
        for name in OLD_SKILLS:
            self.assertNotIn(name, instructions)
        self.assertIn('AgentBrain destination records only', instructions)
        self.assertNotIn('Do not importance-filter existing memories', instructions)
        server.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        listed = server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
        catalog = next(tool for tool in listed['tools'] if tool['name'] == 'content_catalog')
        self.assertIn('does not discover Harness-native sources', catalog['description'])
        put = next(tool for tool in listed['tools'] if tool['name'] == 'content_put')
        record_schema = put['inputSchema']['properties']['record']
        self.assertEqual(len(record_schema['oneOf']), 3)
        self.assertIn('description', record_schema['oneOf'][2]['required'])
        self.assertIn('SKILL.md', record_schema['oneOf'][2]['properties']['files']['required'])


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
            self.assertEqual(result['onboard_check']['next_action'], ONBOARDING_GUIDANCE)
            self.assertFalse(result['onboard_check']['data_transferred'])
            self.assertFalse(result['onboard_check']['source_scan_performed'])
            self.assertIn(ONBOARD_PATH, result['onboard_check']['next_action'])
            for name in OLD_SKILLS:
                self.assertNotIn(name, result['onboard_check']['next_action'])
            self.assertIn('source_scan_performed', result['onboard_check'])
            self.assertFalse(result['onboard_check']['data_transferred'])
        self.assertEqual(original, before)
        self.assertNotIn('onboard_check', installation_follow_up({'installed': False}))

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

    def test_actual_startup_routes_to_public_docs_and_keeps_user_mappings(self):
        import test_content_outbox as fixtures
        from agent_brain_client.interfaces.mcp.content import ContentServer

        fixture = fixtures.Outbox(
            "test_write_saved_and_fsynced_before_network_and_restart_reconnect"
        )
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cloud.records["rule", "synthetic-rule"] = dict(
            fixtures.RULE, enabled=True, trigger={"mode": "always", "patterns": []}
        )
        fixture.cloud.records["skill", "synthetic-skill"] = dict(fixtures.SKILL, enabled=True)
        native_agents = fixture.home / ".codex/AGENTS.md"
        native_agents.parent.mkdir(exist_ok=True)
        custom_rule = "Synthetic local user instructions"
        native_agents.write_text(custom_rule + "\n", encoding="utf-8")
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
        mapped = native_agents.read_text(encoding="utf-8")
        self.assertTrue(mapped.startswith(custom_rule))
        self.assertIn(fixtures.RULE["body"], mapped)
        self.assertIn(guidance.HOST_GUIDANCE, mapped)
        self.assertIn(ONBOARD_PATH, mapped)
        for name in OLD_SKILLS:
            self.assertNotIn(name, mapped)
        skills = fixture.home / ".agents/skills"
        self.assertEqual([path.name for path in skills.iterdir()], ["synthetic-skill"])
        self.assertEqual(
            (skills / "synthetic-skill/SKILL.md").read_text(encoding="utf-8"),
            fixtures.SKILL["files"]["SKILL.md"],
        )

    def test_daily_cli_only_reports_metadata(self):
        sources, rows = self.material()
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "src/agent_brain_client/interfaces/cli/sources.py")],
            input=json.dumps(
                {"mode": "daily", "sources": sources, "candidates": rows}
            ),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["data_transferred"])
        self.assertNotIn("migrate all existing memories", MEMORY_GUIDANCE)


    def test_guidance_distinguishes_server_save_from_backup_and_protocol_defers_behavior(self):
        self.assertIn("local-confirmed", MEMORY_GUIDANCE)
        self.assertIn("local_durable=true", MEMORY_GUIDANCE)
        self.assertIn("github_confirmed=false", MEMORY_GUIDANCE)
        self.assertIn("Server saved the write", MEMORY_GUIDANCE)
        self.assertIn("GitHub backup", MEMORY_GUIDANCE)
        self.assertIn("remote-confirmed with durable=true", MEMORY_GUIDANCE)

        protocol = (ROOT / "docs/reference/PROTOCOL.md").read_text()
        for path in (ONBOARD_PATH, MEMORY_PATH, MAINTENANCE_PATH):
            self.assertIn(path, protocol)
        for name in OLD_SKILLS:
            self.assertNotIn(name, protocol)


if __name__ == "__main__":
    unittest.main()
