"""
Synthetic WinOLS file builder for the test-suite.

Real .ols/.kp files are proprietary and cannot be shipped with the
repository, so the tests generate files that follow the on-disk layout the
reader implements.  The writer is deliberately written field-by-field (one
``has()`` check per feature flag) so it stays an independent description of
the format rather than a copy of the reader's optimised code paths.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from dataclasses import dataclass, field

BLOB_MARKER = 0x42007899
MAP_LIST_MARKER = 0x11883377
BLOB_CHECKSUM = 0x98728833

HEADER_FIELDS = (
    "make", "model", "engine", "year", "fuel_type", "displacement", "power",
    "gearbox", "memory_type", "ecu_manufacturer", "ecu_type", "sw_number",
    "hw_number", "hw_code", "user",
)


@dataclass
class AxisSpec:
    description: str = ""
    id: str = ""
    factor: float = 1.0
    offset: float = 0.0
    address: int = 0
    type_kind: int = 3
    data_source: int = 1
    translations: list[str] = field(default_factory=list)
    blob: bytes = b""  # feature 73 byte array
    extra_string: str = ""  # feature 354
    extra_texts: list[str] = field(default_factory=list)  # feature 372


@dataclass
class MapSpec:
    name: str = ""
    description: str = ""
    type_code: int = 2
    type_kind: int = 3
    folder_id: int = 0
    dim1: int = 1
    dim2: int = 1
    unit: str = ""
    factor: float = 1.0
    offset: float = 0.0
    data_offset: int = 0
    axis1: AxisSpec = field(default_factory=AxisSpec)
    axis2: AxisSpec = field(default_factory=AxisSpec)
    alt_description: str = ""
    translations: list[str] = field(default_factory=list)
    u32_array: list[int] = field(default_factory=list)  # feature 476
    string_array: list[str] = field(default_factory=list)  # feature 476


@dataclass
class VersionSpec:
    name: str
    description: str = ""


@dataclass
class FolderSpec:
    idx: int
    name: str
    description: str = ""


class Writer:
    """Little-endian writer with WinOLS version-aware string encoding."""

    def __init__(self, version: int) -> None:
        self.version = version
        self.buf = bytearray()

    def has(self, feature: int) -> bool:
        return feature <= self.version

    def raw(self, data: bytes) -> None:
        self.buf += data

    def u8(self, v: int) -> None:
        self.buf += struct.pack("<B", v)

    def u16(self, v: int) -> None:
        self.buf += struct.pack("<H", v)

    def u32(self, v: int) -> None:
        self.buf += struct.pack("<I", v)

    def i32(self, v: int) -> None:
        self.buf += struct.pack("<i", v)

    def f64(self, v: float) -> None:
        self.buf += struct.pack("<d", v)

    def string(self, s: str) -> None:
        enc = s.encode("cp1252")
        self.u32(len(enc))
        if not enc:
            return
        self.raw(enc)
        if not self.has(439):
            self.raw(b"\x00")  # pre-v439 strings carry a NUL terminator

    def lang_string(self, s: str) -> None:
        self.string(s)
        if self.has(345):
            self.u32(0x409)  # lang id

    def mls(self, s: str, translations: list[str] | tuple[str, ...] = ()) -> None:
        if not self.has(330):
            self.string(s)
            return
        self.lang_string(s)
        if self.has(345):
            self.u32(0)  # flags
        self.u32(len(translations))
        for t in translations:
            self.lang_string(t)

    def axis_unit_desc(self, m: MapSpec) -> None:
        self.string(m.unit)
        self.string("")  # alt unit
        self.f64(m.factor)
        self.f64(m.offset)
        self.u32(m.data_offset)
        self.u32(0)  # end offset
        self.u32(0)  # field_4c
        if self.has(264):
            self.f64(0.0)
        if self.has(61):
            self.u32(0)
        if self.has(105):
            self.u32(0)
            self.u32(0)
            self.u32(0)
            self.i32(-1)

    def axis(self, a: AxisSpec) -> None:
        self.mls(a.description, a.translations)
        self.string(a.id)
        self.f64(a.factor)
        self.f64(a.offset)
        self.u32(0)  # type code
        self.u32(a.address)
        self.u32(a.type_kind)
        self.u32({1: 1, 3: 2, 5: 4, 7: 4}.get(a.type_kind, 2))
        self.u32(0x0A)
        self.u8(0)
        self.u8(1)
        if self.has(264):
            self.f64(0.0)
        if self.has(241):
            self.u32(0)
        if self.has(8):
            self.u32(a.data_source)
        if self.has(805):
            self.u32(0)
        if self.has(12):
            self.u8(0)
        if self.has(73):
            self.u32(len(a.blob))
            self.raw(a.blob)
        if self.has(77):
            self.u32(0)
        if self.has(91):
            self.i32(0)
        if self.has(354):
            self.string(a.extra_string)
        if self.has(372):
            self.u32(len(a.extra_texts))
            for t in a.extra_texts:
                self.mls(t)
        if self.has(440):
            self.i32(0)
        if self.has(805):
            for _ in range(4):
                self.u32(0)

    def map_record(self, m: MapSpec) -> None:
        if self.has(268):
            self.u8(0)
        if self.has(282):
            self.u32(0)
        if self.has(287):
            self.mls(m.alt_description)
        if self.has(93):
            self.u8(0)
        self.mls(m.description, m.translations)
        self.u32(m.type_code)
        self.u32(0)
        self.u32(m.type_kind)
        self.u32({1: 1, 3: 2, 5: 4, 7: 4}.get(m.type_kind, 2))
        self.u32(0x0A)
        if self.has(80):
            self.u32(m.folder_id)
            self.string(m.name)
        if self.has(298):
            self.u32(0)
        if self.has(299):
            self.u32(0)
        if self.has(94):
            self.u32(0)
        if self.has(74):
            self.u8(0)
        if self.has(123):
            self.u32(0)
        if self.has(300):
            for _ in range(6):
                self.f64(0.0)
        if self.has(67):
            for _ in range(6):
                self.f64(0.0)
        elif self.has(66):
            self.f64(0.0)
            self.f64(0.0)
        elif not self.has(59):
            self.u32(0)
            self.u32(0)
        for _ in range(4):
            self.u8(0)
        self.u32(m.dim1)
        self.u32(m.dim2)
        self.u32(0)
        self.u32(0)
        self.u32(0)
        self.axis_unit_desc(m)
        self.axis(m.axis1)
        self.axis(m.axis2)
        if self.has(58):
            self.u8(0)
        if self.has(68):
            self.u8(0)
        if self.has(90):
            self.u32(0)
        if self.has(9):
            self.u32(0)
            self.u32(0)
        if self.has(49):
            self.u32(0)
            self.u8(0)
            self.u8(0)
            self.f64(0.0)
            self.f64(0.0)
        if self.has(51):
            self.u32(0)
        if self.has(53):
            self.u8(0)
            self.f64(0.0)
            self.f64(0.0)
            self.f64(0.0)
            self.u32(0)
        if self.has(54):
            self.f64(0.0)
        if self.has(55):
            self.u8(0)
            self.u8(0)
            self.u8(0)
        if self.has(315):
            self.u32(0)
        if self.has(383):
            self.i32(0)
            self.raw(bytes(16))
        if self.has(329):
            self.u32(0)
        if self.has(346):
            self.u32(0)
            self.u32(0)
        if self.has(395):
            self.u32(0)
        if self.has(476):
            self.u32(0)
            self.u32(len(m.u32_array))
            for v in m.u32_array:
                self.u32(v)
            self.u32(len(m.string_array))
            for s in m.string_array:
                self.string(s)
        if self.has(503):
            self.u32(0)
            self.f64(0.0)
        if self.has(596):
            self.u32(0)
        if self.has(822):
            self.u32(0)


def header_bytes(version: int, header: dict[str, str] | None = None) -> bytes:
    header = header or {}
    w = Writer(version)
    w.u32(0x0B)
    w.raw(b"WinOLS File\x00")
    w.u16(version)
    w.raw(b"\x00" * 6)  # flags / timestamp
    for name in HEADER_FIELDS:
        enc = header.get(name, "").encode("cp1252")
        w.u32(len(enc))
        w.raw(enc)
    return bytes(w.buf)


def map_list_bytes(version: int, maps: list[MapSpec]) -> bytes:
    w = Writer(version)
    for m in maps:
        w.map_record(m)
    return bytes(w.buf)


def _pad_to(buf: bytearray, offset: int) -> None:
    if len(buf) > offset:
        raise ValueError(f"layout overflow: 0x{len(buf):x} > 0x{offset:x}")
    buf += b"\x00" * (offset - len(buf))


def build_ols(
    version: int = 804,
    *,
    header: dict[str, str] | None = None,
    maps: list[MapSpec] | None = None,
    map_count: int | None = None,
    compressed: bool = False,
    versions: list[VersionSpec] | None = None,
    base_index: int = 0,
    blob_size: int = 0x10000,
    blobs: list[bytes] | None = None,
    folders: list[FolderSpec] | None = None,
    fill: bytes = b"",
    trailer: bytes = b"",
) -> bytes:
    """
    Build a synthetic OLS file.

    Layout: header | version records @0x100 | map list @0x800 |
    binary blobs (4 KiB aligned) | fill | folder entries | trailer | 0xFFFFFFFF.
    ``blobs`` holds the post-checksum payload of each version slot.
    """
    maps = maps or []
    buf = bytearray(header_bytes(version, header))
    _pad_to(buf, 0x100)

    version_rec_pos = len(buf)
    versions = versions or []
    if versions:
        # Placeholder for blob offset; patched once the blobs are placed.
        base = versions[0]
        name = base.name.encode("cp1252")
        desc = base.description.encode("cp1252")
        buf += struct.pack("<IIII", base_index, 0, blob_size, BLOB_MARKER)
        buf += struct.pack("<II", len(versions), len(name)) + name
        buf += struct.pack("<I", len(desc)) + desc
        for v in versions[1:]:
            name = v.name.encode("cp1252")
            desc = v.description.encode("cp1252")
            buf += struct.pack("<II", 0xFFFFFFFF, len(name)) + name
            buf += struct.pack("<I", len(desc)) + desc

    _pad_to(buf, 0x800)
    body = map_list_bytes(version, maps)
    count = len(maps) if map_count is None else map_count
    buf += struct.pack("<IBI", MAP_LIST_MARKER, 1 if compressed else 0, count)
    buf += zlib.compress(body) if compressed else body

    if versions:
        blob_offset = (len(buf) + 0x1000) & ~0xFFF
        _pad_to(buf, blob_offset)
        payloads = blobs or []
        for i in range(len(versions)):
            payload = payloads[i] if i < len(payloads) else b""
            payload = payload[: blob_size - 4]
            payload += b"\x00" * (blob_size - 4 - len(payload))
            buf += struct.pack("<I", BLOB_CHECKSUM) + payload
        struct.pack_into("<I", buf, version_rec_pos + 4, blob_offset)

    buf += fill
    for f in folders or []:
        text = f'{f.name}: "{f.description}"'.encode("cp1252")
        buf += struct.pack("<IIIII", 0xFFFFFFFF, 0, f.idx, 0, len(text)) + text

    buf += trailer
    buf += b"\xff\xff\xff\xff"
    return bytes(buf)


@dataclass
class KPRecord:
    name: str
    description: str = ""
    type_code: int = 2
    cols: int = 1
    rows: int = 1
    data_offset: int = 0x10000
    axis_address: int = 0


def kp_intern_bytes(records: list[KPRecord]) -> bytes:
    """
    Build a KP "intern" payload.

    The reader scans for record headers in 4-byte steps and, after a hit,
    resumes 0x100 bytes past the name; records are placed on that scan grid
    so every one of them is found.
    """
    buf = bytearray(b"\x00" * 64)
    phase = 0
    for r in records:
        desc = r.description.encode("cp1252")
        desc += b"\x00" * (-len(desc) % 4)  # keep the length field on the grid
        buf += b"\x00" * ((phase - len(buf)) % 4)
        if r.description:
            buf += struct.pack("<I", len(desc)) + desc
        name = r.name.encode("ascii")
        meta = (r.type_code, 0, 0, 0, r.cols, r.rows)
        buf += struct.pack("<6I", *meta)
        buf += struct.pack("<I", len(name)) + name
        phase = len(buf) % 4
        post = bytearray(0x110)
        struct.pack_into("<I", post, 0x9C, r.data_offset)
        struct.pack_into("<I", post, 0xA0, r.axis_address)
        buf += post
        buf += b"\x00" * 0x40
    buf += b"\x00" * 0x200
    return bytes(buf)


def build_kp(
    version: int = 600,
    *,
    header: dict[str, str] | None = None,
    records: list[KPRecord] | None = None,
    intern: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buf = bytearray(header_bytes(version, header))
    _pad_to(buf, 0x200)
    payload = intern if intern is not None else kp_intern_bytes(records or [])
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=compression) as zf:
        zf.writestr("intern", payload)
    buf += zbuf.getvalue()
    return bytes(buf)
