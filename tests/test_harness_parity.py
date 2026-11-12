"""The same real content contract through each host, plus a forbidden-host runtime."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.runtime.harness_content import content_host
from agent_brain_client.domain import contract
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.domain.errors import CacheBlocked
from test_content_outbox import Cloud, POLICY, MEMORY, RULE, SKILL

ROOT = Path(__file__).resolve().parents[1]


class HarnessParity(unittest.TestCase):
    def test_same_content_cas_queue_offline_and_mapping_contract(self):
        outcomes = []
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cloud = Cloud()
                cloud.queue_identity = {"service": "synthetic", "host_adapter": harness}
                cloud.records = {("rule", "synthetic-rule"): dict(RULE, enabled=True, trigger={"mode": "always", "patterns": []}),
                                 ("skill", "synthetic-skill"): dict(SKILL, enabled=True)}
                adapter = ContentSession(home, home / "cache", copy.deepcopy(POLICY), cloud,
                                         host=content_host(harness), native_home=home / (".codex" if harness == "codex" else ".codebuddy"))
                self.assertEqual(adapter.refresh()["status"], "content-refreshed")
                skill = home / (".agents" if harness == "codex" else ".codebuddy") / "skills/synthetic-skill/SKILL.md"
                self.assertEqual(skill.read_text(), SKILL["files"]["SKILL.md"])
                request = {"request_id": "synthetic-parity-write", "method": "content_put", "params": {
                    "kind": "memory", "id": "synthetic-topic", "expected_revision": "missing", "record": MEMORY}}
                result = adapter.invoke(request)
                self.assertTrue(result["persistence"]["durable"])
                self.assertEqual(adapter.invoke(request)["local_queue"]["state"], "confirmed")
                cloud.offline = True
                read = adapter.invoke({"request_id": "synthetic-offline-read", "method": "content_read",
                                       "params": {"kind": "memory", "id": "synthetic-topic"}})
                self.assertTrue(read["local_cache"]["stale"])
                outcomes.append((result["local_queue"]["state"], read["result"]["record"], adapter.queue_status()))
                if harness == "codebuddy":
                    self.assertFalse((home / ".codex").exists())
                    self.assertFalse((home / ".agents").exists())
                    self.assertIn("alwaysApply: true", (home / ".codebuddy/rules/agent-brain-synthetic-rule.md").read_text())
        self.assertEqual(outcomes[0], outcomes[1])

    def test_codebuddy_ide_memory_uses_observed_host_directory_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            adapter = ContentSession(home, home / "cache", POLICY, Cloud(), host=content_host("codebuddy"), native_home=home / ".codebuddy")
            source = adapter.sources.provider.ide / "synthetic.mdc"
            source.parent.mkdir(parents=True)
            source.write_text("---\ntitle: Synthetic IDE memory\n---\nA synthetic preference")
            row = adapter.sources.inventory_memory_sources()["sources"][0]
            self.assertEqual(adapter.sources.read_memory_source(row["source_id"], row["source_fingerprint"])["text"], source.read_bytes().decode())
            self.assertFalse((home / ".codex").exists())

    def test_shared_session_requires_explicit_host(self):
        with self.assertRaises(TypeError):
            ContentSession("synthetic", "cache", POLICY, Cloud())

    def test_codebuddy_source_review_import_and_foreign_source_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / ".codebuddy"
            source = native / "memories/global/preferences.md"
            source.parent.mkdir(parents=True)
            source.write_text("Synthetic preference only")
            adapter = ContentSession(home, home / "cache", POLICY, Cloud(), host=content_host("codebuddy"), native_home=native)
            inventory = adapter.sources.inventory_memory_sources()
            row = inventory["sources"][0]
            self.assertTrue(row["source_id"].startswith("codebuddy-memory:"))
            self.assertEqual(adapter.sources.read_memory_source(row["source_id"], row["source_fingerprint"])["text"], source.read_bytes().decode())
            with self.assertRaises(CacheBlocked):
                adapter.sources.stage_memory_import(row["source_id"], row["source_fingerprint"], "memory", "synthetic-topic", MEMORY)
            adapter.sources.review_memory_source(row["source_id"], row["source_fingerprint"], "shareable", "safe-cross-project-personal-summary")
            adapter.sources.stage_memory_import(row["source_id"], row["source_fingerprint"], "memory", "synthetic-topic", MEMORY)
            self.assertEqual(adapter.sources.import_status()["counts"].get("inventoried"), 1)
            with self.assertRaises(CacheBlocked):
                adapter.sources.read_memory_source("codex-memory:" + "a" * 64, "a" * 64)

    def test_codebuddy_only_import_and_real_mcp_refresh_without_codex(self):
        code = '''
import importlib.abc, sys, pathlib, tempfile, copy
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
class NoCodex(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('agent_brain_client.adapters.harnesses.codex'):
            raise AssertionError('CodeBuddy loaded Codex: ' + fullname)
sys.meta_path.insert(0, NoCodex())
from agent_brain_client.runtime import collection, updates, host_binding
from agent_brain_client.adapters.installation import installation
from agent_brain_client.interfaces.mcp.content import ContentServer
from agent_brain_client.interfaces.management.server import installed_adapter
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.runtime.harness_content import content_host
POLICY = {'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'dependencies':{'commands':[],'python_modules':[]}}
class Cloud:
    grant = {'scopes':['read','put'],'artifact_types':POLICY['kinds'],'ids':POLICY['ids']}
    def request(self, request):
        return {'request_id':request['request_id'], 'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]}}
with tempfile.TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp).resolve()
    adapter = ContentSession(home, home/'cache', copy.deepcopy(POLICY), Cloud(), host=content_host('codebuddy'), native_home=home/'.codebuddy')
    server = ContentServer(adapter)
    response = server.handle({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'synthetic','version':'1'}}})
    assert response['serverInfo']['name'] == 'agent-brain-content', response
    assert not (home/'.codex').exists()
    assert not (home/'.agents').exists()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class QueueParity(unittest.TestCase):
    def test_manual_sync_drains_both_original_queues_and_rechecks_admission(self):
        from types import SimpleNamespace
        from agent_brain_client.runtime.capture_queues import open_queue
        from agent_brain_client.runtime.collection_management import flush
        from agent_brain_client.application.memory.observations import stage_messages
        from agent_brain_client.adapters.storage import metadata, registration
        from test_event_hook import accepted
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            policy = home / "policy.json"
            metadata.write(policy, POLICY)
            value = {"device_home": str(home), "scope": {"synthetic": True}}
            local = {"kind": "non-project", "cwd": str(home), "project_root": None, "repository_root": None}
            sent = []
            class Client:
                grant = {"scopes": ["read", "put"], "artifact_types": POLICY["kinds"], "ids": POLICY["ids"]}
                def __init__(self):
                    self.queue_identity = {"service": "synthetic"}
                def request(self, request):
                    sent.append(copy.deepcopy(request))
                    return accepted(request)
                def status(self, session):
                    return {"source_admitted": True}
            services = SimpleNamespace(CollectionClient=lambda *a: Client(), approved=lambda value: True)
            services.build_delivery_queue = lambda value, local, harness: open_queue(value, local, harness, services=services)
            enrolled = {"runtime": {"content_policy": str(policy), "content_policy_sha256": metadata.digest(policy.read_bytes()),
                                     "codex_home": str(home / "shared-native")}}
            with patch.object(registration, "registration", return_value=({}, enrolled)), patch("agent_brain_client.runtime.collection_management.select", return_value={"sharing": "cross-project"}):
                originals = []
                for harness in ("codex", "codebuddy"):
                    _, queue = services.build_delivery_queue(value, local, harness)
                    metadata.write(queue.cache / "origin.json", local, cache=True)
                    stage_messages(queue, project_id="synthetic-project", session_id="synthetic-session", operation_id="synthetic-turn",
                                   phase="prompt", messages=[{"role": "user", "content": "Synthetic future observation"}])
                    originals.extend(row["request"] for row in queue._queued())
                self.assertFalse(sent)
                result = flush(value, services=services)
                self.assertEqual(result["status"], "global-original-queue-sync")
                self.assertEqual(set(result["hosts"]), {"codex", "codebuddy"})
                self.assertEqual(sorted(sent, key=lambda row: row["request_id"]), sorted(originals, key=lambda row: row["request_id"]))
                sent.clear()
                flush(value, services=services)
                self.assertFalse(sent)
                self.assertFalse((home / ".codex").exists())
                self.assertFalse((home / ".agents").exists())

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    def test_codebuddy_only_verified_install_source_setup_and_binding(self):
        code = '''
import sys, pathlib, importlib.abc
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
import test_mac_first_install as fixture
f = fixture.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
f.setUp()
try:
    f.fixture()
    f.harness = 'codebuddy'
    f.native = f.home / '.config/agent-brain-client/shared-native'
    f.native.mkdir()
    f.config = f.native / 'config.toml'
    for name in list(sys.modules):
        if name.startswith('agent_brain_client.adapters.harnesses.codex'):
            del sys.modules[name]
    class NoCodex(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith('agent_brain_client.adapters.harnesses.codex'):
                raise AssertionError('CodeBuddy installation loaded Codex: ' + fullname)
    sys.meta_path.insert(0, NoCodex())
    result = f.run_wizard()
    assert result['installed'], result
    from types import SimpleNamespace
    from agent_brain_client.runtime.host_maintenance import install_sources
    from agent_brain_client.runtime.host_binding import load_core, backend
    from agent_brain_client.adapters.harnesses.codebuddy.configuration import install, status
    install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None, operator=None, operator_sha256=None))
    core = load_core(f.home)
    install(f.home, pathlib.Path(sys.argv[1]).parent, core, surface='both')
    from agent_brain_client.adapters.installation.installation import inspect
    from unittest.mock import patch
    with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version":"synthetic-fixture"}):
        checked = inspect(f.home)
        assert status(f.home, core, backend_check=backend)["installed"]
    assert checked.get('installed'), checked
    assert not (f.home/'.codex').exists()
    assert not (f.home/'.agents').exists()
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.codex') for name in sys.modules)
finally:
    f.doCleanups()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class ProjectParity(unittest.TestCase):
    def test_project_settings_paths_and_native_hooks_are_host_isolated(self):
        from agent_brain_client.adapters.storage.project_settings import project_settings
        from agent_brain_client.adapters.harnesses.codebuddy.project_configuration import install
        from agent_brain_client.adapters.harnesses.codebuddy.configuration import Document
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "work"
            project.mkdir()
            codex = project_settings(home, project, harness="codex")
            buddy = project_settings(home, project, harness="codebuddy")
            self.assertNotEqual(codex, buddy)
            private_directory(buddy.parent)
            metadata.write(buddy, {"synthetic": True})
            settings = {"harness": "codebuddy", "project_root": str(project), "source_mode": "recall-only"}
            config = project / ".codebuddy/settings.local.json"
            config.parent.mkdir()
            original = '{// synthetic unrelated settings\n"model":"keep", "hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep"}]}]}}'
            config.write_text(original)
            install(settings, buddy, metadata.digest(buddy.read_bytes()))
            state = Document(config.read_text()).value
            self.assertEqual(state["model"], "keep")
            self.assertEqual(len(state["hooks"]["Stop"]), 2)
            install(settings, buddy, metadata.digest(buddy.read_bytes()))
            self.assertEqual(len(Document(config.read_text()).value["hooks"]["Stop"]), 2)
            self.assertFalse((project / ".codex").exists())

    def test_codebuddy_recall_only_project_never_reads_transcripts(self):
        import io
        from unittest.mock import Mock
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.harnesses.codebuddy import transcript
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            settings = {"harness": "codebuddy", "enabled": True, "device_home": str(home), "project_root": str(home),
                        "capture_policy": None, "source_mode": "recall-only"}
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "native-session", "cwd": str(home), "prompt": "Synthetic current question"}
            stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
            recall = Mock(return_value=[{"content": "Synthetic linked memory"}])
            with patch.object(project_memory, "active", return_value=True), patch.object(project_memory.sys, "stdin", stdin), \
                    patch.object(project_memory, "recalled", recall), patch.object(transcript, "observe", side_effect=AssertionError("No capture authority")), \
                    patch.object(transcript, "assistant", side_effect=AssertionError("No capture authority")):
                result = project_memory.hook_payload(settings)
            self.assertIn("Synthetic linked memory", str(result))
            recall.assert_called_once()
            self.assertFalse((home / ".codebuddy/turns").exists())

    def test_codebuddy_project_completion_resumes_from_exact_observed_marker(self):
        from unittest.mock import Mock
        from agent_brain_client.adapters.harnesses.codebuddy import transcript, project_configuration as native
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native_file = home / ".codebuddy/projects/native-session.jsonl"
            native_file.parent.mkdir(parents=True)
            native_file.write_text("")
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "native-session", "generation_id": "generation-1",
                       "cwd": str(home), "prompt": "Synthetic prompt", "transcript_path": str(native_file)}
            settings = {"harness": "codebuddy", "device_home": str(home), "project_root": str(home), "capture_policy": "synthetic"}
            transcript.observe(home, payload, None)
            stop = dict(payload, hook_event_name="Stop", stop_hook_active=False)
            turn = native.register_completion(settings, stop, home / "settings.json", "a" * 64)
            native_file.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "Synthetic completed answer"}}) + "\n")
            task = {"schema": 1, "harness": "codebuddy-project-final", "turn_id": turn,
                    "settings": str(home / "settings.json"), "settings_sha256": "a" * 64}
            staged = Mock(return_value={"local_queue": {"saved": True, "state": "pending"}})
            with patch.object(project_memory, "load", return_value=settings), patch.object(project_memory, "active", return_value=True), \
                    patch.object(project_memory, "capture_enabled", return_value=True), patch.object(project_memory, "capture", staged), patch("agent_brain_client.runtime.delivery.schedule_project"):
                self.assertFalse(project_memory.finish_codebuddy_project(home, task))
                self.assertFalse(project_memory.finish_codebuddy_project(home, task))
            staged.assert_called_once()
            self.assertEqual(staged.call_args.args[1]["last_assistant_message"], "Synthetic completed answer")
            self.assertTrue(metadata.private_json(transcript.marker(home, payload), cache=True)["complete"])


class SharedPorts(unittest.TestCase):
    def test_common_content_runs_with_both_concrete_host_packages_forbidden(self):
        code = '''
import sys, importlib.abc, pathlib, tempfile
sys.path[:0] = [sys.argv[1], str(pathlib.Path(sys.argv[1]).parent)]
class NoHarness(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('agent_brain_client.adapters.harnesses.'):
            raise AssertionError('A shared service selected a concrete host: ' + fullname)
sys.meta_path.insert(0, NoHarness())
from agent_brain_client.adapters.storage.content_session import ContentSession
from agent_brain_client.adapters.storage.native_mapping import NativeMapping
from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
from agent_brain_client.interfaces.mcp.content import ContentServer
class Mapping(NativeMapping):
    skill_prefix = '.synthetic/skills'
    instruction_keys = frozenset()
    def _mapping(self, snapshot, owned): return {}
    def _path(self, name): raise AssertionError('Empty native projection')
class Sources:
    prefix = 'synthetic-memory:'
    def __init__(self, session): pass
    def inventory(self): return {'schema':1,'status':'complete','sources':[],'cursor':'a'*64}
class Host:
    name = 'synthetic'
    mapping = Mapping
    sources = Sources
    protected = ('.synthetic',)
    @staticmethod
    def default_home(home): return pathlib.Path(home)/'.synthetic'
    @staticmethod
    def binding(home, native): return {'device_home':str(home),'native_home':str(native),'host':'synthetic'}
class Cloud:
    grant={'scopes':['read','put'],'artifact_types':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']}}
    def request(self, request): return {'request_id':request['request_id'],'result':{'content_api_version':1,'commit':'a'*40,'artifacts':[]}}
with tempfile.TemporaryDirectory() as tmp:
    home = pathlib.Path(tmp).resolve()
    policy={'schema':1,'adapter':'codex','kinds':['memory','rule','skill'],'ids':{'memory':'*','rule':['synthetic-rule'],'skill':['synthetic-skill']},'dependencies':{'commands':[],'python_modules':[]}}
    session=ContentSession(home,home/'cache',policy,Cloud(),host=Host)
    assert session.refresh()['status']=='content-refreshed'
    server=ContentServer(session)
    assert server.adapter is session
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.') for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src")], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_secondary_projection_removal_keeps_pending_operations_and_is_repeatable(self):
        from agent_brain_client.adapters.installation import projection_removal
        for harness in ("codex", "codebuddy"):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp).resolve()
                cloud = Cloud()
                cloud.records = {("skill", "synthetic-skill"): dict(SKILL, enabled=True)}
                session = ContentSession(home, home / "cache", POLICY, cloud, host=content_host(harness))
                session.refresh()
                before = session.mapping.owned_snapshot()
                cloud.offline = True
                request = {"request_id": "synthetic-pending-removal", "method": "content_put", "params": {
                    "kind": "memory", "id": "synthetic-topic", "expected_revision": "missing", "record": MEMORY}}
                session.invoke(request)
                with self.assertRaises(ValueError):
                    projection_removal.prepare(session)
                self.assertEqual(session.mapping.owned_snapshot(), before)
                cloud.offline = False
                session.invoke(request)
                projection_removal.remove(session)
                projection_removal.remove(session)
                self.assertFalse(session.mapping.owned_snapshot()["files"])
                self.assertEqual(session.queue_status()["confirmed"], 1)
                self.assertTrue((session.cache / "snapshots").exists())

    def test_codebuddy_hook_uninstall_and_update_preserve_explicit_pause(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime.host_maintenance import global_entry
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            configuration.install(home, ROOT, None, surface="both")
            native = home / ".codebuddy/settings.json"
            value = configuration.Document(native.read_text()).value
            value["permissions"] = {"deny": ["Synthetic preserved permission"]}
            native.write_text(json.dumps(value))
            result = global_entry(SimpleNamespace(device_home=str(home)), harness="codebuddy", remove=True)
            self.assertEqual(result["status"], "global-hook-removed")
            configuration.refresh(home, ROOT)
            receipt = metadata.private_json(configuration.state_directory(home) / "registration.json")
            self.assertFalse(receipt["hooks_enabled"])
            checked = configuration.status(home, None, backend_check=lambda *a: (None, {"status": "synthetic-verified"}))
            self.assertTrue(checked["installed"])
            self.assertFalse(checked["collection_hooks_installed"])
            self.assertEqual(configuration.Document(native.read_text()).value["permissions"], value["permissions"])
            self.assertTrue(all(not groups for groups in configuration.Document(native.read_text()).value.get("hooks", {}).values()))
            configuration.install(home, ROOT, None, surface="both", enable_hooks=True)
            self.assertTrue(configuration.status(home, None, backend_check=lambda *a: (None, {"status": "synthetic-verified"}))["collection_hooks_installed"])

    def test_retained_codebuddy_project_pause_does_not_stop_codex(self):
        from agent_brain_client.adapters.storage.project_settings import seed_host_stops, source_active, clear_host_stop
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "synthetic-project"
            project.mkdir()
            seed_host_stops(home, [{"harness": "codebuddy", "root": str(project), "stopped": True, "enabled": True}])
            self.assertFalse(source_active(home, project, "codebuddy"))
            self.assertTrue(source_active(home, project, "codex"))
            clear_host_stop(home, project, "codex")
            self.assertFalse(source_active(home, project, "codebuddy"))
            clear_host_stop(home, project, "codebuddy")
            self.assertTrue(source_active(home, project, "codebuddy"))

    def test_paused_codebuddy_project_never_observes_native_bodies(self):
        import io
        from agent_brain_client.runtime import project_memory
        from agent_brain_client.adapters.harnesses.codebuddy import transcript
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            p = {"harness": "codebuddy", "device_home": str(home), "project_root": str(home),
                 "capture_policy": "synthetic-paused-policy", "source_mode": "codex-project"}
            payload = {"hook_event_name": "UserPromptSubmit", "session_id": "synthetic-session", "cwd": str(home), "prompt": "Synthetic prompt"}
            stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})()
            with patch.object(project_memory, "active", return_value=True), patch.object(project_memory, "capture_enabled", return_value=False), \
                    patch.object(project_memory.sys, "stdin", stdin), patch.object(project_memory, "capture", return_value={"status": "disabled"}), \
                    patch.object(project_memory, "recalled", return_value=[]), patch("agent_brain_client.runtime.delivery.schedule_project"), \
                    patch.object(transcript, "observe", side_effect=AssertionError("Paused source must not be observed")):
                project_memory.hook_payload(p, settings_path=str(home / "settings.json"), settings_sha256="a" * 64)

    def test_multi_host_reinstall_keeps_codex_stop_out_of_codebuddy_scope(self):
        from agent_brain_client.adapters.storage.project_settings import retained_controls, seed_host_stops, source_active
        from agent_brain_client.adapters.storage import metadata
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            project = home / "synthetic-project"
            project.mkdir()
            directory = home / ".config/agent-brain-client/project-memory/synthetic-control"
            private_directory(directory)
            off = directory / "memory.off"
            off.touch(mode=0o600)
            metadata.write(directory / "settings.json", {"schema": 1, "harness": "codex", "enabled": True,
                "device_home": str(home), "project_root": str(project), "off_file": str(off), "source_mode": "codex-project"})
            controls = retained_controls(home, [], isolate_hosts=True)
            self.assertEqual(controls["blocked_roots"], [])
            seed_host_stops(home, controls["states"], include_legacy=controls["host_scoped"])
            self.assertFalse(source_active(home, project, "codex"))
            self.assertTrue(source_active(home, project, "codebuddy"))

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    def test_common_install_and_source_runtime_do_not_require_any_concrete_host(self):
        code = '''
import sys, pathlib, importlib.abc
sys.path[:0] = [sys.argv[1], sys.argv[2], str(pathlib.Path(sys.argv[1]).parent)]
import test_mac_first_install as fixture
f=fixture.FirstInstall('test_first_install_prepared_environment_and_exact_native_transaction')
f.setUp()
try:
    f.fixture()
    f.harness='codebuddy'
    f.native=f.home/'.config/agent-brain-client/shared-native'
    f.native.mkdir()
    f.config=f.native/'config.toml'
    for name in list(sys.modules):
        if name.startswith('agent_brain_client.adapters.harnesses.'):
            del sys.modules[name]
    class NoHarness(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith('agent_brain_client.adapters.harnesses.'):
                raise AssertionError('Shared install required native implementation: '+fullname)
    sys.meta_path.insert(0,NoHarness())
    result=f.run_wizard()
    assert result['installed'],result
    from types import SimpleNamespace
    from agent_brain_client.runtime.host_maintenance import install_sources
    prepared=install_sources(SimpleNamespace(device_home=str(f.home),profile_id=None,operator=None,operator_sha256=None))
    assert prepared['harness']=='codebuddy',prepared
    from agent_brain_client.runtime.host_binding import load_core
    load_core(f.home)
    from agent_brain_client.runtime.updates import reexec_update_runtime
    reexec_update_runtime(f.home,'check-update',runner=lambda *a,**k:SimpleNamespace(returncode=0))
    assert not any(name.startswith('agent_brain_client.adapters.harnesses.') for name in sys.modules)
    assert not (f.home/'.codex').exists()
    assert not (f.home/'.agents').exists()
finally:
    f.doCleanups()
'''
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(ROOT / "src"), str(ROOT / "tests")], capture_output=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_fresh_reinstall_retains_both_mapping_ledgers_and_codebuddy_pause(self):
        from test_fresh_installation import FreshInstallation
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage import metadata
        f = FreshInstallation("test_retains_old_files_and_queue_restores_only_memory_projection")
        f.setUp()
        self.addCleanup(f.doCleanups)
        configuration.install(f.home, ROOT, None, surface="cli", enable_hooks=False)
        cloud = Cloud()
        cloud.grant = f.grant
        cloud.records = {("memory", "personal-owner"): copy.deepcopy(MEMORY)}
        buddy_cache = f.root / "codebuddy/content-cache"
        session = ContentSession(f.home, buddy_cache, f.content, cloud, host=content_host("codebuddy"))
        self.assertEqual(session.refresh()["status"], "content-refreshed")
        original = session.mapping.owned_snapshot()
        old_queue = buddy_cache / "outbox/retained-original-request.json"
        old_queue.parent.mkdir(exist_ok=True)
        old_queue.write_bytes(b"SYNTHETIC_OLD_QUEUE_NOT_REPLAYED")
        old_queue.chmod(0o600)
        result = f.prepare()
        self.assertEqual(result["status"], "fresh-install-ready")
        record = f.port.load()
        self.assertEqual(record["mapping"]["additional_projections"]["codebuddy"]["mapping"]["owned"], original)
        self.assertEqual(record["mapping"]["registered_surfaces"]["codebuddy"], {"surface": "cli", "hooks_enabled": False})
        from agent_brain_client.runtime.fresh_installation import retained_installation
        switched = retained_installation(f.home, Path(record["native"]), target_host=content_host("codebuddy"))
        switched.restore_mapping(buddy_cache, f.content)
        codex_cache = f.root / "codex/content-cache"
        self.assertEqual(metadata.private_json(codex_cache / "owned.json", cache=True), record["mapping"]["owned"])
        self.assertEqual(metadata.private_json(buddy_cache / "owned.json", cache=True), original)
        self.assertFalse(old_queue.exists())
        self.assertEqual((Path(record["archive"]) / "codebuddy/content-cache/outbox/retained-original-request.json").read_bytes(), b"SYNTHETIC_OLD_QUEUE_NOT_REPLAYED")
        self.assertEqual(f.memory_path.read_text(), "Keep the existing remembered fact.")

    @unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
    def test_public_uninstall_checks_secondary_queue_then_removes_both_owned_projections(self):
        from test_client_uninstall import ClientUninstall
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.runtime import client_lifecycle
        from agent_brain_client.adapters.storage import metadata
        f = ClientUninstall("test_cancel_is_read_only_and_status_never_reads_credentials")
        f.setUp()
        self.addCleanup(f.doCleanups)
        configuration.install(f.home, ROOT, None, surface="cli")
        cache = f.state / "codebuddy/content-cache"
        session = ContentSession(f.home, cache, f.adapter.policy, f.cloud, host=content_host("codebuddy"))
        session.refresh()
        f.cloud.offline = True
        request = {"request_id": "synthetic-secondary-pending", "method": "content_put", "params": {
            "kind": "memory", "id": "synthetic-memory", "expected_revision": contract.envelope("memory", "synthetic-memory", MEMORY)["revision"],
            "record": dict(MEMORY, body="Synthetic revised preference")}}
        session.invoke(request)
        config_before = f.f.config.read_bytes()
        result = client_lifecycle.uninstall(f.home, lambda message: "Continue")
        self.assertEqual((result["status"], result["harness"]), ("uninstall-unconfirmed-writes", "codebuddy"))
        self.assertEqual(f.f.config.read_bytes(), config_before)
        f.cloud.offline = False
        session.invoke(request)
        result = client_lifecycle.uninstall(f.home, lambda message: "Continue")
        self.assertEqual(result["status"], "client-uninstalled")
        self.assertFalse(session.mapping.owned_snapshot()["files"])
        self.assertTrue(metadata.private_json(configuration.state_directory(f.home) / "registration.json")["removed"])
        self.assertEqual(session.queue_status()["confirmed"], 1)
        self.assertEqual(client_lifecycle.uninstall(f.home, lambda message: "Continue")["status"], "client-uninstalled")

    def test_incomplete_manual_queue_sync_returns_nonzero(self):
        import io
        from agent_brain_client.interfaces.cli import collection as cli
        from agent_brain_client.runtime import collection
        output = io.StringIO()
        with patch.object(collection, "load", return_value={}), patch.object(collection, "flush", return_value={"status": "global-queue-sync-incomplete"}), \
                patch.object(cli.sys, "stdout", output):
            self.assertEqual(cli.main(["global-sync"]), 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "global-queue-sync-incomplete")


@unittest.skipUnless(sys.platform == "darwin", "Native Mac installation contract")
class IndependentHostControl(unittest.TestCase):
    def shared_install(self, harness):
        from test_mac_first_install import FirstInstall
        f = FirstInstall("test_first_install_prepared_environment_and_exact_native_transaction")
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.fixture()
        f.harness = harness
        f.native = f.home / ".config/agent-brain-client/shared-native"
        private_directory(f.native)
        f.config = f.native / "config.toml"
        self.assertTrue(f.run_wizard()["installed"])
        return f

    def test_both_install_orders_keep_native_pause_and_removal_out_of_shared_control(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage.registration import registration
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.host_binding import backend
        from agent_brain_client.interfaces.cli.install import selected_native
        for first in ("codex", "codebuddy"):
            with self.subTest(first=first):
                f = self.shared_install(first)
                native = f.home / "selected-codex-profile"
                if first == "codex":
                    binding.install(f.home, native=native)
                install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                                 operator=None, operator_sha256=None, harness=first))
                configuration.install(f.home, ROOT, None, surface="cli")
                if first == "codebuddy":
                    self.assertFalse(native.exists())
                    binding.install(f.home, native=native)
                    install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                                     operator=None, operator_sha256=None, harness="codex"))
                _, policy = registration(f.home)
                runtime = policy["runtime"]
                codex_native, codex_cache = content_host("codex").location(f.home, runtime)
                buddy_native, buddy_cache = content_host("codebuddy").location(f.home, runtime)
                self.assertEqual(codex_native, native)
                self.assertEqual(buddy_native, f.home / ".codebuddy")
                self.assertNotEqual(codex_cache, buddy_cache)
                self.assertEqual(selected_native(f.home, "codex"), f.native)
                self.assertEqual(selected_native(f.home, "codebuddy"), f.native)
                project = f.home / "synthetic-project"
                project.mkdir()
                from agent_brain_client.runtime import project_memory
                from agent_brain_client.adapters.storage.metadata import private_json
                for harness in ("codex", "codebuddy"):
                    prepared = project_memory.setup(SimpleNamespace(device_home=str(f.home), project_root=str(project),
                        output=str(f.home / ".config/agent-brain-client/project-memory" / harness / "settings.json"),
                        harness=harness, query="Synthetic", server_bundle=None))
                    settings = project_memory.load(prepared["settings"], prepared["settings_sha256"])
                    self.assertEqual(settings["codex_home"], str(native if harness == "codex" else buddy_native))
                    self.assertEqual(private_json(prepared["settings"])["harness"], harness)
                shared_before = f.config.read_bytes()
                config = native / "config.toml"
                config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
                with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
                    self.assertTrue(backend(f.home, None)[1]["installed"])
                self.assertFalse(binding.status(f.home, runtime)["service_enabled"])
                binding.install(f.home)
                self.assertFalse(binding.status(f.home, runtime)["service_enabled"])
                self.assertEqual(f.config.read_bytes(), shared_before)
                # A removed/edited native profile is a host repair issue. The
                # other host still authenticates against private shared control.
                config.unlink()
                with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
                    self.assertTrue(backend(f.home, None)[1]["installed"])
                self.assertEqual(f.config.read_bytes(), shared_before)
                self.assertFalse((f.home / ".codex").exists())

    def test_early_hook_recovery_uses_native_profile_and_authenticates_shared_control(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding, collection_installation as hooks
        from agent_brain_client.adapters.storage import host_files
        from agent_brain_client.adapters.storage.collection import settings_path
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.installation_completion import resume_setup
        f = self.shared_install("codex")
        native = f.home / "selected-codex-profile"
        binding.install(f.home, native=native)
        install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                         operator=None, operator_sha256=None, harness="codex"))
        shared_before = f.config.read_bytes()
        native_file = native / "config.toml"
        before = native_file.read_bytes()
        original_apply = host_files.host_apply
        def interrupt(plan, save):
            original_apply(plan, save)
            raise SystemExit("Synthetic interruption after native commit")
        owned = hooks.split_owned(before)[1]
        wanted = owned.replace("timeout = 120", "timeout = 121")
        self.assertNotEqual(owned, wanted)
        with patch.object(host_files, "host_apply", side_effect=interrupt), self.assertRaises(SystemExit):
            hooks.transact(native, settings_path(f.home).parent, wanted)
        self.assertNotEqual(native_file.read_bytes(), before)
        self.assertIsNone(resume_setup(f.home, f.native))
        self.assertEqual(native_file.read_bytes(), before)
        self.assertEqual(f.config.read_bytes(), shared_before)
        self.assertEqual(hooks.read_receipt(settings_path(f.home).parent / "hook-install.json")["phase"], "rolled-back")

    def test_private_control_uninstall_detaches_both_native_entries_and_retains_data(self):
        from types import SimpleNamespace
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.harnesses.codebuddy import configuration
        from agent_brain_client.adapters.storage.registration import registration
        from agent_brain_client.runtime.host_maintenance import install_sources
        from agent_brain_client.runtime.client_lifecycle import uninstall
        f = self.shared_install("codebuddy")
        native = f.home / "selected-codex-profile"
        binding.install(f.home, native=native)
        install_sources(SimpleNamespace(device_home=str(f.home), profile_id=None,
                                         operator=None, operator_sha256=None, harness="codex"))
        configuration.install(f.home, ROOT, None, surface="cli")
        _, policy = registration(f.home)
        cloud = Cloud()
        cloud.grant = copy.deepcopy(f.fixture_grant)
        from agent_brain_client.adapters.storage.metadata import private_json
        content = private_json(policy["runtime"]["content_policy"], policy["runtime"]["content_policy_sha256"])
        cloud.records = {("memory", "synthetic-memory"): MEMORY}
        sessions = []
        for harness in ("codex", "codebuddy"):
            port = content_host(harness)
            actual, cache = port.location(f.home, policy["runtime"])
            session = ContentSession(f.home, cache, content, cloud, host=port, native_home=actual)
            self.assertEqual(session.refresh()["status"], "content-refreshed")
            sessions.append(session)
        config = native / "config.toml"
        config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
        with patch("agent_brain_client.adapters.packages.client.ClientReleases.verified_active", return_value={"version": "synthetic"}):
            result = uninstall(f.home, lambda message: "Continue")
        self.assertEqual(result["status"], "client-uninstalled", result)
        self.assertNotIn(b"agent_brain_server", (native / "config.toml").read_bytes())
        self.assertNotIn(b"Agent Brain global memory", (native / "config.toml").read_bytes())
        self.assertTrue(binding.receipt(f.home)["removed"])
        for session in sessions:
            self.assertFalse(session.mapping.owned_snapshot()["files"])
            self.assertTrue(session.cache.is_dir())
        self.assertTrue(f.run_wizard()["installed"])
        from agent_brain_client.runtime.installation_completion import complete
        with patch("agent_brain_client.runtime.installation_completion.source_setup", return_value={"collection_enabled": True}):
            result = complete(f.home, {"installed": True}, harness="codex",
                              refresh=lambda *args: {"status": "content-current"})
        self.assertTrue(result["local_setup_complete"], result)
        self.assertFalse(binding.receipt(f.home)["removed"])
        self.assertFalse(binding.status(f.home, policy["runtime"])["service_enabled"])
        self.assertIn(b"Agent Brain global memory", (native / "config.toml").read_bytes())


class NativeBindingTransaction(unittest.TestCase):
    def test_native_codex_transaction_preserves_pause_and_unrelated_toml(self):
        from agent_brain_client.adapters.harnesses.codex import binding
        from agent_brain_client.adapters.storage import registration
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / "native-profile"
            private_directory(native)
            file = native / "config.toml"
            file.write_text('model = "synthetic"\n[mcp_servers.other]\ncommand = "preserve"\n')
            file.chmod(0o600)
            policy = {"runtime": {"codex_home": str(home / ".config/agent-brain-client/shared-native")}}
            command = {"command": str(Path(sys.executable).resolve()), "args": ["--harness", "codebuddy"],
                       "env": {}, "enabled": True}
            with patch.object(registration, "registration", return_value=({}, policy)), patch(
                "agent_brain_client.adapters.installation.installation.registered_context", return_value=(policy, command, None, None)):
                binding.install(home, native=native)
                raw = file.read_bytes().replace(b"enabled = true", b"enabled = false")
                file.write_bytes(raw)
                binding.install(home)
                self.assertIn(b"enabled = false", file.read_bytes())
                self.assertIn(b'command = "preserve"', file.read_bytes())
                self.assertIn(b'model = "synthetic"', file.read_bytes())
                binding.install(home, remove=True)
                self.assertNotIn(b"agent_brain_server", file.read_bytes())
                self.assertIn(b'command = "preserve"', file.read_bytes())

    def test_native_collision_is_rejected_before_shared_enrollment(self):
        import os
        from agent_brain_client.interfaces.cli.install import selected_native, lifecycle_exit
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            native = home / "native-profile"
            private_directory(native)
            file = native / "config.toml"
            original = b'[mcp_servers.agent_brain_server]\ncommand = "preserve-legacy-service"\n'
            file.write_bytes(original)
            file.chmod(0o600)
            with patch.dict(os.environ, CODEX_HOME=str(native)):
                with self.assertRaises(ValueError):
                    selected_native(home, "codex")
                self.assertEqual(selected_native(home, "codebuddy"), home / ".config/agent-brain-client/shared-native")
            self.assertEqual(file.read_bytes(), original)
            self.assertFalse((home / ".config/agent-brain-client").exists())
        self.assertEqual(lifecycle_exit("install", {"installed": True,
            "installation_stages": {"setup": {"stage": "native-mcp", "status": "pending-review-or-resume"}}}), 2)
