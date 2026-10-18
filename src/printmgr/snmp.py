"""Minimal read-only SNMPv2c client (GET / GETNEXT) for the standard Printer MIB (RFC 3805)."""

from __future__ import annotations

import itertools
import socket
import time
from typing import Any

INTEGER = 0x02
OCTET_STRING = 0x04
NULL = 0x05
OBJECT_ID = 0x06
SEQUENCE = 0x30
COUNTER32 = 0x41
GAUGE32 = 0x42
TIMETICKS = 0x43
COUNTER64 = 0x46
NO_SUCH_OBJECT = 0x80
NO_SUCH_INSTANCE = 0x81
END_OF_MIB_VIEW = 0x82

PDU_GET = 0xA0
PDU_GETNEXT = 0xA1
PDU_RESPONSE = 0xA2

OID = tuple[int, ...]


class SnmpError(Exception):
    pass


class _End:
    def __repr__(self) -> str:
        return "END"


END = _End()  # noSuchObject / noSuchInstance / endOfMibView


class Counter(int):
    """An int that encodes as Counter32 (used by the fake agent in tests)."""


def parse_oid(text: str) -> OID:
    return tuple(int(x) for x in text.strip(".").split("."))


def _length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _length(len(payload)) + payload


def _int_bytes(v: int, signed: bool) -> bytes:
    n = max(1, (v.bit_length() + 8) // 8)  # room for the sign bit
    return v.to_bytes(n, "big", signed=signed)


def enc_oid(oid: OID) -> bytes:
    body = bytearray()
    for sub in [oid[0] * 40 + oid[1], *oid[2:]]:
        chunk = [sub & 0x7F]
        sub >>= 7
        while sub:
            chunk.append(0x80 | (sub & 0x7F))
            sub >>= 7
        body += bytes(reversed(chunk))
    return tlv(OBJECT_ID, bytes(body))


def encode_value(v: Any) -> bytes:
    if v is None:
        return tlv(NULL, b"")
    if v is END:
        return tlv(END_OF_MIB_VIEW, b"")
    if isinstance(v, Counter):
        return tlv(COUNTER32, _int_bytes(int(v), signed=False))
    if isinstance(v, int):
        return tlv(INTEGER, _int_bytes(v, signed=True))
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return tlv(OCTET_STRING, v)
    if isinstance(v, tuple):
        return enc_oid(v)
    raise TypeError(f"cannot encode {v!r}")


def encode_message(
    community: str,
    pdu_tag: int,
    request_id: int,
    varbinds: list[tuple[OID, Any]],
    error_status: int = 0,
) -> bytes:
    vbs = b"".join(tlv(SEQUENCE, enc_oid(oid) + encode_value(val)) for oid, val in varbinds)
    pdu = tlv(
        pdu_tag,
        encode_value(request_id) + encode_value(error_status) + encode_value(0) + tlv(SEQUENCE, vbs),
    )
    return tlv(SEQUENCE, encode_value(1) + encode_value(community) + pdu)  # version 1 == v2c


def _read_tlv(data: bytes, pos: int) -> tuple[int, bytes, int]:
    if pos + 2 > len(data):
        raise SnmpError("truncated")
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos : pos + n], "big")
        pos += n
    end = pos + length
    if end > len(data):
        raise SnmpError("truncated")
    return tag, data[pos:end], end


def _children(payload: bytes) -> list[tuple[int, bytes]]:
    out = []
    pos = 0
    while pos < len(payload):
        tag, value, pos = _read_tlv(payload, pos)
        out.append((tag, value))
    return out


