"""Settings persisted as JSON under %APPDATA%\\PrinterManager."""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger("printmgr")
_save_lock = threading.Lock()


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / "PrinterManager"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    host: str = ""  # printer IP / hostname; empty = auto-discover
    model: str = ""
    ipp_path: str = "/ipp/print"
    escl_url: str = ""  # cached eSCL base URL, e.g. http://192.168.0.50/eSCL
    poll_seconds: int = 60
    low_percent: int = 10  # alert threshold when the printer does not report its own
    snmp_community: str = "public"
    scan_dir: str = str(Path.home() / "Documents" / "Scans")
    scan_source: str = "Platen"  # Platen | Feeder | Duplex
    scan_color: str = "RGB24"  # RGB24 | Grayscale8 | BlackAndWhite1
    scan_dpi: int = 300
    scan_size: str = "Letter"  # Letter | Legal | A4
    scan_format: str = "pdf"  # pdf | jpg


def _coerce(value: object, default: object) -> object:
    """Value converted to the default's type, or the default when that is impossible."""
    if isinstance(default, bool) or value is None:
        return default
    if isinstance(default, int):
        try:
            return int(value)  # type: ignore[call-overload]
        except (TypeError, ValueError, OverflowError):
            return default
    return value if isinstance(value, str) else default


def from_dict(data: object) -> Config:
    if not isinstance(data, dict):
        return Config()
    defaults = Config()
    values = {f.name: _coerce(data[f.name], getattr(defaults, f.name)) for f in fields(Config) if f.name in data}
    cfg = Config(**values)  # type: ignore[arg-type]
    cfg.poll_seconds = min(max(cfg.poll_seconds, 15), 3600)
    cfg.low_percent = min(max(cfg.low_percent, 0), 100)
    if not 0 < cfg.scan_dpi <= 1200:
        cfg.scan_dpi = defaults.scan_dpi
    if len(cfg.host) > 253:  # longest legal DNS name
        cfg.host = ""
    if not cfg.scan_dir.strip():
        cfg.scan_dir = defaults.scan_dir
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or app_dir() / "config.json"
    try:
        raw = path.read_bytes()
    except OSError:
        return Config()
    try:
        # utf-8-sig tolerates Notepad's BOM; UnicodeDecodeError (an ANSI or UTF-16 save) is a
        # ValueError, and so is bad JSON; RecursionError is absurdly deep nesting.
        return from_dict(json.loads(raw.decode("utf-8-sig")))
    except (ValueError, RecursionError):
        # Keep the unreadable file instead of letting the next save silently replace it.
        try:
            shutil.copyfile(path, path.with_suffix(".json.bad"))
        except OSError:
            pass
        log.warning("config.json is unreadable; using defaults (copy kept as config.json.bad)")
        return Config()


def save(cfg: Config, path: Path | None = None) -> bool:
    """Atomic, thread-safe save. Never raises: a failed save is logged and reported as False."""
    path = path or app_dir() / "config.json"
    data = json.dumps(asdict(cfg), indent=2)
    with _save_lock:
        for attempt in range(5):
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="config.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
                return True
            except OSError:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                # Antivirus / the search indexer can hold config.json open for a moment.
                time.sleep(0.05 * (attempt + 1))
    log.warning("could not save %s", path)
    return False
