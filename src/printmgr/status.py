"""Printer status: IPP for state/toner, SNMP Printer-MIB for drum/page count, plus alert diffing."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from printmgr import ipp, snmp
from printmgr.config import Config

# Printer-MIB (RFC 3805) prtMarkerSuppliesTable columns, device index 1.
OID_SUPPLY_DESC = snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.6.1")
OID_SUPPLY_TYPE = snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.5.1")
OID_SUPPLY_MAX = snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.8.1")
OID_SUPPLY_LEVEL = snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.9.1")
OID_PAGE_COUNT = snmp.parse_oid("1.3.6.1.2.1.43.10.2.1.4.1.1")  # prtMarkerLifeCount

SNMP_SUPPLY_KINDS = {3: "toner", 4: "waste", 9: "drum", 10: "drum", 15: "fuser"}

REASON_TEXT = {
    "media-empty": "Out of paper",
    "media-needed": "Load paper",
    "media-jam": "Paper jam",
    "media-low": "Paper low",
    "toner-low": "Toner low",
    "toner-empty": "Toner empty",
    "marker-supply-low": "Toner low",
    "marker-supply-empty": "Toner empty",
    "marker-waste-almost-full": "Waste toner almost full",
    "marker-waste-full": "Waste toner full",
    "developer-low": "Drum nearing end of life",
    "developer-empty": "Replace drum",
    "opc-near-eol": "Drum nearing end of life",
    "opc-life-over": "Replace drum",
    "fuser-under-temperature": "Warming up",
    "door-open": "Cover open",
    "cover-open": "Cover open",
    "interlock-open": "Cover open",
    "input-tray-missing": "Paper tray missing",
    "output-area-full": "Output tray full",
    "output-area-almost-full": "Output tray almost full",
    "offline": "Printer offline",
    "paused": "Paused",
    "shutdown": "Shutting down",
    "connecting-to-device": "Connecting",
}
ERROR_REASONS = {
    "media-empty", "media-needed", "media-jam", "toner-empty", "marker-supply-empty",
    "marker-waste-full", "developer-empty", "opc-life-over", "door-open", "cover-open",
    "interlock-open", "input-tray-missing", "output-area-full", "offline", "shutdown",
}
IGNORED_REASONS = {"none", "sleep", "fuser-under-temperature", "connecting-to-device"}


def reason_base(reason: str) -> str:
    for suffix in ("-error", "-warning", "-report"):
        if reason.endswith(suffix):
            return reason[: -len(suffix)]
    return reason


def reason_level(reason: str) -> str:
    """'error' | 'warning' | 'info'."""
    base = reason_base(reason)
    if reason.endswith("-report") or base in IGNORED_REASONS:
        return "info"
    if reason.endswith("-error") or base in ERROR_REASONS:
        return "error"
    return "warning"


def reason_text(reason: str) -> str:
    base = reason_base(reason)
    return REASON_TEXT.get(base, base.replace("-", " ").capitalize())


# IPP marker-names are often bare color codes (the MFC-L2760DW reports its toner as "BK").
COLOR_CODES = {"BK": "Black", "K": "Black", "C": "Cyan", "M": "Magenta", "Y": "Yellow"}


def friendly_name(name: str, kind: str) -> str:
    color = COLOR_CODES.get(name.strip().upper())
    return f"{color} {kind}" if color and kind == "toner" else name


def classify(name: str, kind: str = "") -> str:
    text = f"{name} {kind}".lower()
    if "drum" in text or "opc" in text:
        return "drum"
    if "waste" in text:
        return "waste"
    if "toner" in text:
        return "toner"
    return kind or "other"


@dataclass
class Supply:
    name: str
    percent: int | None  # None = printer reports it as unknown / "some remaining"
    low_percent: int
    kind: str = "other"

    @property
    def low(self) -> bool:
        return self.percent is not None and self.percent <= self.low_percent


@dataclass
class PrinterStatus:
    reachable: bool
    host: str = ""
    model: str = ""
    state: str = "unknown"
    reasons: list[str] = field(default_factory=list)
    message: str = ""
    supplies: list[Supply] = field(default_factory=list)
    page_count: int | None = None
    error: str = ""

    @property
    def problems(self) -> list[tuple[str, str]]:
        """[(level, reason)] for reasons worth showing, errors first."""
        out = [(reason_level(r), r) for r in self.reasons]
        out = [(lvl, r) for lvl, r in out if lvl != "info"]
        return sorted(out, key=lambda p: p[0] != "error")

    @property
    def severity(self) -> str:
        if not self.reachable:
            return "offline"
        problems = self.problems
        if self.state == "stopped" or any(lvl == "error" for lvl, _ in problems):
            return "error"
        if problems or any(s.low for s in self.supplies):
            return "warning"
        return "ok"

    @property
    def headline(self) -> str:
        if not self.host:
            return "No printer set up"
        if not self.reachable:
            return "Printer offline"
        if self.problems:
            return reason_text(self.problems[0][1])
        low = [s for s in self.supplies if s.low]
        if low:
            return f"{low[0].name} low ({low[0].percent}%)"
        return {"idle": "Ready", "printing": "Printing", "stopped": "Stopped"}.get(self.state, "Ready")

    def alerts(self) -> dict[str, str]:
        """Stable key -> message for every condition that deserves a notification."""
        if not self.host:
            return {}
        if not self.reachable:
            return {"offline": "Printer is offline or unreachable"}
        out: dict[str, str] = {}
        for _lvl, reason in self.problems:
            out[f"reason:{reason_base(reason)}"] = reason_text(reason)
        for s in self.supplies:
            if s.low:
                out[f"low:{s.kind}:{s.name}"] = f"{s.name} at {s.percent}%"
        return out


class AlertTracker:
    """Reports only conditions that newly appeared.

    While the printer is unreachable its last-known conditions stay active, so a network blip
    does not re-announce low toner on reconnect. With a `path`, the active set survives restarts,
    so a login does not re-announce conditions the user was already told about.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._active: set[str] = set()
        if path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._active = {k for k in data if isinstance(k, str)} if isinstance(data, list) else set()
            except (OSError, ValueError):
                pass

    def update(self, alerts: dict[str, str]) -> list[str]:
        if "offline" in alerts:
            current = self._active | {"offline"}  # keep what we knew before it went away
        else:
            current = set(alerts)
        new = [msg for key, msg in alerts.items() if key not in self._active]
        if current != self._active:
            self._active = current
            self._persist()
        seen: set[str] = set()
        return [m for m in new if not (m in seen or seen.add(m))]

    def _persist(self) -> None:
        if not self._path:
            return
        try:
            self._path.write_text(json.dumps(sorted(self._active)), encoding="utf-8")
        except OSError:
            pass


