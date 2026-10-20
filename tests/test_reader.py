from __future__ import annotations

import json
import math
import struct

import pytest

import ols_reader
from ols_reader import OLSFile, OLSReader, read_ols
from olsbuild import (
    BLOB_CHECKSUM,
    HEADER_FIELDS,
    AxisSpec,
    FolderSpec,
    KPRecord,
    MapSpec,
    VersionSpec,
    build_kp,
    build_ols,
)

# Every version at which the map record layout changes, plus neighbours.
LAYOUT_FEATURES = [8, 9, 12, 49, 51, 53, 54, 55, 58, 59, 61, 66, 67, 68, 73, 74, 77, 80, 90, 91, 93,
                   94, 105, 123, 241, 264, 268, 282, 287, 298, 299, 300, 315, 329, 330, 345, 346, 354,
                   372, 383, 395, 439, 440, 476, 503, 596, 805, 822]
VERSIONS = sorted({v for f in LAYOUT_FEATURES for v in (f - 1, f)} | {250, 597, 804, 834})


def parse(data: bytes, **kwargs) -> OLSFile:
    return OLSReader.from_bytes(data, "test.ols", **kwargs).parse()


def sample_maps() -> list[MapSpec]:
    return [
        MapSpec(
            name="KFMIRL", description="Sollfüllung", type_code=4, type_kind=3, folder_id=3,
            dim1=12, dim2=16, unit="%", factor=0.0234375, offset=-1.5, data_offset=0x1234,
            translations=["Desired load"], alt_description="alt",
            axis1=AxisSpec(description="Drehzahl", id="n_32", factor=40.0, address=0x2000, type_kind=1,
                           data_source=2, translations=["rpm"], blob=b"\x01\x02\x03", extra_string="x",
                           extra_texts=["t1", "t2"]),
            axis2=AxisSpec(description="Last", id="rl_w", factor=0.1, offset=5.0, address=0x3000, type_kind=5),
            u32_array=[1, 2, 3], string_array=["a", "bb"],
        ),
        MapSpec(name="KLAF", description="A curve", type_code=3, type_kind=7, dim1=8, dim2=99,
                data_offset=0x4000, axis1=AxisSpec(id="ub", address=0x5000)),
        MapSpec(name="", description="Hexdump", type_code=1),
        MapSpec(name="CWLAMBDA", description="Codeword", type_code=2, type_kind=1, data_offset=0x10,
                folder_id=5000, factor=1.0),
    ]


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("compressed", [False, True])
def test_map_records_all_versions(version: int, compressed: bool) -> None:
    ols = parse(build_ols(version, maps=sample_maps(), compressed=compressed))

    assert ols.version == version
    assert ols.warnings == []
    named = version >= 80
    assert [p.name for p in ols.parameters] == (["KFMIRL", "KLAF", "CWLAMBDA"] if named else ["", "", ""])

    kf, curve, value = ols.parameters
    assert (kf.param_type, kf.data_type, kf.cols, kf.rows) == ("MAP", "UWORD", 12, 16)
    assert kf.description == "Sollfüllung"
    assert (kf.data_offset, kf.unit, kf.factor, kf.offset, kf.type_code) == (0x1234, "%", 0.0234375, -1.5, 4)
    assert kf.folder_id == (3 if named else 0)
    # first axis block is Y for maps, second is X
    assert kf.y_axis is not None and kf.x_axis is not None
    assert (kf.y_axis.points, kf.y_axis.address, kf.y_axis.id, kf.y_axis.description) == (16, 0x2000, "n_32", "Drehzahl")
    assert (kf.y_axis.factor, kf.y_axis.data_type) == (40.0, "UBYTE")
    assert kf.y_axis.data_source == (2 if version >= 8 else 0)
    assert (kf.x_axis.points, kf.x_axis.address, kf.x_axis.offset, kf.x_axis.data_type) == (12, 0x3000, 5.0, "ULONG")

    assert (curve.param_type, curve.data_type, curve.cols, curve.rows) == ("CURVE", "FLOAT32", 8, 1)
    assert curve.x_axis is not None and curve.y_axis is None
    assert (curve.x_axis.points, curve.x_axis.address) == (8, 0x5000)

    assert (value.param_type, value.data_type, value.data_offset) == ("VALUE", "UBYTE", 0x10)
    assert value.x_axis is None and value.y_axis is None
    assert value.folder_id == 0  # ids above 1000 are not folder ids