def dec_oid(b: bytes) -> OID:
    subids: list[int] = []
    sub = 0
    for byte in b:
        sub = (sub << 7) | (byte & 0x7F)
        if not byte & 0x80:
            subids.append(sub)
            sub = 0
    if not subids:
        return ()
    top = min(subids[0] // 40, 2)  # the first subid packs the first two arcs (X.690 8.19.4)
    return (top, subids[0] - 40 * top, *subids[1:])


def decode_value(tag: int, raw: bytes) -> Any:
    if tag == INTEGER:
        return int.from_bytes(raw, "big", signed=True)
    if tag == OCTET_STRING:
        try:
            return raw.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            return raw.decode("latin-1").rstrip("\x00")
    if tag == OBJECT_ID:
        return dec_oid(raw)
    if tag == NULL:
        return None
    if tag in (COUNTER32, GAUGE32, TIMETICKS, COUNTER64):
        return int.from_bytes(raw, "big", signed=False)
    if tag in (NO_SUCH_OBJECT, NO_SUCH_INSTANCE, END_OF_MIB_VIEW):
        return END
    return raw


def decode_message(data: bytes) -> tuple[int, int, int, list[tuple[OID, Any]]]:
    """Returns (pdu-tag, request-id, error-status, varbinds)."""
    tag, body, _ = _read_tlv(data, 0)
    if tag != SEQUENCE:
        raise SnmpError("not an SNMP message")
    parts = _children(body)
    if len(parts) < 3:
        raise SnmpError("malformed message")
    pdu_tag, pdu = parts[2]
    fields = _children(pdu)
    if len(fields) < 4:
        raise SnmpError("malformed PDU")
    request_id = int.from_bytes(fields[0][1], "big", signed=True)
    error_status = int.from_bytes(fields[1][1], "big", signed=True)
    varbinds = []
    for _, vb in _children(fields[3][1]):
        items = _children(vb)
        if len(items) >= 2:
            varbinds.append((dec_oid(items[0][1]), decode_value(items[1][0], items[1][1])))
    return pdu_tag, request_id, error_status, varbinds


class Client:
    def __init__(
        self,
        host: str,
        community: str = "public",
        port: int = 161,
        timeout: float = 1.5,
        retries: int = 1,
    ) -> None:
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = retries
        self._ids = itertools.count(1)
        self._addr: str | None = None

    def _call(self, pdu_tag: int, oid: OID) -> tuple[int, OID, Any]:
        rid = next(self._ids)
        msg = encode_message(self.community, pdu_tag, rid, [(oid, None)])
        if self._addr is None:
            self._addr = socket.gethostbyname(self.host)  # once per client, not per walk step
        target = (self._addr, self.port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _ in range(self.retries + 1):
                s.sendto(msg, target)
                # One fixed deadline per attempt: stray or spoofed datagrams cannot extend it.
                deadline = time.monotonic() + self.timeout
                reply = None
                while reply is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    s.settimeout(remaining)
                    try:
                        data, addr = s.recvfrom(65535)
                    except (TimeoutError, ConnectionResetError):
                        break  # Windows surfaces ICMP port-unreachable as ConnectionResetError
                    if addr[0] != target[0]:
                        continue
                    try:
                        tag, got_rid, err, vbs = decode_message(data)
                    except (SnmpError, IndexError, ValueError):
                        continue  # a malformed datagram is not the answer; keep waiting
                    if tag == PDU_RESPONSE and got_rid == rid:
                        reply = (err, vbs)
                if reply is None:
                    continue
                err, vbs = reply
                if err or not vbs:
                    return err or 5, oid, END
                return 0, vbs[0][0], vbs[0][1]
        raise SnmpError(f"no SNMP response from {self.host}")

    def get(self, oid: OID) -> Any:
        _err, _oid, value = self._call(PDU_GET, oid)
        return value

    def walk(self, prefix: OID, limit: int = 256) -> list[tuple[OID, Any]]:
        out: list[tuple[OID, Any]] = []
        cur = prefix
        for _ in range(limit):
            err, oid, value = self._call(PDU_GETNEXT, cur)
            if err or value is END or oid[: len(prefix)] != prefix or oid <= cur:
                break
            out.append((oid, value))
            cur = oid
        return out
