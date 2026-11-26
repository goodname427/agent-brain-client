"""Verified offline read fallback: synthetic records/homes, no credentials or network."""

from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_content_outbox import Cloud,MEMORY,RULE,SKILL,POLICY
from agent_brain_client.adapters.harnesses.codebuddy.content_ports import CodeBuddyContentPorts
from agent_brain_client.adapters.harnesses.codex.content_ports import CodexContentPorts
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.application.tools.catalog import enabled_tools
from agent_brain_client.domain.content import validate_lifecycle_target
from agent_brain_client.domain.errors import CacheBlocked
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.interfaces.mcp.framing import ContentParameterError, ContentToolUnavailable
import agent_brain_client.domain.contract as contract


class LifecycleContract(unittest.TestCase):
    def test_explicit_read_and_write_shapes_and_scope(self):
        target = {'kind': 'rule', 'id': 'synthetic-rule'}
        contract.validate('content_read', dict(target, include_archived=True))
        for kind in ('memory', 'other'):
            with self.assertRaises(ValueError):
                contract.validate('content_read', dict(target, kind=kind, include_archived=True))
        with self.assertRaises(ValueError):
            contract.validate('content_read', dict(target, include_archived=False))
        for method in ('content_archive', 'content_restore'):
            contract.validate(method, dict(target, expected_revision='a' * 64))
            with self.assertRaises(ValueError):
                contract.validate(method, dict(target, expected_revision='missing'))
        cloud = Cloud()
        self.assertIn('content_lifecycle', enabled_tools(POLICY, cloud.grant))
        self.assertNotIn('content_lifecycle', enabled_tools(POLICY, dict(cloud.grant, scopes=['read'])))
        archived = dict(contract.envelope('rule', target['id'], None), archived=True, revision='b' * 64)
        validate_lifecycle_target(archived, target['kind'], target['id'])
        with self.assertRaises(ValueError):
            validate_lifecycle_target(dict(archived, record=RULE), target['kind'], target['id'])


    def test_authorized_remove_preserves_scope_original_cas_and_receipt_for_both_hosts(self):
        class RemoveTransport(Cloud):
            def request(self, request):
                if request["method"] != "content_remove":
                    return super().request(request)
                self.calls.append(copy.deepcopy(request))
                contract.validate("content_remove", request["params"])
                params, rid = request["params"], request["request_id"]
                target = params["kind"], params["id"]
                if params["expected_revision"] != contract.envelope(*target, self.records.get(target))["revision"]:
                    return {"request_id": rid, "error": {"code": "revision_conflict", "retryable": False},
                            "persistence": {"durable": False}}
                self.records.pop(target)
                self.puts += 1
                return {"request_id": rid, "result": contract.envelope(*target, None),
                        "persistence": {"durable": True, "state": "remote-confirmed", "commit": "a" * 40}}

        for host in (CodexContentPorts, CodeBuddyContentPorts):
            with self.subTest(host=host.name), tempfile.TemporaryDirectory() as temporary:
                home = Path(temporary).resolve()
                cloud = RemoveTransport()
                cloud.grant = dict(cloud.grant, scopes=["read", "remove"])
                cloud.records[("memory", "synthetic-topic")] = copy.deepcopy(MEMORY)
                session = ContentSession(home, home / "cache", POLICY, cloud, host=host, native_home=home / ("." + host.name))
                server = ContentServer(session)
                server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": host.name, "version": "1"},
                }})
                server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})

                def rpc(method, **params):
                    return server.handle({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})

                def call(name, **arguments):
                    return rpc("tools/call", name=name, arguments=arguments)["structuredContent"]

                tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
                schema = tools["content_lifecycle"]["inputSchema"]
                self.assertEqual(schema["properties"]["action"]["enum"], ["remove"])
                self.assertFalse({"content_archive", "content_restore", "content_remove"} & set(tools))
                target = {"kind": "memory", "id": "synthetic-topic"}
                revision = call("content_read", **target)["result"]["revision"]
                remove = dict(target, action="remove", expected_revision=revision, request_id="synthetic_remove_id")
                cloud.calls.clear()
                with self.assertRaises(ContentToolUnavailable):
                    call("content_lifecycle", **dict(remove, action="archive"))
                with self.assertRaises(ContentParameterError):
                    call("content_lifecycle", **dict(remove, action=[]))
                with self.assertRaises(ContentParameterError):
                    call("content_lifecycle", **dict(remove, kind="unsupported"))
                denied = call("content_lifecycle", **dict(remove, kind="rule", id="out-of-scope"))
                self.assertEqual(denied["error"]["code"], "content_adapter_blocked")
                self.assertEqual(cloud.calls, [])

                stale = call("content_lifecycle", **dict(remove, expected_revision="b" * 64, request_id="synthetic_stale_rm"))
                self.assertEqual(stale["error"]["code"], "revision_conflict")
                self.assertFalse(stale["persistence"]["durable"])
                self.assertEqual(call("content_read", **target)["result"]["revision"], revision)

                saved = call("content_lifecycle", **remove)
                self.assertTrue(saved["persistence"]["durable"])
                self.assertTrue(saved["local_queue"]["server_saved"])
                self.assertIsNone(saved["result"]["record"])
                self.assertEqual(call("content_lifecycle", **remove)["result"], saved["result"])
                self.assertEqual(cloud.puts, 1)
                self.assertEqual([request for request in cloud.calls if request["request_id"] == remove["request_id"]], [{
                    "method": "content_remove",
                    "request_id": remove["request_id"],
                    "params": dict(target, expected_revision=revision),
                }])
                self.assertEqual(call("content_catalog", kinds=["memory"])["result"]["artifacts"], [])


