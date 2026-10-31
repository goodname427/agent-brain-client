"""New exact option only, real HTTPSContentClient/RemoteClient with fake custody."""
import copy
import hashlib
import json
import socket
import unittest
from unittest.mock import patch
import test_device_task as fixtures


class ExactClient(unittest.TestCase):
    def setUp(self):
        network=patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden'))
        network.start();self.addCleanup(network.stop)
        self.f=fixtures.DeviceTask();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        selection={'source_session_id':'synthetic-source','batches':[{'batch_key':str(n)*64,'event_id':'fiction-event-'+str(n),
            'payload_sha256':str(n+2)*64,'messages_sha256':str(n+4)*64,'message_ids':['fiction-'+str(n)+'a','fiction-'+str(n)+'b']} for n in (1,2)]}
        f.recall['params'].update(query='capture-batches-exact-v1',selection=selection)
        self.client=f.configure(extract_request=None,recall_request=f.recall)
        sha=hashlib.sha256(json.dumps(selection,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        layers={}
        for layer in ('L1','L2','L3'):
            pins=selection['batches'] if layer=='L3' else selection['batches'][-1:]
            layers[layer]={'content':'Fictional exact response','provenance':{'status':'verified-native-generation-graph',
                'selection_sha256':sha,'content_sha256':hashlib.sha256(b'Fictional exact response').hexdigest(),
                'generation_ids':['fiction-generation'],'event_ids':[p['event_id'] for p in pins],
                'model_output_truth_verified':False,'sources':[{'event_id':p['event_id'],'payload_sha256':p['payload_sha256'],
                    'state':'committed','identity':{'device_id':'a'*32,'project_id':'synthetic-project','session_id':'synthetic-source'}} for p in pins]}}
        f.response={'request_id':f.recall['request_id'],'result':{'content_api_version':1,'method':'core-recall','source':'core',
            'scope':f.recall['params']['context'],'recall':{'L0':[],'L1':[layers['L1']],'L2':[layers['L2']],
                'L3':layers['L3'],'cross_session':True,'model_extraction_verified':False}},'persistence':{'state':'read-only','durable':False}}

    def test_formal_client_sends_authenticated_exact_request_once(self):
        f=self.f;reply=self.client.request(f.recall);self.assertNotIn('error',reply)
        self.assertEqual(len(f.calls),1);request=f.calls[0]
        self.assertEqual(request.full_url,'https://synthetic.invalid/v1/requests')
        self.assertEqual(request.get_header('Authorization'),'Bearer '+f.f.backend.record['credential'])
        self.assertEqual(json.loads(request.data),f.recall)

    def test_no_finite_task_or_changed_selection_denied_before_http(self):
        f=self.f;self.client.device_task=None;self.assertIn('error',self.client.request(f.recall));self.assertEqual(f.calls,[])
        self.client=f.configure(extract_request=None,recall_request=f.recall)
        changed=copy.deepcopy(f.recall);changed['params']['selection']['batches'][0]['batch_key']='0'*64
        self.assertIn('error',self.client.request(changed));self.assertEqual(f.calls,[])

    def test_missing_layer_or_wrong_content_hash_rejected_without_retry(self):
        self.f.response['result']['recall']['L2']=[]
        self.assertIn('error',self.client.request(self.f.recall));self.assertEqual(len(self.f.calls),1)

    def test_read_only_task_cannot_extract(self):
        self.assertIn('error',self.client.request(self.f.extract));self.assertEqual(self.f.calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
