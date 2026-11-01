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
    def test_refresh_cannot_change_identity_authority_environment_or_native_preimage(self):
        old = {'package': '/synthetic/old', 'package_sha256': 'a' * 64,
            'mcp': {'args': ['-I', '-B', '/synthetic/old/adapters/codex/client_startup.py'], 'env': {'PATH': '/synthetic/tools'}},
            'config_after_sha256': 'b' * 64, 'replace_existing': False, 'legacy_agents': None,
            'release_policy': '/synthetic/policy', 'release_policy_sha256': 'c' * 64,
            'before': {'/synthetic/config.toml': 'd' * 64}}
        new = copy.deepcopy(old)
        new.update(package='/synthetic/new', package_sha256='e' * 64, config_after_sha256='f' * 64)
        new['mcp']['args'][2] = '/synthetic/new/adapters/codex/client_startup.py'
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
        self.root.chmod(0o700)
        self.home = brain_lock.private_directory(self.root / 'User Space & 文本')
        eligibility = patch.object(recovery, 'WINDOWS_PLATFORMS', (brain_lock.native_platform(),))
        eligibility.start()
        self.addCleanup(eligibility.stop)

    test_shared_pre_native_recovery = windows_fixtures.WindowsNative.test_enrolled_pre_native_050_recovers_to_verified_release_without_new_input


if __name__ == '__main__':
    unittest.main()