def _num(values: list[Any], i: int) -> int | None:
    """values[i] if it is a plain int (IPP out-of-band values decode as bytes)."""
    v = values[i] if i < len(values) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _percent(level: int | None, high: int | None) -> int | None:
    if level is None or level < 0 or high is None or high <= 0:
        return None
    return min(100, round(level * 100 / high))


def supplies_from_ipp(attrs: dict[str, list[Any]], default_low: int) -> list[Supply]:
    names = attrs.get("marker-names", [])
    levels = attrs.get("marker-levels", [])
    highs = attrs.get("marker-high-levels", [])
    lows = attrs.get("marker-low-levels", [])
    types = attrs.get("marker-types", [])
    out = []
    for i, name in enumerate(names):
        high = _num(highs, i)
        high = high if high and high > 0 else 100
        low = _num(lows, i)
        # A threshold at/above "full" is nonsense and would flag the supply as permanently low.
        low_pct = _percent(low, high) if low and 0 < low < high else default_low
        kind = classify(str(name), str(types[i]) if i < len(types) else "")
        out.append(Supply(friendly_name(str(name), kind), _percent(_num(levels, i), high), low_pct or default_low, kind))
    return out


def supplies_from_snmp(client: snmp.Client, default_low: int) -> list[Supply]:
    def column(oid: snmp.OID) -> dict[int, Any]:
        return {o[-1]: v for o, v in client.walk(oid)}

    desc = column(OID_SUPPLY_DESC)
    if not desc:
        return []
    kinds, maxes, levels = column(OID_SUPPLY_TYPE), column(OID_SUPPLY_MAX), column(OID_SUPPLY_LEVEL)
    out = []
    for idx, name in sorted(desc.items()):
        mx, lv = maxes.get(idx), levels.get(idx)
        percent = _percent(lv if isinstance(lv, int) else None, mx if isinstance(mx, int) else None)
        kind = classify(str(name), SNMP_SUPPLY_KINDS.get(kinds.get(idx, 0), ""))
        out.append(Supply(str(name), percent, default_low, kind))
    return out


def merge_supplies(primary: list[Supply], extra: list[Supply]) -> list[Supply]:
    """IPP wins per kind; SNMP fills in what IPP does not report (typically the drum)."""
    have_kinds = {s.kind for s in primary if s.kind != "other"}
    have_names = {s.name.lower() for s in primary}
    merged = list(primary)
    for s in extra:
        if s.kind in have_kinds or s.name.lower() in have_names:
            continue
        merged.append(s)
        have_kinds.add(s.kind)
        have_names.add(s.name.lower())
    order = {"toner": 0, "drum": 1}
    return sorted(merged, key=lambda s: order.get(s.kind, 2))


def fetch(cfg: Config, ipp_port: int = 631, snmp_port: int = 161) -> PrinterStatus:
    st = PrinterStatus(reachable=False, host=cfg.host)
    if not cfg.host:
        return st
    ipp_supplies: list[Supply] = []
    try:
        attrs = ipp.get_printer_attributes(cfg.host, cfg.ipp_path, port=ipp_port)
    except (OSError, ipp.IppError) as e:
        st.error = f"IPP: {e}"
    else:
        st.reachable = True
        st.model = str(ipp.first(attrs, "printer-make-and-model", ""))
        st.state = ipp.PRINTER_STATES.get(ipp.first(attrs, "printer-state"), "unknown")
        st.reasons = [r for r in attrs.get("printer-state-reasons", []) if isinstance(r, str) and r != "none"]
        st.message = str(ipp.first(attrs, "printer-state-message", ""))
        ipp_supplies = supplies_from_ipp(attrs, cfg.low_percent)

    snmp_supplies: list[Supply] = []
    if st.reachable:  # skip SNMP timeouts when the printer is plainly off
        client = snmp.Client(cfg.host, cfg.snmp_community, port=snmp_port)
        try:
            count = client.get(OID_PAGE_COUNT)
            st.page_count = count if isinstance(count, int) and 0 <= count < 10**12 else None
            snmp_supplies = supplies_from_snmp(client, cfg.low_percent)
        except (OSError, snmp.SnmpError):
            pass
    st.supplies = merge_supplies(ipp_supplies, snmp_supplies)
    return st
