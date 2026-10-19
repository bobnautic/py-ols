"""
OLS File Reader - Parse WinOLS .ols and .kp (Kennfeld Pack) files

This module provides a clean interface for reading WinOLS .ols files and
.kp (map pack) files, extracting parameter definitions, addresses, and
embedded binary data.

OLS files are proprietary project files used by WinOLS ECU tuning software.
They contain vehicle metadata, parameter definitions with addresses, and
optionally embedded binary data.

KP files (Kennfeld Pack / Map Pack) are a variant used for sharing map
definitions. They use ZIP compression for parameter storage and contain
direct file offsets rather than embedded binary data.

Supported OLS Versions:
    - v250-285: Old format (limited binary version support)
    - v300-399: Intermediate format (parent ID based versions)
    - v400-596: Standard format (parent ID based versions)
    - v597+:    Multi-version format (edit history with version slots)
    - v597+ KP: Map pack format with ZIP-compressed parameters
    - v804+:    WinOLS 5 format (15 metadata strings, derived version records)

The reader is pure Python with no dependencies.  Input files are treated as
untrusted: every read is bounds-checked, decompression is capped by
``max_decompressed_size`` and malformed data never raises anything other
than :class:`OLSError` (non-WinOLS input) or :class:`OLSLimitError`
(resource limit exceeded).  Recoverable problems are reported in
:attr:`OLSFile.warnings`.

Usage:
    from ols_reader import read_ols, OLSReader

    # Quick read (works for both .ols and .kp files)
    ols = read_ols("file.ols")
    print(f"Parameters: {len(ols.parameters)}")

    # KP files contain direct file offsets
    ols = read_ols("mappack.kp")
    for param in ols.parameters:
        print(f"{param.name}: @0x{param.data_offset:x}")

    # With binary extraction (OLS files only)
    reader = OLSReader("file.ols")
    ols = reader.parse()
    for version in ols.binary_versions:
        data = reader.extract_binary(version)

    # Parse from memory (e.g. an upload)
    ols = OLSReader.from_bytes(payload, filename="upload.ols").parse()
"""

from __future__ import annotations

import codecs
import encodings.cp1252
import io
import logging
import math
import os
import re
import struct
import sys
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

__version__ = "1.2.0"

__all__ = [
    "AxisInfo",
    "BinaryVersion",
    "CALBlock",
    "DEFAULT_MAX_DECOMPRESSED_SIZE",
    "FolderEntry",
    "OLSError",
    "OLSFile",
    "OLSFormatError",
    "OLSLimitError",
    "OLSReader",
    "Parameter",
    "main",
    "read_ols",
]

logger = logging.getLogger(__name__)

#: Upper bound for data inflated from a compressed map list or a KP archive.
#: Guards against decompression bombs; raise it via ``max_decompressed_size``
#: if a legitimate file needs more.
DEFAULT_MAX_DECOMPRESSED_SIZE = 256 * 1024 * 1024


class OLSError(ValueError):
    """Base class for all errors raised while reading WinOLS files."""


class OLSFormatError(OLSError):
    """The input is not a WinOLS .ols/.kp file."""


class OLSLimitError(OLSError):
    """The input exceeds a resource limit (e.g. a decompression bomb)."""


# ---------------------------------------------------------------------------
# Binary layout helpers (pre-compiled once at import time)
# ---------------------------------------------------------------------------

_U16 = struct.Struct('<H').unpack_from
_U32 = struct.Struct('<I').unpack_from
_U32X2 = struct.Struct('<2I').unpack_from
_U32X3 = struct.Struct('<3I').unpack_from
_U32X6 = struct.Struct('<6I').unpack_from
_UNIT_DESC = struct.Struct('<ddI').unpack_from  # factor, offset, data_offset
_AXIS_DESC = struct.Struct('<ddIII').unpack_from  # factor, offset, type, address, kind

_OLS_MAGIC = 0x0B  # length of the "WinOLS File" signature string
_OLS_SIGNATURE = "WinOLS File"
_BLOB_MARKER = struct.pack('<I', 0x42007899)
_MAP_LIST_MARKER = struct.pack('<I', 0x11883377)
_DERIVED_MARKER = b'\xff\xff\xff\xff'
_FOLDER_MARKER = b'\xff\xff\xff\xff\x00\x00\x00\x00'  # marker + zero padding
_ZIP_SIGNATURE = b'PK\x03\x04'
_KP_ENTRY = 'intern'

_CAS_RE = re.compile(rb'CAS[A-Z][A-Z0-9]{4}')
_EPK_RE = re.compile(rb'S[A-Z][0-9A-Z]{5}\x00')
_EPK_TEXT_RE = re.compile(r'(S[A-Z][0-9A-Z]{5})')

# CAL block: at most 2 MiB, terminated by more than 32 KiB of 0xFF fill
_CAL_MAX_SIZE = 0x200000
_CAL_MIN_SIZE = 0x10000
_CAL_FILL_RUN = b'\xff' * 0x8001
_CAL_ALIGN = 0x1000

# Value-type code (1/3/5/7) -> data type name.  OLS does not store signedness.
_TYPE_KINDS = {1: "UBYTE", 3: "UWORD", 5: "ULONG", 7: "FLOAT32"}
_PARAM_TYPES = {1: "VALUE", 2: "VALUE", 3: "CURVE", 4: "MAP", 5: "MAP"}

# EPK prefix -> CAL offset inside a full flash image
_CAL_OFFSETS_BY_EPK = (
    (('SCG',), 0x820000),  # Simos18.10 (Aurix TC29x)
    (('SC8',), 0x800000),  # Simos18.1 (TC1793)
    (('SC1', 'SA3'), 0x40000),  # Simos12 (TC1797)
    (('SC4',), 0x100000),  # Simos16
    (('S82', 'S85'), 0x40000),  # MED17
)

# Candidate KP record header: type code 2..20, four u32 values < 1024, any
# u32, then a name length of 4..59.  The exact checks happen in Python; the
# regex only lets the C regex engine skip the bytes that cannot match.
_KP_HEADER_RE = re.compile(
    rb'[\x02-\x14]\x00\x00\x00'
    rb'(?:[\x00-\xff][\x00-\x03]\x00\x00){4}'
    rb'[\x00-\xff]{4}'
    rb'[\x04-\x3b]\x00\x00\x00'
)

_UNSET: Any = object()

_CP1252_TABLE = encodings.cp1252.decoding_table


def _decode_cp1252(raw: bytes) -> str:
    """
    Decode Windows-1252 text (the encoding of all OLS strings).

    Same result as ``raw.decode('cp1252', errors='replace')`` but several
    times faster: ASCII takes the C fast path and everything else skips the
    codec registry lookup.
    """
    if raw.isascii():
        return raw.decode('ascii')
    # typeshed omits str decoding tables, which encodings.cp1252 itself uses
    return codecs.charmap_decode(raw, 'replace', _CP1252_TABLE)[0]  # type: ignore[arg-type]


@dataclass
class FolderEntry:
    """Folder/function entry from ols structure"""
    idx: int  # Folder index (matches folder_id in parameters)
    name: str  # Folder name (e.g., "ACQ_KNK_sensor_signal_gen")
    description: str = ""  # Description (e.g., "Acquisition of knock sensor signal (gen)")


@dataclass
class AxisInfo:
    """Axis definition for CURVE/MAP parameters"""
    points: int
    address: int = 0  # CAL offset/address for axis data
    offset: float = 0.0  # Conversion offset (physical = raw * factor + offset)
    unit: str = ""
    factor: float = 1.0  # Conversion factor (physical = raw * factor + offset)
    id: str = ""  # Axis ID (e.g., "n_32", "_CNV_A_R_LINEAR_____33_CM")
    description: str = ""  # Axis description (e.g., "Engine speed -Resolution 32 rpm")
    data_source: int = 0  # Data source type (1=values, 2=eeprom, etc.)
    data_type: str = "UWORD"  # Data type (UBYTE, UWORD, etc.)


