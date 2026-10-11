"""Two real worker processes, historical adapter and synthetic cache only."""
import json
import hashlib
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


@unittest.skipUnless(os.name == 'posix', 'Mac/POSIX content lifecycle fixture')
class ContentRuntimeLifetime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-content-lifetime-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.home = self.base/'home';self.home.mkdir(mode=0o700)
        self.native = self.base/'native';self.native.mkdir(mode=0o700)
        self.cache = self.base/'cache';self.cache.mkdir(mode=0o700)
        self.processes = []
        self.orphans = set()
        self.addCleanup(self.close_workers)
        self.state = self.base/'synthetic-remote.json'
        self.remote(False, 'Synthetic server base')
        # Frozen exact historical implementations also work in source archives.
        fixtures=ROOT/'tests/fixtures/content_runtime_lifetime'
        manifest=json.loads((fixtures/'manifest.json').read_text())
        for item in manifest['files']:
            raw=(fixtures/item['fixture']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(),item['sha256'])
            (self.base/item['fixture'].removesuffix('.fixture')).write_bytes(raw)
        worker = '''import importlib.util,json,os,sys
from pathlib import Path
ROOT=Path(ROOT_LITERAL)
sys.path[:0]=[str(ROOT/'tests'),str(ROOT/'adapters/codex'),str(ROOT/'scripts')]
from test_content_outbox import Cloud as FakeCloud,POLICY,MEMORY
import content_contract as contract
def arg(name):return sys.argv[sys.argv.index(name)+1]
base=Path(arg('--cache')).parent
version=arg('--client-version')
if version=='0.2.0':
 spec=importlib.util.spec_from_file_location('synthetic_legacy',base/'legacy_cache.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
else:
 import content_cache as module
class Cloud(FakeCloud):
 def request(self,request):
  state=json.loads((base/'synthetic-remote.json').read_bytes());self.offline=state['offline']
  self.records={('memory','synthetic-topic'):dict(MEMORY,body=state['body'])}
  return super().request(request)
cloud=Cloud();adapter=module.ContentAdapter(arg('--device-home'),arg('--cache'),POLICY,cloud,codex_home=arg('--codex-home'))
lock=base/'cache'/'.content-runtime.lock';inherited=False
if lock.exists():
 for fd in range(3,64):
  try:
   if os.fstat(fd).st_ino==lock.stat().st_ino:inherited=True
  except OSError:pass
print(json.dumps({'ready':os.getpid(),'lease_inherited':inherited,'native_is_synthetic':adapter.native_home==base/'native','home_env_is_synthetic':os.environ.get('HOME')==str(base/'home'),'codex_env_is_synthetic':os.environ.get('CODEX_HOME')==str(base/'native'),'parent_secret_absent':'SYNTHETIC_PARENT_SECRET' not in os.environ}),flush=True)
for line in sys.stdin:
 action=json.loads(line)
 if action['action']=='refresh':result=adapter.refresh()
 else:
  base_record=dict(MEMORY,body='Synthetic server base')
  request={'request_id':'SYNTHETIC_PENDING_REQUEST_00000001','method':'content_put','params':{'kind':'memory','id':'synthetic-topic','record':dict(MEMORY,body='Synthetic queued local edit'),'expected_revision':contract.envelope('memory','synthetic-topic',base_record)['revision']}}
  result=adapter.invoke(request)
 print(json.dumps(result),flush=True)
'''.replace('ROOT_LITERAL', repr(str(ROOT)))
        for name in ('legacy-package', 'modern-package'):
            path = self.base/name/'adapters/codex';path.mkdir(parents=True)
            (path/'content_mcp.py').write_text(worker)
        driver = '''import importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(ROOT_LITERAL)
sys.path[:0]=[str(ROOT/'adapters/codex'),str(ROOT/'scripts')]
base=Path(sys.argv[1]);version=sys.argv[2]
if sys.argv[3]=='baseline':
 spec=importlib.util.spec_from_file_location('synthetic_baseline',base/'baseline_startup.py');startup=importlib.util.module_from_spec(spec);spec.loader.exec_module(startup)
else:
 import client_startup as startup
import content_install
content_install.inspect=lambda home:{'installed':True}
active={'version':version,'commit':'a'*40,'package':str(base/('legacy-package' if version=='0.2.0' else 'modern-package'))}
policy={'capabilities':startup.SSH_CONTENT_CAPABILITIES,'runtime':{'device_home':str(base/'home'),'codex_home':str(base/'native'),'content_cache':str(base/'cache')}}
fixed=SimpleNamespace(policy=policy,update=lambda:{'status':'offline','active':active},verified_active=lambda:active)
startup.ClientReleases=lambda *args:fixed
result=startup.startup('synthetic-unused-policy','b'*64)
print(json.dumps(result),flush=True)
'''.replace('ROOT_LITERAL', repr(str(ROOT)))
        self.driver = self.base/'driver.py';self.driver.write_text(driver)

    def remote(self, offline, body):
        self.state.write_text(json.dumps({'offline': offline, 'body': body}))

    def spawn(self, version, guard='current'):
        process = subprocess.Popen([PYTHON, '-I', '-B', str(self.driver), str(self.base), version, guard],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env={'PATH':os.defpath,'HOME':str(self.home),'CODEX_HOME':str(self.native)},
                                   text=True, encoding='utf-8', bufsize=1)
        self.processes.append(process)
        result = self.read(process)
        return process, result

    def read(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], 'Synthetic worker did not return')
        line = process.stdout.readline()
        self.assertTrue(line, 'Synthetic worker exited before response')
        return json.loads(line)

    def send(self, process, action):
        process.stdin.write(json.dumps({'action': action})+'\n');process.stdin.flush()
        return self.read(process)

    def stop(self, process):
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=3)

    def close_workers(self):
        for process in self.processes:
            if process.poll() is None:
                try:self.stop(process)
                except (OSError, subprocess.TimeoutExpired):process.kill();process.wait(timeout=3)
            for stream in (process.stdin, process.stdout):
                if stream and not stream.closed:stream.close()
        # Only orphan workers created by this synthetic fixture are signalled.
        for pid in self.orphans:
            try:os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:pass

    def test_baseline_reproduces_legacy_mapping_past_new_pending_ledger(self):
        old, _ = self.spawn('0.2.0', 'baseline')
        self.assertEqual(self.send(old, 'refresh')['status'], 'content-refreshed')
        modern, _ = self.spawn('0.2.1', 'baseline')
        self.remote(True, 'Synthetic server base')
        self.assertEqual(self.send(modern, 'queue')['local_queue']['state'], 'pending')
        pending = self.cache/'outbox/SYNTHETIC_PENDING_REQUEST_00000001.json';saved = pending.read_bytes()
        self.remote(False, 'Synthetic newer remote body')
        self.assertEqual(self.send(old, 'refresh')['status'], 'content-refreshed')
        mapping = self.home/'.config/agent-brain-client/content/memory/synthetic-topic.md'
        self.assertEqual(mapping.read_text(), 'Synthetic newer remote body')
        self.assertEqual(pending.read_bytes(), saved)  # No claim that pending bytes were lost.

    def test_inherited_external_homes_remain_unchanged_for_both_adapters(self):
        external_home=self.base/'external-home';external_home.mkdir()
        external_native=self.base/'external-native';external_native.mkdir()
        (external_home/'.codex').mkdir()
        for target in (external_home/'.codex/AGENTS.md',external_native/'AGENTS.md'):
            target.write_bytes(b'Synthetic external instructions: preserve exactly.\n')
        def inventory():
            return {str(p.relative_to(self.base)):(p.stat().st_mode,p.read_bytes() if p.is_file() else None) for directory in (external_home,external_native) for p in directory.rglob('*')}
        before=inventory()
        with patch.dict(os.environ,{'HOME':str(external_home),'CODEX_HOME':str(external_native),'SYNTHETIC_PARENT_SECRET':'must-not-inherit'}):
            for version in ('0.2.0','0.2.1'):
                worker,ready=self.spawn(version)
                self.assertTrue(ready['native_is_synthetic'])
                self.assertTrue(ready['home_env_is_synthetic'])
                self.assertTrue(ready['codex_env_is_synthetic'])
                self.assertTrue(ready['parent_secret_absent'])
                self.assertIn(self.send(worker,'refresh')['status'],('content-refreshed','content-current'))
                self.stop(worker)
        self.assertTrue((self.native/'AGENTS.md').is_file())
        self.assertEqual(inventory(),before)

    def test_legacy_lifetime_blocks_modern_then_exit_allows_queue_and_blocks_rollback(self):
        old, ready = self.spawn('0.2.0')
        self.assertTrue(ready['lease_inherited'])
        blocked, result = self.spawn('0.2.1')
        self.assertEqual(result['status'], 'content-runtime-in-use');blocked.wait(timeout=3)
        self.assertFalse((self.cache/'outbox').exists())
        self.stop(old)
        modern, _ = self.spawn('0.2.1')
        self.remote(True, 'Synthetic server base')
        self.assertEqual(self.send(modern, 'queue')['local_queue']['state'], 'pending')
        blocked, result = self.spawn('0.2.0')
        self.assertEqual(result['status'], 'content-outbox-runtime-incompatible');blocked.wait(timeout=3)

    def test_modern_shared_sessions_block_legacy_before_any_ledger_exists(self):
        modern, _ = self.spawn('0.2.1')
        another, ready = self.spawn('0.2.1')
        self.assertIn('ready', ready)
        self.assertFalse((self.cache/'outbox').exists())
        blocked, result = self.spawn('0.2.0')
        self.assertEqual(result['status'], 'content-runtime-in-use');blocked.wait(timeout=3)
        self.stop(modern);self.stop(another)
        old, ready = self.spawn('0.2.0')
        self.assertIn('ready', ready)

    def test_bootstrap_crash_keeps_child_lease_until_owned_child_exits(self):
        old, ready = self.spawn('0.2.0')
        self.assertTrue(ready['lease_inherited'])
        self.orphans.add(ready['ready'])
        old.kill();old.wait(timeout=3)
        blocked, result = self.spawn('0.2.1')
        self.assertEqual(result['status'], 'content-runtime-in-use');blocked.wait(timeout=3)
        os.kill(ready['ready'], signal.SIGKILL)
        self.orphans.discard(ready['ready'])
        deadline = time.monotonic()+3
        while True:
            modern, result = self.spawn('0.2.1')
            if 'ready' in result:break
            modern.wait(timeout=3)
            if time.monotonic() >= deadline:self.fail('Crash lease did not release')
            time.sleep(.02)
        self.assertTrue((self.cache/'.content-runtime.lock').exists())

    def test_worker_crash_releases_lease_without_deleting_persistent_lock(self):
        old, ready = self.spawn('0.2.0')
        os.kill(ready['ready'], signal.SIGKILL);old.wait(timeout=3)
        modern, result = self.spawn('0.2.1')
        self.assertIn('ready', result)
        self.assertTrue((self.cache/'.content-runtime.lock').exists())


if __name__ == '__main__':
    unittest.main()
