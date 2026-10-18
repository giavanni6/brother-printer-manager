"""Start-with-Windows toggle via the per-user Run registry key (no admin needed)."""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "PrinterManager"


def command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else exe}" -m printmgr'


def repair() -> None:
    """If start-with-Windows is on but its target exe no longer exists (the install moved),
    re-point it here. Only for the packaged exe, and never while the target still exists, so
    running a dist/ build cannot hijack the installed app's entry."""
    import winreg

    if not getattr(sys, "frozen", False):
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            current, _ = winreg.QueryValueEx(key, VALUE)
    except OSError:
        return  # disabled: leave it disabled
    target = str(current).strip().split('"')[1] if str(current).startswith('"') else str(current).split(" ")[0]
    if current != command() and not Path(target).exists():
        set_enabled(True)


def is_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
            return True
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE)
            except FileNotFoundError:
                pass
