"""Codex stdio MCP bridge to the existing HTTP client; no Core or native writes."""
import argparse
import json
from pathlib import Path
import platform
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from remote_http import RemoteClient

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26')
MAX_MESSAGE = 131072
WRITES = {'remember', 'forget'}
FIELDS = {
    'topic': ('id',), 'recall': ('query', 'limit'),
    'remember': ('id', 'title', 'description', 'body', 'expected_revision', 'type'),
    'forget': ('id', 'expected_revision'), 'client_updates': ('installed_version',),
}
REQUIRED = {
    'topic': ('id',), 'recall': ('query',),
    'remember': ('request_id', 'id', 'title', 'description', 'body', 'expected_revision'),
    'forget': ('request_id', 'id', 'expected_revision'), 'client_updates': (),
}
DESCRIPTIONS = {
    'topic': 'Read one relevant synthetic personal topic and its revision.',
    'recall': 'Find up to three relevant synthetic personal topics before reading or revising them.',
    'remember': 'Write a synthetic topic with the read revision (missing for new). Keep request_id and parameters unchanged on retry; success requires durable=true.',
    'forget': 'Forget a synthetic topic only when requested, using its read revision and a stable request_id.',
    'client_updates': 'Discover the approved Codex client release channel. not-configured does not authorize installation.',
}


DAILY_INSTRUCTIONS = ('Daily maintenance of non-sensitive personal shared memory is authorized. '
    'Read at most three relevant topics when useful for the current task. Before saving stable personal preferences, collaboration habits or personal facts, '
    'recall and read the matching topic, merge corrections into that topic, preserve unrelated valid facts, and use its revision for a CAS update. '
    'Create a focused topic only when none matches; do not append conversation logs or duplicate each event as a new topic. '
    'Use stable request_id and unchanged parameters for retries; only durable=true confirms persistence. '
    'Deletion is disabled. Do not collect raw conversations, native history/databases, project content, sensitive/private/local-only material or credentials. '
    'Do not read or copy custom approval rules. Use this server rather than old local Core/direct Git writers. '
    'Native memory remains available under its existing authorization.')


class ProtocolError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def keys(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ProtocolError(-32602, 'Invalid parameters; preserve existing data.')


def tool(name,data_mode='synthetic',daily=False):
    properties = {field: {'type': 'string'} for field in (*FIELDS[name], 'request_id')}
    properties['request_id']['pattern'] = '^[A-Za-z0-9_-]{16,128}$'
    if name == 'recall':
        properties['limit'] = {'type': 'integer', 'minimum': 1, 'maximum': 3}
    if name == 'remember':
        properties['type']['enum'] = ['user', 'feedback', 'reference']
    description=DESCRIPTIONS[name] if data_mode=='synthetic' else DESCRIPTIONS[name].replace('synthetic','explicitly authorized personal')
    if daily and name=='forget':description='Deletion is disabled for daily personal memory maintenance.'
    if daily and name=='remember':description='Maintain a matching non-sensitive personal shared topic with its read revision; do not create a conversation log or duplicate event topics. Keep request_id on retries; only durable=true confirms persistence.'
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': list(REQUIRED[name]), 'additionalProperties': False},
            'annotations': {'readOnlyHint': name not in WRITES, 'destructiveHint': name in WRITES,
                            'idempotentHint': True, 'openWorldHint': True}}


