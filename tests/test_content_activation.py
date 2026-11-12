"""User activation, pull and loopback UI on isolated homes and fictional transport."""

import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

import test_content_outbox as fixture
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.application.tools.catalog import enabled_tools
from agent_brain_client.interfaces.management.server import ManagementHTTP, PAGE
from agent_brain_client.application.content.management import Management, LiveReadRequired
import agent_brain_client.domain.contract as contract


class Cloud(fixture.Cloud):
    def request(self, request):
        method = request['method']
        if method != 'content_set_enabled':
            return super().request(request)
        self.calls.append(copy.deepcopy(request))
        if self.offline:
            raise OSError('Fictional disconnection')
        rid = request['request_id']
        params = request['params']
        contract.validate(method, params)
        if rid in self.journal:
            return copy.deepcopy(self.journal[rid])
        key = (params['kind'], params['id'])
        old = self.records.get(key)
        if params['expected_revision'] != contract.envelope(*key, old)['revision']:
            return {'request_id': rid, 'error': {'code': 'revision_conflict', 'retryable': False}, 'persistence': {'durable': False}}
        self.records[key] = dict(old, enabled=params['enabled'])
        self.puts += 1
        response = {'request_id': rid, 'result': contract.envelope(*key, self.records[key]),
                    'persistence': {'durable': True, 'state': 'remote-confirmed', 'commit': format(self.puts, '040x')}}
        self.journal[rid] = copy.deepcopy(response)
        if self.lose_ack:
            self.lose_ack = False
            raise OSError('Fictional lost receipt')
        return response


