import agent_brain_client.interfaces.cli.collection as collection_cli
import agent_brain_client.runtime.updates as runtime_updates
import agent_brain_client.adapters.storage.collection as source_policy
import agent_brain_client.adapters.storage.metadata as metadata
"""Source activation/control regressions with synthetic state and no network."""
import copy
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_brain_client.runtime.collection as gm
import agent_brain_client.runtime.source_enrollment as operator
from agent_brain_client.adapters.platforms.filesystem import private_directory
from agent_brain_client.domain.scopes import digest


class GlobalMemoryActivation(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='synthetic-source-activation-')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.file = source_policy.settings_path(self.home)
        private_directory(self.file.parent)
        self.scope = {'schema':1, 'profile_id':'a' * 32,
                      'include_roots':[{'root':str(self.home), 'sharing':'personal'}],
                      'exclude_roots':[], 'non_project_roots':[], 'recall_personal':True}
        self.value = {'device_home':str(self.home), 'enabled':True, 'scope':self.scope,
                      'approved_scope_sha256':digest(self.scope),
                      'server_scope_sha256':digest(self.scope), 'legacy_projects':[], 'operator':None}
        self.off = self.file.parent / 'global.off'

    def test_empty_source_resume_cannot_claim_activation_or_access_credentials(self):
        value = copy.deepcopy(self.value)
        value.update(enabled=False, approved_scope_sha256=None, server_scope_sha256=None)
        value['scope']['include_roots'] = []
        self.off.touch()
        before = self.off.read_bytes()
        with patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')):
            result = gm.resume(value)
        self.assertEqual(result['status'], 'global-memory-activation-required')
        self.assertFalse(result['remote_confirmed'])
        self.assertFalse(result['collection_enabled'])
        self.assertEqual(self.off.read_bytes(), before)
        self.assertFalse(self.file.exists())

    def test_registration_retries_exact_approved_metadata_without_new_identity(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        packet = {'schema':2, 'scope':self.scope, 'approved_scope_sha256':digest(self.scope),
                  'client':{'source_device_id':'c'*32}}
        response = {'result':{'source_registered':True,'source_device_id':'c'*32,
                             'global_profile_id':self.scope['profile_id'],'scope_sha256':digest(self.scope)}}
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=Mock(side_effect=[
            {'error':{'code':'transport_unconfirmed','retryable':True}}, response]))
        before = copy.deepcopy(value)
        with (patch('agent_brain_client.adapters.server.collection.CollectionClient',return_value=client),
              patch.object(operator,'activate',return_value={'activated':True}) as activate):
            first = operator.register(value,packet)
            self.assertFalse(first['activated']); activate.assert_not_called()
            self.assertEqual(value,before)
            self.assertTrue(operator.register(value,packet)['activated'])
        self.assertEqual(client.wire.call_args_list[0],client.wire.call_args_list[1])
        self.assertEqual(client.wire.call_args.args[2],'source_'+digest(packet))
        activate.assert_called_once_with(value,self.scope)

    def test_wrong_packet_cannot_register_or_resume(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        packet = {'schema':2,'scope':self.scope,'approved_scope_sha256':'0'*64,
                  'client':{'source_device_id':'c'*32}}
        with patch('agent_brain_client.adapters.server.collection.CollectionClient',side_effect=AssertionError('No credential access')):
            with self.assertRaises(ValueError): operator.register(value,packet)

    def test_each_missing_approval_keeps_source_fenced_without_server_request(self):
        for field, missing in (('enabled', False), ('approved_scope_sha256', None),
                               ('server_scope_sha256', None)):
            with self.subTest(field=field):
                value = dict(self.value, **{field:missing})
                self.off.touch()
                with patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')):
                    self.assertFalse(gm.resume(value)['remote_confirmed'])
                self.assertTrue(self.off.exists())

    def test_failed_or_nonconfirming_resume_retains_fence_and_exact_denial(self):
        error = {'code':'global_source_device_denied', 'retryable':False}
        responses = [{'error':error}, {'result':{}},
                     {'result':{'schema':1, 'stopped':True}},
                     {'result':{'schema':1, 'stopped':0}}]
        for response in responses:
            with self.subTest(response=response):
                client = SimpleNamespace(harness="codex", wire=Mock(return_value=response))
                with patch.object(gm, 'CollectionClient', return_value=client):
                    result = gm.resume(self.value)
                self.assertEqual(result['status'], 'global-memory-resume-unconfirmed')
                self.assertFalse(result['collection_enabled'])
                self.assertEqual(result['server_error'], response.get('error'))
                self.assertTrue(self.off.exists())
                client.wire.assert_called_once_with('memory_global_resume', {})
                self.assertFalse(self.file.exists())

    def test_transport_exception_leaves_even_previously_running_source_fenced(self):
        client = SimpleNamespace(harness="codex", wire=Mock(side_effect=OSError('Synthetic transport failure')))
        with patch.object(gm, 'CollectionClient', return_value=client), self.assertRaises(OSError):
            gm.resume(self.value)
        self.assertTrue(self.off.exists())

    def test_confirmed_resume_only_clears_local_fence_without_changing_scope(self):
        before = copy.deepcopy(self.value)
        self.off.touch()
        client = SimpleNamespace(harness="codex", wire=Mock(return_value={'result':{'schema':1, 'stopped':False}}))
        with patch.object(gm, 'CollectionClient', return_value=client): result = gm.resume(self.value)
        self.assertTrue(result['remote_confirmed'])
        self.assertTrue(result['collection_enabled'])
        self.assertFalse(self.off.exists())
        self.assertEqual(self.value, before)
        self.assertFalse(self.file.exists())

    def test_local_stop_guidance_resumes_original_approval_without_recreating_packet(self):
        self.off.touch()
        self.assertEqual(source_policy.admission_state(self.value)['next_action'],
                         'explicit-global-resume-for-the-existing-approved-source')
        pending = dict(self.value, server_scope_sha256=None)
        self.assertEqual(source_policy.admission_state(pending)['next_action'],
                         'check-server-and-have-existing-operator-apply-the-saved-device-packet')

    def test_legacy_resume_keeps_original_project_control_and_no_expanded_source_claim(self):
        value = dict(self.value, enabled=False, approved_scope_sha256=None,
                     server_scope_sha256=None,
                     legacy_projects=[{'root':'synthetic-project', 'settings':'synthetic-settings', 'settings_sha256':'b' * 64}])
        source = {'off_file':str(self.home / 'synthetic-project.off')}
        with (patch.object(gm, 'CollectionClient', side_effect=AssertionError('No global credentials')),
              patch('agent_brain_client.adapters.storage.project_settings.load', return_value=source),
              patch('agent_brain_client.runtime.project_memory.daily_command', return_value={}) as command):
            result = gm.resume(value)
        command.assert_called_once_with(source, 'resume')
        self.assertEqual(result['status'], 'global-memory-legacy-resumed')
        self.assertTrue(result['legacy_capture_resumed'])
        self.assertFalse(result['collection_enabled'])

    def test_cli_pending_resume_returns_nonzero_and_actionable_json(self):
        value = dict(self.value, enabled=False)
        output = io.StringIO()
        with (patch.object(gm, 'load', return_value=value),
              patch.object(sys, 'argv', ['global_memory', 'global-resume']),
              patch.object(sys, 'stdout', output),
              patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials'))):
            self.assertEqual(collection_cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue())['status'], 'global-memory-activation-required')

    def test_migrated_legacy_sources_are_not_reenabled_while_global_admission_is_pending(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None,
                     legacy_projects=[{'capture_delegated':False}])
        with (patch.object(gm, 'CollectionClient', side_effect=AssertionError('No credentials')),
              patch('agent_brain_client.runtime.project_memory.daily_command', side_effect=AssertionError('No legacy reactivation'))):
            result = gm.resume(value)
        self.assertEqual(result['status'], 'global-memory-activation-required')

    def test_cli_denied_activation_returns_nonzero_with_original_error(self):
        output = io.StringIO()
        result = {'status':'global-memory-admission-unconfirmed', 'activated':False,
                  'server_error':{'code':'global_source_device_denied', 'retryable':False}}
        with (patch.object(operator, 'configure', return_value=result),
              patch.object(sys, 'argv', ['global_memory', 'global-configure', '--activate-packet', 'synthetic-packet']),
              patch.object(sys, 'stdout', output)):
            self.assertEqual(collection_cli.main(), 2)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_activation_does_not_clear_fence_on_missing_resume_confirmation(self):
        value = dict(self.value, enabled=False, server_scope_sha256=None)
        status = {'result':{'source_registered':True, 'scope_sha256':digest(self.scope),
                            'global_profile_id':self.scope['profile_id'], 'source_device_id':'c' * 32}}
        client = SimpleNamespace(harness="codex", device_id='c' * 32, wire=Mock(side_effect=[status, {'result':{}}]))
        with patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client):
            result = operator.activate(value, self.scope)
        self.assertFalse(result['activated'])
        self.assertFalse(result['collection_enabled'])
        self.assertFalse(self.off.exists())
        self.assertEqual(value, dict(self.value, enabled=False, server_scope_sha256=None))

    def test_configuration_connection_failure_keeps_current_source_active(self):
        before = copy.deepcopy(self.value)
        args = SimpleNamespace(harness="codex", device_home=str(self.home), include_root=[], isolate_root=[],
            exclude_root=[], non_project_root=[], all_projects=True, activate_packet=None)
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=Mock(return_value={'error':{'code':'transport_unconfirmed'}}))
        with patch('agent_brain_client.adapters.storage.collection.load', return_value=self.value), patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client):
            result = operator.configure(args)
        self.assertEqual(result['status'], 'global-memory-connection-pending')
        self.assertEqual(self.value, before)
        self.assertFalse(self.off.exists())
        self.assertFalse(self.file.exists())
        client.wire.assert_called_once_with('memory_global_status', {})

    def test_scope_edit_keeps_each_pause_and_only_explicit_resume_lifts_it(self):
        from agent_brain_client.adapters.packages.frozen import runtime_pins
        from agent_brain_client.adapters.storage import registration
        from agent_brain_client.application.installation.update import ClientUpdate

        scope = dict(
            self.scope, all_projects=False,
            include_roots=[{"root": str(self.home / "projects"), "sharing": "project"}],
            exclude_roots=[str(self.home / "old-exclusion")],
            non_project_roots=[str(self.home / "notes")], recall_personal=False,
        )
        excluded = str(self.home / "new-exclusion")
        expected = dict(scope, exclude_roots=[excluded])
        queue = self.file.parent / "outboxes" / "synthetic-queue"
        queue.parent.mkdir()
        queue.write_bytes(b"original synthetic event")
        profile = self.file.parent / "synthetic-profile.json"
        metadata.write(profile, {"endpoint": "https://synthetic.invalid",
                                 "grant": {"actor": "synthetic-owner"}})
        runtime = {"https_profile": str(profile),
                   "https_profile_sha256": metadata.digest(profile.read_bytes()),
                   "content_policy_sha256": "f" * 64}
        code_pins = runtime_pins(source_policy.ROOT, fallback=source_policy.FILES)

        def cli(*args):
            with patch.object(sys, "stdout", io.StringIO()) as output:
                code = collection_cli.main([*args, "--device-home", str(self.home)])
            return code, json.loads(output.getvalue())

        for local_stop, server_stop, enabled, lose_reply in (
            (True, False, True, False), (False, True, True, False),
            (True, True, True, False), (True, True, True, True),
            (False, False, True, False), (False, False, False, False),
        ):
            with self.subTest(local_stop=local_stop, server_stop=server_stop,
                              enabled=enabled, lose_reply=lose_reply):
                if local_stop:
                    self.off.touch(mode=0o600)
                original = dict(self.value, enabled=enabled, scope=scope, schema=1,
                                code_sha256=code_pins, operator_sha256=None, recall={"maxResults": 3},
                                approved_scope_sha256=digest(scope), server_scope_sha256=digest(scope))
                metadata.write(self.file, original)
                before = self.file.read_bytes()
                remote_scope, stopped, calls = scope, server_stop, []
                device = "c" * 32

                def wire(method, params, rid=None):
                    nonlocal remote_scope, stopped
                    calls.append(method)
                    if method == "memory_global_register":
                        # Existing-device registration edits policy in place.
                        remote_scope = copy.deepcopy(params["scope"])
                        if lose_reply:
                            return {"error": {"code": "transport_unconfirmed"}}
                    elif method == "memory_global_resume":
                        stopped = False
                        return {"result": {"schema": 1, "stopped": stopped}}
                    else:
                        self.assertEqual(method, "memory_global_status")
                    return {"result": {"schema": 1, "metadata_only": True,
                        "source_registered": True, "source_device_id": device,
                        "global_profile_id": scope["profile_id"],
                        "scope_sha256": digest(remote_scope), "stopped": stopped}}

                client = SimpleNamespace(device_id=device, wire=wire)
                capture_enabled = enabled and not local_stop and not server_stop
                with (patch.object(registration, "registration", return_value=(None, {"runtime": runtime})),
                      patch("agent_brain_client.adapters.server.collection.CollectionClient", return_value=client),
                      patch.object(gm, "CollectionClient", return_value=client),
                      patch.object(ClientUpdate, "_run", return_value={"status": "client-update-current"})):
                    update = runtime_updates.manage_update(self.home, "update")
                    self.assertEqual(update["source_setup"]["collection_enabled"], capture_enabled)
                    self.assertEqual(self.file.read_bytes(), before)
                    self.assertIs(remote_scope, scope)
                    self.assertEqual(stopped, server_stop)
                    self.assertEqual(self.off.exists(), local_stop)
                    self.assertNotIn("memory_global_resume", calls)
                    calls.clear()

                    if lose_reply:
                        code, receipt = cli("global-configure", "--exclude-root", excluded)
                        self.assertEqual(code, 2, receipt)
                        self.assertEqual(receipt["status"], "global-memory-registration-pending")
                        self.assertEqual(receipt["next_action"], operator.RETRY_SCOPE_EDIT)
                        self.assertEqual(self.file.read_bytes(), before)
                        self.assertTrue(stopped and self.off.exists())
                    code, receipt = cli("global-configure", "--exclude-root", excluded)
                    self.assertEqual(code, 0, receipt)
                    self.assertEqual(remote_scope, expected)
                    self.assertEqual(stopped, server_stop)
                    self.assertEqual(self.off.exists(), local_stop)
                    self.assertNotIn("memory_global_resume", calls)
                    self.assertTrue(receipt["scope_applied"])
                    self.assertEqual(receipt["activated"], capture_enabled)
                    self.assertEqual(receipt["collection_enabled"], capture_enabled)
                    self.assertEqual(receipt["server_stopped"], server_stop)
                    self.assertEqual(source_policy.load(self.home), dict(
                        original, scope=expected, approved_scope_sha256=digest(expected),
                        server_scope_sha256=digest(expected),
                    ))
                    calls.clear()
                    self.assertEqual(cli("global-configure", "--exclude-root", excluded)[0], 0)
                    self.assertEqual(calls, ["memory_global_status"])
                    if enabled and (local_stop or server_stop):
                        code, receipt = cli("global-resume")
                        self.assertEqual(code, 0, receipt)
                        self.assertTrue(receipt["collection_enabled"])
                        self.assertFalse(self.off.exists())
                        self.assertFalse(stopped)
                        self.assertEqual(calls.count("memory_global_resume"), 1)
                        self.assertEqual(remote_scope, expected)
                        self.assertEqual(source_policy.load(self.home)["scope"], expected)
                self.assertEqual(queue.read_bytes(), b"original synthetic event")


