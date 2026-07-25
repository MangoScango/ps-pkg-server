"""PS4 and PS5 PKG format parser (metadata only).

Struct layouts mirror LibOrbisPkg (PS4: PKG/Pkg.cs, PKG/Entry.cs, SFO/ParamSfo.cs)
and LibProsperoPkg (PS5: PKG/ProsperoPkgReader.cs, docs/ps5-pkg-format.md).

Both platforms share the same big-endian ``\\x7FCNT`` metadata container: a
0x5A0 header, a 0x20-byte entry table, and unencrypted metadata entries. The
differences:

* PS4 packages are a bare ``\\x7FCNT`` container and carry ``param.sfo`` (entry
  0x1000, a binary key/value blob).
* PS5 installable packages are a little-endian ``\\x7FFIH`` finalized image that
  *wraps* the ``\\x7FCNT`` container at an embedded offset; metadata is
  ``param.json`` (entry 0x2000). Entry data offsets are relative to the CNT
  base, so we add the embedded-CNT offset for FIH images.

Only the header, entry table, and the unencrypted metadata entries (param.sfo /
param.json, icon0.png) are read -- these live near the start of the container,
before the encrypted PFS image, so the parser never reads or decrypts the bulk
of the package.

The ByteSource abstraction lets the same parser run against a local file today
and a partial/streamed source later, without touching parsing logic.
"""

from __future__ import annotations

import json
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional


# --- PKG constants -----------------------------------------------------------

PKG_MAGIC = b"\x7fCNT"      # bare metadata container (PS4 pkg, or PS5 CNT)
FIH_MAGIC = b"\x7fFIH"      # PS5 finalized image (little-endian header)
LIH_MAGIC = b"\x7fLIH"      # PS5 intermediate image (little-endian header)

# CNT header field offsets (big-endian). Shared by PS4 and PS5.
_OFF_MAGIC = 0x00
_OFF_ENTRY_COUNT = 0x10
_OFF_ENTRY_TABLE_OFFSET = 0x18
_OFF_CONTENT_ID = 0x40
_OFF_DRM_TYPE = 0x70
_OFF_CONTENT_TYPE = 0x74
_OFF_CONTENT_FLAGS = 0x78
_OFF_PFS_IMAGE_OFFSET = 0x410
_OFF_PACKAGE_SIZE = 0x430  # declared total package size (u64 BE)

# FIH header field offsets (little-endian). See LibProsperoPkg ProsperoPkgLayout.
_FIH_SIGNED_BYTE = 0x05
_FIH_FORMAT_VERSION = 0x06   # u16 LE; console mount path requires 3 for FIH
_FIH_EMBEDDED_CNT_OFFSET = 0x58
_LIH_EMBEDDED_CNT_OFFSET = 0x30

# Finalized/intermediate image format versions (LibProsperoPkg ProsperoPkgLayout
# FihRequiredFormatVersion / ProsperoNpDrmContentInfo.ResolveContainerOffset).
# The version u16 sits at offset 0x06 and is read in the byte order of the header
# carrying it: little-endian for the LE FIH/LIH images (a v3 image stores the
# bytes 03 00), big-endian for the BE \x7FCNT container. Confirmed against a real
# debug image (bytes 03 00 -> 3); the reference now reads FIH/LIH LE and CNT BE
# (ProsperoNpDrmContentInfo.ResolveContainerOffset), matching this parser.
_FIH_REQUIRED_VERSION = 3
_LIH_REQUIRED_VERSION = 1

_CONTENT_ID_SIZE = 0x30
_ENTRY_SIZE = 0x20
_HEADER_SIZE = 0x5A0

