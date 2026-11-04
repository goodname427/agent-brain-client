"""Explicit receipt negotiation; fake HTTPS, no credential store or network."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adapters/codex'))
from remote_http import RemoteClient


class ReceiptTransport(unittest.TestCase):
    def test_native_ack_is_memory_only_and_requires_valid_checkpoint(self):
        request={'request_id':'synthetic_native_receipt_0001','method':'content_put','params':{'kind':'memory'}}
        response={'request_id':request['request_id'],'result':{},'persistence':{'durable':False,'state':'local-confirmed',
            'local_durable':True,'github_confirmed':False,'storage':'tencent-l1','checkpoint':'c'*64}}
        client=RemoteClient('https://synthetic.invalid',credential_provider=lambda:'SYNTHETIC_TOKEN_12345678901234567890',write_receipt='local-durable-v1')
        client.opener=MagicMock();client.opener.open.return_value.__enter__.return_value.read.side_effect=lambda size:json.dumps(response).encode()
        self.assertNotIn('error',client.request(request))
        for key,bad in [('checkpoint','bad'),('storage','other'),('github_confirmed',True),('local_durable',False)]:
            original=response['persistence'][key];response['persistence'][key]=bad
            self.assertEqual(client.request(request)['error']['code'],'invalid_write_receipt')
            response['persistence'][key]=original
        self.assertEqual(client.request(dict(request,params={'kind':'rule'}))['error']['code'],'invalid_write_receipt')
        self.assertEqual(client.request(dict(request,method='content_remove'))['error']['code'],'invalid_write_receipt')
        client.write_receipt=None
        self.assertEqual(client.request(request)['error']['code'],'invalid_write_receipt')

    def test_local_ack_requires_declaration_and_strict_flags(self):
        request = {'request_id': 'synthetic_receipt_123456', 'method': 'content_put', 'params': {}}
        for mode in (None, 'local-durable-v1'):
            client = RemoteClient('https://synthetic.invalid', credential_provider=lambda: 'SYNTHETIC_TOKEN_12345678901234567890', write_receipt=mode)
            response = {'request_id': request['request_id'], 'result': {}, 'persistence': {'durable': False, 'state': 'local-confirmed', 'local_durable': True, 'github_confirmed': False, 'commit': 'a' * 40}}
            client.opener = MagicMock()
            client.opener.open.return_value.__enter__.return_value.read.side_effect = lambda size: json.dumps(response).encode()
            result = client.request(request)
            sent = client.opener.open.call_args.args[0]
            self.assertEqual(sent.get_header('X-agent-brain-write-receipt'), mode)
            self.assertEqual('error' in result, mode is None)
            if mode:
                for key, bad in [('github_confirmed', True), ('local_durable', False), ('commit', 'bad')]:
                    original = response['persistence'][key]
                    response['persistence'][key] = bad
                    failure=client.request(request)['error']
                    self.assertEqual(failure['code'], 'invalid_write_receipt')
                    self.assertEqual(failure['stage'], 'response-validation')
                    self.assertTrue(failure['retryable'])
                    response['persistence'][key] = original
                client.request(dict(request, method='content_read'))
                self.assertIsNone(client.opener.open.call_args.args[0].get_header('X-agent-brain-write-receipt'))


if __name__ == '__main__':
    unittest.main()
