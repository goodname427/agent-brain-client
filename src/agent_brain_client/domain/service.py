"""Credential-free HTTPS service origin syntax; no transport or I/O."""

from urllib.parse import urlsplit


def endpoint_check(endpoint):
    if not isinstance(endpoint, str):
        raise ValueError("HTTPS origin required")
    value = urlsplit(endpoint)
    if (
        value.scheme != "https"
        or not value.hostname
        or value.username
        or value.password
        or value.query
        or value.fragment
        or value.path not in ("", "/")
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in endpoint)
    ):
        raise ValueError("Credential-free HTTPS origin required")
    return endpoint.rstrip("/")
