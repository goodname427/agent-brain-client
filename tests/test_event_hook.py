"""Synthetic native hooks through public Client classes; no host credentials."""
import copy
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError

import test_content_outbox as fixture
import content_mcp
import content_cache
from content_mcp import ContentServer,ProtocolError,run_event_hook,validate_hook_policy
from content_cache import ContentAdapter,CacheBlocked
from remote_http import RemoteClient
import client_startup
from client_release import HTTPS_CONTENT_CAPABILITIES
import test_https_content as https_fixture


def accepted(request):
    return {'request_id':request['request_id'],'result':{
        'event_id':request['params']['id'],'state':'committed',
        'message_ids':['synthetic-msg-'+str(n) for n in range(len(request['params']['event']['messages']))],
        'core_acceptance':'acknowledged-not-durability-proof','ledger_sync':'sqlite-wal-full',
        'pipeline_state':'unverified','model_extraction_verified':False},
        'persistence':{'state':'core-accepted','durable':False,'github_confirmed':False,'receipt_ledger_durable':True}}


class EventHook(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.Outbox('test_same_id_cannot_change_request_or_cas_base');self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.adapter=self.fixture.adapter;self.sent=[]
        self.project=self.fixture.base/'project';self.project.mkdir()
        self.policy={'schema':1,'enabled':True,'profile_sha256':'a'*64,'content_policy_sha256':'b'*64,
                     'off_file':str(self.fixture.base/'capture.off'),
                     'scopes':[{'cwd':str(self.project),'project_id':'synthetic-project','session_id':'synthetic-session'}]}
        validate_hook_policy(self.policy,profile_sha256='a'*64,content_policy_sha256='b'*64)
        self.payload={'session_id':'synthetic-session','turn_id':'synthetic-turn','cwd':str(self.project),
                      'hook_event_name':'UserPromptSubmit','prompt':'Synthetic user prefers Python',
                      'transcript_path':'/forbidden/synthetic-session.jsonl'}
        self.fixture.cloud.request=self.send

    def send(self,request):
        self.sent.append(copy.deepcopy(request))
        return accepted(request)

    def prompt(self,payload=None):
        return run_event_hook(self.adapter,self.policy,payload or self.payload,clock=lambda:datetime(2026,10,17,12,tzinfo=timezone.utc))

    def stop(self,**changes):
        payload=dict(self.payload,hook_event_name='Stop',stop_hook_active=False,last_assistant_message='Synthetic final answer')
        payload.update(changes);return run_event_hook(self.adapter,self.policy,payload)

    def test_native_prompt_stop_original_outbox_and_replay_without_refresh(self):
        with patch.object(self.adapter,'refresh',side_effect=AssertionError('No native refresh')):
            self.assertEqual(self.prompt()['status'],'staged');self.assertEqual(self.sent,[])
            result=self.stop();self.assertEqual(result['local_queue']['state'],'confirmed')
            self.stop();self.assertEqual(len(self.sent),1)
        request=self.sent[0];event=request['params']['event']
        self.assertEqual(request['method'],'memory_event_submit');self.assertEqual(event['occurred_at'],'2026-10-17T12:00:00.000Z')
        self.assertEqual(event['messages'],[{'role':'user','content':self.payload['prompt']},{'role':'assistant','content':'Synthetic final answer'}])
        self.assertEqual(event['event_id'],request['params']['id']);self.assertEqual(len(event['event_id']),60)
        self.assertFalse(result['persistence']['durable']);self.assertFalse(result['local_queue']['github_confirmed'])
        raw=(self.fixture.cache/'outbox'/(request['request_id']+'.json')).read_text()
        self.assertNotIn('transcript',raw);self.assertNotIn('/forbidden',raw)
        self.assertFalse((self.fixture.home/'.codex/AGENTS.md').exists())

    def test_wrong_scope_recursive_stop_missing_prompt_and_changed_turn(self):
        for changes in ({'session_id':'synthetic-other'},{'cwd':str(self.fixture.home)}):
            with self.assertRaises(ValueError):self.prompt(dict(self.payload,**changes))
        self.assertEqual(self.stop()['status'],'skipped-no-prompt')
        self.prompt()
        with self.assertRaises(ValueError):self.stop(stop_hook_active=True)
        with self.assertRaises(ValueError):self.prompt(dict(self.payload,prompt='Changed prompt'))
        self.stop()
        with self.assertRaises(ValueError):self.stop(last_assistant_message='Changed final')
        self.assertEqual(len(self.sent),1)

    def test_disabled_off_marker_and_policy_binding(self):
        self.policy['enabled']=False
        self.assertEqual(run_event_hook(self.adapter,self.policy,None)['status'],'disabled')
        self.policy['enabled']=True;Path(self.policy['off_file']).touch()
        self.assertEqual(self.prompt()['status'],'disabled');self.assertEqual(self.sent,[])
        for changes in ({'profile_sha256':'c'*64},{'content_policy_sha256':'c'*64},{'scopes':[]}):
            with self.assertRaises(ValueError):validate_hook_policy(dict(self.policy,**changes),profile_sha256='a'*64,content_policy_sha256='b'*64)

    def test_cli_disabled_before_stdin_credentials_and_unregistered_before_auth(self):
        argv=['content_mcp','--content-policy','synthetic-content','--content-policy-sha256','b'*64,
              '--device-home',str(self.fixture.home),'--cache',str(self.fixture.cache),
              '--https-profile','synthetic-profile','--https-profile-sha256','a'*64,
              '--event-hook','--event-hook-policy','synthetic-hook','--event-hook-policy-sha256','c'*64]
        self.policy['enabled']=False
        source=MagicMock();source.buffer.read.side_effect=AssertionError('Disabled hook must not read stdin')
        with patch.object(sys,'argv',argv),patch.object(sys,'stdin',source),patch.object(sys,'stdout',io.StringIO()),patch.object(content_mcp,'pinned',side_effect=[fixture.POLICY,self.policy]),patch('https_content.HTTPSContentClient',side_effect=AssertionError('No credential construction')) as construct:
            content_mcp.main()
        source.buffer.read.assert_not_called();construct.assert_not_called()
        self.policy['enabled']=True
        source.buffer.read.side_effect=None;source.buffer.read.return_value=json.dumps(dict(self.payload,session_id='synthetic-unregistered')).encode()
        with patch.object(sys,'argv',argv),patch.object(sys,'stdin',source),patch.object(content_mcp,'pinned',side_effect=[fixture.POLICY,self.policy]),patch('https_content.HTTPSContentClient',side_effect=AssertionError('No credential construction')) as construct:
            with self.assertRaises(ValueError):content_mcp.main()
        construct.assert_not_called()

    def test_one_button_off_only_writes_private_marker_and_stops_new_upload(self):
        self.prompt()
        with patch.object(sys,'argv',['content_mcp','--event-hook-off',self.policy['off_file']]),patch.object(sys,'stdout',io.StringIO()):content_mcp.main()
        self.assertEqual(self.stop()['status'],'disabled');self.assertEqual(self.sent,[])
        self.assertEqual(Path(self.policy['off_file']).stat().st_mode&0o777,0o600)

    def test_off_while_Stop_waits_for_content_lock_retains_pending_without_upload(self):
        self.prompt();waiting=threading.Event();results=[];errors=[]
        actual_locked=content_cache.locked
        @contextmanager
        def observed(path,*args,**kwargs):
            if path==self.adapter.cache/'content.lock':waiting.set()
            with actual_locked(path,*args,**kwargs) as lease:yield lease
        def stop():
            try:results.append(self.stop())
            except Exception as error:errors.append(type(error).__name__)
        with patch.object(content_cache,'locked',observed):
            with actual_locked(self.adapter.cache/'content.lock'):
                worker=threading.Thread(target=stop);worker.start()
                self.assertTrue(waiting.wait(3));Path(self.policy['off_file']).touch()
            worker.join(3)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[]);self.assertEqual(self.sent,[])
        self.assertEqual(results[0]['local_queue']['state'],'pending')
        original=self.adapter._queued()[0]['request']
        Path(self.policy['off_file']).unlink();self.assertEqual(self.stop()['local_queue']['state'],'confirmed')
        self.assertEqual(self.sent,[original])

    def test_event_gate_failure_keeps_original_pending_without_transport(self):
        self.prompt();self.fixture.cloud.request=MagicMock(side_effect=OSError('Synthetic offline'))
        self.assertEqual(self.stop()['local_queue']['state'],'pending')
        original=self.adapter._queued()[0]['request'];transport=MagicMock(side_effect=AssertionError('Closed gate must not transfer'))
        self.fixture.cloud.request=transport
        for gate in (MagicMock(side_effect=OSError('Synthetic unreadable gate')),MagicMock(side_effect=[True,False])):
            result=self.adapter.invoke(original,event_gate=gate)
            self.assertEqual(result['local_queue']['state'],'pending');transport.assert_not_called()
        self.assertEqual(self.adapter._queued()[0]['request'],original)

    def test_oversize_and_invalid_calendar_fail_before_upload(self):
        with self.assertRaises(ValueError):self.prompt(dict(self.payload,prompt='x'*8193))
        with self.assertRaises(ValueError):self.prompt(dict(self.payload,prompt='\U0001f600'*4097))
        self.assertEqual(self.sent,[])

    def test_lost_ack_pending_survives_restart_no_automatic_capture_drain(self):
        self.prompt();original=self.send
        def lost(request):original(request);raise OSError('Synthetic lost acknowledgement')
        self.fixture.cloud.request=lost
        self.assertEqual(self.stop()['local_queue']['state'],'pending')
        self.adapter=self.fixture.restart();before=len(self.sent)
        self.fixture.cloud.request=original;self.adapter._drain()
        self.assertEqual(len(self.sent),before)
        self.assertEqual(self.stop()['local_queue']['state'],'confirmed')
        self.assertEqual(self.sent[0],self.sent[1])

    def test_malformed_core_receipt_never_marks_saved(self):
        self.prompt()
        for change in ('count','duplicate','event','durable','github','model','commit'):
            def bad(request):
                response=accepted(request)
                if change=='count':response['result']['message_ids']=[]
                elif change=='duplicate':response['result']['message_ids']=['same','same']
                elif change=='event':response['result']['event_id']='wrong'
                elif change=='model':response['result']['model_extraction_verified']=True
                elif change=='commit':response['persistence']['commit']='a'*40
                else:response['persistence']['github_confirmed' if change=='github' else 'durable']=True
                return response
            self.fixture.cloud.request=bad
            self.assertEqual(self.stop()['local_queue']['state'],'pending',change)
        self.fixture.cloud.request=self.send;self.assertEqual(self.stop()['local_queue']['state'],'confirmed')

    def test_numeric_persistence_flags_never_confirm_event_outbox(self):
        for field,value in (('durable',0),('github_confirmed',0),('receipt_ledger_durable',1)):
            with self.subTest(field=field):
                turn='synthetic-numeric-'+field
                self.prompt(dict(self.payload,turn_id=turn))
                def malformed(request):
                    response=accepted(request);response['persistence'][field]=value
                    return response
                self.fixture.cloud.request=malformed
                self.assertEqual(self.stop(turn_id=turn)['local_queue']['state'],'pending')
                self.fixture.cloud.request=self.send
                self.assertEqual(self.stop(turn_id=turn)['local_queue']['state'],'confirmed')

    def test_unknown_Core_receipt_envelope_stays_pending_then_same_request_recovers(self):
        self.prompt()
        def unknown(request):
            response=self.send(request);response['debug']={'synthetic':'unrecognized envelope field'}
            return response
        self.fixture.cloud.request=unknown
        self.assertEqual(self.stop()['local_queue']['state'],'pending')
        original=self.adapter._queued()[0]['request']
        self.fixture.cloud.request=self.send
        self.assertEqual(self.stop()['local_queue']['state'],'confirmed')
        self.assertEqual(self.sent,[original,original])


    def test_device_denial_preserves_blocked_original_request(self):
        self.prompt();self.fixture.cloud.request=lambda request:{'request_id':request['request_id'],'error':{'code':'device_revoked','retryable':False},'persistence':{'durable':False}}
        result=self.stop();self.assertEqual(result['local_queue']['state'],'blocked')
        request=self.adapter._queued()[0]['request'];self.assertEqual(request['params']['event']['session_id'],'synthetic-session')

    def test_event_contract_is_not_exposed_as_unrestricted_mcp_tool(self):
        with self.assertRaises(ProtocolError):ContentServer(self.adapter).invoke('memory_event_submit',{})

    def test_self_check_and_installation_check_keep_original_mcp_capability_boundary(self):
        self.assertEqual(content_mcp.self_check()['content_mcp_self_check'],'passed')
        cloud=fixture.Cloud()
        result=content_mcp.installation_check(cloud,fixture.POLICY,'0.4.0')
        self.assertEqual(result['status'],'content-native-protocol-checked')
        self.assertNotIn('memory_event_submit',result['tools'])
        self.assertFalse(result['native_changed'])
        self.assertTrue(all(request['method']=='content_catalog' for request in cloud.calls))
        self.assertEqual(fixture.contract.mcp_methods(['read','put'])-{'memory_query'},
                         {'content_catalog','content_read','content_put'})

    def test_same_transport_exact_core_receipt_and_endpoint(self):
        self.prompt();self.stop();request=self.sent[0]
        client=RemoteClient('https://synthetic.invalid',credential_provider=lambda:'SYNTHETIC_TOKEN_12345678901234567890')
        response=accepted(request);client.opener=MagicMock()
        client.opener.open.return_value.__enter__.return_value.read.side_effect=lambda size:json.dumps(response).encode()
        self.assertNotIn('error',client.request(request));sent=client.opener.open.call_args.args[0]
        self.assertEqual(sent.full_url,'https://synthetic.invalid/v1/requests')
        self.assertNotIn('SYNTHETIC_TOKEN',sent.data.decode());self.assertIsNone(sent.get_header('X-agent-brain-write-receipt'))
        response['persistence']['durable']=True
        self.assertEqual(client.request(request)['error']['code'],'invalid_write_receipt')

    def test_strict_transport_Core_JSON_stays_pending_then_recovers_same_event(self):
        for malformed in ('duplicate','nan','overflow'):
            for status in (200,400):
                with self.subTest(malformed=malformed,status=status):
                    client=RemoteClient('https://synthetic.invalid',credential_provider=lambda:'SYNTHETIC_TOKEN_12345678901234567890')
                    client.grant=copy.deepcopy(self.fixture.cloud.grant)
                    self.adapter.client=client;sent=[];replies=[];mode={'malformed':True}
                    class Response:
                        def __init__(self,raw):self.raw=raw
                        def __enter__(self):return self
                        def __exit__(self,*args):pass
                        def read(self,size):return self.raw[:size]
                    class Opener:
                        def open(self,request,**kwargs):
                            value=json.loads(request.data);sent.append(value)
                            raw=json.dumps(accepted(value),separators=(',',':'))
                            if mode['malformed']:
                                if malformed=='duplicate':raw=raw.replace('"state":"committed"','"state":"unverified","state":"committed"',1)
                                else:raw=raw[:-1]+',"debug":'+('NaN' if malformed=='nan' else '1e999')+'}'
                                if status==400:raise HTTPError(request.full_url,400,'Synthetic malformed reply',{},io.BytesIO(raw.encode()))
                            return Response(raw.encode())
                    client.opener=Opener();actual=client.request
                    def recording(request):
                        result=actual(request);replies.append(result);return result
                    client.request=recording
                    turn='synthetic-wire-'+malformed+'-'+str(status)
                    self.prompt(dict(self.payload,turn_id=turn))
                    self.assertEqual(self.stop(turn_id=turn)['local_queue']['state'],'pending')
                    self.assertEqual(replies[-1]['error']['code'],'invalid_response_json')
                    self.assertTrue(replies[-1]['error']['retryable'])
                    mode['malformed']=False
                    self.assertEqual(self.stop(turn_id=turn)['local_queue']['state'],'confirmed')
                    self.assertEqual(sent[0],sent[1])


    def test_official_https_profile_active_identity_store_reused_without_new_enrollment(self):
        owned=https_fixture.HTTPSContent('test_content_https_headers_internal_credential_and_no_remove');owned.setUp();self.addCleanup(owned.doCleanups)
        grant=copy.deepcopy(owned.profile['grant']);grant['ids']={kind:'personal-owner' for kind in grant['artifact_types']}
        owned.profile['grant']=grant;owned.profile_path.write_bytes(fixture.contract.encode(owned.profile));owned.digest=hashlib.sha256(owned.profile_path.read_bytes()).hexdigest()
        owned.code=owned.authority.issue(grant,300)
        client=owned.paired();before=copy.deepcopy(owned.backend.record);seen=[]
        class Response:
            def __init__(self,value):self.value=value
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,size):return json.dumps(self.value).encode()
        class Opener:
            def open(self,request,**kwargs):
                seen.append(request);return Response(accepted(json.loads(request.data)))
        client.transport.opener=Opener()
        policy=dict(fixture.POLICY,ids=grant['ids'])
        adapter=ContentAdapter(owned.home,owned.base/'event-cache',policy,client,codex_home=owned.home/'codex')
        hook_policy=dict(self.policy,profile_sha256=owned.digest)
        with patch('https_content.native_store',side_effect=AssertionError('No second credential source')):
            run_event_hook(adapter,hook_policy,self.payload)
            result=run_event_hook(adapter,hook_policy,dict(self.payload,hook_event_name='Stop',stop_hook_active=False,last_assistant_message='Synthetic official path'))
        self.assertEqual(result['local_queue']['state'],'confirmed');self.assertEqual(owned.backend.record,before)
        self.assertEqual(seen[0].full_url,'https://synthetic.invalid/v1/requests')
        self.assertEqual(seen[0].get_header('Authorization'),'Bearer '+before['credential'])
        self.assertEqual(seen[0].get_header('X-agent-brain-data-mode'),'personal')
        self.assertNotIn(before['credential'],(owned.base/'event-cache/outbox'/(json.loads(seen[0].data)['request_id']+'.json')).read_text())


    def test_hook_bootstrap_verifies_active_no_update_and_retains_lease(self):
        active={'version':'0.4.0','package':str(self.fixture.base/'verified-package')}
        policy={'platform':self.adapter.platform,'capabilities':HTTPS_CONTENT_CAPABILITIES,
                'runtime':{'device_home':str(self.fixture.home),'content_cache':str(self.fixture.cache)}}
        releases=SimpleNamespace(policy=policy,verified_active=MagicMock(return_value=active),update=MagicMock(side_effect=AssertionError('No per-turn update')))
        with patch.object(client_startup,'ClientReleases',return_value=releases),patch('content_install.inspect',return_value={'installed':True}),patch.object(client_startup.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            result=client_startup.startup('synthetic-policy','a'*64,event_hook=('synthetic-hook','b'*64))
        self.assertTrue(result['served']);releases.update.assert_not_called();self.assertGreaterEqual(releases.verified_active.call_count,2)
        command=run.call_args.args[0];self.assertIn('--event-hook',command);self.assertIn('--event-hook-policy',command)
        self.assertTrue(run.call_args.kwargs['pass_fds'])

    def test_capture_bootstrap_failure_does_not_block_native_prompt(self):
        argv=['client_startup','--policy-file','synthetic-release','--policy-sha256','a'*64,
              '--event-hook','--event-hook-policy','synthetic-hook','--event-hook-policy-sha256','b'*64]
        output=io.StringIO()
        with patch.object(sys,'argv',argv),patch.object(sys,'stdout',output),patch.object(sys,'stderr',io.StringIO()),patch.object(client_startup,'startup',return_value={'status':'startup-review-required','served':False}):
            self.assertEqual(client_startup.main(),0)
        self.assertEqual(output.getvalue(),'{}\n')


if __name__=='__main__':unittest.main()