def test_compressed_and_plain_map_lists_agree() -> None:
    maps = sample_maps()
    plain = parse(build_ols(804, maps=maps))
    packed = parse(build_ols(804, maps=maps, compressed=True))
    assert plain.parameters == packed.parameters


@pytest.mark.parametrize("type_code, dims, expected", [
    (0, (5, 9), ("VALUE", 5, 1)),
    (2, (0, 0), ("VALUE", 1, 1)),
    (3, (0x4001, 7), ("CURVE", 0x4000, 1)),
    (4, (0xFFFFFFFF, 0x4001), ("MAP", 0x4000, 1)),
    (5, (3, 0), ("MAP", 3, 1)),
    (9, (2, 4), ("MAP", 2, 4)),
])
def test_type_and_dimension_rules(type_code: int, dims: tuple[int, int], expected: tuple[str, int, int]) -> None:
    ols = parse(build_ols(804, maps=[MapSpec(name="P_X", type_code=type_code, dim1=dims[0], dim2=dims[1])]))
    p = ols.parameters[0]
    assert (p.param_type, p.cols, p.rows) == expected


def test_header_metadata() -> None:
    header = {name: f"{name}-ä€" for name in HEADER_FIELDS}
    ols = parse(build_ols(804, header=header))
    for name in HEADER_FIELDS:
        assert getattr(ols, name) == f"{name}-ä€"


def test_header_stops_at_implausible_string() -> None:
    data = bytearray(build_ols(804, header={"make": "VW", "model": "Golf"}))
    # 5th string (fuel_type) gets an absurd length: parsing of the header stops there
    pos = 0x18 + (4 + 2) + (4 + 4) + 4 + 4
    struct.pack_into("<I", data, pos, 5000)
    ols = parse(bytes(data))
    assert (ols.make, ols.model, ols.fuel_type) == ("VW", "Golf", "")


def test_truncated_map_list_keeps_parsed_records_and_warns() -> None:
    data = build_ols(804, maps=sample_maps())
    full = parse(data)
    # chop the file in the middle of the second record
    cut = data.index(b"KLAF") + 10
    ols = parse(data[:cut])
    assert [p.name for p in ols.parameters] == ["KFMIRL"]
    assert ols.parameters[0] == full.parameters[0]
    assert len(ols.warnings) == 1 and "after 1 of 4 records" in ols.warnings[0]


def test_map_count_larger_than_list_warns() -> None:
    ols = parse(build_ols(804, maps=sample_maps(), map_count=10))
    assert len(ols.parameters) == 3
    assert "after 4 of 10 records" in ols.warnings[0]


def test_missing_map_list() -> None:
    data = build_ols(804, maps=sample_maps()).replace(struct.pack("<I", 0x11883377), b"\0\0\0\0")
    assert parse(data).parameters == []


