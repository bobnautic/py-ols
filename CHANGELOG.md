# Changelog

## 1.2.0

### Performance
- Map records are read with a per-version layout computed once per file:
  fixed-size runs of skipped fields collapse into single bounds-checked jumps.
  Large projects parse about 20-25x faster (30,000 maps: 12.0 s → 0.47 s).
- Folder scan searches for marker + padding instead of stepping through
  0xFF fill byte by byte (24 MiB of fill: 12.4 s → 0.03 s).
- CAL end detection uses `bytes.find` instead of a per-byte Python loop.
- The whole-file CAS/EPK search runs once per file instead of once per
  binary version.
- The KP record scan lets the regex engine skip impossible positions.
- cp1252 decoding takes a C fast path for ASCII text.

### Security and robustness
- Decompression is capped (`max_decompressed_size`, default 256 MiB) for
  compressed map lists and KP archives; exceeding it raises `OLSLimitError`.
- Implausible element counts are rejected before iterating.
- Truncated headers no longer crash with `struct.error`.
- Non-WinOLS input raises `OLSFormatError` instead of being parsed as garbage.
- The parser no longer swaps `OLSReader.data` while reading compressed map lists.
- `pyols --extract` sanitises file names taken from the OLS file and refuses
  to overwrite existing files without `--force`.

### Fixes
- KP files: axis addresses were stored in `AxisInfo.offset` (the conversion
  offset) instead of `AxisInfo.address`.
- CLI crashed with `TypeError` for every file whose first parameters include a
  curve or map (`hex()` of the float conversion offset).
- CAL block: the last data byte before the 0xFF fill could be cut off, and the
  4 KiB alignment was applied to the absolute file offset instead of the
  CAL size.
- `AxisInfo.offset` is annotated as `float`, matching the parsed values.

### Added
- `OLSReader.from_bytes()` for in-memory parsing.
- `OLSFile.warnings` lists recoverable problems (e.g. truncated map lists).
- `OLSFile.to_dict()` for JSON export.
- Exception hierarchy: `OLSError` → `OLSFormatError`, `OLSLimitError`.
- CLI (`pyols` / `python -m ols_reader`): `--json`, `--all`, `--limit`,
  `--extract`, `--force`, multiple files, proper exit codes.
- `pyproject.toml` packaging, test-suite, CI for Python 3.9-3.14 on
  Linux, Windows and macOS.
