"""Reject deployment literals in source/public payloads; report locations only.

An optional caller-supplied list of real deployment hosts stays in private memory;
this module contains no personal endpoint and never prints matching values.
"""
import ipaddress
from pathlib import Path
import re

IPV4 = re.compile(rb'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
ACCOUNT_PATH = re.compile(rb'/(?:Users|home)/(?!(?:example|fixture|synthetic|Shared|test)(?:/|\b))[A-Za-z0-9_-]+/')
PRIVATE_KEY = re.compile(rb'-----BEGIN (?:OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY-----[\r\n]+[A-Za-z0-9+/=\r\n]{64,}')


def findings(payload, sensitive_hosts=()):
    """Return file/line/category, never endpoint, path value or credential bytes."""
    result = []
    for name, raw in payload.items():
        if not isinstance(raw, bytes):
            raise ValueError('Publication byte payload required')
        found = set()
        for match in IPV4.finditer(raw):
            try:
                address = ipaddress.ip_address(match.group().decode('ascii'))
            except ValueError:
                continue
            if address.is_global and not address.is_multicast:
                found.add((raw.count(b'\n', 0, match.start())+1, 'public_ip_literal'))
        for expression, category in ((ACCOUNT_PATH, 'personal_account_path'), (PRIVATE_KEY, 'embedded_private_key_material')):
            for match in expression.finditer(raw):
                found.add((raw.count(b'\n', 0, match.start())+1, category))
        for host in sensitive_hosts:
            if not isinstance(host, str) or not host:
                raise ValueError('Explicit private deployment host required')
            needle = host.encode('utf-8');offset = raw.find(needle)
            if offset >= 0:
                found.add((raw.count(b'\n', 0, offset)+1, 'private_deployment_host'))
        result.extend({'path': str(name), 'line': line, 'category': category} for line, category in sorted(found))
    return result


def require_publishable(payload, sensitive_hosts=()):
    found = findings(payload, sensitive_hosts)
    if found:
        # No matching value, body, binary snippet or raw exception in errors.
        locations = ', '.join(f"{v['path']}:{v['line']} ({v['category']})" for v in found)
        raise ValueError('Private deployment material requires forward cleanup: ' + locations)
    return {'status': 'checked', 'file_count': len(payload), 'matching_values_output': False}
