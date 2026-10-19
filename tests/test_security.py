"""Hostile and malformed input: the reader must stay bounded and only raise OLSError."""

from __future__ import annotations

import io
import random
import struct
import time
import zipfile
import zlib

import pytest

from ols_reader import OLSError, OLSFormatError, OLSLimitError, OLSReader
from olsbuild import (
    AxisSpec,
    FolderSpec,
    KPRecord,
    MapSpec,
    VersionSpec,
    build_kp,
    build_ols,
    header_bytes,
)

MAP_LIST = 0x800  # where build_ols places the map list marker
FIRST_MAP = MAP_LIST + 9  # marker, compressed flag, count


def parse(data: bytes, **kwargs):
    return OLSReader.from_bytes(data, "fuzz.ols", **kwargs).parse()


@pytest.mark.parametrize("data", [
    b"",
    b"\x0b\x00\x00\x00WinOLS",
    b"\x0b\x00\x00\x00WinOLS Fil\x00\x00\x00\x00",
    b"PK\x03\x04" + bytes(100),
    b"\x0c\x00\x00\x00WinOLS File\x00\x20\x03" + bytes(100),
    bytes(range(256)) * 10,
])
def test_non_ols_input_is_rejected(data: bytes) -> None:
    with pytest.raises(OLSFormatError):
        parse(data)


@pytest.mark.parametrize("cut", range(18, 0x60, 3))
def test_truncated_header_is_tolerated(cut: int) -> None:
    data = build_ols(804, header={"make": "Volkswagen", "model": "Golf", "engine": "2.0 TSI"})
    ols = parse(data[:cut])
    assert ols.version == 804
    assert ols.parameters == []


def zlib_bomb(size: int) -> bytes:
    compressor = zlib.compressobj(9)
    chunk = bytes(1 << 20)
    out = [compressor.compress(chunk) for _ in range(size >> 20)]
    out.append(compressor.flush())
    return b"".join(out)


def test_compressed_map_list_bomb_is_refused() -> None:
    data = bytearray(header_bytes(804))
    data += bytes(0x800 - len(data))
    data += struct.pack("<IBI", 0x11883377, 1, 1) + zlib_bomb(64 << 20)
    with pytest.raises(OLSLimitError):
        parse(bytes(data), max_decompressed_size=16 << 20)
    # the same file is fine once the caller raises the limit
    assert len(parse(bytes(data), max_decompressed_size=128 << 20).parameters) == 1


def test_kp_zip_bomb_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("intern", bytes(64 << 20))
    data = header_bytes(600) + bytes(0x200 - len(header_bytes(600))) + buf.getvalue()
    with pytest.raises(OLSLimitError):
        parse(data, max_decompressed_size=16 << 20)


def test_corrupt_compressed_map_list_warns() -> None:
    data = bytearray(build_ols(804, maps=[MapSpec(name="A_B")], compressed=True))
    data[FIRST_MAP + 2:FIRST_MAP + 8] = b"\xde\xad\xbe\xef\x00\x01"
    ols = parse(bytes(data))
    assert ols.parameters == []
    assert any("compressed map list" in w for w in ols.warnings)


def test_truncated_compressed_map_list_warns() -> None:
    data = build_ols(804, maps=[MapSpec(name="A_B")] * 50, compressed=True)
    ols = parse(data[:FIRST_MAP + 40])
    assert ols.parameters == []
    assert ols.warnings == ["compressed map list is truncated"]


def test_corrupt_kp_archive_warns() -> None:
    data = bytearray(build_kp(600, records=[KPRecord("AB_CD", "desc", 3, 8)], compression=zipfile.ZIP_DEFLATED))
    local_header = data.index(b"PK\x03\x04")
    data[local_header + 40:local_header + 60] = b"\x00" * 20  # damage the deflate stream
    ols = parse(bytes(data))
    assert ols.is_kp_file and ols.parameters == []
    assert any("intern" in w for w in ols.warnings)


