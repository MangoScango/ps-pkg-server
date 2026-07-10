"""PS4 PKG metadata parser and scanner.

Pure-Python, dependency-free parsing of PS4 .pkg files. Only the unencrypted
metadata region (header + entry table + param.sfo + icon0.png) is read, so no
decryption is required and only the first chunk of each file is touched.
"""

from .pkg import (
    ByteSource,
    FileSource,
    BytesSource,
    ConcatSource,
    Pkg,
    PkgError,
    parse_sfo,
    ENTRY_PARAM_SFO,
    ENTRY_PARAM_JSON,
    ENTRY_ICON0_PNG,
)

__all__ = [
    "ByteSource",
    "FileSource",
    "BytesSource",
    "ConcatSource",
    "Pkg",
    "PkgError",
    "parse_sfo",
    "ENTRY_PARAM_SFO",
    "ENTRY_PARAM_JSON",
    "ENTRY_ICON0_PNG",
]