# Entry IDs (subset we care about for metadata).
ENTRY_DIGESTS = 0x00000001
ENTRY_ENTRY_KEYS = 0x00000010
ENTRY_IMAGE_KEY = 0x00000020
ENTRY_GENERAL_DIGESTS = 0x00000080
ENTRY_METAS = 0x00000100
ENTRY_ENTRY_NAMES = 0x00000200
ENTRY_IMAGEDIGS = 0x0000040A    # imagedigs.dat (present in every finalized PS5 image)
ENTRY_PARAM_SFO = 0x00001000    # PS4 param.sfo
ENTRY_PIC1_PNG = 0x00001006
ENTRY_ICON0_PNG = 0x00001200    # PS4 and PS5
ENTRY_PIC0_PNG = 0x00001220
ENTRY_PARAM_JSON = 0x00002000   # PS5 param.json. Confirmed on a real PS5 image and
                                # agreed by ps5-pkg-format.md and (as of the 7/13
                                # update) LibProsperoPkg's ProsperoEntryId enum, which
                                # was corrected to ParamJson=0x2000 / ParamSfo=0x1000.
ENTRY_PLAYGO_HASH_TABLE_DAT = 0x00002010  # PS5 playgo-hash-table.dat (always present)
ENTRY_PLAYGO_FICM_DAT = 0x00002011        # PS5 playgo-ficm.dat (always present)
ENTRY_PLAYGO_CHUNK_DAT = 0x00001001       # base game playgo-chunk.dat
ENTRY_APP_PLAYGO_CHUNK_DAT = 0x00001008   # update/patch app playgo-chunk.dat

# PS4 "marriage" check (see hippie68/msum). content_flags & 0x0F000000 selects
# which entry's DIGESTS-table digest identifies the base build a package binds
# to. A base game and an update are compatible iff these 32-byte digests match.
_DIGEST_SIZE = 0x20
_CONTENT_FLAGS_KIND_MASK = 0x0F000000
_CONTENT_FLAGS_BASE = 0x0A000000    # base game -> digest of entry 0x1001
_CONTENT_FLAGS_UPDATE = 0x02000000  # update    -> digest of entry 0x1008

# Content type -> human string (PS4: LibOrbisPkg Enums.cs / PS4PKG.bt).
CONTENT_TYPES = {
    0x1A: "GD",  # app / patch / remaster
    0x1B: "AC",  # additional content / theme
    0x1C: "AL",  # additional content (no data)
    0x1E: "DP",  # delta patch
}

# PS5 content types (observed, for display only -- the reference does not
# interpret content_type). 0x20 full application, 0x23 delta patch.
CONTENT_TYPES_PS5 = {
    0x20: "GD",  # full application
    0x23: "DP",  # delta patch
}

# Content-flag bits (header 0x78). Shared by PS4 and PS5
# (LibProsperoPkg ProsperoCntContentFlags / ProsperoNpDrmContentInfo / PS4PKG.bt).
# Patch bits (note DELTA/CUMULATIVE share the SUBSEQUENT bit, so ordering matters
# when classifying -- test the more specific masks first):
_FLAG_FIRST_PATCH = 0x00100000
_FLAG_SUBSEQUENT_PATCH = 0x40000000
_FLAG_DELTA_PATCH = 0x41000000
_FLAG_CUMULATIVE_PATCH = 0x60000000
# PS5 content-classification bits (LibProsperoPkg ProsperoCntContentFlags):
_FLAG_GD_BASE = 0x00020000   # base application content
_FLAG_PATCHGO = 0x00200000
_FLAG_REMASTER = 0x00400000
_FLAG_PS_CLOUD = 0x00800000
_FLAG_GD_AC = 0x02000000     # additional content (DLC)
_FLAG_NON_GAME = 0x04000000  # non-game application

# Header flags field (offset 0x04). Bit 31 = FINALIZED: set on retail (and PS5
# debug) packages that went through Sony finalization; fake/homebrew fpkgs are
# not finalized. (PS4PKG.bt PKG_FLAGS_FINALIZED / LibOrbisPkg PKG_FLAG_FINALIZED.)
_FLAG_FINALIZED = 0x80000000
# FIH signed byte (offset 0x05): 0x80 retail, 0x00 debug.
_FIH_SIGNED_RETAIL = 0x80

# param.sfo value formats (SFO/ParamSfo.cs SfoEntryType).
_SFO_UTF8_SPECIAL = 0x0004
_SFO_UTF8 = 0x0204
_SFO_INTEGER = 0x0404


