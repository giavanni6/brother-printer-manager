from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from PIL import Image

from fakes import CAPS_XML, FakePrinter
from printmgr import escl, ipp, snmp, status
from printmgr.config import Config, load, save


# ---- IPP ---------------------------------------------------------------------------------
def test_ipp_roundtrip_multivalue_and_collection():
    with FakePrinter() as fp:
        _code, _rid, groups = ipp.decode_message(fp.ipp_response())
    printer = dict(groups)[ipp.GROUP_PRINTER]
    assert printer["printer-make-and-model"] == ["Brother MFC-L2760DW series"]
    assert printer["marker-levels"] == [80]
    assert printer["printer-state-message"] == ["Sleep"]  # parsed correctly AFTER the collection


def test_ipp_decode_handcrafted_bytes():
    data = (
        bytes.fromhex("0200000000000001" "04")  # IPP 2.0, successful-ok, request-id 1, printer group
        + b"\x23\x00\x0dprinter-state\x00\x04\x00\x00\x00\x05"
        + b"\x44\x00\x15printer-state-reasons\x00\x0fmedia-jam-error"
        + b"\x44\x00\x00\x00\x0bdoor-open-x"
        + b"\x03"
    )
    code, rid, groups = ipp.decode_message(data)
    assert (code, rid) == (0, 1)
    attrs = groups[0][1]
    assert attrs["printer-state"] == [5]
    assert attrs["printer-state-reasons"] == ["media-jam-error", "door-open-x"]


def test_ipp_rejects_garbage():
    with pytest.raises(ipp.IppError):
        ipp.decode_message(b"\x02\x00")


# ---- SNMP --------------------------------------------------------------------------------
@pytest.mark.parametrize("oid", ["1.3.6.1.2.1.43.11.1.1.9.1.1", "1.3.6.1.4.1.2435.2.3.9.4.2.1.5.5.8.0", "2.999.3"])
def test_oid_roundtrip(oid):
    encoded = snmp.enc_oid(snmp.parse_oid(oid))
    assert snmp.dec_oid(encoded[2:]) == snmp.parse_oid(oid)


@pytest.mark.parametrize("value", [0, 127, 128, -1, -129, 2**31 - 1, 300, "Drum Unit", None])
def test_snmp_value_roundtrip(value):
    raw = snmp.encode_value(value)
    tag, payload, _ = snmp._read_tlv(raw, 0)
    assert snmp.decode_value(tag, payload) == value


def test_snmp_counter_high_bit_is_unsigned():
    raw = snmp.encode_value(snmp.Counter(0xFFFFFFFF))
    tag, payload, _ = snmp._read_tlv(raw, 0)
    assert snmp.decode_value(tag, payload) == 0xFFFFFFFF


def test_snmp_walk_and_get_against_fake_agent():
    with FakePrinter() as fp:
        client = snmp.Client("127.0.0.1", port=fp.snmp_port, timeout=1)
        assert client.get(status.OID_PAGE_COUNT) == 1234
        names = [v for _, v in client.walk(status.OID_SUPPLY_DESC)]
        assert names == ["Black Toner Cartridge", "Drum Unit"]


def test_snmp_timeout_raises():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        silent.bind(("127.0.0.1", 0))
        client = snmp.Client("127.0.0.1", port=silent.getsockname()[1], timeout=0.2, retries=0)
        with pytest.raises(snmp.SnmpError):
            client.get(status.OID_PAGE_COUNT)


# ---- status ------------------------------------------------------------------------------
def _cfg(fp: FakePrinter) -> Config:
    return Config(host="127.0.0.1", ipp_path="/ipp/print")


def test_fetch_merges_ipp_toner_with_snmp_drum():
    with FakePrinter() as fp:
        st = status.fetch(_cfg(fp), ipp_port=fp.http_port, snmp_port=fp.snmp_port)
    assert st.reachable and st.state == "idle"
    assert st.model == "Brother MFC-L2760DW series"
    assert [(s.name, s.percent, s.kind) for s in st.supplies] == [("Black Toner", 80, "toner"), ("Drum Unit", 5, "drum")]
    assert st.page_count == 1234
    assert st.severity == "warning"  # drum at 5% <= default 10%
    assert st.headline == "Drum Unit low (5%)"


def test_fetch_errors_and_alert_diffing():
    tracker = status.AlertTracker()
    with FakePrinter() as fp:
        cfg = _cfg(fp)
        fp.snmp_values = {}  # SNMP disabled on the printer: IPP alone must still work
        assert tracker.update(status.fetch(cfg, fp.http_port, fp.snmp_port).alerts()) == []
        fp.reasons = ["media-empty-error", "toner-low-warning", "sleep-report"]
        fp.toner = 6
        st = status.fetch(cfg, fp.http_port, fp.snmp_port)
        assert st.severity == "error"
        assert st.headline == "Out of paper"
        first = tracker.update(st.alerts())
        assert set(first) == {"Out of paper", "Toner low", "Black Toner at 6%"}
        assert tracker.update(st.alerts()) == []  # no repeat while unchanged
        fp.reasons = ["toner-low-warning"]
        assert tracker.update(status.fetch(cfg, fp.http_port, fp.snmp_port).alerts()) == []
        fp.reasons = ["media-empty-error"]
        assert tracker.update(status.fetch(cfg, fp.http_port, fp.snmp_port).alerts()) == ["Out of paper"]


