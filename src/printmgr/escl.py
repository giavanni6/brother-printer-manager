"""eSCL (AirScan / Mopria scan) client: driverless network scanning over HTTP(S)."""

from __future__ import annotations

import io
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from PIL import Image

from printmgr import net

SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"

# Page sizes in eSCL units (1/300 inch).
PAGE_SIZES = {"Letter": (2550, 3300), "Legal": (2550, 4200), "A4": (2480, 3508)}

REDIRECTS = (301, 302, 303, 307, 308)


class ScanError(Exception):
    pass


@dataclass
class SourceCaps:
    max_width: int = 2550
    max_height: int = 3300
    resolutions: list[int] = field(default_factory=list)
    color_modes: list[str] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)


@dataclass
class ScannerCaps:
    make_and_model: str = ""
    platen: SourceCaps | None = None
    adf: SourceCaps | None = None
    adf_duplex: bool = False


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _int(text: str | None, default: int) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        return default


def _source_caps(el: ET.Element) -> SourceCaps:
    caps = SourceCaps(_int(el.findtext("MaxWidth"), 2550), _int(el.findtext("MaxHeight"), 3300))
    for tag, target in (("ColorMode", caps.color_modes), ("DocumentFormatExt", caps.formats), ("DocumentFormat", caps.formats)):
        for node in el.iter(tag):
            value = (node.text or "").strip()
            if value and value not in target:
                target.append(value)
    for node in el.iter("DiscreteResolution"):
        x = _int(node.findtext("XResolution"), 0)
        if x and x not in caps.resolutions:
            caps.resolutions.append(x)
    caps.resolutions.sort()
    return caps


def parse_capabilities(xml: bytes) -> ScannerCaps:
    try:
        root = _strip_ns(ET.fromstring(xml))
    except (ET.ParseError, LookupError, ValueError) as e:  # LookupError: unknown XML encoding
        raise ScanError(f"Scanner sent unreadable capabilities ({e})") from None
    caps = ScannerCaps(make_and_model=(root.findtext("MakeAndModel") or "").strip())
    platen = root.find("Platen/PlatenInputCaps")
    simplex = root.find("Adf/AdfSimplexInputCaps")
    duplex = root.find("Adf/AdfDuplexInputCaps")
    if platen is not None:
        caps.platen = _source_caps(platen)
    if simplex is not None or duplex is not None:
        caps.adf = _source_caps(simplex if simplex is not None else duplex)  # type: ignore[arg-type]
    options = [(o.text or "").strip() for o in root.iter("AdfOption")]
    caps.adf_duplex = duplex is not None or "Duplex" in options
    return caps


def build_scan_settings(source: str, color: str, dpi: int, width: int, height: int, duplex: bool = False) -> bytes:
    # Sent compact (no whitespace between elements): the MFC-L2760DW's parser treats indented XML
    # as invalid and silently falls back to 200 dpi color (verified on the real printer 2026-10-17).
    duplex_xml = f"<scan:Duplex>{'true' if duplex else 'false'}</scan:Duplex>" if source == "Feeder" else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{SCAN_NS}" xmlns:pwg="{PWG_NS}">
  <pwg:Version>2.6</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{width}</pwg:Width>
      <pwg:Height>{height}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
  <pwg:InputSource>{source}</pwg:InputSource>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <scan:ColorMode>{color}</scan:ColorMode>
  {duplex_xml}