class PkgError(Exception):
    """Raised when a file is not a valid PS4/PS5 PKG or is truncated."""


# --- Byte sources ------------------------------------------------------------

class ByteSource(ABC):
    """Random-access read-only byte source.

    Implementations only need to satisfy ``read(offset, size)``. This is the
    single seam that a future RAR/partial-chunk backend plugs into.
    """

    @abstractmethod
    def read(self, offset: int, size: int) -> bytes:
        """Return exactly ``size`` bytes starting at ``offset``."""

    def close(self) -> None:  # pragma: no cover - optional
        pass

    def __enter__(self) -> "ByteSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class FileSource(ByteSource):
    """Reads from a local file on disk."""

    def __init__(self, path: str):
        self._path = path
        self._f = open(path, "rb")

    def read(self, offset: int, size: int) -> bytes:
        self._f.seek(offset)
        data = self._f.read(size)
        if len(data) != size:
            raise PkgError(
                f"unexpected EOF: wanted {size} bytes at {offset:#x}, got {len(data)}"
            )
        return data

    def close(self) -> None:
        self._f.close()


class ConcatSource(ByteSource):
    """Reads across an ordered list of split part files as one contiguous stream.

    Given parts ``[(path, size), ...]`` in order, a read at a logical offset is
    mapped to the part(s) that cover it, transparently spanning boundaries. This
    lets the parser and the range-download endpoint treat a split package
    (``*_0.pkg``, ``*_1.pkg``, ..., ``*_sc.pkg``) exactly like a single file.
    """

    def __init__(self, parts):
        # parts: list of (path, size). Build cumulative start offsets.
        self._paths = []
        self._starts = []
        self._sizes = []
        total = 0
        for path, size in parts:
            self._paths.append(path)
            self._starts.append(total)
            self._sizes.append(size)
            total += size
        self.total_size = total
        self._open = {}  # index -> file handle (lazily opened)

    def _file(self, i: int):
        f = self._open.get(i)
        if f is None:
            f = open(self._paths[i], "rb")
            self._open[i] = f
        return f

    def read(self, offset: int, size: int) -> bytes:
        if offset < 0 or offset + size > self.total_size:
            raise PkgError(
                f"read out of range: {size} bytes at {offset:#x} (total {self.total_size:#x})"
            )
        out = bytearray()
        remaining = size
        pos = offset
        # Find the starting part via linear scan (part counts are small).
        i = 0
        while i < len(self._paths) and self._starts[i] + self._sizes[i] <= pos:
            i += 1
        while remaining > 0 and i < len(self._paths):
            part_start = self._starts[i]
            local = pos - part_start
            take = min(remaining, self._sizes[i] - local)
            f = self._file(i)
            f.seek(local)
            chunk = f.read(take)
            if len(chunk) != take:
                raise PkgError(f"short read in part {self._paths[i]}")
            out += chunk
            remaining -= take
            pos += take
            i += 1
        if remaining:
            raise PkgError("unexpected EOF across split parts")
        return bytes(out)

    def close(self) -> None:
        for f in self._open.values():
            try:
                f.close()
            except OSError:
                pass
        self._open.clear()


