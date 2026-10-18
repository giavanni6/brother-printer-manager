"""Add the printer to Windows with the built-in Microsoft IPP Class Driver (no vendor driver)."""

from __future__ import annotations

import base64
import re
import subprocess

_HOST = re.compile(r"^[A-Za-z0-9.\-]+\Z")
_PATH = re.compile(r"^/[A-Za-z0-9/_.\-]*\Z")
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9 ._\-]")
_MODEL_CODE = re.compile(r"\b[A-Z]{2,4}-[A-Z0-9]{3,}\b")  # e.g. MFC-L2760DW
CREATE_NO_WINDOW = 0x08000000


class InstallError(Exception):
    pass


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode()


def _ps(script: str, elevated: bool = False, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    if elevated:
        inner = _encoded(script)
        script = (
            "Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
            f"-ArgumentList '-NoProfile','-NonInteractive','-EncodedCommand','{inner}'"
        )
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", _encoded(script)],
            capture_output=True,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise InstallError("Windows did not finish adding the printer in time") from None


def printer_name(model: str) -> str:
    return _NAME_UNSAFE.sub("", model).strip() or "Brother Printer"


def host_pattern(host: str) -> str:
    """Regex (valid in both Python and .NET) matching `host` exactly inside a port name:
    192.168.0.7 must not match a port for 192.168.0.76."""
    return r"(^|[^A-Za-z0-9.])" + re.escape(host) + r"([^A-Za-z0-9.]|$)"


def find_installed(host: str, name: str = "") -> str | None:
    """Name of an existing Windows printer for this device: its port names this exact host, or its
    name carries the same model code (Windows' own WSD install calls it e.g. "Brother MFC-L2760DW series")."""
    if not _HOST.match(host):
        raise InstallError(f"invalid host {host!r}")
    clauses = [f"$_.PortName -match '{host_pattern(host)}'"]
    if name:
        clauses.append(f"$_.Name -eq '{printer_name(name)}'")
        code = _MODEL_CODE.search(printer_name(name))
        if code:
            clauses.append(f"$_.Name -like '*{code.group(0)}*'")
    r = _ps(
        "Get-Printer | Where-Object { " + " -or ".join(clauses) + " } "
        "| Select-Object -First 1 -ExpandProperty Name",
        timeout=60,
    )
    if r.returncode != 0:  # e.g. the Print Spooler is stopped: unknown is not "not installed"
        detail = (r.stderr or "").strip().splitlines()
        raise InstallError(f"Could not list Windows printers{': ' + detail[0] if detail else ''}")
    return r.stdout.strip() or None


def install(host: str, ipp_path: str, model: str) -> str:
    if not _HOST.match(host) or not _PATH.match(ipp_path):
        raise InstallError("invalid printer address")
    name = printer_name(model)
    existing = find_installed(host, name)
    if existing:
        return f'Already installed in Windows as "{existing}".'
    script = f"Add-Printer -Name '{name}' -IppURL 'http://{host}:631{ipp_path}' -ErrorAction Stop"
    r = _ps(script)
    output = f"{r.stderr}\n{r.stdout}".lower()
    if r.returncode != 0 and ("access" in output and "denied" in output or "0x80070005" in output):
        _ps(script, elevated=True, timeout=300)  # one UAC prompt, only when it is actually needed
    if find_installed(host, name):
        return f'Installed "{name}" using the built-in Windows IPP driver.'
    detail = [line for line in (r.stderr or r.stdout).strip().splitlines() if line.strip()]
    raise InstallError(detail[0] if detail else "Add-Printer failed")