def test_fetch_offline():
    st = status.fetch(Config(host="127.0.0.1"), ipp_port=1, snmp_port=1)
    assert not st.reachable and st.severity == "offline"
    assert st.alerts() == {"offline": "Printer is offline or unreachable"}
    assert status.fetch(Config()).headline == "No printer set up"


def test_bare_color_code_marker_names_are_humanized():
    # Captured from the real MFC-L2760DW on 2026-10-17.
    attrs = {"marker-names": ["BK"], "marker-types": ["toner"], "marker-levels": [100],
             "marker-low-levels": [16], "marker-high-levels": [100]}
    [s] = status.supplies_from_ipp(attrs, 10)
    assert (s.name, s.percent, s.low_percent, s.kind) == ("Black toner", 100, 16, "toner")


def test_reason_levels():
    assert status.reason_level("media-jam") == "error"
    assert status.reason_level("toner-low-warning") == "warning"
    assert status.reason_level("whatever-error") == "error"
    assert status.reason_level("toner-low-report") == "info"
    assert status.reason_text("cover-open-error") == "Cover open"
    assert status.reason_text("weird-thing") == "Weird thing"


# ---- eSCL --------------------------------------------------------------------------------
def test_parse_capabilities():
    caps = escl.parse_capabilities(CAPS_XML)
    assert caps.make_and_model == "Brother MFC-L2760DW series"
    assert caps.platen and caps.platen.resolutions == [100, 300, 600]
    assert caps.platen.color_modes == ["RGB24", "Grayscale8"]
    assert "image/jpeg" in caps.platen.formats
    assert caps.adf and caps.adf.max_height == 4200 and not caps.adf_duplex


def _scanner(fp: FakePrinter) -> escl.Scanner:
    s = escl.Scanner.locate("127.0.0.1", f"http://127.0.0.1:{fp.http_port}/eSCL")
    s.retry_delay = 0.01
    return s


def test_feeder_scan_to_multipage_pdf(tmp_path: Path):
    with FakePrinter() as fp:
        progress: list[int] = []
        pages = _scanner(fp).scan("Feeder", "Grayscale8", 250, escl.PAGE_SIZES["Legal"], progress=progress.append)
        settings = fp.scan_settings[0].decode()
        assert fp.deleted == ["/eSCL/ScanJobs/job-1"]
    assert progress == [1, 2]
    assert "<scan:ColorMode>RGB24</scan:ColorMode>" in settings  # feeder only offers color
    assert "<scan:XResolution>300</scan:XResolution>" in settings  # snapped to supported dpi
    assert "<pwg:Height>4200</pwg:Height>" in settings
    assert not re.search(r">\s+<", settings)  # indented XML makes the real MFC-L2760DW ignore dpi/color
    files = escl.save_pages(pages, tmp_path, "Scan", "pdf", 300)
    assert len(files) == 1
    pdf = files[0].read_bytes()
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", pdf)) == 2


def test_platen_scan_single_jpeg_and_unique_names(tmp_path: Path):
    with FakePrinter() as fp:
        pages = _scanner(fp).scan("Platen", "RGB24", 600)
        assert "<scan:XResolution>600</scan:XResolution>" in fp.scan_settings[0].decode()
        assert "Duplex" not in fp.scan_settings[0].decode()
    assert len(pages) == 1
    a = escl.save_pages(pages, tmp_path, "Scan", "jpg", 600)
    b = escl.save_pages(pages, tmp_path, "Scan", "jpg", 600)
    assert a[0].name == "Scan.jpg" and b[0].name == "Scan (2).jpg"
    assert Image.open(io.BytesIO(a[0].read_bytes())).getpixel((10, 10))[1] > 200  # green page


def test_empty_feeder_reports_clearly():
    with FakePrinter() as fp:
        fp.feeder_pages = []
        with pytest.raises(escl.ScanError, match="feeder"):
            _scanner(fp).scan("Feeder")


def test_black_and_white_scans_grayscale_then_thresholds(tmp_path: Path):
    with FakePrinter() as fp:
        pages = _scanner(fp).scan("Platen", "BlackAndWhite1", 300)
        assert "<scan:ColorMode>Grayscale8</scan:ColorMode>" in fp.scan_settings[0].decode()
    assert Image.open(io.BytesIO(pages[0])).mode == "1"
    [png] = escl.save_pages(pages, tmp_path, "Scan", "jpg", 300)
    assert png.suffix == ".png"
    [pdf] = escl.save_pages(pages, tmp_path, "Scan", "pdf", 300)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_busy_scanner_is_retried():
    with FakePrinter() as fp:
        fp.busy_posts = 3
        assert len(_scanner(fp).scan("Platen")) == 1
        assert fp.busy_posts == 0


def test_duplex_rejected_when_unsupported_is_downgraded():
    with FakePrinter() as fp:
        _scanner(fp).scan("Duplex")
        assert "<scan:Duplex>false</scan:Duplex>" in fp.scan_settings[0].decode()


def test_locate_fails_cleanly():
    with pytest.raises(escl.ScanError):
        escl.Scanner.locate("127.0.0.1", "http://127.0.0.1:1/eSCL")


# ---- config ------------------------------------------------------------------------------
def test_config_roundtrip_ignores_unknown_keys(tmp_path: Path):
    path = tmp_path / "config.json"
    save(Config(host="10.0.0.5", scan_dpi=600), path)
    assert load(path).host == "10.0.0.5" and load(path).scan_dpi == 600
    path.write_text('{"host": "1.2.3.4", "bogus": 1}', encoding="utf-8")
    assert load(path).host == "1.2.3.4"
    path.write_text("not json", encoding="utf-8")
    assert load(path) == Config()