class BytesSource(ByteSource):
    """Reads from an in-memory buffer (e.g. the first chunk of a file)."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self, offset: int, size: int) -> bytes:
        chunk = self._data[offset : offset + size]
        if len(chunk) != size:
            raise PkgError(
                f"unexpected EOF: wanted {size} bytes at {offset:#x}, got {len(chunk)}"
            )
        return chunk


# --- Data structures ---------------------------------------------------------

@dataclass
class PkgEntry:
    """One record from the PKG entry table (0x20 bytes)."""

    id: int
    name_offset: int
    flags1: int
    flags2: int
    offset: int
    size: int
    index: int = 0  # position in the entry table (used by the DIGESTS table)

    @property
    def encrypted(self) -> bool:
        return (self.flags1 & 0x80000000) != 0

    @property
    def key_index(self) -> int:
        return (self.flags2 & 0xF000) >> 12


@dataclass
class Pkg:
    """Parsed PKG metadata (PS4 or PS5)."""

    platform: str  # "PS4" or "PS5"
    content_id: str
    drm_type: int
    content_type: int
    content_flags: int
    entry_count: int
    entry_table_offset: int
    pfs_image_offset: int
    cnt_base: int = 0  # embedded-CNT offset (0 for a bare CNT / PS4)
    edition: str = ""  # Retail / Debug / fpkg
    digests_base: int = 0  # absolute offset of the DIGESTS table (entry index 0)
    package_size: int = 0  # declared total package size (header 0x430)
    entries: Dict[int, PkgEntry] = field(default_factory=dict)
    sfo: Dict[str, object] = field(default_factory=dict)   # PS4 param.sfo
    param: Dict[str, object] = field(default_factory=dict)  # PS5 param.json

    _source: Optional[ByteSource] = None

    # --- derived / convenience metadata (platform-aware) ---

    @property
    def title(self) -> Optional[str]:
        if self.platform == "PS5":
            return _ps5_title(self.param)
        return self.sfo.get("TITLE")

    @property
    def title_id(self) -> Optional[str]:
        if self.platform == "PS5":
            return self.param.get("titleId") or title_id_from_content_id(self.content_id)
        return self.sfo.get("TITLE_ID")

    @property
    def version(self) -> Optional[str]:
        if self.platform == "PS5":
            # contentVersion reflects the actual (patched) version, e.g.
            # 01.905.000; masterVersion is the original app version (01.00).
            return self.param.get("contentVersion") or self.param.get("masterVersion")
        # PS4: games/apps use VERSION; updates use APP_VER.
        return self.sfo.get("APP_VER") or self.sfo.get("VERSION")

    @property
    def category(self) -> Optional[str]:
        if self.platform == "PS5":
            v = self.param.get("applicationCategoryType")
            return str(v) if v is not None else None
        return self.sfo.get("CATEGORY")

    @property
    def content_type_name(self) -> str:
        table = CONTENT_TYPES_PS5 if self.platform == "PS5" else CONTENT_TYPES
        return table.get(self.content_type, f"0x{self.content_type:X}")

    @property
    def kind(self) -> str:
        """High-level package kind: Base Game / Update / DLC / App / Other."""
        if self.platform == "PS5":
            return classify_kind_ps5(self.content_flags, self.content_type)
        # PS4: base vs update needs the SFO CATEGORY (gd* vs gp*); both share
        # content_type GD. content_type is a fallback when CATEGORY is missing.
        return classify_kind(self.category, self.content_type)

    @property
    def region(self) -> str:
        return region_from_content_id(self.content_id)

    @property
    def is_delta_patch(self) -> bool:
        """True for a delta patch (PS5 content_type 0x23 / DELTA_PATCH flag, or
        PS4 content_type DP 0x1E). A delta only carries a chunk-copy map, not the
        changed data, so it is not installable via our offline flow."""
        if self.platform == "PS5":
            return self.content_type == 0x23 or (
                (self.content_flags & _FLAG_DELTA_PATCH) == _FLAG_DELTA_PATCH
            )
        return self.content_type == 0x1E  # PS4 DP

    def is_metadata_fragment(self, available_size: int) -> bool:
        """True if this is a metadata-only tail (an SC segment) rather than a
        full package: the CNT declares a package far larger than the bytes we
        actually have (a real full package has package_size ~= its size)."""
        return self.package_size > available_size * 2

    def has_icon0(self) -> bool:
        return ENTRY_ICON0_PNG in self.entries

    def read_entry(self, entry_id: int) -> Optional[bytes]:
        """Read the raw bytes of an entry by id, or None if absent.

        Requires the Pkg to have been created with a live source.
        """
        entry = self.entries.get(entry_id)
        if entry is None:
            return None
        if self._source is None:
            raise PkgError("no source attached; re-open with Pkg.open()")
        if entry.encrypted:
            # Metadata entries (param.sfo, icon0) are unencrypted in retail/fake
            # PKGs. If we ever hit an encrypted one, surface it rather than
            # returning garbage.
            raise PkgError(f"entry {entry_id:#x} is encrypted; decryption unsupported")
        return self._source.read(entry.offset, entry.size)

    def read_icon0(self) -> Optional[bytes]:
        return self.read_entry(ENTRY_ICON0_PNG)

    def marriage_digest(self) -> Optional[str]:
        """PS4 base<->update compatibility digest (see hippie68/msum).

        Returns the hex of the 32-byte DIGESTS-table entry that identifies the
        base build this package binds to:
          - base game  -> digest of the playgo-chunk.dat entry (0x1001)
          - update     -> digest of the app playgo-chunk.dat entry (0x1008)
        A base and an update are compatible ("married") iff these match.

        Base-vs-update is chosen by ``kind`` (from the SFO CATEGORY), NOT by the
        header content_flags. The content_flags "kind" bits (0x0A000000 base /
        0x02000000 update) are an fpkg convention; retail base games ship
        content_flags 0x02000000 (which looks like "update"), so keying on them
        made retail bases target the absent 0x1008 entry and return None -- which
        broke grouping (the retail base couldn't marry its retail update).

        Returns None for DLC, PS5, or packages that don't carry the digest.
        """
        if self.platform != "PS4":
            return None  # PS5 uses a different (split/merge) model
        if self.content_type == 0x1B:  # DLC / additional content
            return None

        kind = self.kind
        if kind in ("Base Game", "App"):
            target_id = ENTRY_PLAYGO_CHUNK_DAT      # 0x1001
        elif kind == "Update":
            target_id = ENTRY_APP_PLAYGO_CHUNK_DAT  # 0x1008
        else:
            return None

        target = self.entries.get(target_id)
        if target is None:
            return None
        if self._source is None:
            raise PkgError("no source attached; re-open with Pkg.open()")

        pos = self.digests_base + target.index * _DIGEST_SIZE
        digest = self._source.read(pos, _DIGEST_SIZE)
        return digest.hex().upper()

    # --- construction ---

    @classmethod
    def open(cls, path: str) -> "Pkg":
        """Parse a PKG from a local file path."""
        return cls.from_source(FileSource(path), keep_source=True)

    @classmethod
    def open_split(cls, parts) -> "Pkg":
        """Parse a split PKG from ordered ``[(path, size), ...]`` parts."""
        return cls.from_source(ConcatSource(parts), keep_source=True)

    @classmethod
    def from_source(cls, source: ByteSource, keep_source: bool = True) -> "Pkg":
        """Parse a PKG from any ByteSource."""
        pkg = _parse(source)
        pkg._source = source if keep_source else None
        return pkg

    def close(self) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None

    def __enter__(self) -> "Pkg":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- Parsing -----------------------------------------------------------------

def _u32be(b: bytes, off: int) -> int:
    return struct.unpack_from(">I", b, off)[0]


def _u64be(b: bytes, off: int) -> int:
    return struct.unpack_from(">Q", b, off)[0]


def _cstr(b: bytes) -> str:
    end = b.find(b"\x00")
    if end != -1:
        b = b[:end]
    return b.decode("utf-8", errors="replace")


def _u64le(b: bytes, off: int) -> int:
    return struct.unpack_from("<Q", b, off)[0]


def _locate_cnt(source: ByteSource):
    """Return (cnt_base, fih_signed_byte) for the \\x7FCNT container.

    PS4 packages and bare PS5 CNT containers start with the magic at 0. PS5
    installable packages are \\x7FFIH (or intermediate \\x7FLIH) images that wrap
    the CNT at an embedded offset stored in their little-endian header. The FIH
    signed byte (0x05) is returned for retail/debug detection (None otherwise).
    """
    head = source.read(0, 0x60)
    magic = head[0:4]
    if magic == PKG_MAGIC:
        # Bare \x7FCNT: PS4 package or a bare PS5 CNT. The 0x06 field is part of
        # the big-endian header flags here (not a FIH format version), so we do
        # not version-check it -- the CNT magic path is shared by PS4 and PS5.
        return 0, None
    if magic == FIH_MAGIC:
        version = struct.unpack_from("<H", head, _FIH_FORMAT_VERSION)[0]
        if version != _FIH_REQUIRED_VERSION:
            raise PkgError(
                f"unsupported FIH format version {version} (expected {_FIH_REQUIRED_VERSION})"
            )
        return _u64le(head, _FIH_EMBEDDED_CNT_OFFSET), head[_FIH_SIGNED_BYTE]
    if magic == LIH_MAGIC:
        version = struct.unpack_from("<H", head, _FIH_FORMAT_VERSION)[0]
        if version != _LIH_REQUIRED_VERSION:
            raise PkgError(
                f"unsupported LIH format version {version} (expected {_LIH_REQUIRED_VERSION})"
            )
        return _u64le(head, _LIH_EMBEDDED_CNT_OFFSET), None
    raise PkgError("not a PS4/PS5 PKG (bad magic)")


def _parse(source: ByteSource) -> Pkg:
    cnt_base, fih_signed = _locate_cnt(source)

    # CNT header (big-endian), 0x5A0 bytes; read a bit extra to cover 0x410.
    header = source.read(cnt_base, 0x600)
    if header[_OFF_MAGIC : _OFF_MAGIC + 4] != PKG_MAGIC:
        raise PkgError("embedded container is not a \\x7FCNT")

    header_flags = _u32be(header, 0x04)
    entry_count = _u32be(header, _OFF_ENTRY_COUNT)
    entry_table_offset = _u32be(header, _OFF_ENTRY_TABLE_OFFSET)
    content_id = _cstr(header[_OFF_CONTENT_ID : _OFF_CONTENT_ID + _CONTENT_ID_SIZE])
    drm_type = _u32be(header, _OFF_DRM_TYPE)
    content_type = _u32be(header, _OFF_CONTENT_TYPE)
    content_flags = _u32be(header, _OFF_CONTENT_FLAGS)
    pfs_image_offset = _u64be(header, _OFF_PFS_IMAGE_OFFSET)
    package_size = _u64be(header, _OFF_PACKAGE_SIZE)

    if entry_count > 0x10000:
        raise PkgError(f"implausible entry_count {entry_count}; file likely corrupt")

    # Entry table: read all entries at once. Entry data offsets are relative to
    # the CNT base, so store them as absolute file offsets.
    table = source.read(cnt_base + entry_table_offset, entry_count * _ENTRY_SIZE)
    entries: Dict[int, PkgEntry] = {}
    digests_base = 0
    for i in range(entry_count):
        base = i * _ENTRY_SIZE
        eid = _u32be(table, base)
        entry = PkgEntry(
            id=eid,
            name_offset=_u32be(table, base + 0x04),
            flags1=_u32be(table, base + 0x08),
            flags2=_u32be(table, base + 0x0C),
            offset=cnt_base + _u32be(table, base + 0x10),
            size=_u32be(table, base + 0x14),
            index=i,
        )
        if i == 0:
            # The DIGESTS table (entry index 0) holds a 32-byte digest per entry,
            # indexed by entry position.
            digests_base = entry.offset
        # First occurrence wins if duplicated.
        entries.setdefault(eid, entry)

    # Platform: PS5 if a param.json entry exists (or we came in via FIH/LIH),
    # else PS4.
    is_ps5 = ENTRY_PARAM_JSON in entries or cnt_base != 0
    platform = "PS5" if is_ps5 else "PS4"

    pkg = Pkg(
        platform=platform,
        content_id=content_id,
        drm_type=drm_type,
        content_type=content_type,
        content_flags=content_flags,
        entry_count=entry_count,
        entry_table_offset=entry_table_offset,
        pfs_image_offset=pfs_image_offset,
        cnt_base=cnt_base,
        edition=detect_edition(fih_signed, header_flags),
        digests_base=digests_base,
        package_size=package_size,
        entries=entries,
    )

    if platform == "PS5":
        json_entry = entries.get(ENTRY_PARAM_JSON)
        if json_entry is not None and not json_entry.encrypted:
            raw = source.read(json_entry.offset, json_entry.size)
            try:
                pkg.param = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                pkg.param = {}
    else:
        sfo_entry = entries.get(ENTRY_PARAM_SFO)
        if sfo_entry is not None and not sfo_entry.encrypted:
            sfo_bytes = source.read(sfo_entry.offset, sfo_entry.size)
            try:
                pkg.sfo = parse_sfo(sfo_bytes)
            except PkgError:
                pkg.sfo = {}

    return pkg


def _ps5_title(param: Dict[str, object]) -> Optional[str]:
    """Extract the display title from a PS5 param.json.

    Prefers the default language's titleName, then any language's, then a
    top-level titleName.
    """
    lp = param.get("localizedParameters")
    if isinstance(lp, dict):
        default_lang = lp.get("defaultLanguage")
        if isinstance(default_lang, str):
            entry = lp.get(default_lang)
            if isinstance(entry, dict) and entry.get("titleName"):
                return str(entry["titleName"])
        for value in lp.values():
            if isinstance(value, dict) and value.get("titleName"):
                return str(value["titleName"])
    name = param.get("titleName")
    return str(name) if name else None


def title_id_from_content_id(content_id: str) -> str:
    """Title id = the segment between the first '-' and the following '_'.

    e.g. ``UP4433-PPSA19639_00-...`` -> ``PPSA19639``.
    """
    if not content_id:
        return ""
    dash = content_id.find("-")
    if dash < 0:
        return ""
    start = dash + 1
    underscore = content_id.find("_", start)
    end = underscore if underscore >= 0 else len(content_id)
    return content_id[start:end]


def parse_sfo(data: bytes) -> Dict[str, object]:
    """Parse a param.sfo blob into a name -> value dict.

    Mirrors LibOrbisPkg SFO/ParamSfo.FromStream. Integers become ``int``,
    strings become ``str``. Header fields are little-endian; an optional SCEC
    wrapper (0x53434543) shifts the SFO start by 0x800.
    """
    start = 0
    if len(data) >= 4 and struct.unpack_from(">I", data, 0)[0] == 0x53434543:
        start = 0x800

    if len(data) < start + 0x14:
        raise PkgError("SFO too small")

    # Magic is stored as bytes "\0PSF"; LibOrbisPkg reads it big-endian == 0x00505346.
    if struct.unpack_from(">I", data, start)[0] != 0x00505346:
        raise PkgError("missing SFO magic")

    key_table_start = struct.unpack_from("<I", data, start + 0x08)[0]
    data_table_start = struct.unpack_from("<I", data, start + 0x0C)[0]
    num_values = struct.unpack_from("<i", data, start + 0x10)[0]

    result: Dict[str, object] = {}
    for i in range(num_values):
        rec = start + 0x14 + i * 0x10
        key_offset = struct.unpack_from("<H", data, rec)[0]
        fmt = struct.unpack_from("<H", data, rec + 0x02)[0]
        length = struct.unpack_from("<i", data, rec + 0x04)[0]
        # max_len at +0x08 (unused for reading)
        data_offset = struct.unpack_from("<I", data, rec + 0x0C)[0]

        name = _cstr(data[start + key_table_start + key_offset : start + key_table_start + key_offset + 256])
        vpos = start + data_table_start + data_offset

        if fmt == _SFO_INTEGER:
            value: object = struct.unpack_from("<i", data, vpos)[0]
        elif fmt == _SFO_UTF8:
            raw = data[vpos : vpos + (length - 1 if length > 0 else 0)]
            value = raw.decode("utf-8", errors="replace")
        elif fmt == _SFO_UTF8_SPECIAL:
            raw = data[vpos : vpos + length]
            value = raw.decode("utf-8", errors="replace")
        else:
            # Unknown type; skip rather than fail the whole parse.
            continue
        result[name] = value

    return result


def classify_kind(category: Optional[str], content_type: int) -> str:
    """Classify a package as Base Game / Update / DLC / App / Other.

    Primary signal is the SFO CATEGORY code (see LibOrbisPkg SfoData.SfoTypes):
      - ``gp*`` -> Update (game/app patch)
      - ``ac``  -> DLC (additional content)
      - ``gd``  -> Base Game
      - ``gd*`` (gda/gdc/gdd/gde/gdk/gdl/...) -> App (non-game applications)
    content_type is a fallback: AC/AL (0x1B/0x1C) -> DLC, DP (0x1E) -> Update,
    GD (0x1A) -> Base Game.
    """
    c = (category or "").lower()
    if c.startswith("gp"):
        return "Update"
    if c == "ac":
        return "DLC"
    if c == "gd" or c == "gc" or c == "bd":
        return "Base Game"
    if c.startswith("gd"):
        return "App"
    # Fallbacks based on content_type when CATEGORY is absent/unknown.
    if content_type in (0x1B, 0x1C):  # AC, AL
        return "DLC"
    if content_type == 0x1E:  # DP (delta patch)
        return "Update"
    if content_type == 0x1A:  # GD
        return "Base Game"
    return "Other"


def classify_kind_ps5(content_flags: int, content_type: int) -> str:
    """Classify a PS5 package as Update / DLC / App / Base Game.

    Uses the content-flag bits (LibProsperoPkg ProsperoCntContentFlags /
    ProsperoNpDrmContentInfo), which is the reference/console signal -- not
    content_type, which the reference does not interpret.

    - Only a DELTA patch (0x41000000) is an incremental update that needs a base.
      PS5 app images are cumulative, so a "subsequent" or "cumulative" full image
      (e.g. Astro's Playroom shipped at 01.905.000) is a complete, self-contained
      installable game, not a separate update -- classified here as Base Game.
    - DLC / additional content carries GD_AC (0x02000000). This is heuristic: a
      real base/homebrew image can set GD_AC *alongside* the base-content bit
      GD_BASE (0x00020000) -- observed on the LibProsperoPkg homebrew sample,
      flags 0x02020000 -- so we only treat GD_AC as DLC when GD_BASE is *not*
      also set. This still lacks a confirmed retail add-on sample; treat DLC
      here as best-effort.
    - NON_GAME (0x04000000) marks a non-game application.

    content_type (0x20 full app / 0x23 delta) corroborates but is not the signal.
    """
    if (content_flags & _FLAG_DELTA_PATCH) == _FLAG_DELTA_PATCH:
        return "Update"
    if (content_flags & _FLAG_GD_AC) and not (content_flags & _FLAG_GD_BASE):
        return "DLC"
    if content_flags & _FLAG_NON_GAME:
        return "App"
    return "Base Game"


def detect_edition(fih_signed: Optional[int], header_flags: int) -> str:
    """Classify a package as Retail / Debug / fpkg.

    - PS5 finalized images carry a signed byte (FIH offset 0x05): 0x80 = retail,
      0x00 = debug. This is definitive when present.
    - Otherwise (PS4 packages, bare PS5 CNTs) we use the FINALIZED flag (header
      offset 0x04, bit 31). Retail/official packages are finalized; fake/homebrew
      packages (fpkg) are not.
    """
    if fih_signed is not None:
        return "Retail" if fih_signed == _FIH_SIGNED_RETAIL else "Debug"
    return "Retail" if (header_flags & _FLAG_FINALIZED) else "fpkg"


def region_from_content_id(content_id: str) -> str:
    """Best-effort region from the content_id prefix.

    Content IDs look like ``UP0006-CUSA00001_00-...``; the leading letter of the
    label encodes the publishing region. This is heuristic (there is no explicit
    region field in the SFO).
    """
    if not content_id:
        return "Unknown"
    prefix = content_id[0].upper()
    return {
        "U": "US",
        "E": "EU",
        "J": "JP",
        "H": "Asia",
        "K": "KR",
        "I": "INT",
    }.get(prefix, "Unknown")