class Activation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.cache = self.root / 'cache'
        self.cloud = Cloud()
        self.cloud.records = {('rule', 'synthetic-rule'): dict(fixture.RULE, trigger={'mode':'always','patterns':[]}),
                              ('skill', 'synthetic-skill'): copy.deepcopy(fixture.SKILL)}
        self.adapter = self.restart()
        self.manager = Management(self.adapter)
        self.assertEqual(self.adapter.refresh()['status'], 'content-refreshed')

    def restart(self, home=None, cache=None):
        return ContentSession(home or self.home, cache or self.cache, fixture.POLICY,
                              self.cloud, host=CodexContentPorts, native_home=(home or self.home) / '.codex')

    def action(self, kind, enabled, revision=None, rid=None, reconciles=None):
        return {'request_id': rid or uuid.uuid4().hex, 'kind': kind, 'id': 'synthetic-' + kind,
                'enabled': enabled, 'expected_revision': revision or contract.envelope(kind, 'synthetic-' + kind, self.cloud.records[kind, 'synthetic-' + kind])['revision'],
                'reconcile_request_ids': reconciles or []}

    def test_enable_disable_repeated_action_and_server_receipt_are_separate(self):
        agents = self.home / '.codex/AGENTS.md'
        skill = self.home / '.agents/skills/synthetic-skill/SKILL.md'
        for kind in ('rule', 'skill'):
            request = self.action(kind, True)
            saved = self.manager.change(request)
            self.assertTrue(saved['local_queue']['server_saved'])
            self.assertIn(saved['local_refresh']['status'], ('content-current', 'content-refreshed'))
            before = self.cloud.puts
            self.assertTrue(self.manager.change(request)['local_queue']['server_saved'])
            self.assertEqual(self.cloud.puts, before)
            self.assertTrue(skill.is_file() if kind == 'skill' else fixture.RULE['body'] in agents.read_text())
            self.assertEqual(self.adapter.mapping.mapping_status(kind, request['id'], saved['result']['revision'])['status'], 'current')
            disabled = self.manager.change(self.action(kind, False))
            self.assertTrue(disabled['local_queue']['server_saved'])
            self.assertFalse(skill.exists() if kind == 'skill' else fixture.RULE['body'] in agents.read_text())
            self.assertEqual(disabled['local_refresh']['model_loaded'], 'unknown')

    def test_cross_device_read_does_not_map_and_explicit_pull_does(self):
        other_home = self.root / 'second-device'
        other_home.mkdir()
        other = self.restart(other_home, self.root / 'second-cache')
        other.refresh()
        old = (other_home / '.codex/AGENTS.md').read_bytes()
        self.manager.change(self.action('rule', True))
        manager = Management(other)
        state = manager.state()
        self.assertTrue(next(row for row in state['records'] if row['kind'] == 'rule')['enabled'])
        self.assertEqual(next(row for row in state['records'] if row['kind'] == 'rule')['mapping']['status'], 'pull-required')
        self.assertEqual((other_home / '.codex/AGENTS.md').read_bytes(), old)
        before = self.cloud.puts
        pulled = manager.pull({})
        self.assertFalse(pulled['server_write_requested'])
        self.assertEqual(self.cloud.puts, before)
        self.assertIn(fixture.RULE['body'], (other_home / '.codex/AGENTS.md').read_text())
        self.manager.change(self.action('rule', False))
        manager.pull({})
        self.assertNotIn(fixture.RULE['body'], (other_home / '.codex/AGENTS.md').read_text())

    def resource_skill(self):
        self.cloud.records['skill','synthetic-skill']['files']['scripts/nested/helper.py']='# synthetic resource'
        self.manager.change(self.action('skill',True))
        return self.home/'.agents/skills/synthetic-skill'

    def test_disable_prunes_only_created_empty_resource_directories_and_is_repeatable(self):
        directory=self.resource_skill()
        self.assertTrue((directory/'scripts/nested/helper.py').is_file())
        reply=self.manager.change(self.action('skill',False))
        self.assertEqual(reply['local_refresh']['status'],'content-refreshed')
        self.assertFalse(directory.exists())
        self.assertTrue(directory.parent.is_dir())
        self.assertEqual(self.restart().refresh()['status'],'content-current')
        self.manager.change(self.action('skill',True))
        self.assertTrue((directory/'scripts/nested/helper.py').is_file())

    def test_custom_files_and_preexisting_empty_directories_are_preserved(self):
        directory=self.home/'.agents/skills/synthetic-skill'
        (directory/'scripts').mkdir(parents=True)
        self.resource_skill()
        custom=directory/'scripts/user notes.txt'
        custom.write_text('user content')
        self.manager.change(self.action('skill',False))
        self.assertEqual(custom.read_text(),'user content')
        self.assertFalse((directory/'scripts/nested').exists())
        custom.unlink()
        self.restart().refresh()
        self.assertTrue((directory/'scripts').is_dir())
        self.assertTrue(directory.is_dir())

    def test_removed_resources_prune_empty_children_but_keep_active_skill(self):
        directory=self.resource_skill()
        del self.cloud.records['skill','synthetic-skill']['files']['scripts/nested/helper.py']
        self.cloud.puts+=1
        self.assertEqual(self.restart().refresh()['status'],'content-refreshed')
        self.assertFalse((directory/'scripts').exists())
        self.assertTrue((directory/'SKILL.md').is_file())

    def test_failed_apply_rolls_back_new_directories(self):
        self.cloud.records['skill','synthetic-skill']['files']['scripts/helper.py']='# synthetic'
        with patch('agent_brain_client.adapters.storage.native_mapping.host_apply',side_effect=PermissionError('Synthetic busy file')):
            reply=self.manager.change(self.action('skill',True))
        self.assertEqual(reply['local_refresh']['status'],'content-refresh-blocked')
        self.assertFalse((self.home/'.agents/skills/synthetic-skill').exists())
        self.assertEqual(self.restart().refresh()['status'],'content-refreshed')

    def test_empty_only_removal_preserves_concurrently_added_file(self):
        directory=self.resource_skill()
        original=Path.rmdir
        def concurrent(path):
            if path==directory/'scripts/nested':
                (path/'custom.txt').write_text('keep')
            return original(path)
        with patch.object(Path,'rmdir',concurrent):
            self.manager.change(self.action('skill',False))
        self.assertEqual((directory/'scripts/nested/custom.txt').read_text(),'keep')

    def test_legacy_mapping_does_not_claim_unproven_directory_ownership(self):
        directory=self.resource_skill()
        (self.cache/'skill-directories.json').unlink()
        self.manager.change(self.action('skill',False))
        self.assertTrue((directory/'scripts/nested').is_dir())
        self.assertEqual(list(directory.rglob('*')),[directory/'scripts',directory/'scripts/nested'])

    def test_replaced_directory_is_not_removed(self):
        directory=self.resource_skill()
        state=json.loads((self.cache/'skill-directories.json').read_text())
        key='.agents/skills/synthetic-skill/scripts/nested'
        state['directories'][key][1]+=1
        (self.cache/'skill-directories.json').write_text(json.dumps(state))
        self.manager.change(self.action('skill',False))
        self.assertTrue((directory/'scripts/nested').is_dir())

    def test_unicode_and_spaces_in_device_path(self):
        home=self.root/'synthetic 用户 Home'
        home.mkdir()
        adapter=self.restart(home,self.root/'unicode-cache')
        manager=Management(adapter)
        self.cloud.records['skill','synthetic-skill']['files']['resources/marker.txt']='synthetic resource'
        manager.change(self.action('skill',True))
        self.assertTrue((home/'.agents/skills/synthetic-skill/resources/marker.txt').is_file())
        manager.change(self.action('skill',False))
        self.assertFalse((home/'.agents/skills/synthetic-skill').exists())

    def test_lost_ack_restart_and_identical_retry(self):
        request = self.action('rule', True)
        self.cloud.lose_ack = True
        reply = self.manager.change(request)
        self.assertEqual(reply['local_queue']['state'], 'pending')
        self.assertFalse(reply['local_queue']['server_saved'])
        count = self.cloud.puts
        self.manager = Management(self.restart())
        reply = self.manager.retry({'request_id': request['request_id']})
        self.assertTrue(reply['local_queue']['server_saved'])
        self.assertEqual(self.cloud.puts, count)
        self.assertEqual(self.manager.state()['operations'], [])
        with self.assertRaises(ValueError):
            self.manager.retry({'request_id': request['request_id']})

    def test_pull_never_retries_pending_write_and_offline_state_is_not_live(self):
        self.cloud.offline = True
        request = self.action('rule', True)
        self.assertEqual(self.manager.change(request)['local_queue']['state'], 'pending')
        self.cloud.offline = False
        calls = len(self.cloud.calls)
        self.assertEqual(self.manager.pull({})['local_refresh']['status'], 'content-local-changes-retained')
        self.assertEqual(len(self.cloud.calls), calls)
        self.cloud.offline = True
        with self.assertRaises(LiveReadRequired):
            self.manager.state()
        self.assertEqual(self.adapter.queue_status()['pending'], 1)

    def test_permission_failure_never_claims_save_and_explicit_exact_retry(self):
        original = self.cloud.request
        def denied(request):
            if request['method'] == 'content_set_enabled':
                return {'request_id': request['request_id'], 'error': {'code': 'permission_denied', 'retryable': False}, 'persistence': {'durable': False}}
            return original(request)
        request = self.action('skill', True)
        with patch.object(self.cloud, 'request', side_effect=denied):
            reply = self.manager.change(request)
        self.assertFalse(reply['local_queue']['server_saved'])
        self.assertEqual(reply['local_queue']['state'], 'blocked')
        self.assertFalse((self.home / '.agents/skills/synthetic-skill/SKILL.md').exists())
        reply = self.manager.retry({'request_id': request['request_id']})
        self.assertTrue(reply['local_queue']['server_saved'])

    def test_failed_local_write_keeps_receipt_then_pull_recovers_without_second_save(self):
        before = (self.home / '.codex/AGENTS.md').read_bytes()
        with patch('agent_brain_client.adapters.storage.native_mapping.host_apply', side_effect=PermissionError('Fictional Windows sharing violation')):
            reply = self.manager.change(self.action('rule', True))
        self.assertTrue(reply['local_queue']['server_saved'])
        self.assertEqual(reply['local_refresh']['status'], 'content-refresh-blocked')
        self.assertEqual(reply['local_refresh']['stage'], 'native-apply')
        self.assertEqual((self.home / '.codex/AGENTS.md').read_bytes(), before)
        count = self.cloud.puts
        self.assertEqual(self.manager.pull({})['local_refresh']['status'], 'content-refreshed')
        self.assertEqual(self.cloud.puts, count)

    def test_custom_instructions_and_unmanaged_skill_are_preserved(self):
        home = self.root / 'custom-home'
        (home / '.codex').mkdir(parents=True)
        custom = b'# Existing custom instructions\nKeep user content.\n'
        (home / '.codex/AGENTS.md').write_bytes(custom)
        directory = home / '.agents/skills/local-custom'
        directory.mkdir(parents=True)
        sentinel = directory / 'SKILL.md'
        sentinel.write_text('Local user skill')
        adapter = self.restart(home, self.root / 'custom-cache')
        manager = Management(adapter)
        manager.change(self.action('rule', True))
        manager.change(self.action('skill', True))
        manager.change(self.action('skill', False))
        manager.change(self.action('rule', False))
        self.assertTrue((home / '.codex/AGENTS.md').read_bytes().startswith(custom))
        self.assertEqual(sentinel.read_text(), 'Local user skill')
        # A same-name user Skill is never adopted or overwritten.
        conflict = home / '.agents/skills/synthetic-skill/SKILL.md'
        conflict.parent.mkdir(exist_ok=True)
        conflict.write_text('User owned same-name skill')
        saved = manager.change(self.action('skill', True))
        self.assertTrue(saved['local_queue']['server_saved'])
        self.assertEqual(saved['local_refresh']['status'], 'content-refresh-blocked')
        self.assertEqual(conflict.read_text(), 'User owned same-name skill')

    def test_local_drift_and_missing_dependencies_are_visible(self):
        request = self.action('rule', True)
        (self.home / '.codex/AGENTS.md').write_text('User changed instructions')
        saved = self.manager.change(request)
        self.assertTrue(saved['local_queue']['server_saved'])
        self.assertEqual(saved['local_refresh']['status'], 'content-refresh-blocked')
        self.assertEqual(self.adapter.mapping.mapping_status('rule', request['id'], saved['result']['revision'])['status'], 'review-required')
        self.assertEqual((self.home / '.codex/AGENTS.md').read_text(), 'User changed instructions')
        # Separate home for dependency failure, without relaxing the pinned policy.
        home = self.root / 'dependency-home'
        home.mkdir()
        manager = Management(self.restart(home, self.root / 'dependency-cache'))
        self.cloud.records['skill', 'synthetic-skill']['dependencies']['commands'] = ['unapproved-command']
        reply = manager.change(self.action('skill', True))
        self.assertTrue(reply['local_queue']['server_saved'])
        self.assertEqual(reply['local_refresh']['stage'], 'native-mapping')
        self.assertFalse((home / '.agents/skills/synthetic-skill/SKILL.md').exists())

    def test_cas_conflict_explicit_current_review_and_resolution(self):
        request = self.action('rule', True)
        self.cloud.records['rule', 'synthetic-rule']['body'] = 'Other device changed body'
        self.cloud.puts += 1
        conflict = self.manager.change(request)
        self.assertEqual(conflict['local_queue']['state'], 'conflict')
        reviewed = self.action('rule', True, reconciles=[request['request_id']])
        saved = self.manager.change(reviewed)
        self.assertTrue(saved['local_queue']['server_saved'])
        self.assertEqual(self.adapter.queue_status()['reconciled'], 1)
        self.assertIn('Other device changed body', (self.home / '.codex/AGENTS.md').read_text())

    def test_ui_shapes_grants_mcp_and_receipt_validation(self):
        request = self.action('rule', True)
        for changes in ({'enabled': 1}, {'kind': 'memory'}, {'expected_revision': 'missing'}, {'record': fixture.RULE}):
            with self.assertRaises((ValueError, CacheBlocked)):
                self.manager.change(dict(request, **changes))
        bad = copy.deepcopy(request)
        bad['id'] = 'outside-grant'
        with self.assertRaises(CacheBlocked):
            self.manager.change(bad)
        server = ContentServer(self.adapter)
        server.state = 'ready'
        schemas = server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['tools']
        activation = next(t for t in schemas if t['name'] == 'content_set_enabled')
        self.assertEqual(activation['inputSchema']['properties']['enabled'], {'type':'boolean'})
        self.assertEqual(activation['inputSchema']['properties']['kind']['enum'], ['rule', 'skill'])
        self.assertFalse(activation['annotations']['readOnlyHint'])
        read_grant = dict(self.cloud.grant, scopes=['read'])
        self.assertNotIn('content_set_enabled', enabled_tools(fixture.POLICY, read_grant))
        wire = {'request_id':request['request_id'],'method':'content_set_enabled','params':{k:request[k] for k in ('kind','id','enabled','expected_revision')}}
        response = self.cloud.request(wire)
        response['result'] = contract.envelope('rule', 'synthetic-rule', dict(fixture.RULE, enabled=False))
        self.assertFalse(self.adapter._server_saved(response, wire))
        self.assertNotIn('innerHTML', PAGE)