@dataclass
class Parameter:
    """ECU parameter definition extracted from OLS file"""
    name: str
    description: str = ""
    param_type: str = "VALUE"  # VALUE, CURVE, MAP, or MAP_INVERSE
    data_type: str = "UWORD"  # UBYTE, SBYTE, UWORD, SWORD, ULONG, SLONG
    cols: int = 1
    rows: int = 1
    data_offset: int = 0  # CAL offset for parameter data
    x_axis: Optional[AxisInfo] = None
    y_axis: Optional[AxisInfo] = None
    unit: str = ""
    factor: float = 1.0  # Conversion factor (physical = raw * factor + offset)
    offset: float = 0.0  # Conversion offset
    folder_id: int = 0  # Folder/category ID (see OLSFile.folders)
    type_code: int = 0  # Raw type code from metadata (2=VALUE, 3=CURVE, 4=MAP, 5=MAP_INVERSE)


@dataclass
class BinaryVersion:
    """
    Binary version entry (e.g., "Original", "NOCS", "Daten")

    For standard format (v300-596):
        - parent_id indicates the parent folder (typically 1)
        - blob_offset is the direct file offset to binary data

    For multi-version format (v597+):
        - version_index is the slot index (0-based)
        - version_count is total edit history depth
        - blob_offset is calculated from: blob_base + version_index * blob_size

    Verification:
        - epk: Identification string found in binary (e.g., "SC1C910", "SC8F830")
        - epk_offset: Offset relative to blob_offset where EPK was found
    """
    name: str
    description: str = ""  # Import path/creation description
    parent_id: Optional[int] = None  # None = root folder (0x003fffff marker)
    is_root: bool = False
    blob_offset: int = 0  # File offset where binary blob starts
    blob_size: int = 0  # Binary blob size in bytes
    version_index: int = 0  # Version index (0-based) for multi-version files (v597+)
    version_count: int = 0  # Total number of version slots (v597+)
    epk: str = ""  # EPK/identification string found in binary
    epk_offset: int = 0  # Offset relative to blob_offset where EPK was found


@dataclass
class CALBlock:
    """Embedded calibration binary data"""
    offset: int  # File offset where CAL starts
    cas_id: str  # CAS identifier (e.g., "CASC2E55")
    epk: str  # EPK string (e.g., "SC8F830")
    data: bytes = b""


@dataclass
class OLSFile:
    """Parsed OLS file contents"""
    filename: str
    version: int = 0  # OLS format version (e.g., 250, 440, 478, 804)
    make: str = ""
    model: str = ""
    engine: str = ""
    year: str = ""
    fuel_type: str = ""
    displacement: str = ""
    power: str = ""
    gearbox: str = ""
    memory_type: str = ""
    ecu_manufacturer: str = ""
    ecu_type: str = ""
    sw_number: str = ""
    hw_number: str = ""
    hw_code: str = ""
    user: str = ""
    parameters: list[Parameter] = field(default_factory=list)
    binary_versions: list[BinaryVersion] = field(default_factory=list)
    cal_block: Optional[CALBlock] = None
    is_kp_file: bool = False  # True if this is a KP (Kennfeld Pack) file
    big_endian_data: bool = False  # True if binary data should be read as big-endian (common for KP/DSG)
    folders: list[FolderEntry] = field(default_factory=list)  # Folder/Function entries
    warnings: list[str] = field(default_factory=list)  # Recoverable problems found while parsing

    def to_dict(self, *, include_cal_data: bool = False) -> dict[str, Any]:
        """
        Return the parsed contents as plain dicts/lists (e.g. for ``json.dumps``).

        The CAL block's raw bytes are replaced by their ``size``; pass
        ``include_cal_data=True`` to also get them as a hex string.
        """
        result = asdict(self)
        cal = result["cal_block"]
        if cal is not None:
            raw = cal.pop("data")
            cal["size"] = len(raw)
            if include_cal_data:
                cal["data"] = raw.hex()
        return result


# ---------------------------------------------------------------------------
# Map record reader
# ---------------------------------------------------------------------------

