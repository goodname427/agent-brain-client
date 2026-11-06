"""New official source can install the unchanged released layout before publishing."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

import client_release as releases
import content_install as install
import test_content_install as fixtures
from build_client import release


@unittest.skipIf(sys.platform == 'win32', 'Synthetic Mac enrollment; shared membership logic is platform independent')
class ReleasedLayout(unittest.TestCase):
    def test_released_package_install_then_new_layout_preserves_original_policy(self):
        fixture = fixtures.ContentInstall('test_prepare_read_only_then_apply_inspect_reuse_and_single_link_state')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        output = fixture.base / 'historical-layout'
        manifest = release(fixtures.ROOT, output, '0.7.0', 'personal-content')
        names = releases.PRE_LAYOUT_FILE_SETS[1]
        tools = install.enabled_tools(fixture.content_value, fixture.grant)
        health = {'content_mcp_self_check': 'passed', 'core_imported': False,
                  'native_changed': False, 'keychain_accessed': False, 'network_used': False}
        files = {}
        # Synthetic executable declarations, never a copied historical runtime
        # or private build source. Preserve the exact reviewed old membership.
        for name in names:
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == 'adapters/codex/content_mcp.py':
                raw = ('import json\ndef enabled_tools(policy,grant):return ' + repr(tools) +
                       '\nif __name__=="__main__":print(json.dumps(' + repr(health) + '))\n').encode()
            elif name == 'native/DeviceComponent.swift':
                raw = (fixtures.ROOT / name).read_bytes()
            else:
                raw = b'# Synthetic historical member.\n'
            path.write_bytes(raw)
            files[name] = hashlib.sha256(raw).hexdigest()
        manifest['files'] = files
        (output / 'releases/manifest.json').write_text(json.dumps(manifest))
        publisher = fixture.fixture.publisher
        for name in (*names, 'releases/manifest.json'):
            target = publisher / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(output / name, target)
        fixture.fixture.git(publisher, 'add', '--', *names, 'releases/manifest.json')
        fixture.fixture.git(publisher, 'commit', '-m', 'Synthetic already released layout')
        fixture.fixture.git(publisher, 'push', 'origin', 'releases/stable')
        policy_before = fixture.policy_path.read_bytes()
        discovered = fixture.channel.discover('codex', 'darwin-arm64', '0.2.0')
        self.assertEqual(discovered['status'], 'compatible-update', discovered)
        updated = fixture.channel.update()
        self.assertEqual(updated['status'], 'updated', updated)
        fixture.package = Path(updated['active']['package'])
        self.assertEqual(set(json.loads((fixture.package / 'verified.json').read_bytes())['manifest']['files']), set(names))
        plan = fixture.plan()
        self.assertEqual(plan['mcp']['args'][2], str(fixture.package / 'adapters/codex/client_startup.py'))
        self.assertTrue(install.apply(plan, fixture.digest(plan), lambda _: None)['installed'])
        self.assertTrue(install.inspect(fixture.home)['installed'])
        self.assertEqual(fixture.policy_path.read_bytes(), policy_before)
        # Only complete known layouts are valid; mixtures cannot exploit the
        # union of permitted paths at the generic release-engine boundary.
        mixed = copy.deepcopy(manifest)
        mixed['files'].pop('adapters/codex/remote_http.py')
        mixed['files']['scripts/brain_cache.py'] = 'b' * 64
        with self.assertRaises(releases.UpdateBlocked):
            fixture.channel.engine.adapter.preflight(output, mixed)
        fixture.publish('0.8.0')
        self.assertEqual(fixture.channel.update()['status'], 'updated')
        self.assertTrue(install.inspect(fixture.home)['registration_verified'])
        self.assertEqual(fixture.policy_path.read_bytes(), policy_before)
