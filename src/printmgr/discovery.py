"""Find printers on the LAN: mDNS/Bonjour first, then a /24 sweep for IPP as a fallback."""

from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import socket
import time
from dataclasses import dataclass

from printmgr import ipp

MDNS_TYPES = ["_ipp._tcp.local.", "_uscan._tcp.local.", "_uscans._tcp.local."]


@dataclass
class Found:
    host: str
    name: str = ""
    model: str = ""
    ipp_path: str = "/ipp/print"
    escl_url: str = ""

    @property
    def is_brother(self) -> bool:
        return "brother" in f"{self.model} {self.name}".lower()

    @property
    def label(self) -> str:
        return f"{self.model or self.name or 'Printer'} — {self.host}"


def rank(found: list[Found], model_hint: str = "") -> list[Found]:
    hint = model_hint.lower()
    return sorted(
        found,
        key=lambda f: (
            not (hint and hint in f.model.lower()),
            not f.is_brother,
            f.host,
        ),
    )


def discover(timeout: float = 4.0, sweep: bool = True) -> list[Found]:
    found = _mdns(timeout)
    if not found and sweep:
        found = _sweep()
    return rank(list(found.values()))


def _mdns(timeout: float) -> dict[str, Found]:
    try:
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
    except ImportError:
        return {}

    seen: set[tuple[str, str]] = set()

    class Listener:
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            seen.add((type_, name))

        update_service = add_service

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    out: dict[str, Found] = {}
    try:
        zc = Zeroconf(ip_version=IPVersion.V4Only)
    except OSError:
        return out
    try:
        ServiceBrowser(zc, MDNS_TYPES, Listener())
        time.sleep(timeout)
        for type_, name in sorted(seen):
            info = zc.get_service_info(type_, name, timeout=1500)
            if not info:
                continue
            addrs = info.parsed_addresses()
            if not addrs:
                continue
            host = addrs[0]
            props = {
                k.decode(errors="replace"): (v.decode(errors="replace") if v else "")
                for k, v in info.properties.items()
            }
            f = out.setdefault(host, Found(host=host))
            f.name = f.name or name.split("._")[0]
            f.model = f.model or props.get("ty", "")
            if type_.startswith("_ipp."):
                rp = props.get("rp", "")
                if rp:
                    f.ipp_path = "/" + rp.strip("/")
            elif info.port:
                scheme = "https" if type_.startswith("_uscans") else "http"
                url = f"{scheme}://{host}:{info.port}/{props.get('rs', 'eSCL').strip('/')}"
                if not f.escl_url or scheme == "http":
                    f.escl_url = url
    finally:
        zc.close()
    return out


def local_ipv4() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent; just selects the outbound interface
            return s.getsockname()[0]
        except OSError:
            return None


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _sweep() -> dict[str, Found]:
    me = local_ipv4()
    if not me:
        return {}
    net = ipaddress.ip_network(f"{me}/24", strict=False)
    hosts = [str(h) for h in net.hosts() if str(h) != me]
    with cf.ThreadPoolExecutor(64) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(lambda h: _port_open(h, 631), hosts)) if ok]
    out: dict[str, Found] = {}
    for host in open_hosts:
        try:
            path, attrs = ipp.probe(host, timeout=3)
        except (OSError, ipp.IppError):
            continue
        out[host] = Found(
            host=host,
            name=str(ipp.first(attrs, "printer-name", "")),
            model=str(ipp.first(attrs, "printer-make-and-model", "")),
            ipp_path=path,
        )
    return out