class _MapRecordReader:
    """
    Sequential reader for WinOLS map records (COlsMap::ReadFromFile).

    A record is a sequence of fields; every field introduced with format
    version N is only present when ``N <= file version`` ("feature N").  The
    field order is documented in ``__init__`` where the per-version layout is
    computed once: runs of fixed-size fields that the reader does not need
    collapse into a single bounds-checked jump, so a record costs about a
    dozen operations instead of ~100 individual reads.

    Error semantics are those of a field-by-field reader: ``EOFError`` when a
    field extends past the buffer, ``ValueError`` for an implausible length
    or element count.  Counts that cannot possibly fit into the remaining
    data are rejected up front instead of being iterated.
    """

    __slots__ = (
        'buf', 'end', 'str_pad', 'mls', 'lang', 'min_lang_str', 'min_mls',
        'head', 'alt_desc', 'pre_desc', 'named', 'mid', 'unit_fixed',
        'axis_fixed', 'axis_source', 'axis_array', 'axis_mid', 'axis_string',
        'axis_texts', 'axis_tail', 'tail', 'tail_arrays', 'tail2',
    )

    def __init__(self, buf: bytes, version: int) -> None:
        def has(feature: int) -> bool:
            return feature <= version

        self.buf = buf
        self.end = len(buf)

        # string:   u32 length + bytes (+ NUL terminator before v439)
        # mls:      string [u32 lang_id, u32 flags (v345+)] u32 count
        #           + count * (string [u32 lang_id (v345+)])       (v330+)
        self.str_pad = 0 if has(439) else 1
        self.mls = has(330)
        self.lang = 4 if has(345) else 0
        self.min_lang_str = 4 + self.lang
        self.min_mls = 4 + 2 * self.lang + 4 if self.mls else 4

        # --- record head ----------------------------------------------------
        #   [268: u8] [282: u32] [287: mls alt_description] [93: u8]
        #   mls description
        #   u32 type_code, u32, u32 type_kind (1/3/5/7), u32 byte width,
        #   u32 legacy (always 0x0a)
        #   [80: u32 folder_id, string name]
        self.head = (1 if has(268) else 0) + (4 if has(282) else 0)
        self.alt_desc = has(287)
        self.pre_desc = 1 if has(93) else 0
        self.named = has(80)

        #   [298: u32] [299: u32] [94: u32] [74: bool] [123: u32 (file mode 1)]
        #   [300: 6 x f64] then [67: 6 x f64 | 66: 2 x f64 | <59: 2 x u32]
        #   4 x bool, then u32 dim1, u32 dim2, 3 x u32
        mid = ((4 if has(298) else 0) + (4 if has(299) else 0)
               + (4 if has(94) else 0) + (1 if has(74) else 0)
               + (4 if has(123) else 0) + (48 if has(300) else 0))
        if has(67):
            mid += 48
        elif has(66):
            mid += 16
        elif not has(59):
            mid += 8
        self.mid = mid + 4

        # --- axis unit descriptor --------------------------------------------
        #   string unit, string alt_unit, f64 factor, f64 offset,
        #   u32 data_offset, u32 end_offset, u32 field_4c,
        #   [264: f64] [61: u32] [105: 3 x u32, i32]
        self.unit_fixed = (28 + (8 if has(264) else 0) + (4 if has(61) else 0)
                           + (16 if has(105) else 0))

        # --- axis data block (COlsAxisData_ReadFromFile, Ghidra: 0x003d1684) --
        #   mls description, string axis_id, f64 factor, f64 offset,
        #   u32 type_code (axis+0x64), u32 address (axis+0x70),
        #   u32 type_kind 1/3/5/7 (axis+0x74), u32 byte width (axis+0x78),
        #   u32 data_format (axis+0x7c, always 0xa), bool, bool,
        #   [264: f64] [241: u32] [8: u32 data_source] [805: u32] [12: bool]
        #   [73: u32 count + bytes] [77: u32] [91: i32] [354: string]
        #   [372: u32 count + count * mls] [440: i32] [805: 4 x u32]
        # The breakpoint COUNT is not stored here; it comes from the record's
        # dimensions (cols for X, rows for Y).
        fixed = 36 + 2 + (8 if has(264) else 0) + (4 if has(241) else 0)
        self.axis_source = fixed if has(8) else -1
        self.axis_fixed = (fixed + (4 if has(8) else 0) + (4 if has(805) else 0)
                           + (1 if has(12) else 0))
        self.axis_array = has(73)
        self.axis_mid = (4 if has(77) else 0) + (4 if has(91) else 0)
        self.axis_string = has(354)
        self.axis_texts = has(372)
        self.axis_tail = (4 if has(440) else 0) + (16 if has(805) else 0)

        # --- record tail -------------------------------------------------------
        #   [58: bool] [68: bool] [90: u32] [9: 2 x u32]
        #   [49: u32, bool, bool, f64, f64] [51: u32] [53: bool, 3 x f64, u32]
        #   [54: f64] [55: 3 x bool] [315: u32] [383: i32 + 16 bytes]
        #   [329: u32] [346: 2 x u32] [395: u32]
        #   [476: u32, u32 count + count * u32, u32 count + count * string]
        #   [503: u32, f64] [596: u32] [822: u32]
        self.tail = sum(size for feature, size in (
            (58, 1), (68, 1), (90, 4), (9, 8), (49, 22), (51, 4), (53, 29),
            (54, 8), (55, 3), (315, 4), (383, 20), (329, 4), (346, 8), (395, 4),
        ) if has(feature))
        self.tail_arrays = has(476)
        self.tail2 = (12 if has(503) else 0) + (4 if has(596) else 0) + (4 if has(822) else 0)

    # -- primitives ------------------------------------------------------------

    def skip(self, pos: int, size: int) -> int:
        """Skip ``size`` bytes of fixed-size fields (no-op for size 0)."""
        if size and pos + size > self.end:
            raise EOFError(f"{size} bytes at 0x{pos:x}")
        return pos + size

    # read_str/skip_str are the hottest functions; keep them flat.

    def read_str(self, pos: int) -> tuple[str, int]:
        if pos + 4 > self.end:
            raise EOFError(f"string length at 0x{pos:x}")
        length = _U32(self.buf, pos)[0]
        pos += 4
        if length == 0 or length >= 0xFFFFFFF0:
            return "", pos
        if length > 0x100000:
            raise ValueError(f"string at 0x{pos:x}: length {length}")
        end = pos + length
        if end > self.end:
            raise EOFError(f"string at 0x{pos:x}: need {length}")
        return _decode_cp1252(self.buf[pos:end]).rstrip('\x00'), end + self.str_pad

    def skip_str(self, pos: int) -> int:
        if pos + 4 > self.end:
            raise EOFError(f"string length at 0x{pos:x}")
        length = _U32(self.buf, pos)[0]
        pos += 4
        if length == 0 or length >= 0xFFFFFFF0:
            return pos
        if length > 0x100000:
            raise ValueError(f"string at 0x{pos:x}: length {length}")
        pos += length
        if pos > self.end:
            raise EOFError(f"string at 0x{pos - length:x}: need {length}")
        return pos + self.str_pad

    def _skip_mls_rest(self, pos: int) -> int:
        # lang_id + flags (v345+) and the translation count
        size = 2 * self.lang + 4
        if pos + size > self.end:
            raise EOFError(f"multi-language string at 0x{pos:x}")
        count = _U32(self.buf, pos + size - 4)[0]
        pos += size
        if count:
            if pos + count * self.min_lang_str > self.end + 1:
                raise EOFError(f"{count} translations at 0x{pos:x}")
            lang = self.lang
            for _ in range(count):
                pos = self.skip(self.skip_str(pos), lang)
        return pos

    def read_mls(self, pos: int) -> tuple[str, int]:
        text, pos = self.read_str(pos)
        return text, (self._skip_mls_rest(pos) if self.mls else pos)

    def skip_mls(self, pos: int) -> int:
        pos = self.skip_str(pos)
        return self._skip_mls_rest(pos) if self.mls else pos

    def _skip_byte_array(self, pos: int) -> int:
        if pos + 4 > self.end:
            raise EOFError(f"array length at 0x{pos:x}")
        count: int = _U32(self.buf, pos)[0]
        pos += 4
        if count == 0 or count >= 0xFFFFFFF0:
            return pos
        if count > 0x1000000:
            raise ValueError(f"array at 0x{pos:x}: count {count}")
        if pos + count > self.end:
            raise EOFError(f"array at 0x{pos:x}: need {count}")
        return pos + count

    # -- axis blocks -------------------------------------------------------------

    def _skip_axis_rest(self, pos: int) -> int:
        if self.axis_array:
            pos = self._skip_byte_array(pos)
        pos = self.skip(pos, self.axis_mid)
        if self.axis_string:
            pos = self.skip_str(pos)
        if self.axis_texts:
            if pos + 4 > self.end:
                raise EOFError(f"axis text count at 0x{pos:x}")
            count = _U32(self.buf, pos)[0]
            pos += 4
            if count:
                if pos + count * self.min_mls > self.end + 1:
                    raise EOFError(f"{count} axis texts at 0x{pos:x}")
                for _ in range(count):
                    pos = self.skip_mls(pos)
        return self.skip(pos, self.axis_tail)

    def read_axis(self, pos: int) -> tuple[AxisInfo, int]:
        description, pos = self.read_mls(pos)
        axis_id, pos = self.read_str(pos)
        if pos + self.axis_fixed > self.end:
            raise EOFError(f"axis data at 0x{pos:x}")
        factor, offset, _, address, type_kind = _AXIS_DESC(self.buf, pos)
        source = self.axis_source
        data_source = _U32(self.buf, pos + source)[0] if source >= 0 else 0
        pos = self._skip_axis_rest(pos + self.axis_fixed)
        return AxisInfo(
            points=0,
            address=address,
            offset=offset,
            factor=factor,
            id=axis_id,
            description=description,
            data_source=data_source,
            data_type=_TYPE_KINDS.get(type_kind, "UWORD"),
        ), pos

    def skip_axis(self, pos: int) -> int:
        pos = self.skip_str(self.skip_mls(pos))
        return self._skip_axis_rest(self.skip(pos, self.axis_fixed))

    # -- record ------------------------------------------------------------------

    def read_map(self, pos: int) -> tuple[Parameter, int]:
        """Read the map record at ``pos``; return it and the next position."""
        buf = self.buf
        end = self.end

        pos = self.skip(pos, self.head)
        if self.alt_desc:
            pos = self.skip_mls(pos)
        pos = self.skip(pos, self.pre_desc)
        description, pos = self.read_mls(pos)

        if pos + 20 > end:
            raise EOFError(f"map type at 0x{pos:x}")
        type_code, _, type_kind = _U32X3(buf, pos)
        pos += 20

        name = ""
        folder_id = 0
        if self.named:
            if pos + 4 > end:
                raise EOFError(f"folder id at 0x{pos:x}")
            folder_id = _U32(buf, pos)[0]
            if folder_id > 1000:
                folder_id = 0
            name, pos = self.read_str(pos + 4)

        if pos + self.mid + 20 > end:
            raise EOFError(f"map dimensions at 0x{pos:x}")
        dim1, dim2 = _U32X2(buf, pos + self.mid)
        pos += self.mid + 20

        unit, pos = self.read_str(pos)
        pos = self.skip_str(pos)  # alt unit
        if pos + self.unit_fixed > end:
            raise EOFError(f"unit descriptor at 0x{pos:x}")
        factor, conv_offset, data_offset = _UNIT_DESC(buf, pos)
        pos += self.unit_fixed

        param_type = _PARAM_TYPES.get(type_code, "MAP" if type_code > 5 else "VALUE")

        # Two axis blocks follow: the first is Y for maps / X for curves.
        x_axis: Optional[AxisInfo] = None
        y_axis: Optional[AxisInfo] = None
        if param_type == "MAP":
            y_axis, pos = self.read_axis(pos)
            x_axis, pos = self.read_axis(pos)
        elif param_type == "CURVE":
            x_axis, pos = self.read_axis(pos)
            pos = self.skip_axis(pos)
        else:
            pos = self.skip_axis(self.skip_axis(pos))

        pos = self.skip(pos, self.tail)
        if self.tail_arrays:
            if pos + 8 > end:
                raise EOFError(f"u32 array at 0x{pos:x}")
            count = _U32(buf, pos + 4)[0]
            pos += 8
            if count:
                if count > 0x100000:
                    raise ValueError(f"u32 array at 0x{pos:x}: count {count}")
                pos = self.skip(pos, 4 * count)
            if pos + 4 > end:
                raise EOFError(f"string array at 0x{pos:x}")
            count = _U32(buf, pos)[0]
            pos += 4
            if count:
                if pos + 4 * count > end + 1:
                    raise EOFError(f"{count} strings at 0x{pos:x}")
                for _ in range(count):
                    pos = self.skip_str(pos)
        pos = self.skip(pos, self.tail2)

        cols = max(1, min(dim1, 0x4000))
        rows = 1
        if type_code > 3:
            rows = max(1, dim2)
            if rows > 0x4000:
                rows = 1
        if x_axis is not None:
            x_axis.points = cols
        if y_axis is not None:
            y_axis.points = rows

        return Parameter(
            name=name,
            description=description,
            param_type=param_type,
            data_type=_TYPE_KINDS.get(type_kind, "UWORD"),
            cols=cols,
            rows=rows,
            data_offset=data_offset,
            x_axis=x_axis,
            y_axis=y_axis,
            unit=unit,
            factor=factor,
            offset=conv_offset,
            folder_id=folder_id,
            type_code=type_code,
        ), pos


