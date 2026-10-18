"""Minimal IPP client (RFC 8010/8011): just enough for Get-Printer-Attributes."""

from __future__ import annotations

import struct
from typing import Any

from printmgr import net

OP_GET_PRINTER_ATTRIBUTES = 0x000B

GROUP_OPERATION = 0x01
GROUP_END = 0x03
GROUP_PRINTER = 0x04

TAG_INTEGER = 0x21
TAG_BOOLEAN = 0x22
TAG_ENUM = 0x23
TAG_TEXT_LANG = 0x35
TAG_NAME_LANG = 0x36
TAG_TEXT = 0x41
TAG_NAME = 0x42
TAG_KEYWORD = 0x44
TAG_URI = 0x45
TAG_CHARSET = 0x47
TAG_LANGUAGE = 0x48

PRINTER_STATES = {3: "idle", 4: "printing", 5: "stopped"}

STATUS_ATTRS = [
    "printer-make-and-model",
    "printer-name",
    "printer-state",
    "printer-state-reasons",
    "printer-state-message",
    "marker-names",
    "marker-types",
    "marker-levels",
    "marker-low-levels",
    "marker-high-levels",
]

IPP_PATHS = ("/ipp/print", "/ipp", "/")

Groups = list[tuple[int, dict[str, list[Any]]]]
Attr = tuple[int, str, list[Any]]


class IppError(Exception):
    pass


def encode_attribute(tag: int, name: str, values: list[Any]) -> bytes:
    out = bytearray()
    for i, value in enumerate(values):
        n = name.encode() if i == 0 else b""  # extra values of a 1setOf have an empty name
        if tag in (TAG_INTEGER, TAG_ENUM):
            data = struct.pack(">i", value)
        elif tag == TAG_BOOLEAN:
            data = b"\x01" if value else b"\x00"
        elif isinstance(value, bytes):
            data = value
        else:
            data = str(value).encode("utf-8")
        out += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(data)) + data
    return bytes(out)


def encode_message(code: int, request_id: int, groups: list[tuple[int, list[Attr]]]) -> bytes:
    """`code` is the operation-id for a request or the status-code for a response."""
    out = bytearray(struct.pack(">BBHI", 2, 0, code, request_id))
    for group_tag, attrs in groups:
        out.append(group_tag)
        for tag, name, values in attrs:
            out += encode_attribute(tag, name, values)
    out.append(GROUP_END)
    return bytes(out)


def _decode_value(tag: int, raw: bytes) -> Any:
    if tag in (TAG_INTEGER, TAG_ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == TAG_BOOLEAN and len(raw) == 1:
        return raw != b"\x00"
    if tag in (TAG_TEXT_LANG, TAG_NAME_LANG) and len(raw) >= 4:
        lang_len = int.from_bytes(raw[0:2], "big")
        text_len = int.from_bytes(raw[2 + lang_len : 4 + lang_len], "big")
        return raw[4 + lang_len : 4 + lang_len + text_len].decode("utf-8", "replace")
    if 0x40 <= tag <= 0x4A:
        return raw.decode("utf-8", "replace")
    return raw


def decode_message(data: bytes) -> tuple[int, int, Groups]:
    """Returns (status-or-operation code, request-id, [(group-tag, {name: [values]})]).

    Collections are not interpreted, but their flat TLV encoding keeps the parse aligned.
    """
    if len(data) < 9:
        raise IppError("response too short")
    code = int.from_bytes(data[2:4], "big")
    request_id = int.from_bytes(data[4:8], "big")
    groups: Groups = []
    current: dict[str, list[Any]] | None = None
    last_name: str | None = None
    pos = 8
    while pos < len(data):
        tag = data[pos]
        pos += 1
        if tag == GROUP_END:
            break
        if tag < 0x10:
            current = {}
            groups.append((tag, current))
            last_name = None
            continue
        if pos + 2 > len(data):
            raise IppError("truncated attribute")
        name_len = int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
        name = data[pos : pos + name_len].decode("utf-8", "replace")
        pos += name_len
        value_len = int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
        raw = data[pos : pos + value_len]
        pos += value_len
        if pos > len(data) or current is None:
            raise IppError("malformed response")
        value = _decode_value(tag, raw)
        if name:
            last_name = name
            current.setdefault(name, []).append(value)
        elif last_name is not None:
            current[last_name].append(value)
    return code, request_id, groups


def get_printer_attributes(
    host: str,
    path: str = "/ipp/print",
    attrs: list[str] | None = None,
    port: int = 631,
    timeout: float = 5.0,
) -> dict[str, list[Any]]:
    uri = f"ipp://{host}:{port}{path}"
    body = encode_message(
        OP_GET_PRINTER_ATTRIBUTES,
        1,
        [
            (
                GROUP_OPERATION,
                [
                    (TAG_CHARSET, "attributes-charset", ["utf-8"]),
                    (TAG_LANGUAGE, "attributes-natural-language", ["en"]),
                    (TAG_URI, "printer-uri", [uri]),
                    (TAG_KEYWORD, "requested-attributes", attrs or STATUS_ATTRS),
                ],
            )
        ],
    )
    resp = net.request(
        "POST", f"http://{host}:{port}{path}", body, {"Content-Type": "application/ipp"}, timeout
    )
    if resp.status != 200:
        raise IppError(f"HTTP {resp.status}")
    code, _, groups = decode_message(resp.body)
    if code >= 0x0100:
        raise IppError(f"IPP status 0x{code:04x}")
    merged: dict[str, list[Any]] = {}
    for group_tag, group in groups:
        if group_tag == GROUP_PRINTER:
            merged.update(group)
    return merged


def probe(host: str, port: int = 631, timeout: float = 5.0) -> tuple[str, dict[str, list[Any]]]:
    """Find the printer's IPP path. Returns (path, attributes)."""
    last: Exception = IppError("no IPP endpoint")
    for path in IPP_PATHS:
        try:
            return path, get_printer_attributes(host, path, port=port, timeout=timeout)
        except (OSError, IppError) as e:
            last = e
    raise last


def first(attrs: dict[str, list[Any]], name: str, default: Any = None) -> Any:
    values = attrs.get(name)
    return values[0] if values else default
