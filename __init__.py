"""
py-ols - WinOLS File Reader

Parse WinOLS .ols project files to extract parameter definitions,
CAL offsets, and embedded binary data.
"""

if __package__:  # repository vendored as a package (e.g. git submodule)
    from .ols_reader import (
        DEFAULT_MAX_DECOMPRESSED_SIZE,
        AxisInfo,
        BinaryVersion,
        CALBlock,
        FolderEntry,
        OLSError,
        OLSFile,
        OLSFormatError,
        OLSLimitError,
        OLSReader,
        Parameter,
        __version__,
        read_ols,
    )
else:  # loaded stand-alone, e.g. by test collectors
    from ols_reader import (  # type: ignore[no-redef]
        DEFAULT_MAX_DECOMPRESSED_SIZE,
        AxisInfo,
        BinaryVersion,
        CALBlock,
        FolderEntry,
        OLSError,
        OLSFile,
        OLSFormatError,
        OLSLimitError,
        OLSReader,
        Parameter,
        __version__,
        read_ols,
    )

__all__ = [
    "DEFAULT_MAX_DECOMPRESSED_SIZE",
    "OLSReader",
    "OLSFile",
    "Parameter",
    "AxisInfo",
    "BinaryVersion",
    "CALBlock",
    "FolderEntry",
    "OLSError",
    "OLSFormatError",
    "OLSLimitError",
    "read_ols",
    "__version__",
]