class AutomaticCapture(unittest.TestCase):
    setUp = GlobalMemoryActivation.setUp

    def test_first_setup_binds_placeholder_and_update_reconciles_lost_resume_reply(self):
        from agent_brain_client.runtime.installation_completion import source_setup
        from agent_brain_client.domain.scopes import select
        scope = dict(self.scope, include_roots=[])
        value = dict(self.value, enabled=False, scope=scope, approved_scope_sha256=None, server_scope_sha256=None)
        metadata.write(self.file, value)
        profile = self.file.parent/'synthetic-profile.json'
        metadata.write(profile, {'endpoint':'https://synthetic.invalid',
            'grant':{'actor':'synthetic-owner','platform':'win32-amd64','scopes':['read','put']}})
        runtime = {'https_profile':str(profile),'https_profile_sha256':metadata.digest(profile.read_bytes()),
            'content_policy_sha256':'f'*64}
        state = {'scope':None, 'stopped':None, 'calls':[]}
        def wire(method, params, rid=None):
            state['calls'].append(method)
            scope = state['scope']
            if method == 'memory_global_status':
                return {'result':{'metadata_only':True,'source_registered':scope is not None,
                    'source_device_id':'c'*32,'global_profile_id':'b'*32,
                    'scope_sha256':digest(scope) if scope else None, 'stopped':state['stopped']}}
            if method == 'memory_global_register':
                self.assertEqual(params['scope']['profile_id'], 'b'*32)
                state['scope'] = copy.deepcopy(params['scope'])
                state['stopped'] = True
                return {'result':{'source_registered':True,'source_device_id':'c'*32,
                    'global_profile_id':'b'*32,'scope_sha256':digest(state['scope'])}}
            if method == 'memory_global_resume':
                state['stopped'] = False
                return {'error':{'code':'transport_unconfirmed'}}
            self.fail('No model or history request: ' + method)
        client = SimpleNamespace(harness="codex", device_id='c'*32, wire=wire)
        def load(*args, **kwargs): return metadata.private_json(self.file)
        with (patch('agent_brain_client.adapters.storage.collection.load', side_effect=load),
              patch('agent_brain_client.adapters.server.collection.CollectionClient', return_value=client),
              patch('agent_brain_client.adapters.storage.registration.registration', return_value=(None, {'runtime':runtime})),
              patch.object(gm, 'discover', side_effect=AssertionError('No history scan'))):
            before = self.file.read_bytes()
            self.assertFalse(source_setup(self.home, initialize=True, choose=lambda _:self.fail('No approval prompt'))['collection_enabled'])
            self.assertEqual(self.file.read_bytes(), before)
            self.assertTrue(source_setup(self.home)['collection_enabled'])
        self.assertEqual(state['calls'].count('memory_global_register'), 1)
        self.assertEqual(state['calls'].count('memory_global_resume'), 1)
        stored = load()
        self.assertTrue(stored['scope']['all_projects'])
        self.assertTrue(source_policy.approved(stored))
        for root in ('C:/synthetic/new-project', 'Z:/future-volume/project', '/synthetic/elsewhere'):
            local = {'kind':'project','cwd':root,'project_root':root,'repository_root':root}
            self.assertEqual(select(stored['scope'], local)['sharing'], 'personal')
        ordinary = {'kind':'non-project','cwd':str(self.home),
                    'project_root':None,'repository_root':None}
        self.assertTrue(stored['scope']['all_non_projects'])
        self.assertEqual(select(stored['scope'], ordinary)['sharing'], 'personal')
        self.assertEqual(select(stored['scope'], ordinary, recall=True)['sharing'], 'personal')

    def test_noninteractive_update_runs_source_setup_and_status_does_not(self):
        from agent_brain_client.application.installation.update import ClientUpdate
        import agent_brain_client.runtime.installation as wizard
        import agent_brain_client.runtime.installation_completion as guide
        result = {'status':'client-update-current','channel_checked':True}
        with (patch.object(ClientUpdate, '_run', return_value=result),
              patch.object(guide, 'source_setup', return_value={'collection_enabled':True}) as setup):
            metadata.write(self.file, self.value)
            self.assertTrue(runtime_updates.manage_update(self.home, 'update')['source_setup']['collection_enabled'])
            setup.assert_called_once_with(self.home)
            setup.reset_mock()
            runtime_updates.manage_update(self.home, 'check-update')
            setup.assert_not_called()

    def test_registered_update_retries_exact_expansion_and_collects_only_new_events(self):
        from agent_brain_client.adapters.installation.updates import RegisteredClientInstallation
        from agent_brain_client.adapters.packages.frozen import runtime_pins
        from agent_brain_client.adapters.storage import registration
        from agent_brain_client.application.installation.update_ports import RegisteredInstallation
        from agent_brain_client.application.memory.observations import stage_messages
        from agent_brain_client.domain.scopes import namespace, select
        from test_content_outbox import Outbox

        ordinary = self.home / "ordinary"
        isolated = self.home / "isolated"
        denied = self.home / "excluded"
        for directory in (ordinary, isolated, denied):
            directory.mkdir()
        (isolated / ".git").mkdir()
        scope = dict(self.scope, all_projects=True, exclude_roots=[str(denied)],
                     include_roots=[{"root": str(isolated), "sharing": "project"}])
        pin = digest(scope)
        value = dict(self.value, schema=1, scope=scope, approved_scope_sha256=pin,
                     server_scope_sha256=pin, operator_sha256=None,
                     code_sha256=runtime_pins(gm.ROOT, fallback=source_policy.FILES))
        metadata.write(self.file, value)
        before = self.file.read_bytes()
        pending = self.file.with_name("pending-scope.json")
        metadata.write(pending, scope)
        pending_before = pending.read_bytes()
        profile = self.file.parent / "synthetic-profile.json"
        metadata.write(profile, {"endpoint": "https://synthetic.invalid",
                                 "grant": {"actor": "synthetic-owner"}})
        runtime = {"https_profile": str(profile), "https_profile_sha256": metadata.digest(profile.read_bytes()),
                   "content_policy_sha256": "f" * 64}
        old_queue = Outbox("test_same_id_cannot_change_request_or_cas_base")
        old_queue.setUp()
        self.addCleanup(old_queue.doCleanups)
        current_queue = Outbox("test_same_id_cannot_change_request_or_cas_base")
        current_queue.setUp()
        self.addCleanup(current_queue.doCleanups)
        old_body = "Synthetic formerly observed project turn"
        project_id = namespace("synthetic-owner", scope["profile_id"])
        stage_messages(old_queue.adapter, project_id=project_id, session_id="old-session",
                       operation_id="old-turn", phase="prompt",
                       messages=[{"role": "user", "content": old_body}])
        old_file = next((old_queue.cache / "outbox").glob("*.json"))
        original = old_file.read_bytes()

        remote_scope = copy.deepcopy(scope)
        calls = []
        old_server, lose_reply = True, True
        device = "c" * 32

        def wire(method, params, rid=None):
            nonlocal remote_scope, lose_reply
            calls.append((method, copy.deepcopy(params), rid))
            if method == "memory_global_register":
                if old_server:
                    return {"error": {"code": "invalid_global_source_approval", "retryable": False}}
                self.assertEqual(params["previous_scope_sha256"], digest(remote_scope))
                remote_scope = copy.deepcopy(params["scope"])
                if lose_reply:
                    lose_reply = False
                    return {"error": {"code": "transport_unconfirmed", "retryable": True}}
            elif method != "memory_global_status":
                self.fail("Update may only check or register authenticated source metadata")
            return {"result": {"metadata_only": True, "source_registered": True,
                               "source_device_id": device, "global_profile_id": scope["profile_id"],
                               "scope_sha256": digest(remote_scope), "stopped": False}}

        client = SimpleNamespace(device_id=device, wire=wire, actor="synthetic-owner")
        active = {"version": "0.8.29", "commit": "a" * 40}
        updated = {"version": "0.8.30", "commit": "b" * 40}

        def update_package():
            nonlocal active
            status = "current" if active == updated else "updated"
            active = updated
            return {"status": status}

        channel = SimpleNamespace(verified_active=lambda: active, update=update_package)
        with (
            patch.object(registration, "registration", return_value=(None, {"runtime": runtime})),
            patch("agent_brain_client.adapters.server.collection.CollectionClient", return_value=client),
            patch.multiple(
                RegisteredClientInstallation,
                recover_hooks=Mock(return_value=False),
                handover_pending=Mock(return_value=False),
                inspect=Mock(return_value={"registration_verified": True, "installation_state": "verified"}),
                load_registration=Mock(return_value=RegisteredInstallation("darwin-arm64", True, {}, "codex")),
                environment=Mock(side_effect=nullcontext),
                open_channel=Mock(return_value=channel),
                tool_discovery=Mock(return_value={"registered_enabled_tools": []}),
                refresh_launcher=Mock(return_value=False),
                refresh_hooks=Mock(return_value={"status": "global-hook-current", "native_changed": False}),
            ),
        ):
            with patch.object(gm, "discover", side_effect=AssertionError("Update must not probe history")):
                def update(expected="client-update-current"):
                    result = runtime_updates.manage_update(self.home, "update")
                    self.assertEqual(result["status"], expected)
                    return result["source_setup"]
                fallback = update("client-update-updated")
                self.assertTrue(fallback["collection_enabled"])
                self.assertTrue(fallback["ordinary_directory_collection_pending"])
                self.assertEqual(fallback["server_error"]["code"], "invalid_global_source_approval")
                self.assertEqual(self.file.read_bytes(), before)
                old_server = False
                self.assertTrue(update()["ordinary_directory_collection_pending"])
                self.assertEqual(self.file.read_bytes(), before)
                self.assertTrue(update()["activated"])
                self.assertEqual(update()["status"], "global-memory-source-current")
            self.assertEqual([method for method, _, _ in calls], [
                "memory_global_status", "memory_global_register",
                "memory_global_status", "memory_global_register",
                "memory_global_status", "memory_global_status",
            ])
            self.assertEqual(calls[1], calls[3])
            expanded = source_policy.load(self.home)
            self.assertEqual(expanded["scope"], dict(scope, all_non_projects=True))
            self.assertEqual(expanded["approved_scope_sha256"], digest(remote_scope))
            self.assertEqual(expanded["server_scope_sha256"], digest(remote_scope))
            self.assertEqual(pending.read_bytes(), pending_before)
            self.assertFalse((current_queue.cache / "outbox").exists())
            self.assertEqual(old_file.read_bytes(), original)
            self.assertEqual(select(expanded["scope"], gm.discover(str(isolated)))["sharing"], "project")
            self.assertIsNone(select(expanded["scope"], gm.discover(str(denied))))

            new_body = "Synthetic future ordinary-directory prompt"
            payload = {"cwd": str(ordinary), "hook_event_name": "UserPromptSubmit",
                       "session_id": "new-session", "turn_id": "new-turn", "prompt": new_body}
            stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))
            with (
                patch.object(os, "getcwd", return_value=str(ordinary)),
                patch.object(sys, "stdin", stream),
                patch.object(gm, "build_delivery_queue", return_value=(client, current_queue.adapter)),
                patch.object(gm, "schedule_delivery") as schedule,
                patch.object(gm, "recall_prompt", return_value=[]) as recall,
            ):
                gm.hook(expanded)
            schedule.assert_called_once()
            recall.assert_called_once()
            captured, = current_queue.adapter._queued()
            event = captured["request"]["params"]["event"]
            self.assertEqual(event["messages"], [{"role": "user", "content": new_body}])
            self.assertEqual(event["project_id"], project_id)
            self.assertEqual(old_file.read_bytes(), original)

    def test_update_keeps_explicit_source_choices_and_pauses(self):
        default = dict(self.scope, all_projects=True)
        for change, stopped, local_stop, explicit_pending in (
            ({}, True, False, False),
            ({}, False, True, False),
            ({"all_projects": False}, False, False, False),
            ({"all_non_projects": False}, False, False, False),
            ({"non_project_roots": [str(self.home)]}, False, False, False),
            ({"recall_personal": False}, False, False, False),
            ({}, False, False, True),
        ):
            with self.subTest(change=change, stopped=stopped, local_stop=local_stop,
                              explicit_pending=explicit_pending):
                scope = dict(default, **change)
                pin, device = digest(scope), "c" * 32
                value = dict(self.value, scope=scope, approved_scope_sha256=pin, server_scope_sha256=pin)
                client = SimpleNamespace(device_id=device, wire=Mock())
                client.wire.return_value = {"result": {
                    "metadata_only": True, "source_registered": True, "source_device_id": device,
                    "global_profile_id": scope["profile_id"], "scope_sha256": pin, "stopped": stopped,
                }}
                if local_stop:
                    self.off.touch()
                if explicit_pending:
                    metadata.write(self.file.with_name("pending-scope.json"),
                                   dict(default, recall_personal=False))
                with (
                    patch.object(source_policy, "load", return_value=value),
                    patch("agent_brain_client.adapters.server.collection.CollectionClient",
                          return_value=client),
                    patch.object(operator, "packet_for", side_effect=AssertionError("No scope replacement")),
                ):
                    result = operator.maintain(self.home)
                self.assertEqual(result["collection_enabled"], not (stopped or local_stop))
                if local_stop:
                    client.wire.assert_not_called()
                    self.assertTrue(self.off.exists())
                    self.off.unlink()
                else:
                    client.wire.assert_called_once_with("memory_global_status", {})


if __name__ == '__main__': unittest.main()
