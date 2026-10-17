#!/usr/bin/env python3
"""Thin JSON-line HTTP adapter. No Core, repository, Git or host discovery."""
import argparse
from contextlib import contextmanager
import http.client
import json
import math
from pathlib import Path
import re
import sys
import threading
import time
import socket
import subprocess
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


class RequestStopped(Exception):
    """Safe cancellation/deadline signal; never includes transport or secrets."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


_request_scope = threading.local()
_resolver_slots = threading.BoundedSemaphore(8)


class RequestControl:
    """One MCP request, including queue time; abort only its owned read I/O."""
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self.lock = threading.Lock()
        self.reason = None
        self.finished = False
        self.aborters = []
        self.timer = threading.Timer(timeout, self.cancel, args=('request_deadline_exceeded',))
        self.timer.daemon = True
        self.timer.start()

    def cancel(self, reason='request_cancelled'):
        with self.lock:
            if self.finished or self.reason is not None:
                return
            self.reason = reason
            aborters = list(self.aborters)
        for abort in aborters:
            try: abort()
            except Exception: pass

    def check(self):
        if time.monotonic() >= self.deadline:
            self.cancel('request_deadline_exceeded')
        with self.lock:
            reason = self.reason
        if reason is not None:
            raise RequestStopped(reason)

    def register(self, abort):
        with self.lock:
            stopped = self.reason is not None
            if not stopped and not self.finished:
                self.aborters.append(abort)
        if stopped:
            try: abort()
            except Exception: pass
            self.check()

    def close(self, *, check=False):
        self.timer.cancel()
        with self.lock:
            if check:
                reason=self.reason
                if reason is None and time.monotonic()>=self.deadline:
                    reason='request_deadline_exceeded'
                if reason is not None:raise RequestStopped(reason)
            self.finished = True
            self.aborters.clear()

    @contextmanager
    def activate(self):
        previous = getattr(_request_scope, 'control', None)
        _request_scope.control = self
        try:
            self.check()
            yield
            self.check()
        finally:
            _request_scope.control = previous


def current_request():
    return getattr(_request_scope, 'control', None)


def check_request():
    control = current_request()
    if control is not None:
        control.check()


def request_timeout(timeout):
    control = current_request()
    if control is None:
        return timeout
    control.check()
    return max(.001, min(timeout, control.deadline - time.monotonic()))


def controlled_run(runner, command, **kwargs):
    """Read-only helper invocation; unchanged runner for pairing and writes."""
    control = current_request()
    if control is None or runner is not subprocess.run:
        return runner(command, **kwargs)
    control.check()
    options = dict(kwargs)
    data = options.pop('input', None)
    timeout = request_timeout(options.pop('timeout'))
    options.pop('capture_output')
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **options) as process:
        control.register(lambda: process.kill() if process.poll() is None else None)
        try:
            stdout, stderr = process.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            control.cancel('request_deadline_exceeded')
            process.kill()
            process.communicate()
            control.check()
            raise
        control.check()
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _watch_socket(sock):
    control = current_request()
    if control is None:
        return
    def abort():
        if sock is not None:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            sock.close()
    control.register(abort)
    control.check()


def _read_socket(address, timeout=None, source_address=None):
    """Register the owned socket before connect, not only after HTTP headers."""
    check_request()
    candidates = _resolve_address(address)
    check_request()
    error = None
    for family, kind, protocol, _, destination in candidates:
        sock = socket.socket(family, kind, protocol)
        try:
            sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            _watch_socket(sock)
            sock.connect(destination)
            check_request()
            return sock
        except OSError as exc:
            sock.close()
            check_request()
            error = exc
        except BaseException:
            sock.close()
            raise
    if error is not None:
        raise error
    raise OSError('No connection address')


def _resolve_address(address):
    control=current_request()
    if control is None:return socket.getaddrinfo(address[0],address[1],type=socket.SOCK_STREAM)
    control.check()
    if not _resolver_slots.acquire(blocking=False):raise OSError('Name resolver busy')
    done=threading.Event();result=[];errors=[]
    def resolve():
        try:result.append(socket.getaddrinfo(address[0],address[1],type=socket.SOCK_STREAM))
        except Exception as error:errors.append(error)
        finally:
            _resolver_slots.release();done.set()
    # OS DNS is not interruptible. Only a bounded, credential-free lookup may
    # finish late; it cannot send HTTP, touch the adapter or publish its result.
    threading.Thread(target=resolve,name='content-read-dns',daemon=True).start()
    control.register(done.set)
    if not done.wait(request_timeout(90)):control.cancel('request_deadline_exceeded')
    control.check()
    if errors:raise errors[0]
    return result[0]


class ReadHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _read_socket

    def connect(self):
        check_request()
        super().connect()


class ReadHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _read_socket

    def connect(self):
        check_request()
        http.client.HTTPConnection.connect(self)
        server_hostname = getattr(self,'_tunnel_host',None) or self.host
        self.sock = getattr(self,'_context').wrap_socket(self.sock, server_hostname=server_hostname,
                                              do_handshake_on_connect=False)
        _watch_socket(self.sock)
        self.sock.do_handshake()
        check_request()


class ReadHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(ReadHTTPConnection, req)


class ReadHTTPSHandler(HTTPSHandler):
    def __init__(self, debuglevel=0, context=None):
        super().__init__(debuglevel,context=context)
        self.read_context=context

    def https_open(self, req):
        return self.do_open(ReadHTTPSConnection, req, context=self.read_context)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteClient:
    def __init__(self, endpoint, token_file=None, data_mode='synthetic', *, credential_provider=None, write_receipt=None):
        if data_mode not in ('synthetic','personal'):raise ValueError('Unsupported data mode')
        if write_receipt not in (None,'local-durable-v1'):raise ValueError('Unsupported write receipt')
        self.write_receipt=write_receipt
        self.data_mode=data_mode
        url = urlsplit(endpoint)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ('', '/')
                or url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('Use HTTPS, or loopback HTTP through an approved SSH tunnel.')
        if (token_file is None)==(credential_provider is None):raise ValueError('One internal credential source required')
        if credential_provider is not None and url.scheme!='https':raise ValueError('Device credentials require HTTPS')
        self.endpoint, self.token_file = endpoint.rstrip('/'), Path(token_file) if token_file is not None else None
        self.credential_provider=credential_provider
        self.opener = build_opener(ProxyHandler({}),NoRedirect(),ReadHTTPHandler(),ReadHTTPSHandler())

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
        check_request()
        rid = request.get('request_id') if isinstance(request, dict) else None
        if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,128}', rid):
            raise ValueError('Supply a stable request_id; retry a write with the same ID and parameters.')
        token = self._token()
        check_request()
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', token):
            raise ValueError('Invalid credential source')
        data = json.dumps(request, ensure_ascii=False).encode()
        if len(data) > 131072:
            raise ValueError('Request exceeds 128 KB')
        req = Request(self.endpoint + '/v1/requests', data=data, method='POST',
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        req.add_header('X-Agent-Brain-Adapter', 'codex')
        req.add_header('X-Agent-Brain-Protocol', '1')
        req.add_header('X-Agent-Brain-State-Schema', '1')
        if self.data_mode=='personal':req.add_header('X-Agent-Brain-Data-Mode','personal')
        if self.write_receipt is not None and request.get('method') in ('remember','forget','content_put','content_remove'):
            req.add_header('X-Agent-Brain-Write-Receipt',self.write_receipt)
        write=request.get('method') in ('remember','forget','content_put','content_remove','memory_event_submit')
        core=(request.get('method')=='memory_event_submit' or request.get('method')=='memory_query'
              and isinstance(request.get('params'),dict) and 'context' in request['params'])
        def response_json(raw):
            if not core:return json.loads(raw)
            def unique(items):
                value={}
                for key,item in items:
                    if key in value:raise ValueError('Duplicate Core response field')
                    value[key]=item
                return value
            def reject_constant(value):raise ValueError('Nonfinite Core response number')
            def finite_float(value):
                number=float(value)
                if not math.isfinite(number):raise ValueError('Nonfinite Core response number')
                return number
            return json.loads(raw,object_pairs_hook=unique,parse_constant=reject_constant,parse_float=finite_float)
        def failed(code,stage,retryable=False,**metadata):
            return {'request_id':rid,'error':{'code':code,'retryable':retryable,'stage':stage,**metadata},'persistence':{'durable':False}}
        try:
            with self.opener.open(req, timeout=request_timeout(timeout)) as response:
                value = response.read(262145)
                status = getattr(response, 'status', None)
            check_request()
            if len(value) > 262144:
                return failed('response_too_large','response-parse',write)
            try:result = response_json(value)
            except (ValueError,UnicodeError):return failed('invalid_response_json','response-parse',write)
            if not isinstance(result, dict):return failed('invalid_response_shape','response-validation',write)
            if result.get('request_id') != rid:return failed('response_id_mismatch','response-validation',write)
            if 'error' in result:
                error=result['error']
                if not isinstance(error,dict) or not isinstance(error.get('code'),str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',error['code']) or type(error.get('retryable')) is not bool:
                    return failed('invalid_response_error','response-validation',write)
                metadata={'http_status':status} if type(status) is int else {}
                return failed(error['code'],'http',error['retryable'],**metadata)
            if write and 'error' not in result:
                if request.get('method')=='memory_event_submit':
                    from content_contract import core_receipt
                    return result if core_receipt(result,request) else failed('invalid_write_receipt','response-validation',True)
                receipt=result.get('persistence',{})
                local=(self.write_receipt=='local-durable-v1' and isinstance(receipt,dict)
                       and receipt.get('state')=='local-confirmed' and receipt.get('durable') is False
                       and receipt.get('local_durable') is True and receipt.get('github_confirmed') is False
                       and isinstance(receipt.get('commit'),str) and re.fullmatch(r'[a-f0-9]{40}',receipt['commit'])
                       and isinstance(result.get('result'),dict))
                if not isinstance(receipt,dict) or receipt.get('durable') is not True and not local:
                    return failed('invalid_write_receipt','response-validation',True)
            return result
        except HTTPError as error:
            # Redirects are never followed with the bearer credential.
            status=error.code
            try:
                raw=error.read(262145 if core else 262144)
                if core and len(raw)>262144:return failed('response_too_large','response-parse',write,http_status=status)
                try:result=response_json(raw)
                except (ValueError,UnicodeError):
                    if core:return failed('invalid_response_json','response-parse',write,http_status=status)
                    raise
                if isinstance(result, dict) and result.get('request_id') in (None, rid) and isinstance(result.get('error'), dict):
                    error=result['error'];code=error.get('code')
                    if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',code) and type(error.get('retryable'))==bool:
                        return failed(code,'http',error['retryable'],http_status=status)
            except (ValueError, OSError, http.client.HTTPException):
                pass
            check_request()
            return failed('http_response_unconfirmed','http',status>=500 or status in (408,429),http_status=status)
        except (URLError, OSError, http.client.HTTPException) as error:
            check_request()
            reason=getattr(error,'reason',error)
            kind='timeout' if isinstance(reason,TimeoutError) else 'tls' if isinstance(reason,ssl.SSLError) else 'http-framing' if isinstance(reason,http.client.HTTPException) else 'connection'
            return failed('transport_unconfirmed','transport',True,failure_kind=kind)

    def _token(self):
        if self.credential_provider is not None:return self.credential_provider()
        assert self.token_file is not None  # Constructor requires one credential source.
        if self.token_file.stat().st_mode & 0o077:
            raise ValueError('Token file must have mode 0600.')
        return self.token_file.read_text(encoding='utf-8').strip()


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
