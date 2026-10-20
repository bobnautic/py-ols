"""
Parse-time benchmark on large synthetic WinOLS files.

    python benchmarks/bench.py                       # current ols_reader
    python benchmarks/bench.py --compare old.py      # also time another copy

The files mimic real projects: tens of thousands of maps, several 4 MiB
flash images with large 0xFF fill areas, a CAL block and folder entries.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from olsbuild import (  # noqa: E402
    AxisSpec,
    FolderSpec,
    KPRecord,
    MapSpec,
    VersionSpec,
    build_kp,
    build_ols,
)


def flash_image(rng: random.Random, size: int) -> bytes:
    """Random code/data interleaved with 0xFF fill, CAL header at 0x40000."""
    out = bytearray()
    while len(out) < size:
        out += rng.randbytes(rng.randint(0x1000, 0x20000))
        out += b"\xff" * rng.randint(0x1000, 0x80000)
    out[0x40000:0x40010] = b"CASC2E55SC1C910\x00"
    return bytes(out[:size])


def build_project(path: Path, maps: int, compressed: bool) -> None:
    rng = random.Random(42)
    specs = []
    for i in range(maps):
        kind = rng.choice((2, 3, 4))
        specs.append(MapSpec(
            name=f"KF_PARAM_{i:05d}",
            description=f"Calibration parameter {i} - engine load correction",
            type_code=kind, dim1=rng.randint(1, 16), dim2=rng.randint(1, 16),
            unit="%", factor=0.0234375, data_offset=rng.randint(0, 0x3FFFFF), folder_id=i % 900,
            translations=["Kalibrierparameter"],
            axis1=AxisSpec(description="Engine speed", id="n_32", factor=32.0, address=0x41000, translations=["Drehzahl"]),
            axis2=AxisSpec(description="Relative load", id="rl_w", factor=0.0234, address=0x42000),
        ))
    blob_size = 0x400000
    data = build_ols(
        834, header={"make": "VW", "model": "Golf", "ecu_type": "SIMOS"}, maps=specs, compressed=compressed,
        versions=[VersionSpec("Original"), VersionSpec("Stage1"), VersionSpec("Stage2"), VersionSpec("Test")],
        blob_size=blob_size, blobs=[flash_image(rng, blob_size - 4) for _ in range(4)],
        folders=[FolderSpec(i, f"FUNC_{i:04d}_ctrl", f"Function {i}") for i in range(900)],
    )
    path.write_bytes(data)


def build_mappack(path: Path, records: int) -> None:
    path.write_bytes(build_kp(600, records=[
        KPRecord(f"MAP_{i:05d}", f"Description {i:05d}", 3 + i % 2, 8, 8, 0x60000 + 64 * i, 0x70000 + 16 * i)
        for i in range(records)
    ]))


def load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up while executing it
    spec.loader.exec_module(module)
    return module


def best_of(fn, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare", type=Path, help="another ols_reader.py to time")
    parser.add_argument("--maps", type=int, default=30000)
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    modules = {"current": load(ROOT / "ols_reader.py", "ols_reader_current")}
    if args.compare:
        modules["compare"] = load(args.compare, "ols_reader_compare")

    with tempfile.TemporaryDirectory() as tmp:
        files = {
            f"project, {args.maps} maps": Path(tmp, "project.ols"),
            f"project, {args.maps} maps, compressed": Path(tmp, "compressed.ols"),
            "map pack, 3000 records": Path(tmp, "pack.kp"),
        }
        paths = list(files.values())
        build_project(paths[0], args.maps, compressed=False)
        build_project(paths[1], args.maps, compressed=True)
        build_mappack(paths[2], 3000)

        print(f"{'file':42} {'size':>9} " + " ".join(f"{name:>10}" for name in modules))
        for label, path in files.items():
            timings = []
            counts: set[int] = set()
            for module in modules.values():
                def run(m: ModuleType = module, p: Path = path, seen: set[int] = counts) -> None:
                    seen.add(len(m.read_ols(p).parameters))
                timings.append(best_of(run, args.repeat))
            size = f"{os.path.getsize(path) / 2**20:.1f} MiB"
            print(f"{label:42} {size:>9} " + " ".join(f"{t:9.3f}s" for t in timings) + f"   params={sorted(counts)}")


if __name__ == "__main__":
    main()
