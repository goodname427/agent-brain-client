"""Reserved example endpoints and synthetic material only."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from publication_check import findings, require_publishable


class PublicationCheck(unittest.TestCase):
    def test_reserved_examples_runtime_injection_and_multicast_remain_publishable(self):
        payload = {'example.md': b'https://203.0.113.20\nhttp://127.0.0.1:8765\n239.0.0.1:6766\n/Users/example/config\nendpoint=profile["endpoint"]'}
        self.assertEqual(require_publishable(payload)['file_count'], 1)

    def test_private_host_and_account_paths_are_redacted_in_locations(self):
        secret = 'private-fixture.synthetic.invalid'
        account_path='/Users/'+'review-fixture/config'
        payload = {'example.md': ('endpoint='+secret+'\n'+account_path).encode()}
        result = findings(payload, [secret])
        self.assertEqual({v['category'] for v in result}, {'private_deployment_host', 'personal_account_path'})
        with self.assertRaises(ValueError) as denied:
            require_publishable(payload, [secret])
        self.assertNotIn(secret, str(denied.exception))
        self.assertNotIn('/Users/'+'review-fixture', str(denied.exception))

    def test_embedded_synthetic_private_key_material_is_blocked_without_value_output(self):
        material = b'-----BEGIN OPENSSH PRIVATE KEY-----\n' + b'SYNTHETICFIXTURENEVERDEPLOY'*4 + b'\n-----END OPENSSH PRIVATE KEY-----\n'
        with self.assertRaises(ValueError) as denied:
            require_publishable({'synthetic-fixture.txt': material})
        self.assertIn('embedded_private_key_material', str(denied.exception))
        self.assertNotIn('SYNTHETICFIXTURENEVERDEPLOY', str(denied.exception))


if __name__ == '__main__':
    unittest.main()