</scan:ScanSettings>"""
    return re.sub(r">\s+<", "><", xml).encode()


class Scanner:
    def __init__(self, base_url: str, timeout: float = 180.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_delay = 1.0

    @classmethod
    def locate(cls, host: str, hint: str = "") -> Scanner:
        candidates = [hint] if hint else []
        candidates += [f"http://{host}/eSCL", f"https://{host}/eSCL"]
        last: Exception = ScanError(f"No eSCL scanner found at {host}")
        for url in dict.fromkeys(candidates):
            scanner = cls(url)
            try:
                scanner.capabilities()
                return scanner
            except (OSError, ScanError, ValueError) as e:
                last = e
        raise ScanError(f"No network scanner found at {host} ({last})")

    def capabilities(self) -> ScannerCaps:
        url = f"{self.base}/ScannerCapabilities"
        for _ in range(3):
            r = net.request("GET", url, timeout=10)
            if r.status in REDIRECTS and "location" in r.headers:
                url = urljoin(url, r.headers["location"])
                continue
            break
        if r.status != 200:
            raise ScanError(f"ScannerCapabilities returned HTTP {r.status}")
        self.base = url.rsplit("/ScannerCapabilities", 1)[0]  # remember where a redirect landed
        return parse_capabilities(r.body)

    def scan(
        self,
        source: str = "Platen",
        color: str = "RGB24",
        dpi: int = 300,
        size: tuple[int, int] = PAGE_SIZES["Letter"],
        progress: Callable[[int], None] | None = None,
        caps: ScannerCaps | None = None,
        waiting: Callable[[], None] | None = None,
    ) -> list[bytes]:
        """source: Platen | Feeder | Duplex. Returns JPEG bytes, one per page."""
        caps = caps or self.capabilities()
        feeder = source in ("Feeder", "Duplex")
        src = caps.adf if feeder else caps.platen
        if src is None:
            raise ScanError(f"This scanner has no {'document feeder' if feeder else 'flatbed'}")
        if src.formats and "image/jpeg" not in src.formats:
            raise ScanError(f"Scanner does not offer JPEG output (offers {', '.join(src.formats)})")
        if src.resolutions and dpi not in src.resolutions:
            dpi = min(src.resolutions, key=lambda r: abs(r - dpi))
        # JPEG cannot carry 1-bit images and the MFC-L2760DW answers 409 to BlackAndWhite1 + JPEG,
        # so black & white is scanned as grayscale and thresholded locally.
        black_and_white = color == "BlackAndWhite1"
        if black_and_white:
            color = "Grayscale8"
        if src.color_modes and color not in src.color_modes:
            color = "RGB24" if "RGB24" in src.color_modes else src.color_modes[0]
        settings = build_scan_settings(
            "Feeder" if feeder else "Platen",
            color,
            dpi,
            min(size[0], src.max_width),
            min(size[1], src.max_height),
            duplex=source == "Duplex" and caps.adf_duplex,
        )
        job = self._create_job(settings, feeder=feeder, waiting=waiting)
        pages: list[bytes] = []
        try:
            while True:
                data = self._next_document(job)
                if data is None:
                    break
                pages.append(data)
                if progress:
                    progress(len(pages))
                if not feeder:
                    break  # flatbed: exactly one page
        finally:
            try:
                net.request("DELETE", job, timeout=5)
            except OSError:
                pass
        if not pages:
            hint = " Is paper loaded in the document feeder?" if feeder else ""
            raise ScanError("Scanner returned no pages." + hint)
        return [to_black_and_white(p) for p in pages] if black_and_white else pages

    def scanner_status(self) -> tuple[str, str]:
        """(State, AdfState), e.g. ('Idle', 'ScannerAdfLoaded'); empty strings if unknown. Never raises."""
        try:
            r = net.request("GET", f"{self.base}/ScannerStatus", timeout=10)
            if r.status != 200:
                return "", ""
            root = _strip_ns(ET.fromstring(r.body))
        except (OSError, ET.ParseError, LookupError, ValueError):
            return "", ""
        return (root.findtext("State") or "").strip(), (root.findtext("AdfState") or "").strip()

    def _create_job(
        self,
        settings: bytes,
        feeder: bool = False,
        wait: float = 90.0,
        waiting: Callable[[], None] | None = None,
    ) -> str:
        """Measured on the MFC-L2760DW: a job submitted while another runs (or within ~60 s of a
        client abandoning one) gets 409/500/503 while ScannerStatus says Processing; an empty
        feeder gets 500 while Idle. So refusals are retried for as long as the scanner is busy."""
        deadline = time.monotonic() + wait
        idle_refusals = 0
        while True:
            r = net.request("POST", f"{self.base}/ScanJobs", settings, {"Content-Type": "text/xml"}, timeout=30)
            if r.status == 201 and "location" in r.headers:
                return urljoin(self.base + "/", r.headers["location"]).rstrip("/")
            state, adf = self.scanner_status()
            # An unreadable status counts as idle, so a genuinely bad request fails in seconds
            # instead of spinning for the whole wait; a 503 is always taken as busy.
            busy = r.status == 503 or state not in ("Idle", "")
            if feeder and not busy and "empty" in adf.lower():
                raise ScanError("The document feeder is empty — load the pages face up and try again")
            if r.status not in (409, 500, 503):
                raise ScanError(f"Scanner refused the job (HTTP {r.status})")
            if not busy:
                idle_refusals += 1
                if idle_refusals >= 3:  # idle yet refusing: it is the request, not the timing
                    raise ScanError("The scanner rejected these scan settings")
            if time.monotonic() >= deadline:
                raise ScanError("The scanner is still busy with another job — try again in a minute")
            if waiting and busy:
                waiting()
            time.sleep(self.retry_delay)

    def _next_document(self, job: str, wait: float = 90.0) -> bytes | None:
        deadline = time.monotonic() + wait
        while True:
            r = net.request("GET", f"{job}/NextDocument", timeout=self.timeout)
            if r.status == 200:
                return r.body
            if r.status in (404, 410):
                return None  # no more pages
            if r.status == 503 and time.monotonic() < deadline:
                time.sleep(self.retry_delay)
                continue
            raise ScanError(f"Scanner error while fetching a page (HTTP {r.status})")


def to_black_and_white(jpeg: bytes, threshold: int = 160) -> bytes:
    """Grayscale JPEG -> 1-bit PNG (keeps the JPEG's dpi)."""
    img = Image.open(io.BytesIO(jpeg))
    dpi = img.info.get("dpi")
    bw = img.convert("L").point(lambda v: 255 if v >= threshold else 0).convert("1")
    buf = io.BytesIO()
    bw.save(buf, "PNG", **({"dpi": dpi} if dpi else {}))
    return buf.getvalue()


def _unique(path: Path) -> Path:
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


def save_pages(pages: list[bytes], folder: Path, basename: str, fmt: str, dpi: int) -> list[Path]:
    """fmt: 'pdf' (one multi-page file) or 'jpg' (one file per page)."""
    folder.mkdir(parents=True, exist_ok=True)
    if fmt == "pdf":
        images = []
        for data in pages:
            img = Image.open(io.BytesIO(data))
            images.append(img if img.mode in ("RGB", "L", "1") else img.convert("RGB"))
        path = _unique(folder / f"{basename}.pdf")
        # Trust the JPEG's own density over what we asked for, so page size is right even if a
        # scanner substitutes a resolution.
        embedded = images[0].info.get("dpi")
        resolution = float(embedded[0]) if embedded and embedded[0] > 1 else float(dpi)
        images[0].save(path, "PDF", save_all=True, append_images=images[1:], resolution=resolution)
        return [path]
    out = []
    for i, data in enumerate(pages, 1):
        jpeg = data.startswith(b"\xff\xd8")
        ext = "jpg" if jpeg else "png"  # black & white pages stay lossless 1-bit PNG
        name = basename if len(pages) == 1 else f"{basename}_{i:03d}"
        path = _unique(folder / f"{name}.{ext}")
        path.write_bytes(data)
        out.append(path)
    return out