class OLSReader:
    """
    Reader for WinOLS .ols project files.

    Supports OLS format versions 100 through 834+, including:
    - Sequential parameter extraction (reads fields in WinOLS order)
    - Binary version extraction (Original, edit history)
    - CAL block extraction (Simos-specific)

    Usage:
        reader = OLSReader("file.ols")
        ols = reader.parse()

        # Access parameters
        for param in ols.parameters:
            print(f"{param.name}: offset={param.data_offset}")

        # Extract binary data
        for version in ols.binary_versions:
            data = reader.extract_binary(version)

    Attributes:
        filepath: Path to the OLS file
        data: Raw file bytes
        version: OLS format version (set during parsing)
        max_decompressed_size: Limit for decompressed map lists / KP archives
    """

    # Format version thresholds (for binary version / folder detection only)
    NEW_FORMAT_VERSION = 300
    NEWER_FORMAT_VERSION = 400

    def __init__(
        self,
        filepath: str | os.PathLike[str],
        *,
        max_decompressed_size: int = DEFAULT_MAX_DECOMPRESSED_SIZE,
    ) -> None:
        self.filepath = Path(filepath)
        self.data = self.filepath.read_bytes()
        self._setup(max_decompressed_size)

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        filename: str = "<memory>",
        *,
        max_decompressed_size: int = DEFAULT_MAX_DECOMPRESSED_SIZE,
    ) -> OLSReader:
        """Create a reader for an in-memory file (``filename`` is informational)."""
        reader = cls.__new__(cls)
        reader.filepath = Path(filename)
        reader.data = bytes(data)
        reader._setup(max_decompressed_size)
        return reader

    def _setup(self, max_decompressed_size: int) -> None:
        if max_decompressed_size <= 0:
            raise ValueError("max_decompressed_size must be positive")
        self.max_decompressed_size = max_decompressed_size
        self.pos = 0
        self.version = 0  # Will be set during header parsing
        self._warnings: list[str] = []
        self._cas_epk: Optional[tuple[int, str]] = _UNSET

    def parse(self) -> OLSFile:
        """
        Parse the OLS file and return structured data.

        Raises:
            OLSFormatError: the data is not a WinOLS file.
            OLSLimitError: compressed content exceeds ``max_decompressed_size``.
        """
        ols = OLSFile(filename=self.filepath.name)
        self._warnings = ols.warnings

        # Parse header for vehicle metadata
        self._parse_header(ols)
        ols.version = self.version

        # KP (Kennfeld Pack) files keep their parameters in a ZIP archive
        archive = self._open_kp_archive()
        if archive is not None:
            ols.is_kp_file = True
            ols.big_endian_data = True  # KP files typically have big-endian data
            with archive:
                intern = self._read_kp_intern(archive)
            if intern is not None:
                ols.parameters = self._extract_kp_parameters(intern)
            # KP files don't have embedded binary versions or CAL blocks
            return ols

        # Extract binary versions (Daten, Original, NOCS, etc.)
        ols.binary_versions = self._extract_binary_versions()

        # Extract parameters with addresses
        ols.parameters = self._extract_parameters()

        # Find and extract CAL block
        ols.cal_block = self._extract_cal_block()

        ols.folders = self._extract_folder_entries()

        return ols

    def _warn(self, message: str) -> None:
        logger.debug("%s: %s", self.filepath.name, message)
        self._warnings.append(message)

    # -------------------------------------------------------------------
    # KP (Kennfeld Pack) files
    # -------------------------------------------------------------------

    def _open_kp_archive(self) -> Optional[zipfile.ZipFile]:
        """
        Open the ZIP archive of a KP (Kennfeld Pack) file.

        KP files are v597+ OLS files that contain ZIP-compressed parameter data
        instead of raw parameter records. They're identified by:
        - Version >= 597
        - ZIP signature (PK\\x03\\x04) present after header
        - Contains "intern" file in the ZIP archive

        Returns None for anything that is not a KP file.
        """
        if self.version < 597:
            return None

        # Look for ZIP signature after header (typically around 0x400-0x500)
        zip_pos = self.data.find(_ZIP_SIGNATURE, 0x100, 0x1000)
        if zip_pos < 0:
            return None

        try:
            archive = zipfile.ZipFile(io.BytesIO(self.data[zip_pos:]))
        except Exception:  # any failure simply means "not a KP archive"
            return None
        if _KP_ENTRY not in archive.namelist():
            archive.close()
            return None
        return archive

    def _read_kp_intern(self, archive: zipfile.ZipFile) -> Optional[bytes]:
        """Read the "intern" entry, refusing to inflate more than the limit."""
        info = archive.getinfo(_KP_ENTRY)
        if info.file_size > self.max_decompressed_size:
            raise OLSLimitError(
                f"{self.filepath.name}: KP entry '{_KP_ENTRY}' would inflate to "
                f"{info.file_size} bytes (limit {self.max_decompressed_size})"
            )
        try:
            # ZipExtFile never returns more than info.file_size bytes
            return archive.read(info)
        except Exception as exc:
            self._warn(f"cannot read KP entry '{_KP_ENTRY}': {exc}")
            return None

    def _extract_kp_parameters(self, intern_data: bytes) -> list[Parameter]:
        """
        Extract parameters from the "intern" file of a KP (Kennfeld Pack) file.

        KP files store parameters in a ZIP-compressed "intern" file with structure:
        - Metadata (6 x u32): type_code, and dimension info
        - Name length (u32) + name string
        - Post-name data (~0x9c-0xb0 bytes) containing address

        Records are located by scanning 4-byte steps for a plausible header;
        after a record is found the scan continues 0x100 bytes past its name.
        Addresses in KP files are direct file offsets into the corresponding .bin file.
        """
        parameters: list[Parameter] = []
        found_names: set[str] = set()

        data = intern_data
        limit = len(data) - 300
        search = _KP_HEADER_RE.search
        pos = 0
        while pos < limit:
            # Let the regex engine jump to the next plausible header, then
            # realign with the 4-byte scan grid.
            match = search(data, pos)
            if match is None:
                break
            candidate = match.start()
            if candidate >= limit:
                break
            skew = (candidate - pos) % 4
            if skew:
                pos = candidate + 4 - skew
                continue
            pos = candidate

            meta = _U32X6(data, pos)
            if max(meta[1:5]) < 1000:
                name_len = _U32(data, pos + 24)[0]
                try:
                    name = data[pos + 28:pos + 28 + name_len].decode('ascii').rstrip('\x00')
                except UnicodeDecodeError:
                    name = ""
                clean_name = name.replace('_', '').replace('[', '').replace(']', '').replace('.', '')
                if (name.isprintable() and
                        clean_name.isalnum() and
                        len(name) >= 3 and
                        ('_' in name or '.' in name) and
                        name not in found_names):
                    found_names.add(name)
                    param = self._parse_kp_parameter_record(data, pos, name_len, name, meta)
                    if param:
                        parameters.append(param)
                    # Skip to next record
                    pos = pos + 28 + name_len + 0x100
                    continue
            pos += 4

        return parameters

    def _parse_kp_parameter_record(
        self,
        intern_data: bytes,
        meta_pos: int,
        name_len: int,
        name: str,
        meta: tuple[int, ...]
    ) -> Optional[Parameter]:
        """
        Parse a single parameter record from KP intern file.

        Structure:
        - Metadata at meta_pos (6 x u32)
        - Name at meta_pos + 28
        - Post-name data: ~0xa0 bytes containing address and axis info
        - Address is at variable offset 0x9c-0xb0 from name end
        """
        name_end = meta_pos + 28 + name_len
        size = len(intern_data)

        if name_end + 0xb0 > size:
            return None

        # Determine type from metadata
        type_code = meta[0]
        if type_code == 2:
            param_type = "VALUE"
        elif type_code == 3:
            param_type = "CURVE"
        else:
            param_type = "MAP"

        # Extract dimensions from metadata (meta[4] often contains points/cols info)
        cols = 1
        rows = 1
        if param_type == "CURVE":
            cols = meta[4] if 1 < meta[4] < 100 else 1
        elif param_type == "MAP":
            cols = meta[4] if 1 < meta[4] < 100 else 1
            rows = meta[5] if 1 < meta[5] < 100 else 1

        # Find address - scan range 0x9c-0xb0 from name_end
        # KP addresses are direct file offsets stored as little-endian
        # Typically in 0x60000-0x80000 range for DSG, but allow wider range for other ECUs
        data_offset = 0
        for off in range(0x9c, 0xb0):
            addr_pos = name_end + off
            if addr_pos + 4 <= size:
                candidate = _U32(intern_data, addr_pos)[0]
                if 0x1000 <= candidate < 0x800000:
                    data_offset = candidate
                    break

        # Axis address typically follows the data address
        x_axis = None
        y_axis = None
        if data_offset > 0 and param_type in ("CURVE", "MAP"):
            for off in range(0x9c, 0xb0):
                addr_pos = name_end + off
                if addr_pos + 8 <= size:
                    addr1, addr2 = _U32X2(intern_data, addr_pos)
                    if addr1 == data_offset and 0x1000 <= addr2 < 0x800000:
                        x_axis = AxisInfo(points=cols, address=addr2)
                        if param_type == "MAP" and rows > 1:
                            # Y-axis data typically follows X-axis data
                            y_axis = AxisInfo(points=rows, address=addr2 + cols * 2)
                        break

        # Try to extract description (length-prefixed string before metadata)
        description = ""
        if meta_pos > 50:
            for back in range(4, min(200, meta_pos), 4):
                check_pos = meta_pos - back
                str_len = _U32(intern_data, check_pos)[0]
                if 5 < str_len < 200 and check_pos + 4 + str_len <= meta_pos:
                    desc_bytes = intern_data[check_pos + 4:check_pos + 4 + str_len]
                    desc = _decode_cp1252(desc_bytes).rstrip('\x00').strip()
                    if desc and len(desc) > 5 and all(c.isprintable() or c in ' \t' for c in desc):
                        description = desc
                        break

        return Parameter(
            name=name,
            description=description,
            param_type=param_type,
            data_type="UWORD",  # KP files typically use UWORD (big-endian in the bin file)
            cols=cols,
            rows=rows,
            data_offset=data_offset,
            x_axis=x_axis,
            y_axis=y_axis,
        )

    # -------------------------------------------------------------------
    # Header
    # -------------------------------------------------------------------

    def _read_u16(self, offset: int) -> int:
        """Read unsigned 16-bit little-endian value"""
        return int(_U16(self.data, offset)[0])

    def _read_u32(self, offset: int) -> int:
        """Read unsigned 32-bit little-endian value"""
        return int(_U32(self.data, offset)[0])

    def _read_string(self, offset: int) -> tuple[str, int]:
        """Read length-prefixed string, return (string, bytes_consumed)"""
        if offset + 4 > len(self.data):
            return "", 0
        length = self._read_u32(offset)
        if length == 0 or length > 500:
            return "", 4
        # OLS files use Windows-1252 encoding (Western European), not UTF-8
        s = _decode_cp1252(self.data[offset + 4:offset + 4 + length])
        s = s.rstrip('\x00').strip()
        return s, 4 + length

    def _parse_header(self, ols: OLSFile) -> None:
        """
        Parse OLS file header.

        Header structure:
        - [0-3]   Magic number (0x0b, the length of the signature string)
        - [4-15]  Signature "WinOLS File"
        - [16-17] Version (u16)
        - [18-23] Flags / timestamp
        - [24+]   Length-prefixed strings (up to 15):
                   make, model, engine, year, fuel_type, displacement, power,
                   gearbox, memory_type, ecu_manufacturer, ecu_type, sw_number,
                   hw_number, hw_code, user

        Older OLS versions may have fewer strings (minimum 4).
        v597+ typically have all 15 strings (some may be empty).

        Raises:
            OLSFormatError: magic number or signature do not match.
        """
        if (len(self.data) < 18 or
                self._read_u32(0) != _OLS_MAGIC or
                not self.data[4:16].decode('ascii', errors='replace').startswith(_OLS_SIGNATURE)):
            raise OLSFormatError(f"{self.filepath.name}: not a WinOLS file (missing '{_OLS_SIGNATURE}' header)")

        # Read version (determines structure format)
        self.version = self._read_u16(16)

        # Then length-prefixed strings starting at 0x18
        pos = 0x18

        ols.make, consumed = self._read_string(pos)
        pos += consumed

        ols.model, consumed = self._read_string(pos)
        pos += consumed

        ols.engine, consumed = self._read_string(pos)
        pos += consumed

        ols.year, consumed = self._read_string(pos)
        pos += consumed

        # Extended metadata strings (v597+, up to 11 more)
        # Each is length-prefixed; older files may stop early
        extended_fields = [
            'fuel_type', 'displacement', 'power', 'gearbox',
            'memory_type', 'ecu_manufacturer', 'ecu_type',
            'sw_number', 'hw_number', 'hw_code', 'user',
        ]
        for field_name in extended_fields:
            if pos + 4 >= len(self.data):
                break
            # Stop if the length field looks like non-string data
            # (length-prefixed strings should have length < 200)
            if self._read_u32(pos) > 200:
                break
            val, consumed = self._read_string(pos)
            setattr(ols, field_name, val)
            pos += consumed

    # -------------------------------------------------------------------
    # Binary versions
    # -------------------------------------------------------------------

    def _read_text_field(self, pos: int) -> tuple[str, int]:
        """
        Read an optional u32-length-prefixed description at ``pos``.

        Returns (text, declared_length) where declared_length is 0 when no
        plausible description is present.
        """
        data = self.data
        if pos + 4 > len(data):
            return "", 0
        length = self._read_u32(pos)
        if 0 < length < 500 and pos + 4 + length <= len(data):
            return _decode_cp1252(data[pos + 4:pos + 4 + length]).strip(), length
        return "", 0

    def _extract_binary_versions(self) -> list[BinaryVersion]:
        """
        Extract binary version entries (Original, Daten, NOCS, user-defined, etc.)

        WinOLS uses a single record format across all observed versions (v100..v804+),
        identified by the 0x42007899 class marker:

            Base record:
                [version_index:4] [blob_offset:4] [blob_size:4] [0x42007899:4]
                [version_count:4] [name_len:4] [name] [desc_len:4] [desc]

            Derived record (for additional versions like user edits):
                [0xFFFFFFFF:4] [name_len:4] [name] [desc_len:4] [desc]

            Blob storage layout: blob_offset + i * blob_size for version i.
            file_size == blob_offset + version_count * blob_size + 4 (0xFFFFFFFF trailer).
        """
        data = self.data
        size = len(data)
        versions: list[BinaryVersion] = []

        scan_end = min(0x2000, size - 28)
        marker_pos = data.find(_BLOB_MARKER, 0x100, scan_end)

        while marker_pos >= 0:
            # Base record: marker is at offset +12 from record start
            rec_start = marker_pos - 12
            version_index, blob_offset, blob_size = _U32X3(data, rec_start)
            version_count, name_len = _U32X2(data, rec_start + 16)

            # Validate base record structure
            if (version_index < 20 and
                    0 < version_count <= 20 and
                    version_index < version_count and
                    0x10000 <= blob_size <= 0x10000000 and
                    blob_offset + version_count * blob_size <= size + 4 and
                    0 < name_len < 50 and
                    rec_start + 24 + name_len <= size):
                try:
                    name = data[rec_start + 24:rec_start + 24 + name_len].decode('cp1252')
                except UnicodeDecodeError:
                    name = ""
                if name.isprintable() and name.strip():
                    desc, _ = self._read_text_field(rec_start + 24 + name_len)
                    versions.append(BinaryVersion(
                        name=name.strip(),
                        blob_offset=blob_offset + version_index * blob_size,
                        blob_size=blob_size,
                        version_index=version_index,
                        version_count=version_count,
                        description=desc,
                    ))
                    versions.extend(self._extract_derived_versions(
                        marker_pos + 4, blob_offset, blob_size, version_index, version_count))
                    break

            marker_pos = data.find(_BLOB_MARKER, marker_pos + 4, scan_end)

        # Find EPK/verification info for each binary version
        for v in versions:
            if v.blob_offset > 0 and v.blob_size > 0:
                v.epk, v.epk_offset = self._find_binary_verification(v.blob_offset, v.blob_size)

        return versions

    def _extract_derived_versions(
        self,
        start: int,
        blob_offset: int,
        blob_size: int,
        base_index: int,
        version_count: int,
    ) -> list[BinaryVersion]:
        """Scan up to 0x500 bytes after the base record for derived records."""
        data = self.data
        size = len(data)
        versions: list[BinaryVersion] = []
        search_end = min(start + 0x500, size - 8)
        derived_index = 0  # Next slot to assign

        pos = start
        while pos < search_end:
            # find() only reports markers that start before search_end
            pos = data.find(_DERIVED_MARKER, pos, search_end + 3)
            if pos < 0:
                break
            name_len = self._read_u32(pos + 4)
            if 0 < name_len < 50 and pos + 8 + name_len <= size:
                try:
                    name = data[pos + 8:pos + 8 + name_len].decode('cp1252')
                except UnicodeDecodeError:
                    name = ""
                if name.isprintable() and name.strip():
                    # Skip base record's index
                    if derived_index == base_index:
                        derived_index += 1
                    if derived_index >= version_count:
                        break

                    desc_pos = pos + 8 + name_len
                    desc, desc_len = self._read_text_field(desc_pos)
                    versions.append(BinaryVersion(
                        name=name.strip(),
                        blob_offset=blob_offset + derived_index * blob_size,
                        blob_size=blob_size,
                        version_index=derived_index,
                        version_count=version_count,
                        description=desc,
                    ))
                    derived_index += 1
                    # Skip past this derived record
                    pos = desc_pos + (4 + desc_len if desc else 4)
                    continue
            pos += 1
        return versions

    def _find_binary_verification(self, blob_offset: int, blob_size: int) -> tuple[str, int]:
        """
        Search for EPK/identification string within a binary block.

        First searches at known CAL offsets within the binary, then falls back
        to searching the entire file for CAS/EPK patterns.

        Returns:
            (epk, offset) where offset is relative to blob_offset (or 0 if found outside)
        """
        # Known CAL offsets for different ECU types (from EPK prefix)
        # Simos18.10 (Aurix TC29x): SCG -> 0x820000
        # Simos18.1 (TC1793): SC8 -> 0x800000
        # Simos12 (TC1797): SC1, SA3 -> 0x40000
        # Simos16: SC4 -> 0x100000
        # MED17: S82, S85 -> 0x40000
        cal_offsets = (0x40000, 0x100000, 0x800000, 0x820000, 0x0)

        # Method 1: Search at known CAL offsets within binary
        for cal_off in cal_offsets:
            if cal_off + 20 > blob_size:
                continue

            pos = blob_offset + cal_off
            if pos + 20 > len(self.data):
                continue

            epk, rel_offset = self._extract_epk_from_header(self.data[pos:pos + 20])
            if epk:
                return epk, cal_off + rel_offset

        # Method 2: Search entire file for CAS pattern (for files where CAL is separate)
        found = self._first_cas_epk()
        if found is None:
            return "", 0
        cas_pos, epk = found
        # Use known CAL offset based on EPK prefix
        known_offset = self._get_cal_offset_for_epk(epk)
        if known_offset > 0:
            return epk, known_offset + 8  # +8 for CAS header
        # Calculate offset relative to binary start
        rel_offset = cas_pos - blob_offset + 8
        return epk, rel_offset if rel_offset >= 0 else 0

    def _first_cas_epk(self) -> Optional[tuple[int, str]]:
        """First CAS header in the file followed by a plausible EPK (cached)."""
        if self._cas_epk is _UNSET:
            self._cas_epk = None
            data = self.data
            for match in _CAS_RE.finditer(data):
                # EPK is 8 bytes after CAS
                epk_pos = match.start() + 8
                if epk_pos + 7 <= len(data):
                    epk = data[epk_pos:epk_pos + 7].split(b'\x00')[0].decode('ascii', errors='replace')
                    if len(epk) >= 6 and epk[0] == 'S':
                        self._cas_epk = (match.start(), epk)
                        break
        return self._cas_epk

    def _extract_epk_from_header(self, header: bytes) -> tuple[str, int]:
        """Extract EPK from a 20-byte header area."""
        # Look for CAS header, EPK follows at +8
        cas_match = _CAS_RE.search(header)
        if cas_match:
            epk_pos = cas_match.start() + 8
            if epk_pos + 7 <= len(header):
                epk = header[epk_pos:epk_pos + 7].split(b'\x00')[0].decode('ascii', errors='replace')
                if len(epk) >= 6:
                    return epk, epk_pos

        # Direct EPK search (for files without CAS header)
        epk_match = _EPK_RE.search(header)
        if epk_match:
            return epk_match.group()[:-1].decode('ascii'), epk_match.start()

        return "", 0

    def _get_cal_offset_for_epk(self, epk: str) -> int:
        """Return CAL offset based on EPK prefix (for full bin files), 0 if unknown."""
        epk_upper = epk.upper()
        for prefixes, offset in _CAL_OFFSETS_BY_EPK:
            if epk_upper.startswith(prefixes):
                return offset
        return 0

    # -------------------------------------------------------------------
    # Parameters (map list)
    # -------------------------------------------------------------------

    def _find_map_list_start(self) -> Optional[tuple[bytes, int, int]]:
        """
        Find the map list and return (buffer, first_map_pos, map_count).

        The map list is preceded by checksum 0x11883377, then:
        - compressed (bool/u8)
        - count (u32)
        - map records (zlib-compressed if the flag is set; ``buffer`` is then
          the inflated data and ``first_map_pos`` is 0)

        Returns None if no usable map list is present.
        """
        idx = self.data.find(_MAP_LIST_MARKER, 0x100, 0x20000)
        if idx < 0:
            return None

        pos = idx + 4
        if pos + 5 > len(self.data):
            return None

        compressed = self.data[pos]
        count = self._read_u32(pos + 1)
        first_map = pos + 5

        if compressed:
            raw = self._inflate(first_map)
            if raw is None:
                return None
            return raw, 0, count

        if count == 0 or count > 0x100000:
            return None

        return self.data, first_map, count

    def _inflate(self, start: int) -> Optional[bytes]:
        """Inflate the zlib stream at ``start``, bounded by max_decompressed_size."""
        limit = self.max_decompressed_size
        inflater = zlib.decompressobj()
        try:
            raw = inflater.decompress(memoryview(self.data)[start:], limit + 1)
        except zlib.error as exc:
            self._warn(f"compressed map list is corrupt: {exc}")
            return None
        if len(raw) > limit:
            raise OLSLimitError(
                f"{self.filepath.name}: compressed map list inflates beyond "
                f"{limit} bytes (raise max_decompressed_size to allow it)"
            )
        if not inflater.eof:
            self._warn("compressed map list is truncated")
            return None
        return raw

    def _extract_parameters(self) -> list[Parameter]:
        """
        Extract parameter definitions by reading the map list sequentially.

        Locates the map list via the 0x11883377 checksum marker, then reads
        each map record field-by-field in the exact order WinOLS uses.
        """
        found = self._find_map_list_start()
        if found is None:
            return []

        buf, pos, map_count = found
        read_map = _MapRecordReader(buf, self.version).read_map
        parameters: list[Parameter] = []

        for index in range(map_count):
            try:
                param, pos = read_map(pos)
            except (EOFError, ValueError, struct.error) as exc:
                self._warn(f"map list ends after {index} of {map_count} records: {exc}")
                break
            # Skip Hexdump entries — not real parameters
            if param.description == "Hexdump" and not param.name:
                continue
            parameters.append(param)

        self.pos = pos
        return parameters

    # -------------------------------------------------------------------
    # CAL block and folders
    # -------------------------------------------------------------------

    def _extract_cal_block(self) -> Optional[CALBlock]:
        """
        Extract embedded CAL (calibration) binary data.

        CAL block structure:
        - Identified by "CAS" + 5 alphanumeric chars (e.g., "CASC2E55")
        - EPK string at offset 8 (e.g., "SC8F830")
        - Binary data follows, typically 0.5-2 MB
        - End marked by more than 32 KiB of 0xFF fill or EOF; the size is
          rounded up to a 4 KiB boundary (relative to the CAL start)
        """
        data = self.data
        match = _CAS_RE.search(data)
        if not match:
            return None

        offset = match.start()
        cas_id = data[offset:offset + 8].decode('ascii', errors='replace')

        # EPK is at offset 8, typically 7 chars
        epk_match = _EPK_TEXT_RE.match(data[offset + 8:offset + 30].decode('ascii', errors='replace'))
        epk = epk_match.group(1) if epk_match else ""

        # Find CAL end (large 0xFF section or EOF)
        end = min(offset + _CAL_MAX_SIZE, len(data))
        fill = data.find(_CAL_FILL_RUN, offset + _CAL_MIN_SIZE, end)
        if fill >= 0:
            end = fill

        # Align the CAL size to 4KB
        size = (end - offset + _CAL_ALIGN - 1) // _CAL_ALIGN * _CAL_ALIGN
        end = min(offset + size, len(data))

        return CALBlock(
            offset=offset,
            cas_id=cas_id,
            epk=epk,
            data=data[offset:end],
        )

    def _extract_folder_entries(self) -> list[FolderEntry]:
        """
        Extract folder/function entries from OLS file.

        These entries define the folder structure used to organize parameters.
        The folder index (idx) matches the folder_id in Parameter objects.

        Structure:
        - [ffffffff] Marker (4 bytes)
        - [00000000] Padding (4 bytes)
        - [idx]      Folder index (4 bytes)
        - [00000000] Padding (4 bytes)
        - [strlen]   String length (4 bytes)
        - [string]   Format: "NAME: \\"DESCRIPTION\\""

        Example:
        - __DDS_EXPA2_DEFAULT_FUNC__: "" (root folder)
        - ACQ_KNK_sensor_signal_gen: "Calibration Hint: Acquisition of knock sensor signal (gen)"
        """
        entries: list[FolderEntry] = []
        found_names: set[str] = set()
        data = self.data
        size = len(data)

        # Start from 1/3 into file (folders are typically after CAL block).
        # Searching for marker + zero padding (rather than the bare 0xFFFFFFFF
        # marker) skips the 0xFF fill areas of embedded flash images.
        pos = size // 3
        while True:
            pos = data.find(_FOLDER_MARKER, pos)
            if pos < 0 or pos + 20 > size:
                break

            idx, pad2, strlen = _U32X3(data, pos + 8)
            if pad2 == 0 and 5 < strlen < 300 and idx < 2000 and pos + 20 + strlen <= size:
                text = _decode_cp1252(data[pos + 20:pos + 20 + strlen])

                # Must have the NAME: "DESC" format
                if ': "' in text:
                    name, desc = text.split(': "', 1)

                    # Validate name: alphanumeric with underscores
                    if name.replace('_', '').replace('.', '').isalnum() and name not in found_names:
                        found_names.add(name)
                        entries.append(FolderEntry(idx=idx, name=name, description=desc.rstrip('"')))
                        # Skip past this entry
                        pos += 20 + strlen
                        continue

            pos += 1

        return entries

    # -------------------------------------------------------------------
    # Binary extraction
    # -------------------------------------------------------------------

    def extract_binary(self, version: BinaryVersion) -> bytes:
        """
        Extract binary blob data for a given binary version.

        WinOLS prefixes every binary blob with a 4-byte checksum/marker
        (typically 0x98728833 = `33 88 72 98` little-endian, but other values
        appear in older or non-checksummed files). On read, WinOLS consumes
        this u32 via COlsFile_VerifyChecksum (Ghidra: 0x0064fa64) before any
        blob bytes are exposed. The OLS-stored data_offset for each parameter
        is therefore relative to the post-marker data, so we strip the marker
        here so consumers can use data_offset directly.
        """
        if version.blob_offset == 0 or version.blob_size == 0:
            return b""

        # 4-byte WinOLS checksum/marker at the front of every blob
        start = version.blob_offset + 4
        end = min(version.blob_offset + version.blob_size, len(self.data))
        if start >= end:
            return b""
        return self.data[start:end]


