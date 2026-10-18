"""Native Stop versus a live MCP refresh; two isolated synthetic processes."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Both roles use public Client classes and real flock. Only the remote reply and
# content-lock wait duration are synthetic; runtime leases retain production flags.
CHILD = r'''
import json,sys
from contextlib import contextmanager
from pathlib import Path
root,base,role=map(Path,sys.argv[1:4])
sys.path[:0]=[str(root/'tests'),str(root/'adapters/codex'),str(root/'scripts')]
from test_content_outbox import Cloud,POLICY
from test_event_hook import accepted
import content_cache
from content_cache import ContentAdapter
from content_mcp import ContentServer,run_event_hook
from brain_lock import locked

def emit(value):print(json.dumps(value),flush=True)
class Remote(Cloud):
 def __init__(self):super().__init__();self.held=False;self.events=[]
 def request(self,request):
  if str(role)=='worker' and not self.held:
   self.held=True;emit({'phase':'refresh-holds-content-lock'})
   assert sys.stdin.readline().strip()=='release'
  if request['method']=='memory_event_submit':
   self.events.append(request)
   reply=accepted(request)
   if mode=='unknown':reply['unrecognized']=True
   return reply
  return super().request(request)
cloud=Remote();adapter=ContentAdapter(base/'home',base/'cache',POLICY,cloud,codex_home=base/'native')
with locked(base/'cache'/'.content-runtime.lock',shared=True):
 if str(role)=='worker':
  result=ContentServer(adapter,'0.4.0').handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'synthetic','version':'1'}}})
  emit({'phase':'worker-idle','initialized':bool(result)})
  assert sys.stdin.readline().strip()=='exit'
 else:
  original=content_cache.locked
  @contextmanager
  def bounded(path,*args,**kwargs):
   if path==base/'cache'/'content.lock':kwargs['timeout']=0.4
   with original(path,*args,**kwargs) as handle:yield handle
  content_cache.locked=bounded
  policy={'schema':1,'enabled':True,'profile_sha256':'a'*64,'content_policy_sha256':'b'*64,'off_file':str(base/'capture.off'),'scopes':[{'cwd':str(base/'project'),'project_id':'synthetic-project','session_id':'synthetic-session'}]}
  prompt={'session_id':'synthetic-session','turn_id':'synthetic-turn','cwd':str(base/'project'),'hook_event_name':'UserPromptSubmit','prompt':'SYNTHETIC_NATIVE_HOOK_INPUT'}
  run_event_hook(adapter,policy,prompt)
  stop=dict(prompt,hook_event_name='Stop',stop_hook_active=False,last_assistant_message='SYNTHETIC_NATIVE_HOOK_FINAL')
  emit({'phase':'hook-ready'})
  for line in sys.stdin:
   mode=line.strip()
   if mode=='exit':break
   try:
    payload=dict(stop,last_assistant_message='Changed synthetic final') if mode=='changed' else stop
    result=run_event_hook(adapter,policy,payload)
    emit({'phase':'stop','queue':result.get('local_queue',{}).get('state'),'calls':len(cloud.events),'unique_requests':len({json.dumps(r,sort_keys=True) for r in cloud.events})})
   except Exception as error:emit({'phase':'stop','error':type(error).__name__,'calls':len(cloud.events)})
'''


@unittest.skipUnless(sys.platform == 'darwin', 'Mac Client filesystem and flock')
class NativeHookLocking(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-native-hook-lock-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        for name in ('home', 'native', 'cache', 'project'):
            (self.base/name).mkdir(mode=0o700)
        self.processes = []
        self.addCleanup(self.close)

    def start(self, role):
        child = subprocess.Popen([sys.executable, '-I', '-B', '-c', CHILD, str(ROOT), str(self.base), role],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, bufsize=1,
                                 env={'PATH':os.defpath,'HOME':str(self.base/'home'),'CODEX_HOME':str(self.base/'native')})
        self.processes.append(child)
        child.synthetic_role=role
        return child, self.read(child)

    def read(self, child):
        self.assertTrue(select.select([child.stdout], [], [], 5)[0], 'Synthetic child did not reply')
        line = child.stdout.readline()
        self.assertTrue(line, 'Synthetic child exited: '+(child.stderr.read() if child.poll() is not None else ''))
        return json.loads(line)

    def send(self, child, action):
        child.stdin.write(action+'\n');child.stdin.flush()
        return self.read(child)

    def close(self):
        for child in self.processes:
            if child.poll() is None:
                try:child.stdin.write(('release\nexit\n' if child.synthetic_role=='worker' else 'exit\n'));child.stdin.flush()
                except (BrokenPipeError, ValueError):pass
        for child in self.processes:
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Only these exact synthetic children belong to this fixture.
                child.kill();child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):stream.close()

    def test_append_lock_serializes_enqueue_before_transport(self):
        from brain_lock import locked
        hook,ready=self.start('hook');self.assertEqual(ready['phase'],'hook-ready')
        with locked(self.base/'cache'/'outbox-enqueue.lock'):
            hook.stdin.write('stop\n');hook.stdin.flush()
            self.assertFalse(select.select([hook.stdout], [], [], .2)[0])
            self.assertEqual(list((self.base/'cache'/'outbox').glob('*.json')),[])
        result=self.read(hook);self.assertEqual(result['queue'],'confirmed')
        self.assertEqual(result['calls'],1)
        self.assertEqual(len(list((self.base/'cache'/'outbox').glob('*.json'))),1)

    def test_staging_enqueues_while_refresh_holds_content_lock_then_exact_retry(self):
        from brain_lock import locked,LockTimeout
        worker,ready=self.start('worker')
        self.assertEqual(ready['phase'],'refresh-holds-content-lock')
        hook,ready=self.start('hook');self.assertEqual(ready['phase'],'hook-ready')
        # Shared lifetime leases coexist, but exclusive lifecycle work remains blocked.
        with self.assertRaises(LockTimeout):
            with locked(self.base/'cache'/'.content-runtime.lock',timeout=0):pass
        with self.assertRaises(LockTimeout):
            with locked(self.base/'cache'/'content.lock',timeout=0):pass
        result=self.send(hook,'stop')
        self.assertEqual(result,{'phase':'stop','error':'LockTimeout','calls':0})
        staging=list((self.base/'cache'/'hook-turns').glob('*.json'))
        self.assertEqual(len(staging),1)
        row=json.loads(staging[0].read_bytes());self.assertEqual(row['final'],'SYNTHETIC_NATIVE_HOOK_FINAL')
        outbox=list((self.base/'cache'/'outbox').glob('*.json'))
        self.assertEqual(len(outbox),1,'Final staging must reach pending outbox even while refresh holds content.lock')
        before=outbox[0].read_bytes();queued=json.loads(before)
        self.assertEqual(queued['state'],'pending');self.assertIsNone(queued['response'])
        self.assertEqual(queued['request']['request_id'],'agentbrain_event_'+staging[0].stem)
        self.assertEqual(queued['request']['params']['id'],'evt-'+staging[0].stem[:56])
        self.assertIsNone(worker.poll())
        self.assertEqual(self.send(worker,'release')['phase'],'worker-idle')
        self.assertEqual(outbox[0].read_bytes(),before,'MCP refresh must not automatically drain captured events')
        # Runtime lease remains live while the short transaction lock is now free.
        with self.assertRaises(LockTimeout):
            with locked(self.base/'cache'/'.content-runtime.lock',timeout=0):pass
        with locked(self.base/'cache'/'content.lock',timeout=0):pass
        self.assertEqual(self.send(hook,'unknown')['queue'],'pending')
        self.assertEqual(json.loads(outbox[0].read_bytes())['request'],queued['request'])
        result=self.send(hook,'stop');self.assertEqual(result['queue'],'confirmed')
        self.assertEqual(result['calls'],2);self.assertEqual(result['unique_requests'],1)
        self.assertEqual(self.send(hook,'stop')['calls'],2)
        self.assertEqual(self.send(hook,'changed')['error'],'ValueError')
        self.assertEqual(len(list((self.base/'cache'/'outbox').glob('*.json'))),1)
        self.assertEqual(json.loads(outbox[0].read_bytes())['request'],queued['request'])


if __name__ == '__main__':unittest.main()