class Server:
    def __init__(self, client, client_version='0.1.0', client_platform=None, data_mode='synthetic'):
        if data_mode not in ('synthetic','personal'):raise ValueError('Unsupported data mode')
        self.data_mode=data_mode;self.daily=False
        if not re.fullmatch(r'\d+\.\d+\.\d+',client_version):raise ValueError('Invalid client version')
        self.client, self.state, self.client_version = client, 'new', client_version
        self.client_platform=client_platform or sys.platform+'-'+platform.machine().lower()
        if not re.fullmatch(r'(darwin|linux)-(arm64|aarch64|x86_64)',self.client_platform):raise ValueError('Unsupported client checkpoint')

    def invoke(self, name, arguments):
        if name not in FIELDS:
            raise ProtocolError(-32602, 'Unknown tool.')
        keys(arguments, (*FIELDS[name], 'request_id'), REQUIRED[name])
        params = dict(arguments)
        rid = params.pop('request_id', uuid.uuid4().hex)
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', rid):
            raise ProtocolError(-32602, 'Supply a stable 16-128 character request_id.')
        for field, value in params.items():
            if field == 'limit':
                if type(value) is not int or not 1 <= value <= 3:
                    raise ProtocolError(-32602, 'Recall limit must be 1 to 3.')
            elif not isinstance(value, str) or len(value.encode()) > 65536:
                raise ProtocolError(-32602, 'Invalid field type or size.')
        if params.get('type', 'feedback') not in ('user', 'feedback', 'reference'):
            raise ProtocolError(-32602, 'Invalid topic type.')
        if name == 'recall':
            params.setdefault('limit', 3)
        if name == 'client_updates':
            params.update(adapter='codex', platform=self.client_platform)
            params.setdefault('installed_version', self.client_version)
        try:
            result = self.client.request({'request_id': rid, 'method': name, 'params': params})
        except Exception:
            result = {'request_id': rid, 'error': {'code': 'client_credentials_or_transport_unavailable',
                      'retryable': True}, 'persistence': {'durable': False},
                      'next_action': 'Inspect the approved tunnel and private token file; never send secrets to chat.'}
        return {'isError': 'error' in result, 'structuredContent': result,
                'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}

    def handle(self, request):
        if not isinstance(request, dict):
            raise ProtocolError(-32600, 'Invalid JSON-RPC request.')
        keys(request, ('jsonrpc', 'id', 'method', 'params'), ('jsonrpc', 'method'))
        if request['jsonrpc'] != '2.0' or not isinstance(request['method'], str):
            raise ProtocolError(-32600, 'Invalid JSON-RPC request.')
        if 'id' in request and type(request['id']) not in (str, int):
            raise ProtocolError(-32600, 'Invalid JSON-RPC ID.')
        name, params = request['method'], request.get('params', {})
        if isinstance(params, dict) and '_meta' in params:
            if not isinstance(params['_meta'], dict):
                raise ProtocolError(-32602, 'Invalid MCP metadata.')
            params = {key: value for key, value in params.items() if key != '_meta'}
        if 'id' not in request:
            if name == 'notifications/initialized' and self.state == 'negotiated':
                self.state = 'ready'
            return None
        if name == 'ping':
            return {}
        if name == 'initialize':
            if self.state != 'new':
                raise ProtocolError(-32600, 'Already initialized.')
            keys(params, ('protocolVersion', 'capabilities', 'clientInfo'), ('protocolVersion', 'capabilities', 'clientInfo'))
            if not isinstance(params['protocolVersion'], str) or not isinstance(params['capabilities'], dict):
                raise ProtocolError(-32602, 'Invalid initialization.')
            keys(params['clientInfo'], ('name', 'version', 'title', 'description', 'icons', 'websiteUrl'), ('name', 'version'))
            if not all(isinstance(params['clientInfo'][field], str) for field in ('name', 'version')):
                raise ProtocolError(-32602, 'Invalid client metadata.')
            if self.data_mode=='personal' and self.client is not None:
                try:
                    result=self.client.request({'request_id':uuid.uuid4().hex,'method':'client_updates',
                        'params':{'adapter':'codex','platform':self.client_platform,'installed_version':self.client_version}})
                    policy=result.get('result',{}).get('memory_policy',{}) if 'error' not in result else {}
                    self.daily=policy=={'write_mode':'daily-personal','scope':'personal-shared','allowed_write_methods':['remember'],
                                       'proactive_maintenance':True,'raw_history_collection':False}
                except Exception:self.daily=False
            self.state = 'negotiated'
            return {'protocolVersion': params['protocolVersion'] if params['protocolVersion'] in VERSIONS else VERSIONS[0],
                    'serverInfo': {'name': 'agent-brain-remote-codex', 'version': self.client_version},
                    'capabilities': {'tools': {}},
                    'instructions': DAILY_INSTRUCTIONS if self.daily else ('Synthetic personal data only. ' if self.data_mode=='synthetic' else 'Explicitly authorized personal shared memory only. No automatic collection, native history, private/local-only or project data. ')+'Use these tools instead of local Core commands. '
                                    'Read relevant topics before CAS changes; retain write request_id and parameters on retries. '
                                    'Only durable=true confirms GitHub persistence. Report missing credentials, tunnel or release channel; '
                                    'do not read private memory, native databases or credentials, or migrate real memories.'}
        if self.state != 'ready':
            raise ProtocolError(-32002, 'Complete MCP initialization first.')
        if name == 'tools/list':
            keys(params, ('cursor',))
            if params.get('cursor') not in (None, ''):
                raise ProtocolError(-32602, 'Unknown cursor.')
            return {'tools': [tool(name,self.data_mode,self.daily) for name in FIELDS]}
        if name == 'tools/call':
            keys(params, ('name', 'arguments'), ('name',))
            if not isinstance(params['name'], str):
                raise ProtocolError(-32602, 'Invalid tool name.')
            return self.invoke(params['name'], params.get('arguments', {}))
        raise ProtocolError(-32601, 'Unknown MCP method.')


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--client-version', default='0.1.0')
    parser.add_argument('--client-platform',help='Logical approved Harness checkpoint supplied by the fixed startup policy.')
    parser.add_argument('--data-mode',choices=('synthetic','personal'),default='synthetic')
    args = parser.parse_args()
    server = Server(RemoteClient(args.endpoint, args.token_file,args.data_mode), args.client_version,args.client_platform,args.data_mode)
    while True:
        raw = sys.stdin.buffer.readline(MAX_MESSAGE + 1)
        if not raw:
            break
        response_id, request = None, None
        try:
            if len(raw) > MAX_MESSAGE:
                raise ProtocolError(-32600, 'Message exceeds 128 KB; connection closed.')
            request = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
            if isinstance(request, dict) and type(request.get('id')) in (str, int):
                response_id = request['id']
            value = server.handle(request)
            if value is None:
                continue
            response = {'jsonrpc': '2.0', 'id': response_id, 'result': value}
        except ProtocolError as error:
            if isinstance(request, dict) and 'id' not in request:
                continue
            response = {'jsonrpc': '2.0', 'id': response_id, 'error': {'code': error.code, 'message': error.message}}
        except Exception:
            response = {'jsonrpc': '2.0', 'id': response_id, 'error': {'code': -32700, 'message': 'Invalid JSON message.'}}
        print(json.dumps(response, ensure_ascii=False), flush=True)
        if len(raw) > MAX_MESSAGE:
            break


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('MCP startup blocked; inspect approved endpoint and token path without exposing credentials.', file=sys.stderr)
        sys.exit(1)
