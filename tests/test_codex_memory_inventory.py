
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.platforms.tree import is_link
from agent_brain_client.adapters.platforms.posix.tree import PosixTree
from agent_brain_client.adapters.platforms.windows.tree import WindowsTree
import stat
import agent_brain_client.adapters.harnesses.codex.memory_inventory as adapters_harnesses_codex_memory_inventory
"""Synthetic persistent-memory inventory; never reads native session files."""
import json
import io
import os
from unittest.mock import patch
from types import SimpleNamespace
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import agent_brain_client.adapters.storage.content_session as content_cache
from agent_brain_client.adapters.storage.content_session import ContentSession, contract
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.adapters.harnesses.codex.memory_inventory import inventory_codex_memories
from agent_brain_client.adapters.storage.native_sources import inventory_tree
sys.path.insert(0,str(ROOT/'tests'))
import test_content_outbox as base
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.interfaces.mcp.framing import ContentParameterError, ProtocolError
from agent_brain_client.interfaces.mcp.server import serve


class CodexMemoryInventory(unittest.TestCase):
    def make_symlink(self,link,target):
        try:link.symlink_to(target)
        except OSError as exc:
            if os.name=='nt' and getattr(exc,'winerror',None)==1314:
                self.skipTest('Windows symlink privilege unavailable')
            raise

    def test_unicode_paths_keep_source_ids_and_fingerprints_and_ignore_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'合成用户';memories=home/'memories'
            (memories/'中文目录').mkdir(parents=True)
            raw='合成记忆：本周调整了说明牌。'.encode('utf-8')
            (memories/'中文目录'/'经历.md').write_bytes(raw)
            for name in ('sessions','memories_v2'):
                (home/name).mkdir();(home/name/'never-read').write_bytes(b'out of scope')
            row=inventory_codex_memories(home)['sources'][0]
            self.assertEqual(row['source_id'],'codex-memory:'+content_cache.sha('中文目录/经历.md'.encode('utf-8')))
            self.assertEqual(row['source_fingerprint'],content_cache.sha(raw))
            self.assertEqual(len(inventory_codex_memories(home)['sources']),1)

    def test_git_metadata_is_never_visited_and_old_ids_cannot_read_it(self):
        helper = base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        native_home = helper.home / '.codex'
        root = native_home / 'memories'
        files = {
            '.git/config': b'synthetic repository metadata',
            'nested/.GIT/index': b'synthetic repository index',
            'worktree/.GiT': b'gitdir: synthetic-metadata',
            'worktree/.git-notes/remember.md': b'synthetic user memory',
        }
        for relative, raw in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)

        def identifier(relative):
            return 'codex-memory:' + content_cache.sha(relative.encode('utf-8'))

        server = ContentServer(helper.adapter)
        provider = helper.adapter.sources.provider
        # Populate the private ledger as an older Client would.
        with patch.object(provider, 'inventory', return_value=inventory_tree(root, prefix=provider.prefix)):
            previous = helper.adapter.sources.provider.inventory()
        self.assertEqual(len(previous['sources']), len(files))
        rows = {row['source_id']: row for row in previous['sources']}

        backend = WindowsTree if os.name == 'nt' else PosixTree
        original_info = backend.info

        def reject_git_metadata(tree, directory, entry):
            self.assertNotEqual(entry.name.casefold(), '.git', 'Git metadata must be skipped before stat')
            return original_info(tree, directory, entry)

        note = 'worktree/.git-notes/remember.md'
        note_id = identifier(note)
        with patch.object(backend, 'info', reject_git_metadata):
            # Exercise exact reads before a new inventory can remove the old rows.
            for relative, raw in files.items():
                row = rows[identifier(relative)]
                self.assertEqual(row['source_fingerprint'], content_cache.sha(raw))
                params = {key: row[key] for key in ('source_id', 'source_fingerprint')}
                if relative == note:
                    result = helper.adapter.sources.read_onboarding_original(*params.values())
                    self.assertEqual(result[0].encode('utf-8'), raw)
                else:
                    with self.assertRaises(CacheBlocked):
                        helper.adapter.sources.read_onboarding_original(*params.values())

            current = helper.adapter.sources.provider.inventory()
            self.assertEqual(current['status'], 'complete')
            self.assertEqual(current['counts'], {'not-checked': 1})
            self.assertEqual([row['source_id'] for row in current['sources']], [note_id])
            standalone = inventory_codex_memories(native_home)
            self.assertEqual(standalone['sources'], [rows[note_id]])

        self.assertEqual({relative: (root / relative).read_bytes() for relative in files}, files)
        self.assertEqual(helper.cloud.calls, [])

    def test_reparse_attribute_is_excluded_even_for_regular_file_or_directory(self):
        for mode in (stat.S_IFREG,stat.S_IFDIR):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                memories=Path(tmp)/'memories';memories.mkdir();(memories/'placeholder').touch()
                fake=SimpleNamespace(st_mode=mode,st_file_attributes=0x400)
                backend=WindowsTree if os.name=='nt' else PosixTree
                with patch.object(backend,'info',return_value=fake),patch.object(backend,'open_file',side_effect=AssertionError('must not open')),patch.object(backend,'open_directory',side_effect=AssertionError('must not traverse')):
                    result=inventory_codex_memories(tmp)
                self.assertEqual(result['counts'],{'excluded':1})
                self.assertIsNone(result['sources'][0]['source_fingerprint'])

    def test_file_replaced_between_stat_and_open_is_unavailable(self):
        backend=WindowsTree if os.name=='nt' else PosixTree
        original=backend.open_file
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);memories=home/'memories';memories.mkdir()
            target=memories/'one.md';target.write_bytes(b'synthetic before')
            replacement=home/'replacement';replacement.write_bytes(b'synthetic after')
            def replace(tree,directory,entry,info):
                os.replace(replacement,target)
                return original(tree,directory,entry,info)
            with patch.object(backend,'open_file',replace):result=inventory_codex_memories(home)
            self.assertEqual(result['counts'],{'unavailable':1})
            self.assertEqual(result['status'],'incomplete')

    def test_read_file_replaced_between_stat_and_open_keeps_source_unread(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        root=helper.home/'.codex/memories';root.mkdir(parents=True)
        source=root/'one.md';source.write_bytes(b'synthetic source')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        replacement=helper.home/'replacement';replacement.write_bytes(b'synthetic replacement')
        backend=WindowsTree if os.name=='nt' else PosixTree
        original=backend.open_file
        def replace(tree,directory,entry,info):
            os.replace(replacement,source)
            return original(tree,directory,entry,info)
        with patch.object(backend,'open_file',replace),self.assertRaises(CacheBlocked):
            helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        self.assertIsNone(helper.adapter._load('memory-inventory.json'))
        self.assertEqual(helper.cloud.calls,[])

    def test_selected_source_replaced_by_symlink_never_reads_target(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        root=helper.home/'.codex/memories';root.mkdir(parents=True)
        source=root/'one.md';source.write_bytes(b'synthetic source')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        outside=helper.home/'outside';outside.write_bytes(b'synthetic source')
        source.unlink();self.make_symlink(source,outside)
        with self.assertRaises(CacheBlocked):helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        self.assertEqual(helper.adapter.sources.provider.inventory()['counts'],{'excluded':1})

    def test_replacement_and_content_change_require_new_fingerprint_before_read(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source=memories/'经历.md';source.write_bytes('合成原文'.encode('utf-8'))
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        replacement=helper.home/'replacement';replacement.write_bytes('合成新文'.encode('utf-8'))
        os.replace(replacement,source)
        with self.assertRaises(CacheBlocked):helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        fresh=helper.adapter.sources.provider.inventory()['sources'][0]
        self.assertEqual(fresh['source_id'],row['source_id'])
        self.assertNotEqual(fresh['source_fingerprint'],row['source_fingerprint'])
        self.assertEqual(helper.adapter.sources.read_onboarding_original(fresh['source_id'],fresh['source_fingerprint'])[0],'合成新文')
        source.write_bytes(b'changed in place')
        with self.assertRaises(CacheBlocked):helper.adapter.sources.read_onboarding_original(fresh['source_id'],fresh['source_fingerprint'])
        self.assertEqual(helper.cloud.calls,[])

    def test_read_rejects_invalid_utf8_and_oversize_after_inventory(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source=memories/'one.md';source.write_bytes(b'\xff')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        with self.assertRaises(CacheBlocked):helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        source.write_bytes(b'x'*131073)
        with self.assertRaises(CacheBlocked):helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        self.assertEqual(helper.adapter.sources.provider.inventory()['counts'],{'excluded':1})

    def test_fingerprints_only_memory_tree_and_produces_stable_change_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);memories=home/'memories';memories.mkdir()
            (memories/'summary.md').write_text('synthetic personal memory')
            (memories/'group').mkdir();(memories/'group'/'entry').write_text('synthetic correction')
            prohibited=home/'sessions';prohibited.mkdir();(prohibited/'session.jsonl').write_text('must never scan')
            first=inventory_codex_memories(home);again=inventory_codex_memories(home)
            self.assertEqual(first['status'],'complete')
            self.assertEqual(first['counts'],{'not-checked':2})
            self.assertEqual(first['cursor'],again['cursor'])
            self.assertTrue(all('summary.md' not in json.dumps(row) and 'entry' not in json.dumps(row)
                                for row in first['sources']))
            self.assertTrue(all(row['status']=='not-checked' for row in first['sources']))
            (memories/'group'/'entry').write_text('synthetic corrected fact')
            changed=inventory_codex_memories(home)
            self.assertNotEqual(first['cursor'],changed['cursor'])
            self.assertEqual(len(changed['sources']),2)

    def test_symlinks_are_excluded_and_oversized_scan_never_claims_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);memories=home/'memories';memories.mkdir()
            target=home/'outside';target.write_text('synthetic')
            self.make_symlink(memories/'link',target)
            result=inventory_codex_memories(home)
            self.assertEqual(result['counts'],{'excluded':1})
            self.assertEqual(result['sources'][0]['status'],'excluded')
            (memories/'one').write_text('one');(memories/'two').write_text('two')
            limited=inventory_codex_memories(home,max_files=1)
            self.assertEqual(limited['status'],'incomplete')

    def test_absent_memory_root_is_unavailable_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=inventory_codex_memories(Path(tmp))
            self.assertEqual(result['status'],'unavailable')
            self.assertEqual(result['sources'],[])

    def test_existing_ledger_survives_source_change_and_restart(self):
        helper = base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        root = helper.home / '.codex/memories'
        root.mkdir(parents=True)
        source = root / 'synthetic.md'
        source.write_text('synthetic durable fact')
        row = helper.adapter.sources.provider.inventory()['sources'][0]
        key = base.seed_legacy_native_review(helper.adapter, row, 'shareable', 'approved-native-original')
        saved = helper.adapter._load('memory-reviews.json')
        restarted = helper.restart()
        source.write_text('synthetic corrected durable fact')
        changed = restarted.sources.provider.inventory()['sources'][0]
        self.assertNotEqual(row['source_fingerprint'], changed['source_fingerprint'])
        self.assertEqual(restarted.sources.onboarding_evidence(), ({key}, set(), {}))
        self.assertEqual(restarted._load('memory-reviews.json'), saved)

    def test_explicit_plan_rejects_native_sources_and_coverage(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        source_file=memory_root/'experience.md';source_file.write_text('Synthetic user-owned project progress, generalized')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        fake_record={'title':'Synthetic progress','description':'Fixture','body':'Synthetic body','type':'feedback'}
        false_plan={'schema':1,'binding':helper.adapter.binding,
                    'sources':[{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],'kind':'memory','id':'synthetic-topic','record':fake_record}],
                    'coverage':[{'source_id':row['source_id'],'status':'read','reason':'unsubstantiated'}]}
        with self.assertRaises(CacheBlocked):helper.adapter.sources.stage_import(false_plan)
        for status in ('excluded','unavailable','deferred','skipped','duplicate'):
            forged=dict(false_plan,coverage=[{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                                             'status':status,'reason':'caller assertion only'}])
            with self.assertRaises(CacheBlocked):helper.adapter.sources.stage_import(forged)
        exact = row['source_id'], row['source_fingerprint']
        self.assertEqual(helper.adapter.sources.read_onboarding_original(*exact)[0],
                         'Synthetic user-owned project progress, generalized')
        base.seed_legacy_native_review(helper.adapter, row, 'shareable', 'approved-native-original')
        with self.assertRaises(CacheBlocked):
            helper.adapter.sources.stage_import(dict(false_plan, coverage=[]))
        self.assertFalse((helper.cache/'imports.json').exists())
        self.assertEqual(helper.cloud.calls,[])


    def test_incomplete_native_inventory_keeps_ordinary_cas_independent(self):
        helper = base.Outbox("test_write_saved_and_fsynced_before_network_and_restart_reconnect")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        memory_root = helper.home / ".codex/memories"
        memory_root.mkdir(parents=True)
        source_text = "This week I corrected a confusing exhibit sign and checked the shorter queue."
        (memory_root / "progress.md").write_text(source_text, encoding="utf-8")
        (memory_root / "unavailable.md").write_bytes(b"Synthetic inaccessible source")
        backend = WindowsTree if os.name == "nt" else PosixTree
        open_file = backend.open_file

        def open_readable_file(tree, directory, entry, info):
            if entry.name == "unavailable.md":
                raise PermissionError("Synthetic inaccessible source")
            return open_file(tree, directory, entry, info)

        self.enterContext(patch.object(backend, "open_file", open_readable_file))
        self.assertFalse((helper.home / ".agents/skills").exists())
        self.assertEqual(helper.cloud.records, {})

        server = ContentServer(helper.adapter)
        initialized = server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic", "version": "1"},
            },
        })
        self.assertIn("memory_onboard", initialized["instructions"])
        server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {tool["name"] for tool in listed["tools"]}

        def call(name, arguments):
            self.assertIn(name, tools)
            result = server.handle({
                "jsonrpc": "2.0", "id": name, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            self.assertFalse(result["isError"], result)
            return result["structuredContent"]

        self.assertEqual(call("content_catalog", {"kinds": ["memory"]})["result"]["artifacts"], [])
        requests_before_inventory = len(helper.cloud.calls)
        inventory = helper.adapter.sources.provider.inventory()
        self.assertEqual(inventory["status"], "incomplete")
        self.assertEqual(inventory["counts"]["unavailable"], 1)
        self.assertNotIn(source_text, json.dumps(inventory))
        row, = (item for item in inventory["sources"] if item["status"] == "not-checked")
        self.assertEqual(row["source_id"], "codex-memory:" + content_cache.sha(b"progress.md"))
        self.assertEqual(row["source_fingerprint"], content_cache.sha(source_text.encode()))
        source = {field: row[field] for field in ("source_id", "source_fingerprint")}
        self.assertEqual(
            helper.adapter.sources.read_onboarding_original(*source.values())[0], source_text
        )
        self.assertEqual(len(helper.cloud.calls), requests_before_inventory)

        target = {"kind": "memory", "id": "synthetic-topic"}
        baseline = call("content_read", target)["result"]
        self.assertIsNone(baseline["record"])
        self.assertEqual(baseline["revision"], "missing")
        record = {
            "title": "Synthetic ordinary save",
            "description": "Independent of native memory import",
            "body": "A synthetic note for the next meeting.",
            "type": "feedback",
        }
        self.assertNotIn("content_sources_stage", tools)
        self.assertIn("memory_onboard", tools)
        request_id = "synthetic_final_experience_save_01"
        saved = call("content_put", dict(
            target, record=record, expected_revision=baseline["revision"], request_id=request_id,
        ))
        self.assertEqual(saved["request_id"], request_id)
        self.assertEqual(saved["persistence"]["state"], "remote-confirmed")
        self.assertTrue(saved["persistence"]["durable"])
        puts = [request for request in helper.cloud.calls if request["method"] == "content_put"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0]["params"]["expected_revision"], baseline["revision"])
        self.assertEqual(call("content_read", target)["result"], saved["result"])
        self.assertEqual(saved["result"]["record"], record)
        self.assertEqual(helper.adapter.queue_status()["confirmed"], 1)
        self.assertEqual(helper.adapter.sources.import_status()["counts"], {})

        remaining = helper.adapter.sources.provider.inventory()
        self.assertEqual(remaining["status"], "incomplete")
        self.assertEqual(remaining["counts"]["unavailable"], 1)
        self.assertFalse((helper.home / ".agents/skills").exists())

    def test_legacy_stage_without_submitted_request_cannot_start_a_cas_write(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'experience.md').write_text('Synthetic experience evidence')
        server=ContentServer(helper.adapter)
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        record={'title':'Synthetic experience','description':'Fixture','body':'Synthetic normalized experience','type':'feedback'}
        source_key=base.seed_legacy_native_plan(helper.adapter,row,record)
        self.assertEqual(helper.cloud.calls,[])
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.sources.import_status()['counts'],{'inventoried':1})

        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':record,
              'expected_revision':'missing','request_id':'synthetic_explicit_save_001',
              'reconciles':{'request_ids':[],'source_keys':[source_key]}})
        self.assertTrue(saved['isError'],saved)
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.sources.import_status()['counts'],{'inventoried':1})

    def test_nonshareable_review_revokes_staged_and_queued_import(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'experience.md').write_text('Synthetic confidential source fixture')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        record={'title':'Synthetic experience','description':'Fixture','body':'Synthetic normalized experience','type':'feedback'}
        source_key=base.seed_legacy_native_plan(helper.adapter,row,record)

        helper.cloud.offline=True
        request={'request_id':'synthetic_revocable_save_001','method':'content_put',
            'params':{'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing'}}
        base.seed_legacy_native_write(helper.adapter,source_key,request)
        self.assertEqual(helper.adapter.queue_status()['pending'],1)
        base.seed_legacy_native_review(helper.adapter, row, 'excluded', 'secret-localonly-or-confidential')
        helper.cloud.offline=False
        helper.adapter.refresh()
        self.assertEqual(helper.cloud.puts,0)
        self.assertEqual(helper.adapter.queue_status()['blocked'],1)
        self.assertEqual(helper.adapter.sources.import_status()['counts'],{'blocked':1})
        with self.assertRaises(CacheBlocked):
            helper.adapter.invoke({'request_id':'synthetic_revocable_save_001','method':'content_put',
                'params':{'kind':'memory','id':'synthetic-topic','record':record,'expected_revision':'missing'}},
                reconciles={'request_ids':[],'source_keys':[source_key]})
        self.assertEqual(helper.cloud.puts,0)

    def test_native_memory_tools_require_memory_policy_scope(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        policy={'schema':1,'adapter':'codex','kinds':['rule'],'ids':{'rule':['synthetic-rule']},
                'dependencies':{'commands':[],'python_modules':[]}}
        helper.adapter=ContentSession(helper.home,helper.cache,policy,helper.cloud,host=CodexContentPorts, native_home=helper.home/'.codex')
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        (memories/'private.md').write_text('Synthetic fixture; must not be enumerated')
        server=ContentServer(helper.adapter)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        names={tool['name'] for tool in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']}
        self.assertFalse('memory_onboard' in names)
        with self.assertRaises(ContentParameterError):
            server.invoke('memory_onboard',{})
        self.assertFalse((helper.cache/'memory-inventory.json').exists())

    def test_native_memory_tools_require_memory_grant_even_when_policy_allows_it(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        helper.cloud.grant['artifact_types']=['rule','skill']
        server=ContentServer(helper.adapter)
        server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}})
        server.handle({'jsonrpc':'2.0','method':'notifications/initialized'})
        names={tool['name'] for tool in server.handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['tools']}
        self.assertFalse('memory_onboard' in names)
        with self.assertRaises(ContentParameterError):server.invoke('memory_onboard',{})
        self.assertFalse((helper.cache/'memory-inventory.json').exists())

    def test_native_offline_save_conflict_merge_and_durable_reconcile(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source_file=memories/'experience.md';source_file.write_text('Synthetic local experience version one')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
        original={'title':'Synthetic experience','description':'Original normalized candidate',
                  'body':'A synthetic user experience was normalized locally.','type':'feedback'}
        source_key=base.seed_legacy_native_plan(helper.adapter,row,original)
        first_request={'request_id':'synthetic_native_offline_save_001','method':'content_put','params':{
            'kind':'memory','id':'synthetic-topic','record':original,'expected_revision':'missing'}}

        helper.cloud.offline=True
        reconciles=base.seed_legacy_native_write(helper.adapter,source_key,first_request)
        queued=helper.adapter.invoke(first_request,reconciles=reconciles)
        self.assertEqual(queued['local_queue']['state'],'pending')
        source_file.write_text('Synthetic local experience version two')
        helper.adapter.sources.provider.inventory()
        self.assertIn(source_key,helper.adapter._load('memory-reviews.json')['items'])

        remote={'title':'Synthetic concurrent edit','description':'Remote-only detail',
                'body':'A synthetic remote update landed before the offline save replayed.','type':'feedback'}
        helper.cloud.records[('memory','synthetic-topic')]=remote
        helper.cloud.offline=False
        helper.adapter.refresh()
        item=helper.adapter.sources._imports()['items'][source_key]
        self.assertEqual(item['status'],'conflict')
        self.assertEqual(item['request_id'],first_request['request_id'])
        self.assertEqual(item['remote']['record'],remote)

        server=ContentServer(helper.adapter)
        read=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
        self.assertFalse(read['isError'],read)
        self.assertEqual(read['structuredContent']['result']['record'],remote)
        merged_without_source_key={'title':'Synthetic experience and concurrent update','description':'Merged local and remote evidence',
                'body':'The normalized local experience is retained together with the concurrent remote update.',
                'type':'feedback'}
        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':merged_without_source_key,
            'expected_revision':read['structuredContent']['result']['revision'],
            'request_id':'synthetic_native_merged_save_001',
            'reconciles':{'request_ids':[first_request['request_id']],'source_keys':[]}})
        self.assertFalse(saved['isError'],saved)
        self.assertTrue(saved['structuredContent']['persistence']['durable'])
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],merged_without_source_key)
        # Resolving the old outbox request alone is not evidence that its native
        # source was included in the merged record.
        helper.adapter.sources.reconcile_pending()
        self.assertEqual(helper.adapter.sources._imports()['items'][source_key]['status'],'conflict')

        current=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
        merged={'title':'Synthetic experience and concurrent update','description':'Merged local and remote evidence',
                'body':'The explicitly reviewed source and concurrent remote update are both retained.',
                'type':'feedback'}
        saved=server.invoke('content_put',{'kind':'memory','id':'synthetic-topic','record':merged,
            'expected_revision':current['structuredContent']['result']['revision'],
            'request_id':'synthetic_native_source_merge_001',
            'reconciles':{'request_ids':[],'source_keys':[source_key]}})
        self.assertFalse(saved['isError'],saved)
        self.assertTrue(saved['structuredContent']['persistence']['durable'])
        self.assertEqual(helper.cloud.records[('memory','synthetic-topic')],merged)
        self.assertEqual(helper.adapter.sources._imports()['items'][source_key]['status'],'reconciled')
        self.assertEqual(helper.adapter.sources._imports()['items'][source_key]['request_id'],'synthetic_native_source_merge_001')
        self.assertEqual(helper.adapter._load('outbox/'+first_request['request_id']+'.json')['state'],'reconciled')
        self.assertEqual(helper.adapter.queue_status()['confirmed'],2)
        self.assertEqual(helper.adapter.queue_status()['reconciled'],1)

    def test_invalid_onboarding_is_reported_as_tool_error_not_json_parse_error(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        source=memories/'stale.md';source.write_text('Synthetic source before inventory')
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        source.write_text('Synthetic changed source after inventory')
        incoming=[
            {'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'synthetic-test','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'memory_onboard','arguments':{}}},
        ]
        output=io.StringIO()
        serve(ContentServer(helper.adapter),io.BytesIO(('\n'.join(json.dumps(item) for item in incoming)+'\n').encode()),output)
        result=[json.loads(line) for line in output.getvalue().splitlines()]
        error=next(item for item in result if item.get('id')==2)
        self.assertIn('error',error)
        self.assertNotEqual(error['error']['code'],-32700)


    def test_retained_exact_cas_replays_once_after_reconnect(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp()
        try:
            memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
            source_file=memory_root/'progress.md';source_file.write_text('Synthetic short-term progress')
            server=ContentServer(helper.adapter)
            target=server.invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
            revision=target['structuredContent']['result']['revision']
            row=helper.adapter.sources.provider.inventory()['sources'][0]
            helper.adapter.sources.read_onboarding_original(row['source_id'],row['source_fingerprint'])
            record={'title':'Synthetic recent progress','description':'Synthetic offline retry candidate',
                    'body':'Synthetic short-term progress','type':'feedback'}
            source_key=base.seed_legacy_native_plan(helper.adapter,row,record)
            request=helper.request('synthetic_offline_direct_001',revision=revision)
            request['params']['record']=record
            reconciles=base.seed_legacy_native_write(helper.adapter,source_key,request)
            helper.cloud.offline=True
            queued=server.invoke('content_put',dict(request['params'],request_id=request['request_id'],reconciles=reconciles))
            self.assertEqual(queued['structuredContent']['local_queue']['state'],'pending')
            self.assertFalse(queued['structuredContent']['persistence']['durable'])
            self.assertEqual(helper.adapter.sources.import_status()['counts'],{'inventoried':1})
            helper.cloud.offline=False
            helper.adapter.refresh()
            self.assertEqual(helper.adapter.sources.import_status()['counts'],{'reconciled':1})
            self.assertEqual(helper.adapter.queue_status(),{'pending':0,'confirmed':1,'conflict':0,'blocked':0,'reconciled':0})
            requests=[call['request_id'] for call in helper.cloud.calls if call['method']=='content_put']
            self.assertEqual(requests,['synthetic_offline_direct_001','synthetic_offline_direct_001'])
        finally:
            helper.doCleanups()

    def test_memory_tree_traversal_bounds_directory_fanout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'memories';root.mkdir()
            for i in range(8):(root/f'item-{i}').write_text('x')
            result=inventory_codex_memories(tmp,max_entries=4)
            self.assertEqual(result['status'],'incomplete')
            self.assertIn('bounded directory inventory limit',result['reason'])

    def test_legacy_plan_cannot_accept_native_exclusion_claims(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memories=helper.home/'.codex/memories';memories.mkdir(parents=True)
        outside=helper.home/'outside';outside.write_text('synthetic')
        self.make_symlink(memories/'link',outside)
        row=helper.adapter.sources.provider.inventory()['sources'][0]
        self.assertIsNone(row['source_fingerprint'])
        plan={'schema':1,'binding':helper.adapter.binding,'sources':[],
              'coverage':[{'source_id':row['source_id'],'source_fingerprint':None,'status':'excluded','reason':'inventory excluded symlink'}]}
        with self.assertRaises(CacheBlocked):helper.adapter.sources.stage_import(plan)
