"""Windows-only hidden access-key dialog; storage stays in Credential Manager.

This generic application credential prompt never requests Windows/admin logon,
never persists through CredUI and has no terminal/plaintext fallback.
"""

import ctypes as C
import re
import sys


class CredUIInfo(C.Structure):
    _fields_ = [
        ("cbSize", C.c_uint32),
        ("hwndParent", C.c_void_p),
        ("pszMessageText", C.c_wchar_p),
        ("pszCaptionText", C.c_wchar_p),
        ("hbmBanner", C.c_void_p),
    ]


def dialog_api():
    if sys.platform != "win32":
        raise ValueError("Native Windows access-key UI required")
    api = C.WinDLL("credui", use_last_error=True).CredUIPromptForCredentialsW
    api.argtypes = [
        C.POINTER(CredUIInfo),
        C.c_wchar_p,
        C.c_void_p,
        C.c_uint32,
        C.c_wchar_p,
        C.c_uint32,
        C.c_wchar_p,
        C.c_uint32,
        C.POINTER(C.c_int32),
        C.c_uint32,
    ]
    api.restype = C.c_uint32
    return api


def secure_input(endpoint, *, prompt=None):
    from https_content import endpoint_check

    endpoint = endpoint_check(endpoint)
    api = prompt or dialog_api()
    info = CredUIInfo(
        cbSize=C.sizeof(CredUIInfo),
        hwndParent=None,
        pszMessageText="Server: "
        + endpoint
        + "\nEnter the Server-generated Agent Brain access key in the password field. This is not your Windows password.",
        pszCaptionText="Agent Brain Server access",
        hbmBanner=None,
    )
    username = C.create_unicode_buffer("Agent Brain access key", 64)
    password = C.create_unicode_buffer(129)
    save = C.c_int32(0)
    # Generic application credentials; fixed label; no OS/domain identity,
    # no credential lookup shortcut, no CredUI persistence or save checkbox.
    flags = 0x40000 | 0x00080 | 0x00002 | 0x100000
    try:
        status = api(
            C.byref(info),
            "Agent Brain access: " + endpoint,
            None,
            0,
            username,
            len(username),
            password,
            len(password),
            C.byref(save),
            flags,
        )
        if status == 1223:
            return None
        if status != 0:
            raise ValueError("Windows access-key UI unavailable")
        key = password.value
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", key):
            raise ValueError("Invalid access key")
        return key
    finally:
        C.memset(C.addressof(password), 0, C.sizeof(password))
        C.memset(C.addressof(username), 0, C.sizeof(username))
