"""Offline subprocess bridge for explicit Server integration; no host identity."""
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ('src', 'src', 'src')]
import agent_brain_client.runtime.collection as gm
import agent_brain_client.adapters.server.https as https_content


def main():
    config = json.loads(sys.stdin.readline())
    wire_input = sys.stdin
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return wire_input.readline().encode()
    class Opener:
        def open(self, request, **kwargs):
            print(json.dumps({'url': request.full_url, 'headers': dict(request.header_items()),
                              'request': json.loads(request.data)}), flush=True)
            return Response()
    # Only credential storage and the network are replaced. CollectionClient builds
    # the actual authenticated HTTP frame and validates the actual Server reply.
    base = SimpleNamespace(grant={'actor': config['actor']}, data_mode='personal',
                           device_id=config['device_id'], profile_sha='a' * 64,
                           profile={'endpoint': 'https://synthetic.invalid'}, queue_identity={},
                           _credential=lambda: config['token'], transport=SimpleNamespace(opener=Opener()))
    with tempfile.TemporaryDirectory() as directory:
        directory = str(Path(directory).resolve())
        if os.name == 'nt':
            from agent_brain_client.adapters.platforms.windows.native import protect_new
            protect_new(Path(directory), directory=True)
        value = dict(config['value'], device_home=directory)
        payload = {'cwd': config['origin']['cwd'], 'hook_event_name': 'UserPromptSubmit',
                   'session_id': 'synthetic-independent-reader', 'turn_id': 'synthetic-turn', 'prompt': 'Synthetic'}
        registered = {'runtime': {'https_profile': 'synthetic-profile', 'https_profile_sha256': 'a' * 64}}
        observed = []
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')), patch.object(https_content, 'HTTPSContentClient', return_value=base), patch.object(gm.old, 'registration', return_value=(None, registered)), patch.object(gm, 'eligible', return_value=config['origin']), patch.object(gm, 'submit_hook', return_value={'status': 'staged'}), patch.object(gm, 'settings_path', return_value=Path(directory)/'settings.json'), patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode()))):
            result = gm.hook(value, observe=observed.append)
        context = result.get('hookSpecificOutput', {}).get('additionalContext', '')
        receipt = observed[0]
        print(json.dumps({'status': 'synthetic-hook-context-verified', 'observation': receipt,
                          'L2_marker_present': 'Synthetic L2 continuity marker' in context,
                          'L3_marker_present': 'Synthetic L3 preference marker' in context,
                          'model_invoked': False}), flush=True)


if __name__ == '__main__': main()