@pytest.mark.parametrize("field_offset", [
    17,  # translation count of the alt-description (v804 layout)
])
def test_absurd_counts_are_rejected_without_iterating(field_offset: int) -> None:
    data = bytearray(build_ols(804, maps=[MapSpec(name="A_B")] * 3))
    struct.pack_into("<I", data, FIRST_MAP + field_offset, 0x7FFFFFFF)
    start = time.perf_counter()
    ols = parse(bytes(data))
    assert time.perf_counter() - start < 1.0
    assert ols.parameters == []
    assert "translations" in ols.warnings[0]


def test_absurd_string_length_stops_map_list() -> None:
    data = bytearray(build_ols(804, maps=[MapSpec(name="A_B", description="x")] * 3))
    # description string length of the first record (after head + alt description)
    struct.pack_into("<I", data, FIRST_MAP + 5 + 16 + 1, 0x00200000)
    ols = parse(bytes(data))
    assert ols.parameters == []
    assert "length" in ols.warnings[0]


def test_fill_areas_do_not_slow_down_scans() -> None:
    # 24 MiB of 0xFF (typical flash fill) used to be scanned byte by byte
    data = build_ols(804, maps=[MapSpec(name="A_B")], fill=b"\xff" * (24 << 20),
                     folders=[FolderSpec(1, "F_ONE", "first")])
    start = time.perf_counter()
    ols = parse(data)
    assert time.perf_counter() - start < 3.0
    assert [f.name for f in ols.folders] == ["F_ONE"]


def _corpus() -> list[bytes]:
    maps = [
        MapSpec(name="KFMIRL", description="Sollfüllung", type_code=4, dim1=12, dim2=16, unit="%",
                translations=["load"], axis1=AxisSpec(description="rpm", translations=["n"], blob=b"abc",
                                                      extra_texts=["t"]),
                u32_array=[1, 2], string_array=["s"]),
        MapSpec(name="KLAF", description="curve", type_code=3, dim1=8),
        MapSpec(name="CW", description="value", type_code=2),
    ]
    blob = b"CASC2E55SC8F830\x00" + bytes(64) + b"\xff" * 0x9000
    corpus = []
    for version in (250, 330, 345, 439, 597, 804, 834):
        for compressed in (False, True):
            corpus.append(build_ols(
                version, header={"make": "VW"}, maps=maps, compressed=compressed,
                versions=[VersionSpec("Original"), VersionSpec("Stage1")], blobs=[blob, b"x"],
                folders=[FolderSpec(1, "F_ONE", "first")],
            ))
    corpus.append(build_kp(600, records=[KPRecord("AB_CD", "description", 3, 8, 1, 0x61000, 0x62000)]))
    return corpus


INTERESTING = (0, 1, 0xFF, 0x4001, 0x100000, 0x100001, 0x1000001, 0xFFFFFFF0, 0xFFFFFFEF, 0xFFFFFFFF)


def _mutate(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.choice((1, 2, 4, 16))):
        op = rng.random()
        pos = rng.randrange(len(b))
        if op < 0.4:
            b[pos] = rng.randrange(256)
        elif op < 0.75:
            b[pos:pos + 4] = struct.pack("<I", rng.choice(INTERESTING))
        elif op < 0.85:
            del b[pos:]
        else:
            b[pos:pos] = rng.randbytes(rng.randint(1, 8))
        if not b:
            break
    return bytes(b)


@pytest.mark.parametrize("seed", range(8))
def test_mutation_fuzz_only_raises_ols_errors(seed: int) -> None:
    rng = random.Random(seed)
    corpus = _corpus()
    for _ in range(150):
        data = _mutate(rng, rng.choice(corpus))
        try:
            ols = parse(data, max_decompressed_size=8 << 20)
        except OLSError:
            continue
        for p in ols.parameters:
            assert 1 <= p.cols <= 0x4000 and 1 <= p.rows <= 0x4000
        for v in ols.binary_versions:
            assert 0 <= v.version_index < v.version_count <= 20