def read_ols(
    filepath: str | os.PathLike[str],
    *,
    max_decompressed_size: int = DEFAULT_MAX_DECOMPRESSED_SIZE,
) -> OLSFile:
    """
    Convenience function to parse an OLS or KP file.

    Args:
        filepath: Path to .ols/.kp file
        max_decompressed_size: Limit for decompressed map lists / KP archives

    Returns:
        OLSFile with parsed contents

    Raises:
        OSError: the file cannot be read.
        OLSFormatError: the file is not a WinOLS file.
        OLSLimitError: compressed content exceeds ``max_decompressed_size``.
    """
    return OLSReader(filepath, max_decompressed_size=max_decompressed_size).parse()


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

_UNSAFE_FILENAME_CHARS = re.compile(r'[^A-Za-z0-9._-]+')


def _safe_filename(name: str, fallback: str = "binary") -> str:
    """Turn an untrusted name from a file into a harmless file name component."""
    cleaned = _UNSAFE_FILENAME_CHARS.sub('_', name).strip('._-')
    return cleaned[:80] or fallback


def _json_safe(value: Any) -> Any:
    """Replace NaN/Infinity (not valid JSON) with None."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _format_summary(ols: OLSFile, limit: Optional[int]) -> list[str]:
    lines = [
        f"File: {ols.filename}",
        f"OLS Version: {ols.version}",
    ]
    if ols.is_kp_file:
        lines.append("Type: KP (Kennfeld Pack / Map Pack)")
        lines.append(f"Data format: {'Big-endian' if ols.big_endian_data else 'Little-endian'}")
    else:
        lines.append("Type: OLS (Project file)")
    lines.append(f"Vehicle: {ols.make} {ols.model} {ols.engine} ({ols.year})")
    if ols.ecu_type:
        ecu_info = ols.ecu_type
        if ols.ecu_manufacturer:
            ecu_info = f"{ols.ecu_manufacturer} {ecu_info}"
        if ols.sw_number:
            ecu_info += f", SW: {ols.sw_number}"
        lines.append(f"ECU: {ecu_info}")
    lines.append(f"Parameters: {len(ols.parameters)}")

    # Count by type
    type_counts: dict[str, int] = {}
    for p in ols.parameters:
        type_counts[p.param_type] = type_counts.get(p.param_type, 0) + 1
    if type_counts:
        lines.append("  By type: " + ", ".join(f"{t}: {c}" for t, c in sorted(type_counts.items())))

    if ols.binary_versions:
        lines.append(f"\nBinary versions ({len(ols.binary_versions)}):")
        for v in ols.binary_versions:
            if v.version_count > 0:
                idx_str = f"idx={v.version_index}/{v.version_count}"
            elif v.is_root:
                idx_str = "ROOT"
            else:
                idx_str = f"parent={v.parent_id}"
            size_str = f" ({v.blob_size / 1024:.0f} KB)" if v.blob_size else ""
            offset_str = f" @{hex(v.blob_offset)}" if v.blob_offset else ""
            epk_str = f" EPK={v.epk}" if v.epk else ""
            lines.append(f"  {v.name} ({idx_str}){size_str}{offset_str}{epk_str}")

    if ols.cal_block:
        lines.append(f"\nCAL: {ols.cal_block.epk} ({len(ols.cal_block.data) / 1024:.1f} KB)")

    if ols.folders:
        lines.append(f"\nFolders ({len(ols.folders)}):")
        for f in ols.folders[:10]:
            desc_str = f": \"{f.description[:40]}\"" if f.description else ""
            lines.append(f"  [{f.idx:4d}] {f.name}{desc_str}")
        if len(ols.folders) > 10:
            lines.append(f"  ... and {len(ols.folders) - 10} more")

    folder_lookup = {f.idx: f.name for f in ols.folders}
    shown = ols.parameters if limit is None else ols.parameters[:limit]
    if shown:
        lines.append("\nParameters:" if limit is None else "\nSample parameters:")
    for p in shown:
        axis_info = ""
        if p.x_axis:
            axis_info = f" [xaxis@{hex(p.x_axis.address)}]"
        if p.y_axis:
            axis_info += f" [yaxis@{hex(p.y_axis.address)}]"
        folder_name = folder_lookup.get(p.folder_id, "")
        folder_str = f" [{folder_name}]" if folder_name else (f" [folder={p.folder_id}]" if p.folder_id else "")
        dims = f" {p.cols}x{p.rows}" if p.param_type != "VALUE" else ""
        factor_str = f" *{p.factor}" if p.factor != 1.0 else ""
        offset_str = f" {p.offset:+}" if p.offset else ""
        unit_str = f" {p.unit}" if p.unit else ""
        lines.append(f"  {p.param_type:5} {p.data_type:7} {p.name}:{dims} @{hex(p.data_offset)}"
                     f"{factor_str}{offset_str}{unit_str}{axis_info}{folder_str}")
    if limit is not None and len(ols.parameters) > limit:
        lines.append(f"  ... and {len(ols.parameters) - limit} more (use --all to list everything)")
    return lines


def _extract_binaries(reader: OLSReader, ols: OLSFile, out_dir: Path, force: bool) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_filename(Path(ols.filename).stem, "file")
    written = []
    for v in ols.binary_versions:
        data = reader.extract_binary(v)
        if not data:
            continue
        target = out_dir / f"{stem}.{v.version_index:02d}.{_safe_filename(v.name)}.bin"
        # "x" refuses to replace existing files (and symlinks) unless --force
        with open(target, "wb" if force else "xb") as fh:
            fh.write(data)
        written.append(target)
    return written


def main(argv: Optional[list[str]] = None) -> int:
    """Command line entry point (``pyols`` / ``python -m ols_reader``)."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="pyols",
        description="Inspect WinOLS .ols project files and .kp map packs.",
    )
    parser.add_argument("files", nargs="+", type=Path, metavar="FILE", help=".ols or .kp file")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print the full parse result as JSON")
    output.add_argument("--all", action="store_true", help="list every parameter")
    output.add_argument("--limit", type=int, default=10, metavar="N",
                        help="number of sample parameters to list (default: 10)")
    parser.add_argument("--extract", type=Path, metavar="DIR",
                        help="write every embedded binary version to DIR")
    parser.add_argument("--force", action="store_true", help="overwrite existing files with --extract")
    parser.add_argument("--max-decompressed-size", type=int, default=DEFAULT_MAX_DECOMPRESSED_SIZE,
                        metavar="BYTES", help="limit for decompressed data (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must not be negative")
    if args.max_decompressed_size <= 0:
        parser.error("--max-decompressed-size must be positive")

    status = 0
    results = []
    for path in args.files:
        try:
            reader = OLSReader(path, max_decompressed_size=args.max_decompressed_size)
            ols = reader.parse()
        except (OSError, OLSError) as exc:
            print(f"pyols: error: {path}: {exc}", file=sys.stderr)
            status = 1
            continue

        for warning in ols.warnings:
            print(f"pyols: warning: {path}: {warning}", file=sys.stderr)
        if args.json:
            results.append(_json_safe(ols.to_dict()))
        else:
            if len(args.files) > 1:
                print(f"==> {path} <==")
            print("\n".join(_format_summary(ols, None if args.all else args.limit)))
            if len(args.files) > 1:
                print()

        if args.extract:
            try:
                for target in _extract_binaries(reader, ols, args.extract, args.force):
                    print(f"wrote {target}", file=sys.stderr)
            except OSError as exc:
                print(f"pyols: error: {path}: cannot extract binaries: {exc}", file=sys.stderr)
                status = 1

    if args.json:
        payload = results[0] if len(args.files) == 1 and results else results
        print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
    return status


# CLI usage
if __name__ == "__main__":
    sys.exit(main())
