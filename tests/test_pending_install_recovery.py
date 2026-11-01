"""Exact pre-mutation recovery scope and fail-closed platform contracts."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / n) for n in ('tools', 'scripts', 'adapters/codex')]
import pending_install_recovery as recovery
import brain_lock
import test_windows_native as windows_fixtures


class RecoveryContracts(unittest.TestCase):
    def test_missing_profile_resumes_ordinary_preparation_but_retains_migration_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            state = home / '.config/agent-brain-client/first-install'
            state.mkdir(parents=True)
            (state / 'binding.json').write_bytes(b'{"schema":1}')
            with patch.object(recovery, 'native_platform', return_value='win32-amd64'), patch.object(recovery, 'private_json', side_effect=ValueError('Missing profile preserved')) as read:
                recovery.repair_crlf_preparation(home, home / 'codex')
                read.assert_not_called()
                self.assertFalse((state / 'https-profile.json').exists())
                (state / 'crlf-recovery.json').write_bytes(b'{"schema":1}')
                with self.assertRaisesRegex(ValueError, 'Missing profile preserved'):
                    recovery.repair_crlf_preparation(home, home / 'codex')
                read.assert_called_once_with(state / 'https-profile.json')

    def test_refresh_cannot_change_identity_authority_environment_or_native_preimage(self):
        old = {'package': '/synthetic/old', 'package_sha256': 'a' * 64,
            'mcp': {'args': ['-I', '-B', '/synthetic/old/adapters/codex/client_startup.py'], 'env': {'PATH': '/synthetic/tools'}},
            'config_after_sha256': 'b' * 64, 'replace_existing': False, 'legacy_agents': None,
            'release_policy': '/synthetic/policy', 'release_policy_sha256': 'c' * 64,
            'before': {'/synthetic/config.toml': 'd' * 64}}
        new = copy.deepcopy(old)
        new.update(package='/synthetic/new', package_sha256='e' * 64, config_after_sha256='f' * 64)
        new['mcp']['args'][2] = str(Path(new['package']) / 'adapters/codex/client_startup.py')
        recovery.validate_change(old, new)
        for field, value in (('release_policy_sha256', '0' * 64), ('before', {}), ('replace_existing', True)):
            altered = copy.deepcopy(new)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                recovery.validate_change(old, altered)
        new['mcp']['env']['PYTHONPATH'] = '/synthetic/injection'
        with self.assertRaises(ValueError):
            recovery.validate_change(old, new)

    def test_unknown_architecture_and_non_native_os_still_rejected(self):
        with patch.object(sys, 'platform', 'win32'), patch.object(os, 'name', 'nt'), patch.object(brain_lock, 'windows_machine', return_value='unknown'):
            with self.assertRaises(brain_lock.PlatformUnsupported):
                brain_lock.require_platform('synthetic')
        with patch.object(sys, 'platform', 'linux'), patch.object(brain_lock.platform, 'machine', return_value='amd64'):
            with self.assertRaises(brain_lock.PlatformUnsupported):
                brain_lock.require_platform('synthetic', 'win32-amd64')


@unittest.skipUnless(sys.platform == 'darwin', 'Mac shared transaction regression')
class MacPendingRecovery(unittest.TestCase):
    """Run the same transaction case on Mac; only its eligibility gate is injected."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input = self.test_shared_pre_native_recovery
        self.root.chmod(0o700)
        self.home = brain_lock.private_directory(self.root / 'User Space & 文本')
        eligibility = patch.object(recovery, 'WINDOWS_PLATFORMS', (brain_lock.native_platform(),))
        eligibility.start()
        self.addCleanup(eligibility.stop)

    test_shared_pre_native_recovery = windows_fixtures.WindowsNative.test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input
    test_enrolled_055_three_tool_plan_recovers_without_identity_or_scope_change = windows_fixtures.WindowsNative.test_enrolled_055_three_tool_plan_recovers_without_identity_or_scope_change
    test_fresh_normal_entry_compares_real_runtime_tools_before_and_after_install = windows_fixtures.WindowsNative.test_fresh_normal_entry_compares_real_runtime_tools_before_and_after_install


if __name__ == '__main__':
    unittest.main()
