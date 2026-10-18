"""In-process fake printer: IPP + eSCL over HTTP and an SNMP agent over UDP, on localhost."""

from __future__ import annotations

import io
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PIL import Image

from printmgr import ipp, snmp

CAPS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                          xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.63</pwg:Version>
  <pwg:MakeAndModel>Brother MFC-L2760DW series</pwg:MakeAndModel>
  <scan:Platen><scan:PlatenInputCaps>
    <scan:MinWidth>16</scan:MinWidth><scan:MaxWidth>2550</scan:MaxWidth>
    <scan:MinHeight>16</scan:MinHeight><scan:MaxHeight>3508</scan:MaxHeight>
    <scan:SettingProfiles><scan:SettingProfile>
      <scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode></scan:ColorModes>
      <scan:DocumentFormats><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat></scan:DocumentFormats>
      <scan:SupportedResolutions><scan:DiscreteResolutions>
        <scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
        <scan:DiscreteResolution><scan:XResolution>100</scan:XResolution><scan:YResolution>100</scan:YResolution></scan:DiscreteResolution>
        <scan:DiscreteResolution><scan:XResolution>600</scan:XResolution><scan:YResolution>600</scan:YResolution></scan:DiscreteResolution>
      </scan:DiscreteResolutions></scan:SupportedResolutions>
    </scan:SettingProfile></scan:SettingProfiles>
  </scan:PlatenInputCaps></scan:Platen>
  <scan:Adf><scan:AdfSimplexInputCaps>
    <scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>4200</scan:MaxHeight>
    <scan:SettingProfiles><scan:SettingProfile>
      <scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
      <scan:DocumentFormats><pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat></scan:DocumentFormats>
      <scan:SupportedResolutions><scan:DiscreteResolutions>
        <scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
      </scan:DiscreteResolutions></scan:SupportedResolutions>
    </scan:SettingProfile></scan:SettingProfiles>
  </scan:AdfSimplexInputCaps></scan:Adf>
