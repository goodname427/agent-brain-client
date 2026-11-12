"""Fresh worker evidence, with no credentials, fetch, host restart or real data."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from test_content_error_stages import Adapter
from agent_brain_client.interfaces.mcp.content import ContentServer


class RuntimeReload(TestCase):
    def test_source_checkout_does_not_claim_loaded_commit(self):
        server = ContentServer(Adapter(), '0.6.6')
        value = server.client_runtime
        self.assertEqual(value['running_worker_version'], '0.6.6')
        self.assertIsNone(value['reload_required'])
        self.assertEqual(value['model_loaded'], 'unknown')
        self.assertEqual(value['host_tool_visibility'], 'unknown')
        server.state = 'ready'
        offered = server.handle({'jsonrpc':'2.0', 'id':1, 'method':'tools/list'})['tools']
        self.assertEqual(value['offered_tools'], sorted(t['name'] for t in offered))

    def test_worker_compares_fresh_local_active_version_and_commit(self):
        adapter = Adapter()
        adapter.home = Path('/synthetic').resolve()
        with patch('agent_brain_client.interfaces.mcp.content.worker_commit', return_value='a'*40):
            server = ContentServer(adapter, '0.6.6')
        with (patch('agent_brain_client.adapters.storage.private_files.private_json', return_value={'release_policy':'synthetic-policy', 'release_policy_sha256':'b'*64}),
              patch('agent_brain_client.adapters.installation.installation.registered_context', side_effect=AssertionError('No installer or subprocess probe')) as registration,
              patch('agent_brain_client.adapters.packages.client.ClientReleases') as releases):
            releases.return_value.policy = {'runtime': {'device_home': str(adapter.home)}}
            active = releases.return_value.verified_active
            for version, commit, required, reason in (
                ('0.6.6', 'a'*40, False, 'running-worker-current'),
                ('0.6.6', 'c'*40, True, 'running-worker-differs'),
                ('0.6.7', 'c'*40, True, 'running-worker-differs'),
                ('0.6.6', None, None, 'running-worker-commit-unobserved'),
            ):
                with self.subTest(version=version, commit=commit):
                    active.return_value = {'version':version, 'commit':commit}
                    result = server.invoke('content_catalog', {'kinds':['memory']})['structuredContent']['client_runtime']
                    self.assertIs(result['reload_required'], required)
                    self.assertEqual(result['reload_reason'], reason)
                    self.assertEqual(result['active_package_version'], version)
                    self.assertEqual(result['running_worker_commit'], 'a'*40)
            releases.assert_called_with('synthetic-policy', 'b'*64, inspection=True)
            active.assert_called_with(serving=True)
            registration.assert_not_called()
            active.side_effect = ValueError('Synthetic invalid active pointer')
            result = server.invoke('content_catalog', {'kinds':['memory']})['structuredContent']['client_runtime']
            self.assertIsNone(result['reload_required'])
            self.assertIsNone(result['active_package_version'])
            self.assertEqual(result['running_worker_version'], '0.6.6')
            releases.return_value.discover.assert_not_called()
            releases.return_value.update.assert_not_called()

    def test_legacy_worker_commit_does_not_prove_version_equality(self):
        adapter = Adapter()
        adapter.home = Path('/synthetic').resolve()
        with patch('agent_brain_client.interfaces.mcp.content.worker_commit', return_value=None):
            server = ContentServer(adapter, '0.6.6')
        with (patch('agent_brain_client.adapters.storage.private_files.private_json', return_value={'release_policy':'synthetic-policy', 'release_policy_sha256':'b'*64}),
              patch('agent_brain_client.adapters.installation.installation.registered_context'),
              patch('agent_brain_client.adapters.packages.client.ClientReleases') as releases):
            releases.return_value.policy = {'runtime': {'device_home': str(adapter.home)}}
            active = releases.return_value.verified_active
            active.return_value = {'version':'0.6.6', 'commit':'a'*40}
            self.assertIsNone(server.client_runtime['reload_required'])
            active.return_value = {'version':'0.6.7', 'commit':'c'*40}
            self.assertTrue(server.client_runtime['reload_required'])
