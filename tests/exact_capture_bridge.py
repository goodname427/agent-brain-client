"""Synthetic cross-process HTTPS bridge only; never installed in a release."""
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root/'adapters/codex'),str(root/'scripts'),str(root/'tests')]
import test_device_task as fixtures
from test_https_content import canonical
network=patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden'));network.start()
config=json.loads(sys.stdin.readline());f=fixtures.DeviceTask();f.setUp()
try:
    f.profile['grant']['actor']=config['actor'];f.f.profile_path.write_bytes(canonical(f.profile))
    f.sha=hashlib.sha256(f.f.profile_path.read_bytes()).hexdigest()
    f.f.backend.record.update(profile_sha256=f.sha,device_id=config['device_id'],credential=config['token'],grant=f.profile['grant'])
    f.recall=config['request'];client=f.configure(device_id=config['device_id'],actor=config['actor'],profile_sha256=f.sha,extract_request=None,recall_request=f.recall)
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return self.body
    class Opener:
        def open(self,request,**kwargs):
            print(json.dumps({'url':request.full_url,'headers':dict(request.header_items()),'request':json.loads(request.data)}),flush=True)
            response=Response();response.body=sys.stdin.readline().encode();return response
    client.transport.opener=Opener();result=client.request(f.recall)
    print(json.dumps(result),flush=True)
finally:f.doCleanups();network.stop()
