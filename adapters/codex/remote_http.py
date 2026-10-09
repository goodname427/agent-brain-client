#!/usr/bin/env python3
"""Thin JSON-line HTTP adapter. No Core, repository, Git or host discovery."""
import argparse
import json
from pathlib import Path
import re
import sys
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteClient:
    def __init__(self, endpoint, token_file, data_mode='synthetic'):
        if data_mode not in ('synthetic','personal'):raise ValueError('Unsupported data mode')
        self.data_mode=data_mode
        url = urlsplit(endpoint)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('', '/')
                or url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('Use HTTPS, or loopback HTTP through an approved SSH tunnel.')
        self.endpoint, self.token_file = endpoint.rstrip('/'), Path(token_file)
        self.opener = build_opener(NoRedirect())

    def request(self, request, *, timeout=90, deadline=None):
        if deadline is None:return self._request(request,timeout=timeout)
        if not isinstance(request,dict) or request.get('method')!='client_updates':
            raise ValueError('A bounded probe may only discover the read-only policy.')
        # One short-lived daemon worker in this MCP process, never a service/job.
        # A slow HTTP read must not delay initialize; late results cannot change
        # instructions, and no late writes are possible on this read-only path.
        done=threading.Event();result=[]
        def probe():
            try:result.append(self._request(request,timeout=timeout))
            except Exception:result.append(None)
            finally:done.set()
        threading.Thread(target=probe,daemon=True).start()
        if done.wait(deadline) and result and result[0] is not None:return result[0]
        return {'request_id':request.get('request_id'),'error':{'code':'transport_unconfirmed','retryable':True},
                'persistence':{'durable':False}}

    def _request(self, request, *, timeout=90):
        rid = request.get('request_id') if isinstance(request, dict) else None
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', rid):
            raise ValueError('Supply a stable request_id; retry a write with the same ID and parameters.')
        if self.token_file.stat().st_mode & 0o077:
            raise ValueError('Token file must have mode 0600.')
        token = self.token_file.read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
            raise ValueError('Invalid token file')
        data = json.dumps(request, ensure_ascii=False).encode()
        if len(data) > 131072:
            raise ValueError('Request exceeds 128 KB')
        req = Request(self.endpoint + '/v1/requests', data=data, method='POST',
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        req.add_header('X-Agent-Brain-Adapter', 'codex')
        req.add_header('X-Agent-Brain-Protocol', '1')
        req.add_header('X-Agent-Brain-State-Schema', '1')
        if self.data_mode=='personal':req.add_header('X-Agent-Brain-Data-Mode','personal')
        try:
            with self.opener.open(req, timeout=timeout) as response:
                value = response.read(262145)
            if len(value) > 262144:
                raise ValueError('Response exceeds 256 KB')
            result = json.loads(value)
            if not isinstance(result, dict) or result.get('request_id') != rid:
                raise ValueError('Unexpected response ID')
            if request.get('method') in ('remember', 'forget') and 'error' not in result and result.get('persistence', {}).get('durable') is not True:
                raise ValueError('Write persistence was not confirmed')
            return result
        except HTTPError as error:
            # Redirects are never followed with the bearer credential.
            try:
                result = json.loads(error.read(262144))
                if isinstance(result, dict) and result.get('request_id') in (None, rid) and isinstance(result.get('error'), dict):
                    result['request_id'] = rid
                    result['persistence'] = {'durable': False}
                    return result
            except (ValueError, OSError):
                pass
        except (URLError, OSError, ValueError):
            pass
        return {'request_id': rid, 'error': {'code': 'transport_unconfirmed', 'retryable': True},
                'persistence': {'durable': False}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--data-mode',choices=('synthetic','personal'),default='synthetic')
    args = parser.parse_args()
    client = RemoteClient(args.endpoint, args.token_file,args.data_mode)
    for line in sys.stdin:
        try:
            if len(line.encode()) > 131072:
                raise ValueError('Oversized request')
            result = client.request(json.loads(line))
        except Exception:
            result = {'error': {'code': 'invalid_client_request', 'retryable': False}, 'persistence': {'durable': False}}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
