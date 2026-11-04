"""Windows OS boundary. No shell, administrator elevation or plaintext secrets.

The same-user/administrator threat boundary matches the macOS Keychain backend.
Only newly created Agent Brain files receive a DACL; existing files are checked.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re


def libraries():
    if os.name != 'nt':
        raise ValueError('Windows native API required')
    api, kernel = C.WinDLL('advapi32', use_last_error=True), C.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    return api, kernel


def sid_text(pointer):
    api, kernel = libraries(); text = W.LPWSTR()
    api.ConvertSidToStringSidW.argtypes = [C.c_void_p, C.POINTER(W.LPWSTR)]
    kernel.LocalFree.argtypes = [C.c_void_p]; kernel.LocalFree.restype = C.c_void_p
    if not api.ConvertSidToStringSidW(pointer, C.byref(text)):
        raise ValueError('Windows identity unavailable')
    try: return text.value
    finally: kernel.LocalFree(C.cast(text, C.c_void_p))


def user_sid():
    api, kernel = libraries(); token = W.HANDLE(); size = W.DWORD()
    kernel.GetCurrentProcess.restype = W.HANDLE
    api.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]
    api.GetTokenInformation.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 8, C.byref(token)):
        raise ValueError('Windows identity unavailable')
    try:
        api.GetTokenInformation(token, 1, None, 0, C.byref(size))
        raw = C.create_string_buffer(size.value)
        if not api.GetTokenInformation(token, 1, raw, size, C.byref(size)):
            raise ValueError('Windows identity unavailable')
        return sid_text(C.cast(raw, C.POINTER(C.c_void_p))[0])
    finally: kernel.CloseHandle(token)


def canonical(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or not re.match(r'^[A-Za-z]:[\\/]', str(path)) or any(':' in p for p in path.parts[1:]):
        raise ValueError('Canonical local path required')
    for item in (path, *path.parents):
        try: info = item.lstat()
        except FileNotFoundError: continue
        if getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Reparse points are not supported for private storage')
    return path


def protect_new(path, *, directory=False):
    """Apply only at creation, before writing body or credentials."""
    path = canonical(path); api, kernel = libraries(); descriptor = C.c_void_p()
    inheritance = 'OICI' if directory else ''
    sddl = 'O:' + user_sid() + 'D:P' + ''.join('(A;' + inheritance + ';FA;;;' + s + ')' for s in (user_sid(), 'SY', 'BA'))
    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p]
    api.SetFileSecurityW.argtypes = [W.LPCWSTR, W.DWORD, C.c_void_p]
    if not api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, C.byref(descriptor), None):
        raise ValueError('Private Windows DACL unavailable')
    try:
        if not api.SetFileSecurityW(str(path), 5 | 0x80000000, descriptor):
            raise ValueError('Private Windows DACL unavailable')
    finally: kernel.LocalFree(descriptor)
    private(path, directory=directory)


def protect_descriptor(fd):
    """Set the explicit owner/DACL of a newly created, still-empty file."""
    import msvcrt
    _, kernel = libraries()
    kernel.GetFinalPathNameByHandleW.argtypes = [W.HANDLE, W.LPWSTR, W.DWORD, W.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = W.DWORD
    buffer = C.create_unicode_buffer(32768)
    length = kernel.GetFinalPathNameByHandleW(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
    if not 0 < length < len(buffer): raise ValueError('Private file handle unavailable')
    path = buffer.value
    if path.startswith('\\\\?\\'): path = path[4:]
    protect_new(path)


def private(path, *, directory=False, owner=False, blocked=ValueError):
    path = canonical(path); api, kernel = libraries()
    if not (path.is_dir() if directory else path.is_file()) or not directory and path.stat().st_nlink != 1:
        raise blocked('Owned private Windows item required')
    descriptor = C.c_void_p(); owner_sid = C.c_void_p(); acl = C.c_void_p()
    api.GetNamedSecurityInfoW.argtypes = [W.LPWSTR, C.c_int, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p, C.POINTER(C.c_void_p)]
    api.GetNamedSecurityInfoW.restype = W.DWORD
    api.GetAce.argtypes = [C.c_void_p, W.DWORD, C.POINTER(C.c_void_p)]
    if api.GetNamedSecurityInfoW(str(path), 1, 5, C.byref(owner_sid), None, C.byref(acl), None, C.byref(descriptor)):
        raise blocked('Windows DACL could not be checked')
    try:
        current = user_sid()
        if not acl or not owner_sid or owner and sid_text(owner_sid) != current:
            raise blocked('Owned private Windows item required')
        # ACL header: revision(1), reserved(1), size(2), AceCount(2).
        count = C.c_ushort.from_address(acl.value + 4).value
        if count > 64: raise blocked('Bounded Windows DACL required')
        for i in range(count):
            ace = C.c_void_p()
            if not api.GetAce(acl, i, C.byref(ace)): raise blocked('Windows DACL unavailable')
            kind = C.c_ubyte.from_address(ace.value).value
            flags = C.c_ubyte.from_address(ace.value + 1).value
            if flags & 8: continue  # INHERIT_ONLY does not grant access to this item.
            if kind == 1: continue  # DENY cannot disclose content.
            if kind != 0 or sid_text(ace.value + 8) not in (current, 'S-1-5-18', 'S-1-5-32-544'):
                raise blocked('Windows item accessible to another principal')
    finally: kernel.LocalFree(descriptor)
    return path


class Credential(C.Structure):
    _fields_ = [('Flags', W.DWORD), ('Type', W.DWORD), ('TargetName', W.LPWSTR),
                ('Comment', W.LPWSTR), ('LastWritten', W.FILETIME), ('CredentialBlobSize', W.DWORD),
                ('CredentialBlob', C.POINTER(C.c_ubyte)), ('Persist', W.DWORD), ('AttributeCount', W.DWORD),
                ('Attributes', C.c_void_p), ('TargetAlias', W.LPWSTR), ('UserName', W.LPWSTR)]


class Overlapped(C.Structure):
    _fields_ = [('Internal', C.c_size_t), ('InternalHigh', C.c_size_t),
                ('Offset', W.DWORD), ('OffsetHigh', W.DWORD), ('hEvent', W.HANDLE)]


def file_lock(file, shared=False, release=False):
    import msvcrt
    _, api = libraries(); state = Overlapped(); handle = msvcrt.get_osfhandle(file.fileno())
    if release:
        api.UnlockFileEx.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.POINTER(Overlapped)]
        success = api.UnlockFileEx(handle, 0, 1, 0, C.byref(state))
    else:
        api.LockFileEx.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, W.DWORD, C.POINTER(Overlapped)]
        success = api.LockFileEx(handle, 1 | (0 if shared else 2), 0, 1, 0, C.byref(state))
    if not success: raise OSError(C.get_last_error(), 'Windows runtime lease unavailable')


class WindowsCredentials:
    def __init__(self, service):
        if not re.fullmatch(r'agent-brain\.https\.[a-f0-9]{64}', service):
            raise ValueError('Dedicated credential item required')
        self.service = service

    def load(self):
        api, _ = libraries(); pointer = C.POINTER(Credential)()
        api.CredReadW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.POINTER(C.POINTER(Credential))]
        api.CredFree.argtypes = [C.c_void_p]
        if not api.CredReadW(self.service, 1, 0, C.byref(pointer)):
            if C.get_last_error() == 1168: return None
            raise ValueError('Windows credential unavailable')
        try:
            item = pointer.contents
            if not 0 < item.CredentialBlobSize <= 2560 or not item.CredentialBlob or item.Type != 1 or item.TargetName != self.service:
                raise ValueError('Unknown credential record')
            raw = C.string_at(item.CredentialBlob, item.CredentialBlobSize)
            from system_credentials import unique
            value = json.loads(raw, object_pairs_hook=unique)
            if not isinstance(value, dict): raise ValueError('Unknown credential record')
            return value
        finally:
            if pointer.contents.CredentialBlob and 0 < pointer.contents.CredentialBlobSize <= 2560:
                C.memset(pointer.contents.CredentialBlob, 0, pointer.contents.CredentialBlobSize)
            api.CredFree(pointer)

    def save(self, record, expected):
        if self.load() != expected: raise ValueError('Credential concurrently changed')
        raw = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > 2560: raise ValueError('Credential record exceeds limit')
        buffer = (C.c_ubyte * len(raw)).from_buffer_copy(raw)
        item = Credential(Type=1, TargetName=self.service, CredentialBlobSize=len(raw), CredentialBlob=buffer, Persist=2, UserName='Agent Brain')
        api, _ = libraries(); api.CredWriteW.argtypes = [C.POINTER(Credential), W.DWORD]
        try:
            if not api.CredWriteW(C.byref(item), 0): raise ValueError('Windows credential write unconfirmed')
        finally: C.memset(buffer, 0, len(buffer))

    def clear(self, expected):
        if self.load() != expected: raise ValueError('Credential concurrently changed')
        api, _ = libraries(); api.CredDeleteW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD]
        if not api.CredDeleteW(self.service, 1, 0): raise ValueError('Credential removal unconfirmed')

    def secure_input(self):
        # getpass uses the Windows console, not stdin/argv/environment or logs.
        import getpass
        if not __import__('sys').stdin.isatty(): raise ValueError('Interactive Windows console required for invitation')
        value = getpass.getpass('Agent Brain device invitation (hidden; Enter cancels): ')
        if not value: return None
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', value): raise ValueError('Invalid invitation')
        return value

    def confirm_reset(self, reason, device_id):
        import sys
        if not sys.stdin.isatty(): return False
        return input('Clear only the rejected/revoked Agent Brain credential? Type CLEAR: ') == 'CLEAR'