def test_binary_versions_and_extraction() -> None:
    blobs = [b"ORIGINAL" * 4, b"STAGE1" * 4, b"STAGE2" * 4]
    data = build_ols(
        804,
        versions=[VersionSpec("Original", "import.bin"), VersionSpec("Stage1", "user edit"), VersionSpec("Stage2")],
        base_index=1, blob_size=0x10000, blobs=blobs,
    )
    reader = OLSReader.from_bytes(data)
    ols = reader.parse()

    assert [(v.name, v.version_index, v.version_count, v.description) for v in ols.binary_versions] == [
        ("Original", 1, 3, "import.bin"),
        ("Stage1", 0, 3, "user edit"),
        ("Stage2", 2, 3, ""),
    ]
    base = min(v.blob_offset for v in ols.binary_versions)
    for v in ols.binary_versions:
        assert v.blob_offset == base + v.version_index * 0x10000
        assert v.blob_size == 0x10000
        assert struct.unpack_from("<I", data, v.blob_offset)[0] == BLOB_CHECKSUM
        payload = reader.extract_binary(v)
        assert len(payload) == 0x10000 - 4  # checksum stripped
        assert payload.startswith(blobs[v.version_index])


def test_extract_binary_of_empty_version() -> None:
    reader = OLSReader.from_bytes(build_ols(804))
    reader.parse()
    assert reader.extract_binary(ols_reader.BinaryVersion(name="x")) == b""


def test_epk_found_at_known_cal_offset() -> None:
    blob = bytearray(0x48000)
    blob[0x40000 - 4:0x40000 + 12] = b"CASC2E55SC1C910\x00"  # blob offsets exclude the checksum
    data = build_ols(804, versions=[VersionSpec("Original")], blob_size=0x48000, blobs=[bytes(blob)])
    v = parse(data).binary_versions[0]
    assert (v.epk, v.epk_offset) == ("SC1C910", 0x40000 + 8)


def test_epk_fallback_uses_whole_file_search() -> None:
    # CAS header outside the known offsets: whole-file search, EPK prefix maps to the CAL offset
    blob = b"\x00" * 0x100 + b"CASC2E55SC8F830\x00"
    ols = parse(build_ols(804, versions=[VersionSpec("Original"), VersionSpec("Copy")], blobs=[blob, b""]))
    assert [(v.epk, v.epk_offset) for v in ols.binary_versions] == [("SC8F830", 0x800008)] * 2


def test_cal_block_ends_before_fill_and_is_4k_aligned() -> None:
    cal = b"CASC2E55SC8F830\x00" + b"\x5a" * (0x12345 - 16)
    blob = cal + b"\xff" * 0x9000 + b"\x01" * 16
    data = build_ols(804, versions=[VersionSpec("Original")], blob_size=0x40000, blobs=[blob])
    ols = parse(data)
    block = ols.cal_block
    assert block is not None
    assert (block.cas_id, block.epk) == ("CASC2E55", "SC8F830")
    assert data[block.offset:block.offset + 16] == cal[:16]
    assert len(block.data) == 0x13000  # 0x12345 rounded up to 4 KiB
    assert block.data == data[block.offset:block.offset + 0x13000]


def test_cal_block_size_is_capped() -> None:
    blob = b"CASC2E55SC8F830\x00" + b"\x5a" * 0x280000
    data = build_ols(804, versions=[VersionSpec("Original")], blob_size=0x300000, blobs=[blob])
    block = parse(data).cal_block
    assert block is not None and len(block.data) == 0x200000


def test_no_cal_block() -> None:
    assert parse(build_ols(804, maps=sample_maps())).cal_block is None


def test_folders() -> None:
    folders = [
        FolderSpec(0, "__DDS_EXPA2_DEFAULT_FUNC__", ""),
        FolderSpec(3, "ACQ_KNK_sensor_signal_gen", "Calibration Hint: Acquisition of knock sensor"),
        FolderSpec(4, "ACQ_KNK_sensor_signal_gen", "duplicate name is ignored"),
        FolderSpec(5, "not valid!", "rejected"),
        FolderSpec(2500, "IDX_TOO_LARGE", "rejected"),
        FolderSpec(7, "A.B_C", "dotted"),
    ]
    fill = b"\xff" * 0x20000  # flash-style fill must not confuse the scan
    ols = parse(build_ols(804, maps=sample_maps(), folders=folders, fill=fill))
    assert [(f.idx, f.name, f.description) for f in ols.folders] == [
        (0, "__DDS_EXPA2_DEFAULT_FUNC__", ""),
        (3, "ACQ_KNK_sensor_signal_gen", "Calibration Hint: Acquisition of knock sensor"),
        (7, "A.B_C", "dotted"),
    ]