</scan:ScannerCapabilities>"""


def jpeg(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (85, 110), color).save(buf, "JPEG")
    return buf.getvalue()


class FakePrinter:
    def __init__(self) -> None:
        self.reasons = ["none"]
        self.state = 3
        self.toner = 80
        self.feeder_pages = [jpeg((255, 0, 0)), jpeg((0, 0, 255))]
        self.busy_polls = 1  # first NextDocument answers 503, like a real scanner warming up
        self.busy_posts = 0  # ScanJobs refuses this many times while ScannerStatus says Processing
        self.busy_code = 409  # the real printer uses 409, 500 or 503 for "busy"
        self.reject_settings = False  # idle but refusing (e.g. an unsupported combination)
        self.scan_settings: list[bytes] = []
        self.deleted: list[str] = []
        self.snmp_values: dict[snmp.OID, Any] = {
            snmp.parse_oid("1.3.6.1.2.1.43.10.2.1.4.1.1"): snmp.Counter(1234),
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.5.1.1"): 3,
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.5.1.2"): 9,
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.6.1.1"): "Black Toner Cartridge",
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.6.1.2"): "Drum Unit",
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.8.1.1"): 3000,
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.8.1.2"): 12000,
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.9.1.1"): 2400,
            snmp.parse_oid("1.3.6.1.2.1.43.11.1.1.9.1.2"): 600,
        }
        self._pending: list[bytes] = []
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        self._stop = threading.Event()

    @property
    def http_port(self) -> int:
        return self.http.server_address[1]

    @property
    def snmp_port(self) -> int:
        return self.udp.getsockname()[1]

    def ipp_response(self) -> bytes:
        return ipp.encode_message(
            0x0000,
            1,
            [
                (ipp.GROUP_OPERATION, [(ipp.TAG_CHARSET, "attributes-charset", ["utf-8"]), (ipp.TAG_LANGUAGE, "attributes-natural-language", ["en"])]),
                (
                    ipp.GROUP_PRINTER,
                    [
                        (ipp.TAG_TEXT, "printer-make-and-model", ["Brother MFC-L2760DW series"]),
                        (ipp.TAG_ENUM, "printer-state", [self.state]),
                        (ipp.TAG_KEYWORD, "printer-state-reasons", self.reasons),
                        (ipp.TAG_NAME, "marker-names", ["Black Toner"]),
                        (ipp.TAG_KEYWORD, "marker-types", ["toner"]),
                        (ipp.TAG_INTEGER, "marker-levels", [self.toner]),
                        (ipp.TAG_INTEGER, "marker-low-levels", [10]),
                        (ipp.TAG_INTEGER, "marker-high-levels", [100]),
                        (0x34, "media-col-ready", [b""]),  # a collection, to prove the parser stays aligned
                        (0x4A, "", ["media-size"]),
                        (0x37, "", [b""]),
                        (ipp.TAG_TEXT, "printer-state-message", ["Sleep"]),
                    ],
                ),
            ],
        )

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        printer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path == "/ipp/print":
                    code, _rid, groups = ipp.decode_message(body)
                    assert code == ipp.OP_GET_PRINTER_ATTRIBUTES
                    assert "requested-attributes" in groups[0][1]
                    self._send(200, printer.ipp_response(), {"Content-Type": "application/ipp"})
                elif self.path == "/eSCL/ScanJobs" and printer.busy_posts > 0:
                    printer.busy_posts -= 1
                    self._send(printer.busy_code)
                elif self.path == "/eSCL/ScanJobs" and printer.reject_settings:
                    self._send(409)
                elif self.path == "/eSCL/ScanJobs":
                    printer.scan_settings.append(body)
                    feeder = b"<pwg:InputSource>Feeder</pwg:InputSource>" in body
                    printer._pending = list(printer.feeder_pages) if feeder else [jpeg((0, 255, 0))]
                    if feeder and not printer._pending:
                        self._send(500)  # what the real MFC-L2760DW does
                        return
                    self._send(201, headers={"Location": "/eSCL/ScanJobs/job-1"})
                else:
                    self._send(404)

            def do_GET(self) -> None:
                if self.path == "/eSCL/ScannerCapabilities":
                    self._send(200, CAPS_XML, {"Content-Type": "text/xml"})
                elif self.path == "/eSCL/ScannerStatus":
                    state = "Processing" if printer.busy_posts else "Idle"
                    adf = "ScannerAdfLoaded" if printer.feeder_pages else "ScannerAdfEmpty"
                    xml = (
                        '<?xml version="1.0" encoding="UTF-8"?><scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
                        f'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:State>{state}</pwg:State><scan:AdfState>{adf}</scan:AdfState></scan:ScannerStatus>'
                    )
                    self._send(200, xml.encode(), {"Content-Type": "text/xml"})
                elif self.path == "/eSCL/ScanJobs/job-1/NextDocument":
                    if printer.busy_polls > 0:
                        printer.busy_polls -= 1
                        self._send(503)
                    elif printer._pending:
                        self._send(200, printer._pending.pop(0), {"Content-Type": "image/jpeg"})
                    else:
                        self._send(404)
                else:
                    self._send(404)

            def do_DELETE(self) -> None:
                printer.deleted.append(self.path)
                self._send(200)

        return Handler

    def _snmp_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.udp.recvfrom(65535)
            except (TimeoutError, OSError):
                continue
            tag, rid, _err, vbs = snmp.decode_message(data)
            oid = vbs[0][0]
            if tag == snmp.PDU_GET:
                out = (oid, self.snmp_values.get(oid, snmp.END))
            else:
                later = sorted(o for o in self.snmp_values if o > oid)
                out = (later[0], self.snmp_values[later[0]]) if later else (oid, snmp.END)
            self.udp.sendto(snmp.encode_message("public", snmp.PDU_RESPONSE, rid, [out]), addr)

    def __enter__(self) -> FakePrinter:
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self._snmp_loop, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self.http.shutdown()
        self.http.server_close()
        self.udp.close()
