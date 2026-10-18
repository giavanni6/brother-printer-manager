"""Regression tests for failures found by adversarial verification (2026-10-18)."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest

from fakes import FakePrinter
from printmgr import config, escl, install, ipp, net, snmp, status
from printmgr.config import Config


def _tcp_server(handler) -> tuple[socket.socket, int]:
    """One-shot TCP server on localhost running `handler(conn)` per connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1]


# ---- net: every failure is an OSError, and timeout bounds the whole request -------------------
def test_slow_dribbling_response_hits_total_deadline():
    def dribble(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
        try:
            for _ in range(100):
                conn.sendall(b"x")
                time.sleep(0.3)
        except OSError:
            pass

    srv, port = _tcp_server(dribble)
    t = time.monotonic()
    with pytest.raises(OSError):
        net.request("GET", f"http://127.0.0.1:{port}/", timeout=1.5)
    assert time.monotonic() - t < 4
    srv.close()


@pytest.mark.parametrize(
    "reply",
    [b"garbage that is not http\r\n\r\n", b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nshort"],
    ids=["BadStatusLine", "IncompleteRead"],
)
def test_protocol_errors_surface_as_oserror(reply):
    def answer(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(reply)
        conn.close()

    srv, port = _tcp_server(answer)
    with pytest.raises(OSError):
        net.request("GET", f"http://127.0.0.1:{port}/", timeout=3)
    # ...so a non-HTTP service on 631 is just "not a printer", for status and probe alike
    st = status.fetch(Config(host="127.0.0.1"), ipp_port=port, snmp_port=1)
    assert not st.reachable
    with pytest.raises((OSError, ipp.IppError)):
        ipp.probe("127.0.0.1", port=port)
    srv.close()


def test_bad_url_port_is_oserror():
    with pytest.raises(OSError):
        net.request("GET", "http://127.0.0.1:None/eSCL")


# ---- SNMP: stray datagrams cannot extend the wait; malformed ones are ignored ----------------
def test_snmp_wrong_request_id_flood_is_bounded():
    agent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    agent.bind(("127.0.0.1", 0))
    stop = threading.Event()

    def flood() -> None:
        agent.settimeout(0.1)
        peer = None
        while not stop.is_set():
            try:
                _, peer = agent.recvfrom(65535)
            except (TimeoutError, OSError):
                pass
            if peer:
                try:
                    agent.sendto(snmp.encode_message("public", snmp.PDU_RESPONSE, 999999, [((1, 3), 1)]), peer)
                    agent.sendto(b"\x30\x05garbage", peer)
                except OSError:
                    return  # socket closed at teardown

    threading.Thread(target=flood, daemon=True).start()
    client = snmp.Client("127.0.0.1", port=agent.getsockname()[1], timeout=0.5, retries=1)
    t = time.monotonic()
    with pytest.raises(snmp.SnmpError):
        client.get(status.OID_PAGE_COUNT)
    assert time.monotonic() - t < 2.5  # (retries+1) * timeout, plus slack
    stop.set()
    agent.close()


def test_snmp_malformed_datagram_then_real_reply_is_accepted():
    agent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    agent.bind(("127.0.0.1", 0))

    def reply_once() -> None:
        data, peer = agent.recvfrom(65535)
        _tag, rid, _err, vbs = snmp.decode_message(data)
        agent.sendto(b"\x30\x03\x02\x01", peer)  # truncated junk first
        agent.sendto(snmp.encode_message("public", snmp.PDU_RESPONSE, rid, [(vbs[0][0], snmp.Counter(42))]), peer)

    threading.Thread(target=reply_once, daemon=True).start()
    assert snmp.Client("127.0.0.1", port=agent.getsockname()[1], timeout=1).get(status.OID_PAGE_COUNT) == 42
    agent.close()


# ---- status: legal-but-odd values never break a poll ----------------------------------------
def test_out_of_band_and_absurd_marker_values():
    attrs = {
        "marker-names": ["BK", "Drum", "Extra"],
        "marker-levels": [b"", 10**30],  # out-of-band 'unknown', absurd; third missing entirely
        "marker-high-levels": [b"", 100, 0],
        "marker-low-levels": [b"\x00"],
        "marker-types": ["toner"],
    }
    supplies = status.supplies_from_ipp(attrs, 10)
    assert [(s.name, s.percent) for s in supplies] == [("Black toner", None), ("Drum", 100), ("Extra", None)]


def test_huge_snmp_page_count_ignored():
    with FakePrinter() as fp:
        fp.snmp_values[status.OID_PAGE_COUNT] = snmp.Counter(2**40)
        st = status.fetch(Config(host="127.0.0.1"), fp.http_port, fp.snmp_port)
    assert st.reachable and st.page_count is None


# ---- alerts: a blip does not re-announce, and state survives restarts ------------------------
def test_offline_blip_does_not_reannounce_low_supplies(tmp_path: Path):
    low = {"low:toner:Black toner": "Black toner at 15%"}
    tracker = status.AlertTracker(tmp_path / "alerts.json")
    assert tracker.update(low) == ["Black toner at 15%"]
    assert tracker.update({"offline": "Printer is offline or unreachable"}) == ["Printer is offline or unreachable"]
    assert tracker.update(low) == []  # back online, nothing new
    assert status.AlertTracker(tmp_path / "alerts.json").update(low) == []  # restart / next login
    assert tracker.update({}) == []
    assert tracker.update(low) == ["Black toner at 15%"]  # cleared, then recurred


# ---- config: bad files never crash startup or the poller -------------------------------------
@pytest.mark.parametrize(
    "text",
    ["[]", "null", '"x"', '{"poll_seconds": null, "low_percent": "10", "scan_dpi": "abc", "host": 5}',
     '{"poll_seconds": "1", "scan_dir": "   "}'],
)
def test_bad_config_values_fall_back(tmp_path: Path, text: str):
    path = tmp_path / "config.json"
    path.write_text(text, encoding="utf-8")
    cfg = config.load(path)
    assert isinstance(cfg.poll_seconds, int) and 15 <= cfg.poll_seconds <= 3600
    assert isinstance(cfg.low_percent, int) and isinstance(cfg.scan_dpi, int) and cfg.scan_dpi > 0
    assert isinstance(cfg.host, str) and cfg.scan_dir.strip()


def test_config_bom_and_corrupt_backup(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"host": "10.0.0.9"}).encode())
    assert config.load(path).host == "10.0.0.9"
    path.write_text("{not json", encoding="utf-8")
    assert config.load(path) == Config()
    assert (tmp_path / "config.json.bad").read_text(encoding="utf-8") == "{not json"


def test_concurrent_saves_never_raise(tmp_path: Path):
    path = tmp_path / "config.json"
    errors: list[BaseException] = []

    def hammer(n: int) -> None:
        try:
            for i in range(100):
                assert config.save(Config(host=f"10.0.{n}.{i}"), path)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert config.load(path).host.startswith("10.0.")
    assert not list(tmp_path.glob("*.tmp"))


# ---- eSCL: busy vs empty vs rejected, as the real MFC-L2760DW reports them --------------------
def _scanner(fp: FakePrinter) -> escl.Scanner:
    s = escl.Scanner(f"http://127.0.0.1:{fp.http_port}/eSCL")
    s.retry_delay = 0.01
    return s


@pytest.mark.parametrize("code", [409, 500, 503])
def test_busy_refusals_are_retried_and_reported(code):
    with FakePrinter() as fp:
        fp.busy_posts, fp.busy_code = 5, code
        waits: list[int] = []
        assert len(_scanner(fp).scan("Platen", waiting=lambda: waits.append(1))) == 1
    assert waits  # the UI was told it is waiting, not frozen


def test_idle_refusal_reports_rejected_settings():
    with FakePrinter() as fp:
        fp.reject_settings = True
        with pytest.raises(escl.ScanError, match="rejected"):
            _scanner(fp).scan("Platen")


def test_unreadable_capabilities_is_scan_error():
    with pytest.raises(escl.ScanError):
        escl.parse_capabilities(b'<?xml version="1.0" encoding="bogus-enc"?><a/>')
    with pytest.raises(escl.ScanError):
        escl.parse_capabilities(b"<not xml")


def test_locate_skips_bad_cached_url():
    with pytest.raises(escl.ScanError):  # no scanner on localhost:80/443 -> clean ScanError, no ValueError
        escl.Scanner.locate("127.0.0.1", "http://127.0.0.1:None/eSCL")


# ---- install: exact host match --------------------------------------------------------------
def test_host_pattern_is_exact():
    pat = re.compile(install.host_pattern("192.168.0.7"))
    assert pat.search("IP_192.168.0.7")
    assert pat.search("http://192.168.0.7:631/ipp/print")
    assert not pat.search("IP_192.168.0.76")
    assert not pat.search("http://192.168.0.70:631/ipp/print")
    assert not pat.search("WSD-6458eb87-988b-49a5-9296-b3d7a4104697")


# ---- round 2 (re-verification, 2026-10-18) ----------------------------------------------------
def test_slow_headers_hit_total_deadline():
    def drip_headers(conn: socket.socket) -> None:
        conn.recv(65536)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            for i in range(40):
                conn.sendall(f"X-Pad-{i}: y\r\n".encode())
                time.sleep(0.25)
        except OSError:
            pass

    srv, port = _tcp_server(drip_headers)
    t = time.monotonic()
    with pytest.raises(TimeoutError):
        net.request("GET", f"http://127.0.0.1:{port}/", timeout=1.5)
    assert time.monotonic() - t < 3
    srv.close()


@pytest.mark.parametrize("url", ["http://a b/eSCL", "http://[::1/eSCL"])
def test_invalid_urls_are_oserror(url):
    with pytest.raises(OSError):
        net.request("GET", url, timeout=2)


@pytest.mark.parametrize("method,status", [("HEAD", 200), ("GET", 304), ("GET", 204)])
def test_bodyless_replies_with_content_length_are_fine(method, status):
    def answer(conn: socket.socket) -> None:
        conn.recv(65536)
        conn.sendall(f"HTTP/1.1 {status} X\r\nContent-Length: 1234\r\n\r\n".encode())
        time.sleep(0.5)
        conn.close()

    srv, port = _tcp_server(answer)
    r = net.request(method, f"http://127.0.0.1:{port}/", timeout=3)
    assert r.status == status and r.body == b""
    srv.close()


@pytest.mark.parametrize(
    "raw",
    ['{"host": "café"}'.encode("cp1252"), '{"host": "10.0.0.1"}'.encode("utf-16"), b"[" * 100_000],
    ids=["ansi", "utf16", "deep"],
)
def test_unreadable_config_encodings_never_crash(tmp_path: Path, raw: bytes):
    path = tmp_path / "config.json"
    path.write_bytes(raw)
    assert config.load(path) == Config()
    assert (tmp_path / "config.json.bad").read_bytes() == raw


def test_absurd_low_level_is_ignored():
    attrs = {"marker-names": ["BK"], "marker-types": ["toner"], "marker-levels": [90],
             "marker-low-levels": [200], "marker-high-levels": [100]}
    [s] = status.supplies_from_ipp(attrs, 10)
    assert s.low_percent == 10 and not s.low


def test_update_cfg_has_no_lost_updates(tmp_path: Path, monkeypatch):
    """A scan-settings save racing a printer change must keep both, in memory and on disk."""
    from types import SimpleNamespace

    from printmgr.app import App

    monkeypatch.setenv("APPDATA", str(tmp_path))
    fake = SimpleNamespace(cfg=Config(host="192.168.0.76"), _cfg_lock=threading.Lock())
    start = threading.Barrier(2)

    def change_printer() -> None:
        start.wait()
        for i in range(200):
            App.update_cfg(fake, host=f"10.0.0.{i % 250}", model=f"M{i}")

    def save_scan_settings() -> None:
        start.wait()
        for i in range(200):
            App.update_cfg(fake, scan_dpi=600, scan_format="jpg")

    threads = [threading.Thread(target=change_printer), threading.Thread(target=save_scan_settings)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = config.load(tmp_path / "PrinterManager" / "config.json")
    for cfg in (fake.cfg, on_disk):
        assert (cfg.host, cfg.model, cfg.scan_dpi, cfg.scan_format) == ("10.0.0.199", "M199", 600, "jpg")
    # a stale write for the previous printer is refused
    assert not App.update_cfg(fake, only_if_host="192.168.0.76", model="stale")
    assert fake.cfg.model == "M199"