class Loopback(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        self.management = Mock()
        self.management.state.return_value = {'status':'live','records':[]}
        self.server = ManagementHTTP(self.management)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path='/state', body=b'{}', **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        base = {'Content-Type':'application/json', 'Authorization':'Bearer ' + self.server.token, 'Origin':self.server.origin}
        base.update(headers)
        connection.request('POST', path, body, base)
        reply = connection.getresponse()
        return reply.status, json.loads(reply.read()), dict(reply.getheaders())

    def test_host_origin_token_and_request_boundaries(self):
        self.assertEqual(self.request()[0], 200)
        self.management.state.assert_called_once_with()
        for headers in ({'Host':'example.invalid'}, {'Origin':'https://example.invalid'},
                        {'Authorization':'Bearer invalid'}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request(**headers)[0], 403)
        for body in (b'{"x":1,"x":2}', b'[' , b'x' * 4097):
            self.assertEqual(self.request(body=body)[0], 400)
        self.assertEqual(self.request('/unrecognized')[0], 400)
        self.assertEqual(self.request('/state', b'{"kind":"memory"}')[0], 400)
        self.management.change.assert_not_called()
        self.management.pull.assert_not_called()

    def test_page_contains_no_credentials_or_remote_resources_and_errors_are_safe(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET', '/')
        reply = connection.getresponse()
        page = reply.read().decode()
        self.assertNotIn(self.server.token, page)
        self.assertEqual(reply.getheader('Cache-Control'), 'no-store')
        self.assertIn("frame-ancestors 'none'", reply.getheader('Content-Security-Policy'))
        self.management.state.side_effect = RuntimeError('SYNTHETIC_PRIVATE_INTERNAL_ERROR')
        status, value, _ = self.request()
        self.assertEqual(status, 503)
        self.assertNotIn('SYNTHETIC_PRIVATE_INTERNAL_ERROR', json.dumps(value))
        self.management.state.side_effect = LiveReadRequired()
        self.assertEqual(self.request()[1]['status'], 'management-live-read-required')


if __name__ == '__main__':
    unittest.main()
