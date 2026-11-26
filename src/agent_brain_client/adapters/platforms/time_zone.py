"""Read the current system time zone without consulting process TZ overrides."""

from pathlib import Path
import re
import sys


def _mac_zone():
    localtime = Path("/etc/localtime")
    if not localtime.is_symlink():
        return None
    target = localtime.resolve(strict=True)
    for directory in ("/var/db/timezone/zoneinfo", "/usr/share/zoneinfo"):
        root = Path(directory).resolve()
        if target.is_relative_to(root):
            with target.open("rb") as zone:
                if zone.read(4) == b"TZif":
                    return target.relative_to(root).as_posix()
    return None


def _windows_zone():
    import ctypes
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation",
    ) as settings:
        key, _ = winreg.QueryValueEx(settings, "TimeZoneKeyName")
        try:
            daylight_disabled, _ = winreg.QueryValueEx(settings, "DynamicDaylightTimeDisabled")
        except FileNotFoundError:
            daylight_disabled = 0
    if not isinstance(key, str) or not key or daylight_disabled != 0:
        return None

    # Windows ICU supplies the canonical IANA mapping for the active Windows key.
    # Search System32 only, so the client never loads ICU from a workspace.
    convert = ctypes.WinDLL("icu.dll", winmode=0x00000800).ucal_getTimeZoneIDForWindowsID
    convert.argtypes = (
        ctypes.c_wchar_p, ctypes.c_int32, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_wchar), ctypes.c_int32, ctypes.POINTER(ctypes.c_int32),
    )
    convert.restype = ctypes.c_int32
    status = ctypes.c_int32()
    result = ctypes.create_unicode_buffer(128)
    length = convert(key, -1, b"001", result, len(result), ctypes.byref(status))
    if status.value <= 0 and 0 < length < len(result):
        return result.value
    return None


def current_iana_time_zone():
    try:
        if sys.platform == "darwin":
            name = _mac_zone()
        elif sys.platform == "win32":
            name = _windows_zone()
        else:
            return None
    except (OSError, AttributeError, RuntimeError, ValueError):
        return None
    if (
        isinstance(name, str)
        and len(name) <= 127
        and re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", name)
        and name not in {"localtime", "posixrules", "Factory"}
        and not name.startswith(("posix/", "right/"))
    ):
        return name
    return None
