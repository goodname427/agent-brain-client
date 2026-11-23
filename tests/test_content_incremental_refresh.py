"""Bounded refresh traffic and truthful mapping failures, synthetic homes only."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_content_outbox as fixture
from agent_brain_client.adapters.harnesses.codebuddy.content_ports import CodeBuddyContentPorts
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.interfaces.mcp.content import ManagedContentServer
import agent_brain_client.domain.contract as contract


class IncrementalRefresh(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        for index in range(40):
            self.f.cloud.records['memory','synthetic-'+str(index)]=copy.deepcopy(fixture.MEMORY)
        self.assertEqual(self.f.adapter.refresh()['status'],'content-refreshed')
        self.f.cloud.calls.clear()

    def restart(self):
        return ContentSession(self.f.home,self.f.cache,fixture.POLICY,self.f.cloud,host=CodexContentPorts, native_home=self.f.home/'.codex')

    def reads(self):
        return [r['params']['id'] for r in self.f.cloud.calls if r['method']=='content_read']

    def test_confirmed_write_refresh_uses_only_changed_records_with_bounded_transport(self):
        original=self.f.cloud.request
        def bounded(request):
            if len(self.reads())>=4:raise TimeoutError('Synthetic refresh transport budget')
            return original(request)
        self.f.cloud.request=bounded
        result=self.restart().invoke(self.f.request())
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(result['local_refresh']['status'],'content-refreshed')
        self.assertEqual(self.reads(),['synthetic-topic'])
        self.assertEqual((self.f.home/'.config/agent-brain-client/content/memory/synthetic-topic.md').read_bytes(),fixture.MEMORY['body'].encode())
        self.f.cloud.calls.clear()
        self.assertEqual(self.restart().refresh()['status'],'content-current')
        self.assertEqual(self.reads(),[])

    def test_new_revisions_and_deletions_follow_live_catalog(self):
        self.f.cloud.records['memory','synthetic-1']['body']='Synthetic remote change'
        del self.f.cloud.records['memory','synthetic-2'];self.f.cloud.puts+=1
        result=self.restart().refresh()
        self.assertEqual(result['status'],'content-refreshed')
        self.assertEqual(self.reads(),['synthetic-1'])
        self.assertFalse((self.f.home/'.config/agent-brain-client/content/memory/synthetic-2.md').exists())

    def test_unverified_snapshot_is_never_reused(self):
        active=json.loads((self.f.cache/'active.json').read_bytes())
        path=self.f.cache/'snapshots'/(active['commit']+'.json')
        snapshot=json.loads(path.read_bytes());snapshot['records'][0]['record']['body']='Synthetic tamper'
        path.write_bytes(contract.encode(snapshot))
        self.assertEqual(self.restart().refresh()['status'],'content-refreshed')
        self.assertEqual(len(self.reads()),40)

    def test_catalog_race_preserves_active_and_owned_files(self):
        active=(self.f.cache/'active.json').read_bytes();original=self.f.cloud.request;calls=0
        def race(request):
            nonlocal calls
            if request['method']=='content_catalog':
                calls+=1
                if calls==2:self.f.cloud.puts+=1
            return original(request)
        self.f.cloud.request=race
        result=self.restart().refresh()
        self.assertEqual(result['stage'],'remote-snapshot')
        self.assertEqual(result['status'],'content-refresh-blocked')
        self.assertEqual((self.f.cache/'active.json').read_bytes(),active)

    def test_unchanged_busy_host_file_does_not_block_unrelated_write(self):
        import agent_brain_client.adapters.storage.host_files as brain_host_files
        original=brain_host_files.host_stage;written=[]
        def busy(path,raw,mode,expected):
            # Models the Windows sharing violation for an unchanged AGENTS
            # file held open by the host. No real native files are touched.
            if path.name=='AGENTS.md':raise PermissionError('Synthetic host reader excludes writes')
            written.append(path.name)
            return original(path,raw,mode,expected)
        with patch.object(brain_host_files,'host_stage',side_effect=busy):
            result=self.restart().invoke(self.f.request())
        self.assertEqual(result['local_refresh']['status'],'content-refreshed')
        self.assertEqual(written,['synthetic-topic.md'])

    def test_snapshot_only_change_does_not_claim_native_writes(self):
        self.f.cloud.puts+=1
        with patch('agent_brain_client.adapters.storage.native_mapping.host_apply',wraps=__import__('agent_brain_client.adapters.storage.native_mapping',fromlist=['host_apply']).host_apply) as apply:
            result=self.restart().refresh()
        self.assertEqual(result['status'],'content-refreshed')
        self.assertFalse(result['native_changed'])
        self.assertEqual(apply.call_args.args[0],{})

    def test_identical_unowned_file_is_preserved_without_claiming_ownership(self):
        path=self.f.home/'.config/agent-brain-client/content/memory/synthetic-topic.md'
        path.write_bytes(fixture.MEMORY['body'].encode())
        result=self.restart().invoke(self.f.request())
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(result['local_refresh']['status'],'content-refresh-blocked')
        self.assertEqual(result['local_refresh']['stage'],'native-preflight')
        self.assertNotIn('.config/agent-brain-client/content/memory/synthetic-topic.md',json.loads((self.f.cache/'owned.json').read_bytes())['files'])

    def test_owned_drift_and_write_failure_do_not_erase_server_save(self):
        path=self.f.home/'.config/agent-brain-client/content/memory/synthetic-1.md'
        path.write_bytes(b'Synthetic outside edit')
        result=self.restart().invoke(self.f.request())
        self.assertTrue(result['local_queue']['server_saved'])
        self.assertEqual(result['local_refresh']['stage'],'owned-files')
        self.assertEqual(path.read_bytes(),b'Synthetic outside edit')
        self.assertEqual(self.f.cloud.puts,1)

    def test_snapshot_failure_reports_stage_and_rolls_back(self):
        active=(self.f.cache/'active.json').read_bytes();original=self.f.adapter._save
        self.f.cloud.puts+=1
        def fail(name,value):
            if name.startswith('snapshots/'):raise PermissionError('Synthetic private path must not escape')
            return original(name,value)
        with patch.object(self.f.adapter,'_save',side_effect=fail):result=self.f.adapter.refresh()
        self.assertEqual(result['stage'],'snapshot-save')
        self.assertEqual(result['reason'],'file-access-failed')
        self.assertNotIn('Synthetic private path',str(result))
        self.assertEqual((self.f.cache/'active.json').read_bytes(),active)


class BackgroundContentRefreshPort(unittest.TestCase):
    """Exercise the existing no-replay pull used by a background invocation."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.cloud = fixture.Cloud()

    def session(self, host):
        return ContentSession(
            self.home, self.home / (host.name + "-cache"), fixture.POLICY,
            self.cloud, host=host, native_home=self.home / ("." + host.name),
        )

    def test_remote_content_changes_reach_both_projections_on_an_existing_connection(self):
        codex = self.session(CodexContentPorts)
        codebuddy = self.session(CodeBuddyContentPorts)
        servers = [ManagedContentServer(session, "0.8.34", "2025-11-25")
                   for session in (codex, codebuddy)]
        ordinary = self.home / ".codebuddy/rules/user-owned.md"
        ordinary.parent.mkdir(parents=True)
        ordinary.write_bytes(b"Synthetic unmanaged rule\n")
        codex_instructions = self.home / ".codex/AGENTS.md"
        codex_instructions.parent.mkdir()
        codex_instructions.write_bytes(b"Synthetic unmanaged instructions\n")
        base = self.home / ".config/agent-brain-client/content"
        memory_paths = [base / path / "synthetic-topic.md"
                        for path in ("memory", "codebuddy/memory")]
        rule_paths = [base / path / "synthetic-rule.md"
                      for path in ("rule", "codebuddy/rule")]
        skill_paths = [self.home / path / "synthetic-skill/SKILL.md"
                       for path in (".agents/skills", ".codebuddy/skills")]
        hosts = (CodexContentPorts, CodeBuddyContentPorts)

        rule = dict(fixture.RULE, enabled=True, trigger={"mode": "always", "patterns": []})
        skill = dict(fixture.SKILL, enabled=True)
        self.cloud.records.update({
            ("memory", "synthetic-topic"): fixture.MEMORY,
            ("rule", "synthetic-rule"): rule,
            ("rule", "outside-grant"): dict(rule, body="Synthetic unauthorized rule"),
            ("skill", "synthetic-skill"): skill,
        })
        for host in hosts:
            self.assertEqual(self.session(host).refresh(replay=False)["status"], "content-refreshed")
        for path in memory_paths:
            self.assertEqual(path.read_text(), fixture.MEMORY["body"])
        for path in rule_paths:
            self.assertEqual(path.read_text(), rule["body"])
        for path in skill_paths:
            self.assertEqual(path.read_text(), skill["files"]["SKILL.md"])
        self.assertIn(rule["body"], codex_instructions.read_text())
        self.assertIn(rule["body"], (ordinary.parent / "agent-brain-synthetic-rule.md").read_text())
        self.cloud.calls.clear()
        for host in hosts:
            self.assertEqual(self.session(host).refresh(replay=False)["status"], "content-current")
        self.assertTrue(self.cloud.calls)
        self.assertTrue(all(row["method"] == "content_catalog" for row in self.cloud.calls))
        before = {path: path.read_bytes() for path in (*memory_paths, *rule_paths, *skill_paths)}

        revised_skill = skill["files"]["SKILL.md"] + "\nSynthetic changed skill"
        self.cloud.records["skill", "synthetic-skill"] = dict(skill, files={"SKILL.md": revised_skill})
        self.cloud.records["memory", "synthetic-topic"] = dict(fixture.MEMORY, body="Synthetic changed memory")
        revised_rule = dict(rule, body="Synthetic changed rule")
        self.cloud.records["rule", "synthetic-rule"] = revised_rule
        self.cloud.puts += 1
        self.cloud.offline = True
        for host in hosts:
            self.assertEqual(self.session(host).refresh(replay=False)["status"], "content-refresh-blocked")
        self.assertEqual({path: path.read_bytes() for path in before}, before)

        self.cloud.offline = False
        for host in hosts:
            self.assertEqual(self.session(host).refresh(replay=False)["status"], "content-refreshed")
        for path in memory_paths:
            self.assertEqual(path.read_text(), "Synthetic changed memory")
        for path in rule_paths:
            self.assertEqual(path.read_text(), revised_rule["body"])
        for path in skill_paths:
            self.assertEqual(path.read_text(), revised_skill)

        # A remote archive removes the active rows from the catalog.
        del self.cloud.records["rule", "synthetic-rule"]
        del self.cloud.records["skill", "synthetic-skill"]
        self.cloud.puts += 1
        for host in hosts:
            self.assertEqual(self.session(host).refresh(replay=False)["status"], "content-refreshed")
        for path in (*rule_paths, *skill_paths):
            self.assertFalse(path.exists(), path)
        self.assertNotIn(revised_rule["body"], codex_instructions.read_text())
        self.assertTrue(codex_instructions.read_bytes().startswith(b"Synthetic unmanaged instructions\n"))
        self.assertFalse((ordinary.parent / "agent-brain-synthetic-rule.md").exists())
        self.assertEqual(ordinary.read_bytes(), b"Synthetic unmanaged rule\n")
        self.assertFalse(list(self.home.rglob("*outside-grant*")))
        self.assertTrue(all(row["method"] in ("content_catalog", "content_read")
                            and row["params"].get("id") != "outside-grant"
                            for row in self.cloud.calls))
        for server in servers:
            reply = server.handle({"jsonrpc": "2.0", "method": "tools/list", "id": 1})
            self.assertIn("content_read", {tool["name"] for tool in reply["tools"]})

    def test_no_replay_pull_preserves_the_pending_request_bytes(self):
        request = fixture.Outbox().request()
        self.cloud.records["memory", "synthetic-remote"] = fixture.MEMORY
        for host in (CodexContentPorts, CodeBuddyContentPorts):
            with self.subTest(host=host.name):
                session = self.session(host)
                self.assertEqual(session.refresh(replay=False)["status"], "content-refreshed")
                active = (session.cache / "active.json").read_bytes()
                self.cloud.offline = True
                self.assertEqual(session.invoke(request)["local_queue"]["state"], "pending")
                pending = session.cache / "outbox" / (request["request_id"] + ".json")
                saved = pending.read_bytes()
                self.cloud.offline = False
                self.cloud.calls.clear()
                result = self.session(host).refresh(replay=False)
                self.assertEqual(result["status"], "content-local-changes-retained")
                self.assertEqual(pending.read_bytes(), saved)
                self.assertEqual((session.cache / "active.json").read_bytes(), active)
                self.assertEqual(self.cloud.calls, [])


if __name__=='__main__':unittest.main()
