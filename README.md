# py-ols

Python library for reading WinOLS `.ols` and `.kp` (Kennfeld Pack) files.

Extracts parameter definitions, CAL offsets, conversion factors, axis data, and embedded binaries.

- **Zero dependencies**: one pure-Python module (`ols_reader.py`), Python 3.9+
- **Fast**: a 39 MiB project with 30,000 maps parses in about 0.5 s (see [Performance](#performance))
- **Safe on untrusted input**: bounds-checked reads, decompression limits, no crashes on malformed files
- **Typed** (`mypy --strict` clean) and covered by a test-suite that includes mutation fuzzing

## Supported Versions

- v100-285: Old format (marker-based addresses)
- v300-399: Intermediate format (24-bit offsets)
- v400-596: Standard format (full axis extraction)
- v597+: Multi-version format (edit history)
- v597+ KP: Map pack files (ZIP-compressed)
- v804+: WinOLS 5 format

## Installation

```bash
pip install git+https://github.com/bobnautic/py-ols
```

Or copy `ols_reader.py` into your project; it has no dependencies.

## Usage

```python
from ols_reader import read_ols, OLSReader

ols = read_ols("file.ols")
print(f"{ols.make} {ols.model} — {len(ols.parameters)} params")

for p in ols.parameters:
    print(f"{p.name}: @0x{p.data_offset:x} {p.param_type} {p.cols}x{p.rows} "
          f"factor={p.factor} unit={p.unit}")
    if p.x_axis:
        print(f"  x-axis: {p.x_axis.points} points @0x{p.x_axis.address:x}")

# Extract binary data
reader = OLSReader("file.ols")
ols = reader.parse()
for v in ols.binary_versions:
    data = reader.extract_binary(v)  # 4-byte WinOLS checksum stripped
    # v.name comes from the file: never use it as a path unchecked
    with open(f"version_{v.version_index}.bin", "wb") as f:
        f.write(data)
```

Parse from memory (uploads, archives, network):

```python
ols = OLSReader.from_bytes(payload, filename="upload.ols").parse()
```

Export everything as plain data, e.g. for JSON:

```python
import json
json.dumps(ols.to_dict())  # CAL bytes are summarised as "size" unless include_cal_data=True
```

### Errors and warnings

```python
from ols_reader import OLSError, OLSFormatError, OLSLimitError

try:
    ols = read_ols(path)
except OLSFormatError:   # not a WinOLS file
    ...
except OLSLimitError:    # compressed content exceeds max_decompressed_size
    ...

for warning in ols.warnings:  # recoverable problems, e.g. a truncated map list
    print(warning)
```

`OLSError` is the common base class (a subclass of `ValueError`); I/O problems
raise the usual `OSError`.

## CLI

```bash
pyols file.ols                     # summary + 10 sample parameters
pyols file.ols --all               # every parameter
pyols file.ols --json > file.json  # complete parse result as JSON
pyols *.ols --extract out/         # write all embedded binaries to out/
python ols_reader.py file.ols      # works without installation, too
```

`--extract` derives file names from the (sanitised) version names and never
overwrites existing files unless `--force` is given. The exit status is 1 if
any file could not be read.

## Security

WinOLS files are often exchanged with third parties, so the reader treats
every input as untrusted:

- every field read is bounds-checked; malformed data never raises anything
  but `OLSError` (verified by mutation fuzzing in the test-suite)
- compressed map lists and KP archives are inflated with a hard cap
  (`max_decompressed_size`, default 256 MiB) to stop decompression bombs
- implausible lengths and element counts are rejected before any loop runs,
  so crafted files cannot make the parser spin or allocate excessively
- names read from files are sanitised before the CLI uses them as file names

## Performance

`python benchmarks/bench.py --compare <old ols_reader.py>` builds large
synthetic files and times both implementations (best of 3, CPython 3.13):

| file | size | v1.1 | v1.2 |
| --- | ---: | ---: | ---: |
| project, 30,000 maps, 4 × 4 MiB flash images | 38.9 MiB | 12.0 s | 0.47 s |
| same, compressed map list | 16.7 MiB | 9.4 s | 0.53 s |
| map pack, 3,000 records | 0.03 MiB | 0.05 s | 0.025 s |

Projects with large 0xFF fill areas benefit most: a file with 24 MiB of fill
went from 12.4 s to 0.03 s.

## Data Classes

- **`OLSFile`** — Parsed file: vehicle metadata, parameters, binary versions, CAL block, folders, warnings
- **`Parameter`** — Name, description, type (VALUE/CURVE/MAP), data_type, dims, offset, factor, unit, axes
- **`AxisInfo`** — Points, address, factor, offset, unit, id, description, data_source
- **`BinaryVersion`** — Name, blob_offset, blob_size, EPK, version_index
- **`CALBlock`** — CAS id, EPK, raw data
- **`FolderEntry`** — Folder index, name, description

Conversion: `physical = raw * factor + offset`. OLS files do not record
signedness; overlay it from an A2L file if you need it.

## Development

```bash
pip install pytest ruff mypy
python -m pytest          # unit, robustness and CLI tests
ruff check . && mypy      # lint and strict type check
python benchmarks/bench.py
```

The tests generate synthetic `.ols`/`.kp` files with `tests/olsbuild.py`, so no
proprietary sample files are needed.