def test_kp_file() -> None:
    data = build_kp(600, header={"make": "VW"}, records=[
        KPRecord("AB_CD", "Some description", 3, 8, 1, 0x61000, 0x62000),
        KPRecord("XY.Z", "", 4, 6, 7, 0x70000, 0x71000),
        KPRecord("VAL_1", "value", 2, data_offset=0x63000),
        KPRecord("AB_CD", "duplicate", 2),
        KPRecord("noseparator", "rejected", 2),
    ])
    ols = parse(data)
    assert ols.is_kp_file and ols.big_endian_data
    assert ols.make == "VW"
    assert ols.binary_versions == [] and ols.cal_block is None
    assert [(p.name, p.param_type, p.cols, p.rows, p.data_offset, p.description) for p in ols.parameters] == [
        ("AB_CD", "CURVE", 8, 1, 0x61000, "Some description"),
        ("XY.Z", "MAP", 6, 7, 0x70000, ""),
        ("VAL_1", "VALUE", 1, 1, 0x63000, ""),
    ]
    curve, kfmap, _ = ols.parameters
    # axis addresses live in .address; .offset is the conversion offset
    assert curve.x_axis is not None and (curve.x_axis.address, curve.x_axis.offset) == (0x62000, 0.0)
    assert kfmap.x_axis is not None and kfmap.x_axis.address == 0x71000
    assert kfmap.y_axis is not None and kfmap.y_axis.address == 0x71000 + 6 * 2


def test_kp_detection_requires_v597() -> None:
    ols = parse(build_kp(596, records=[KPRecord("AB_CD", "x", 3, 8)]))
    assert not ols.is_kp_file and ols.parameters == []


def test_read_ols_and_from_bytes_agree(tmp_path) -> None:
    data = build_ols(804, maps=sample_maps(), folders=[FolderSpec(3, "ACQ_KNK", "Knock")])
    path = tmp_path / "project.ols"
    path.write_bytes(data)
    from_path = read_ols(path)
    from_str = read_ols(str(path))
    from_memory = OLSReader.from_bytes(bytearray(data), filename="project.ols").parse()
    assert from_path == from_str == from_memory
    assert from_path.filename == "project.ols"


def test_parse_twice_is_stable() -> None:
    reader = OLSReader.from_bytes(build_ols(804, maps=sample_maps(), compressed=True))
    assert reader.parse() == reader.parse()


def test_to_dict_is_json_serialisable() -> None:
    blob = b"CASC2E55SC8F830\x00" + b"\x01" * 32
    maps = sample_maps()
    maps[0].factor = math.nan
    ols = parse(build_ols(804, maps=maps, versions=[VersionSpec("Original")], blobs=[blob]))
    d = ols.to_dict()
    assert d["cal_block"]["size"] == len(ols.cal_block.data) and "data" not in d["cal_block"]
    assert d["parameters"][0]["y_axis"]["address"] == 0x2000
    json.dumps(ols_reader._json_safe(d), allow_nan=False)
    with_data = ols.to_dict(include_cal_data=True)
    assert bytes.fromhex(with_data["cal_block"]["data"]) == ols.cal_block.data


def test_invalid_limit_rejected() -> None:
    with pytest.raises(ValueError):
        OLSReader.from_bytes(build_ols(804), max_decompressed_size=0)


def test_package_exports() -> None:
    for name in ols_reader.__all__:
        assert hasattr(ols_reader, name)
    assert issubclass(ols_reader.OLSFormatError, ols_reader.OLSError)
    assert issubclass(ols_reader.OLSLimitError, ols_reader.OLSError)
    assert issubclass(ols_reader.OLSError, ValueError)
