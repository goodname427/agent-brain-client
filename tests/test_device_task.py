"""Finite manual owner task only; fake credential custody and HTTPS opener."""
import copy,hashlib,json,time,unittest
from unittest.mock import patch
import test_https_content as fixtures
from https_content import HTTPSContentClient,PinnedDeviceTask

class DeviceTask(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HTTPSContent('test_lost_prepare_and_restart_reuse_private_code_nonce_then_remove_code');self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.profile=self.f.profile;self.profile['grant']['ids']={k:'personal-owner' for k in ('memory','rule','skill')}
        self.f.profile_path.write_bytes(fixtures.canonical(self.profile));self.sha=hashlib.sha256(self.f.profile_path.read_bytes()).hexdigest()
        self.f.backend.record={'schema':1,'profile_sha256':self.sha,'phase':'active','nonce':'SYNTHETIC_NONCE_1234567890123456789','credential':'SYNTHETIC_DEVICE_CREDENTIAL_12345678901234','device_id':'a'*32,'grant':self.profile['grant']}
        now=int(time.time());self.extract={'request_id':'synthetic_extract_once_request','method':'memory_extract','params':{'project_id':'synthetic-project','session_id':'synthetic-source'}}
        self.recall={'request_id':'synthetic_recall_once_request','method':'memory_query','params':{'kind':'memory','query':'Fictional Python preference','since':None,'until':None,'limit':3,'context':{'project_id':'synthetic-project','session_id':'synthetic-next','cross_session':True}}}
        self.value={'schema':1,'enabled':True,'not_before':now-1,'expires_at':now+3599,'actor':self.profile['grant']['actor'],'device_id':'a'*32,'endpoint':self.profile['endpoint'],'profile_sha256':self.sha,'extract_request':self.extract,'recall_request':self.recall}
        self.path=self.f.base/'task.json';self.calls=[];self.client=self.configure()
    def configure(self,**changes):
        self.value.update(changes);self.path.write_bytes(fixtures.canonical(self.value));self.path.chmod(0o600)
        self.digest=hashlib.sha256(self.path.read_bytes()).hexdigest()
        client=HTTPSContentClient(self.f.profile_path,self.sha,store=self.f.store,device_task=(self.path,self.digest))
        outer=self
        class Reply:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return json.dumps(outer.response).encode()
        class Opener:
            def open(self,request,**kw):outer.calls.append(request);return Reply()
        client.transport.opener=Opener()
        self.response={'request_id':self.extract['request_id'],'result':{'job_id':'abj-'+'b'*64,'state':'succeeded','stages':['L1','L2','L3'],'http_requests':3},'persistence':{'state':'core-extracted','durable':False,'receipt_ledger_durable':True,'preimage_retained':True}}
        return client
    def test_exact_task_uses_existing_authenticated_transport_and_custody(self):
        before=copy.deepcopy(self.f.backend.record);result=self.client.request(self.extract)
        self.assertNotIn('error',result);self.assertEqual(len(self.calls),1)
        r=self.calls[0];self.assertEqual(r.full_url,'https://synthetic.invalid/v1/requests');self.assertEqual(r.method,'POST');self.assertEqual(json.loads(r.data),self.extract)
        self.assertEqual(r.get_header('Authorization'),'Bearer '+before['credential']);self.assertEqual(self.f.backend.record,before)
        self.assertNotIn(before['credential'],json.dumps(result))
    def test_default_disabled_expired_future_and_drift_fail_before_http(self):
        c=HTTPSContentClient(self.f.profile_path,self.sha,store=self.f.store);c.transport=self.client.transport
        self.assertIn('error',c.request(self.extract))
        for changes in [dict(enabled=False),dict(not_before=1,expires_at=2),dict(not_before=2100000000,expires_at=2100003600)]:
            value=copy.deepcopy(self.value);client=self.configure(**changes);self.assertIn('error',client.request(self.extract));self.value=value
        self.client=self.configure();self.path.write_text('{}');self.assertIn('error',self.client.request(self.extract));self.assertEqual(self.calls,[])
    def test_changed_method_id_source_params_and_recall_query_denied(self):
        for original,mutation in [(self.extract,lambda r:r.update(request_id='synthetic_different_id')),(self.extract,lambda r:r['params'].update(project_id='other')),(self.extract,lambda r:r['params'].update(session_id='other')),(self.extract,lambda r:r['params'].update(extra=True)),(self.extract,lambda r:r.update(method='admin')),(self.recall,lambda r:r['params'].update(query='other')),(self.recall,lambda r:r['params']['context'].update(session_id='synthetic-source'))]:
            r=copy.deepcopy(original);mutation(r);self.assertIn('error',self.client.request(r))
        self.assertEqual(self.calls,[])
    def test_other_device_or_limited_grant_and_credential_change_denied(self):
        self.client.device_id='b'*32;self.assertIn('error',self.client.request(self.extract));self.assertEqual(self.calls,[])
        self.client.device_id='a'*32;self.f.backend.record['device_id']='c'*32;self.assertIn('error',self.client.request(self.extract));self.assertEqual(self.calls,[])
        self.f.backend.record['device_id']='a'*32
        self.client.grant['ids']['memory']='*';self.assertIn('error',self.client.request(self.extract));self.assertEqual(self.calls,[])
    def test_invalid_response_and_timeout_never_retry_or_queue(self):
        self.response['result']['http_requests']=4
        self.assertEqual(self.client.request(self.extract)['error']['code'],'invalid_extraction_receipt');self.assertEqual(len(self.calls),1)
        with patch.object(self.client.transport.opener,'open',side_effect=TimeoutError):
            result=self.client.request(self.extract)
        self.assertEqual(result['error']['code'],'transport_unconfirmed');self.assertFalse(result['persistence']['durable']);self.assertEqual(len(self.calls),1)
    def test_policy_metadata_check_needs_no_credential_io(self):
        with patch.object(self.f.store,'load',side_effect=AssertionError('No credential IO')):
            p=PinnedDeviceTask(self.path,self.digest,self.profile,self.sha);p.check(self.extract,'a'*32)
        self.assertEqual(self.calls,[])
    def test_recall_uses_exact_new_session_and_normal_https(self):
        self.response={'request_id':self.recall['request_id'],'result':{'content_api_version':1,'method':'core-recall','source':'core','scope':self.recall['params']['context'],'recall':{'L0':[],'L1':[],'L2':[],'L3':{'content':None,'provenance':{}},'cross_session':True,'model_extraction_verified':False}},'persistence':{'state':'read-only','durable':False}}
        self.assertNotIn('error',self.client.request(self.recall));self.assertEqual(json.loads(self.calls[0].data),self.recall)

if __name__=='__main__':unittest.main()

class StrictExtractionReceipt(DeviceTask):
    def test_float_request_count_never_claims_verified_extraction(self):
        self.response['result']['http_requests']=3.0
        result=self.client.request(self.extract)
        self.assertEqual(result['error']['code'],'invalid_extraction_receipt')
        self.assertEqual(len(self.calls),1)
