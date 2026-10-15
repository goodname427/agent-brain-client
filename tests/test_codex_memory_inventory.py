"""Synthetic persistent-memory inventory; never reads native session files."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'adapters/codex'))
from content_cache import inventory_codex_memories
sys.path.insert(0,str(ROOT/'tests'))
import test_content_outbox as base
from content_mcp import ContentParameterError, ContentServer


class CodexMemoryInventory(unittest.TestCase):
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
            (memories/'link').symlink_to(target)
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

    def test_private_cache_tracks_inventory_and_import_state_across_restart(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        source_file=memory_root/'synthetic.md';source_file.write_text('synthetic durable fact')
        inventory=helper.adapter.inventory_memory_sources()
        row=inventory['sources'][0]
        self.assertEqual(inventory['counts'],{'not-checked':1})
        self.assertFalse(inventory['account_complete'])
        record={'title':'Synthetic fact','description':'Synthetic fixture','body':'Synthetic durable fact','type':'feedback'}
        plan={'schema':1,'binding':helper.adapter.binding,
              'sources':[{'source_id':row['source_id'],'source_fingerprint':row['source_fingerprint'],
                          'kind':'memory','id':'synthetic-topic','record':record}],
              'coverage':[{'source_id':'synthetic-codex-memory-root','status':'read','reason':'Synthetic only'}]}
        helper.adapter.stage_import(plan)
        self.assertEqual(helper.adapter.import_status()['source_inventory']['counts'],{'inventoried':1})
        helper.adapter.refresh()
        self.assertEqual(helper.adapter.import_status()['source_inventory']['counts'],{'imported':1})
        restarted=helper.restart();restarted.inventory_memory_sources()
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{'imported':1})
        source_file.write_text('synthetic corrected durable fact')
        updated=restarted.inventory_memory_sources()
        self.assertNotEqual(inventory['cursor'],updated['cursor'])
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{'imported':1,'not-checked':1})
        source_file.unlink();removed=restarted.inventory_memory_sources()
        self.assertEqual(removed['counts'],{'excluded':2})
        self.assertEqual(restarted.import_status()['source_inventory']['counts'],{'excluded':2})

    def test_local_inventory_tool_pages_statuses_and_invalidates_changed_cursor(self):
        helper=base.Outbox('test_write_saved_and_fsynced_before_network_and_restart_reconnect')
        helper.setUp();self.addCleanup(helper.doCleanups)
        memory_root=helper.home/'.codex/memories';memory_root.mkdir(parents=True)
        for index in range(70):(memory_root/f'synthetic-{index:03}.md').write_text(f'synthetic {index}')
        server=ContentServer(helper.adapter)
        first=server.invoke('content_sources_inventory',{})['structuredContent']
        self.assertEqual(len(first['sources']),64);self.assertIsNotNone(first['next_cursor'])
        self.assertNotIn('synthetic 0',json.dumps(first))
        second=server.invoke('content_sources_inventory',{'cursor':first['next_cursor']})['structuredContent']
        self.assertEqual(len(second['sources']),6);self.assertIsNone(second['next_cursor'])
        self.assertEqual(first['cursor'],second['cursor'])
        (memory_root/'synthetic-000.md').write_text('synthetic changed')
        with self.assertRaises(ContentParameterError):
            server.invoke('content_sources_inventory',{'cursor':first['next_cursor']})
        self.assertEqual(helper.cloud.calls,[])


if __name__=='__main__':unittest.main()