class OfflineReads(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.home=self.base/'home';self.home.mkdir(mode=0o700)
        self.cache=self.base/'cache';self.cloud=Cloud();self.adapter=self.restart()

    def restart(self):
        return ContentSession(self.home,self.cache,copy.deepcopy(POLICY),self.cloud,host=CodexContentPorts, native_home=self.home/'.codex')

    def warm(self):
        self.cloud.records={('memory','synthetic-topic'):copy.deepcopy(MEMORY),('rule','synthetic-rule'):copy.deepcopy(RULE),('skill','synthetic-skill'):copy.deepcopy(SKILL)}
        self.assertEqual(self.adapter.refresh()['status'],'content-refreshed')

    def request(self,method='content_read',params=None):
        return {'request_id':'synthetic_offline_read_001','method':method,'params':params or {'kind':'memory','id':'synthetic-topic'}}

    def failure(self,code='transport_unconfirmed',retryable=True,rid=None):
        def response(request):
            return {'request_id':rid or request['request_id'],'error':{'code':code,'retryable':retryable},'persistence':{'durable':False}}
        self.cloud.request=response

    def test_mcp_offline_catalog_and_all_typed_reads_are_explicitly_stale(self):
        self.warm();self.cloud.offline=True;server=ContentServer(self.restart(),'0.2.4')
        for kind,identity,record in [('memory','synthetic-topic',MEMORY),('rule','synthetic-rule',RULE),('skill','synthetic-skill',SKILL)]:
            reply=server.invoke('content_read',{'kind':kind,'id':identity});self.assertFalse(reply['isError'],reply)
            value=reply['structuredContent'];self.assertEqual(value['result']['record'],record)
            self.assertEqual(value['persistence'],{'durable':False,'state':'read-only'})
            self.assertTrue(value['local_cache']['stale']);self.assertFalse(value['local_cache']['remote_confirmed'])
        reply=server.invoke('content_catalog',{'kinds':['rule']})['structuredContent']
        self.assertEqual(reply['result']['artifacts'],[{'kind':'rule','id':'synthetic-rule','revision':contract.envelope('rule','synthetic-rule',RULE)['revision']}])
        self.assertFalse((self.home/'.agents/skills/synthetic-skill').exists());self.assertEqual(self.cloud.puts,0)

    def test_archive_read_requires_live_lifecycle_result_preserving_ordinary_fallback(self):
        self.warm()
        target = {'kind': 'rule', 'id': 'synthetic-rule'}
        archive_read = self.request(params=dict(target, include_archived=True))
        with self.assertRaises(ValueError):
            self.adapter.invoke(archive_read)  # An ordinary success has no archived flag.
        self.failure('invalid_request', retryable=False)  # Older Server.
        self.assertEqual(self.adapter.invoke(archive_read)['error']['code'], 'invalid_request')
        self.failure()
        reply = self.adapter.invoke(archive_read)
        self.assertEqual(reply['error']['code'], 'transport_unconfirmed')
        self.assertNotIn('local_cache', reply)
        self.assertTrue(self.adapter.invoke(self.request(params=target))['local_cache']['stale'])
        self.failure('device_revoked', retryable=False)
        self.assertEqual(self.adapter.invoke(archive_read)['error']['code'], 'device_revoked')
        self.failure()
        self.assertEqual(self.adapter.invoke(self.request(params=target))['error']['code'],
                         'content_offline_cache_unverified')

    def test_lifecycle_targets_and_receipts_respect_existing_bounds(self):
        self.cloud.calls.clear()
        target = {'kind': 'rule', 'id': 'synthetic-rule'}
        write = self.request('content_archive', dict(target, expected_revision='a' * 64))
        denied = dict(write, params=dict(write['params'], id='outside-grant'))
        with self.assertRaises(CacheBlocked):
            self.adapter.invoke(denied)
        for changes in ({'kind': 'memory'}, {'expected_revision': 'missing'}, {'enabled': True}):
            with self.assertRaises(ValueError):
                self.adapter.invoke(dict(write, params=dict(write['params'], **changes)))
        with self.assertRaises(ValueError):
            self.adapter.invoke(self.request(params=dict(target, include_archived=False)))
        self.assertEqual(self.cloud.calls, [])

        result = dict(contract.envelope('rule', target['id'], None), archived=True, revision='b' * 64)
        receipt = {'state': 'local-confirmed', 'durable': False, 'local_durable': True,
                   'github_confirmed': False, 'commit': 'c' * 40}
        reply = {'request_id': write['request_id'], 'persistence': receipt, 'result': result}
        self.assertTrue(self.adapter._server_saved(reply, write))
        for changes in ({'id': 'other-rule'}, {'record': RULE}, {'archived': 1}, {'revision': 'missing'}):
            self.assertFalse(self.adapter._server_saved(dict(reply, result=dict(result, **changes)), write))
        restore = dict(write, method='content_restore')
        self.assertFalse(self.adapter._server_saved(reply, restore))
        restored = dict(result, archived=False, record=dict(RULE, enabled=True))
        self.assertFalse(self.adapter._server_saved(dict(reply, result=restored), restore))
        restored['record']['enabled'] = False
        self.assertTrue(self.adapter._server_saved(dict(reply, result=restored), restore))

        self.cloud.grant = dict(self.cloud.grant, scopes=['read'])
        tools = {tool['name']: tool for tool in ContentServer(self.restart()).catalog()}
        self.assertNotIn('content_lifecycle', tools)
        self.assertIn('include_archived', tools['content_read']['inputSchema']['properties'])

    def test_no_snapshot_and_unknown_cached_id_are_errors_not_remote_absence(self):
        self.cloud.offline=True;reply=ContentServer(self.adapter).invoke('content_read',{'kind':'memory','id':'synthetic-topic'})
        self.assertTrue(reply['isError']);self.assertEqual(reply['structuredContent']['error']['code'],'content_offline_cache_miss')
        self.cloud.offline=False;self.warm();self.cloud.offline=True
        reply=self.adapter.invoke(self.request(params={'kind':'memory','id':'unknown-topic'}))
        self.assertEqual(reply['error']['code'],'content_offline_cache_miss');self.assertNotIn('result',reply)

    def test_legacy_snapshot_requires_online_identity_pin_and_no_offline_adoption(self):
        self.warm();pin=self.cache/'offline-snapshot.json';pin.unlink();self.cloud.offline=True
        self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');self.assertFalse(pin.exists())
        self.cloud.offline=False;self.assertEqual(self.restart().refresh()['status'],'content-current');self.cloud.offline=True
        self.assertTrue(self.restart().invoke(self.request())['local_cache']['stale'])

    def test_auth_scope_and_identity_failures_never_use_cache(self):
        self.warm()
        for code,retryable in [('device_revoked',False),('device_authentication_failed',False),('device_authentication_failed',True),('scope_denied',False),('device_transport_blocked',False),('invalid_response',True)]:
            self.failure(code,retryable);reply=self.adapter.invoke(self.request())
            self.assertEqual(reply['error']['code'],code);self.assertNotIn('local_cache',reply)
        self.failure(rid='synthetic_mismatched_reply')
        with self.assertRaises(CacheBlocked):self.adapter.invoke(self.request())

    def test_new_service_principal_and_native_binding_cannot_read_old_snapshot(self):
        self.warm();self.cloud.offline=True;self.cloud.grant=dict(self.cloud.grant,actor='another-owner')
        self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified')
        self.cloud.grant.pop('actor');other=ContentSession(self.home,self.cache,copy.deepcopy(POLICY),self.cloud,host=CodexContentPorts, native_home=self.home/'other-codex')
        self.assertEqual(other.invoke(self.request())['error']['code'],'content_offline_cache_unverified')

    def test_tampered_snapshot_active_pin_permissions_and_links_fail_closed(self):
        self.warm();self.cloud.offline=True;active=json.loads((self.cache/'active.json').read_bytes())
        paths=[self.cache/'active.json',self.cache/'offline-snapshot.json',self.cache/'snapshots'/(active['commit']+'.json')]
        for path in paths:
            before=path.read_bytes();value=json.loads(before)
            if path.name=='active.json':value['snapshot_sha']='f'*64
            elif path.name=='offline-snapshot.json':value['service_sha']='f'*64
            else:value['records'][0]['record']['body']='Synthetic external modification'
            path.write_bytes(contract.encode(value));self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');self.assertEqual(path.read_bytes(),contract.encode(value));path.write_bytes(before)
        path=paths[1];path.chmod(0o644);self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');path.chmod(0o600)
        before=path.read_bytes();path.unlink();target=self.base/'outside-pin.json';target.write_bytes(before);target.chmod(0o600);path.symlink_to(target)
        self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');self.assertTrue(path.is_symlink())

    def test_native_hand_edit_and_uncertain_mapping_preserve_evidence(self):
        self.warm();self.cloud.offline=True;p=self.home/'.config/agent-brain-client/content/memory/synthetic-topic.md';before=p.read_bytes();p.write_text('Synthetic hand edit')
        self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');self.assertEqual(p.read_text(),'Synthetic hand edit');p.write_bytes(before)
        transaction=self.cache/'transaction.json';journal=json.loads(transaction.read_bytes());journal['phase']='unrecognized';transaction.write_bytes(contract.encode(journal))
        self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified');self.assertEqual(json.loads(transaction.read_bytes())['phase'],'unrecognized')

    def test_pending_write_is_not_overlaid_or_confirmed_by_cached_read(self):
        self.warm();self.cloud.offline=True
        write={'request_id':'synthetic_offline_write_001','method':'content_put','params':{'kind':'memory','id':'synthetic-topic','record':dict(MEMORY,body='Synthetic pending replacement'),'expected_revision':contract.envelope('memory','synthetic-topic',MEMORY)['revision']}}
        self.assertEqual(self.adapter.invoke(write)['local_queue']['state'],'pending');p=self.cache/'outbox/synthetic_offline_write_001.json';before=p.read_bytes()
        reply=self.restart().invoke(self.request());self.assertEqual(reply['result']['record'],MEMORY);self.assertEqual(reply['local_cache']['queue']['pending'],1);self.assertFalse(reply['persistence']['durable']);self.assertEqual(p.read_bytes(),before)
        self.cloud.offline=False;self.restart().refresh();self.assertEqual(self.cloud.puts,1);self.assertEqual(json.loads(p.read_bytes())['request'],write)

    def test_cache_policy_scope_and_read_permission_cannot_expand(self):
        self.warm();self.cloud.offline=True
        with self.assertRaises(CacheBlocked):self.adapter.invoke(self.request(params={'kind':'rule','id':'unreviewed-rule'}))
        self.cloud.grant=dict(self.cloud.grant,scopes=['put'])
        with self.assertRaises(CacheBlocked):self.restart()

    def test_reconnection_returns_live_record_without_stale_marker(self):
        self.warm();self.cloud.offline=True;self.assertTrue(self.adapter.invoke(self.request())['local_cache']['stale'])
        self.cloud.offline=False;updated=dict(MEMORY,body='Synthetic remote update');self.cloud.records[('memory','synthetic-topic')]=updated;self.cloud.puts+=1
        reply=self.adapter.invoke(self.request());self.assertEqual(reply['result']['record'],updated);self.assertNotIn('local_cache',reply)

    def test_known_catalog_refusal_blocks_later_timeout_and_survives_restart(self):
        self.warm();original=self.cloud.request
        def mixed(request):
            code='device_revoked' if request['method']=='content_catalog' else 'transport_unconfirmed'
            return {'request_id':request['request_id'],'error':{'code':code,'retryable':code=='transport_unconfirmed'},'persistence':{'durable':False}}
        self.cloud.request=mixed
        refused=self.adapter.invoke(self.request('content_catalog',{'kinds':['memory']}))
        self.assertEqual(refused['error']['code'],'device_revoked')
        reply=self.adapter.invoke(self.request())
        self.assertEqual(reply['error']['code'],'content_offline_cache_unverified')
        self.failure();self.assertEqual(self.restart().invoke(self.request())['error']['code'],'content_offline_cache_unverified')
        self.cloud.request=original;self.assertEqual(self.restart().refresh()['status'],'content-current')
        self.cloud.offline=True;self.assertTrue(self.restart().invoke(self.request())['local_cache']['stale'])

    def test_background_denial_under_mapping_lock_survives_restart(self):
        from agent_brain_client.adapters.platforms.filesystem import cache_locked

        self.warm()
        ordinary = self.home / ".codex/user-note.md"
        ordinary.write_bytes(b"Synthetic unmanaged note\n")
        native = self.home / ".config/agent-brain-client/content/memory/synthetic-topic.md"
        previous = native.read_bytes()
        original = self.cloud.request
        with ExitStack() as mapping:
            def refuse(request):
                mapping.enter_context(cache_locked(self.cache / "content.lock", timeout=0))
                return {"request_id": request["request_id"],
                        "error": {"code": "device_revoked", "retryable": False},
                        "persistence": {"durable": False}}

            self.cloud.request = refuse
            blocked = self.adapter.synchronization.background_pull(still_current=lambda: True)
            self.assertEqual(blocked["status"], "content-refresh-blocked")

        self.cloud.request = original
        self.cloud.offline = True
        for session in (self.adapter, self.restart()):
            response = session.invoke(self.request())
            self.assertEqual(response["error"]["code"], "content_offline_cache_unverified")
            self.assertFalse(response["local_cache"]["available"])
        self.assertEqual(ordinary.read_bytes(), b"Synthetic unmanaged note\n")
        self.assertEqual(native.read_bytes(), previous)

        self.cloud.offline = False
        def stale_catalog(_fetch):
            self.fail("A coalesced catalog must not reauthorize a denied snapshot")

        fresh = self.restart().synchronization.background_pull(
            still_current=lambda: True, catalog_probe=stale_catalog,
        )
        self.assertEqual(fresh["status"], "content-current")
        self.cloud.offline = True
        self.assertTrue(self.restart().invoke(self.request())["local_cache"]["stale"])

    def test_failed_snapshot_pin_save_rolls_back_native_mapping_and_prior_active(self):
        self.warm();path=self.home/'.config/agent-brain-client/content/memory/synthetic-topic.md';before=path.read_bytes();active=(self.cache/'active.json').read_bytes();pin=(self.cache/'offline-snapshot.json').read_bytes()
        self.cloud.records[('memory','synthetic-topic')]=dict(MEMORY,body='Synthetic new live content');self.cloud.puts+=1;original=self.adapter._save
        def fail(name,value):
            if name=='offline-snapshot.json':raise OSError('Synthetic pin save failure')
            return original(name,value)
        with patch.object(self.adapter,'_save',side_effect=fail):self.assertEqual(self.adapter.refresh()['status'],'content-refresh-blocked')
        self.assertEqual(path.read_bytes(),before);self.assertEqual((self.cache/'active.json').read_bytes(),active);self.assertEqual((self.cache/'offline-snapshot.json').read_bytes(),pin)
        self.cloud.offline=True;reply=self.restart().invoke(self.request());self.assertEqual(reply['result']['record'],MEMORY);self.assertTrue(reply['local_cache']['stale'])

    def test_transient_read_error_without_raw_exception_can_use_cache(self):
        self.warm();self.failure();reply=self.restart().invoke(self.request());self.assertTrue(reply['local_cache']['stale'])


if __name__=='__main__':unittest.main()
