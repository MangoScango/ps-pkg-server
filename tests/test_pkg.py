"""Self-contained tests for the PKG parser using a synthetic in-memory PKG."""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkgtool.pkg import (  # noqa: E402
    BytesSource,
    Pkg,
    PkgError,
    parse_sfo,
    ENTRY_PARAM_SFO,
    ENTRY_ICON0_PNG,
    ENTRY_METAS,
    ENTRY_ENTRY_NAMES,
)

_ENTRY_SIZE = 0x20


def build_sfo(values):
    """Build a minimal valid param.sfo blob (mirrors ParamSfo.Write)."""
    # Sort by name, as LibOrbisPkg does.
    items = sorted(values.items())

    # Determine formats and encode data.
    def encode(v):
        if isinstance(v, int):
            return 0x0404, struct.pack("<i", v), 4
        b = v.encode("utf-8") + b"\x00"
        return 0x0204, b, len(b)

    index_size = 0x14 + len(items) * 0x10
    key_table = b""
    key_offsets = []
    for name, _ in items:
        key_offsets.append(len(key_table))
        key_table += name.encode("ascii") + b"\x00"

    key_table_start = index_size
    data_table_start = key_table_start + len(key_table)
    if data_table_start % 4:
        data_table_start += 4 - (data_table_start % 4)

    entries = b""
    data_table = b""
    for (name, value), koff in zip(items, key_offsets):
        fmt, encoded, length = encode(value)
        max_len = length
        doff = len(data_table)
        entries += struct.pack("<HHiiI", koff, fmt, length, max_len, doff)
        data_table += encoded

    header = struct.pack(
        ">I", 0x00505346
    ) + struct.pack("<I", 0x0101) + struct.pack(
        "<III", key_table_start, data_table_start, len(items)
    )
    assert len(header) == 0x14

    blob = bytearray(header + entries)
    # pad to key_table_start
    blob += b"\x00" * (key_table_start - len(blob))
    blob += key_table
    blob += b"\x00" * (data_table_start - len(blob))
    blob += data_table
    return bytes(blob)


def build_pkg(sfo_values, icon0=b"", extra_entries=None):
    """Build a synthetic PKG image as bytes."""
    entries = []  # (id, data)
    sfo_blob = build_sfo(sfo_values)
    entries.append((ENTRY_METAS, b""))
    entries.append((ENTRY_ENTRY_NAMES, b"\x00"))
    entries.append((ENTRY_PARAM_SFO, sfo_blob))
    if icon0:
        entries.append((ENTRY_ICON0_PNG, icon0))
    for eid, data in (extra_entries or []):
        entries.append((eid, data))

    entry_count = len(entries)
    entry_table_offset = 0x800
    data_start = entry_table_offset + entry_count * _ENTRY_SIZE
    # align data start
    if data_start % 0x10:
        data_start += 0x10 - (data_start % 0x10)

    # Lay out entry data.
    table = bytearray()
    data_region = bytearray()
    cursor = data_start
    for eid, data in entries:
        off = cursor
        size = len(data)
        table += struct.pack(">IIIIII", eid, 0, 0, 0, off, size)
        table += b"\x00" * 8  # pad
        data_region += data
        cursor += size

    total = cursor
    buf = bytearray(b"\x00" * max(0x600, total))
    if len(buf) < total:
        buf += b"\x00" * (total - len(buf))

    # Header
    buf[0:4] = b"\x7fCNT"
    struct.pack_into(">I", buf, 0x10, entry_count)
    struct.pack_into(">I", buf, 0x18, entry_table_offset)
    cid = b"UP0006-CUSA00001_00-TESTTESTTEST0001"
    buf[0x40 : 0x40 + len(cid)] = cid
    struct.pack_into(">I", buf, 0x70, 0xF)  # drm_type PS4
    struct.pack_into(">I", buf, 0x74, 0x1A)  # content_type GD
    struct.pack_into(">Q", buf, 0x410, 0x100000)  # pfs_image_offset

    # Entry table + data
    buf[entry_table_offset : entry_table_offset + len(table)] = table
    buf[data_start : data_start + len(data_region)] = data_region
    return bytes(buf)


def test_sfo_roundtrip():
    values = {
        "TITLE": "Test Game",
        "TITLE_ID": "CUSA00001",
        "VERSION": "01.00",
        "APP_VER": "01.02",
        "CATEGORY": "gd",
        "PARENTAL_LEVEL": 5,
    }
    blob = build_sfo(values)
    parsed = parse_sfo(blob)
    assert parsed["TITLE"] == "Test Game"
    assert parsed["TITLE_ID"] == "CUSA00001"
    assert parsed["VERSION"] == "01.00"
    assert parsed["APP_VER"] == "01.02"
    assert parsed["PARENTAL_LEVEL"] == 5


def test_pkg_parse_metadata():
    icon = b"\x89PNG\r\n\x1a\nFAKEICONDATA"
    values = {
        "TITLE": "Hello World",
        "TITLE_ID": "CUSA12345",
        "VERSION": "01.00",
        "CATEGORY": "gd",
    }
    img = build_pkg(values, icon0=icon)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.title == "Hello World"
        assert pkg.title_id == "CUSA12345"
        assert pkg.version == "01.00"
        assert pkg.content_type_name == "GD"
        assert pkg.kind == "Base Game"
        assert pkg.region == "US"
        # build_pkg sets no FINALIZED flag -> treated as a homebrew fpkg.
        assert pkg.edition == "fpkg"
        assert pkg.content_id.startswith("UP0006-CUSA00001")
        assert pkg.has_icon0()
        assert pkg.read_icon0() == icon


def test_bad_magic():
    try:
        Pkg.from_source(BytesSource(b"\x00" * 0x1000))
    except PkgError:
        pass
    else:
        raise AssertionError("expected PkgError for bad magic")


def test_app_ver_preferred_for_updates():
    values = {"TITLE": "T", "TITLE_ID": "CUSA1", "VERSION": "01.00", "APP_VER": "02.50"}
    img = build_pkg(values)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.version == "02.50"


def build_cnt(entries_data, content_id, content_type=0x20, content_flags=0, package_size=0):
    """Build a bare \\x7FCNT container. entries_data: list of (id, bytes).

    Entry data offsets are relative to the CNT start (as on disk).
    """
    entry_count = len(entries_data)
    entry_table_offset = 0x800
    data_start = entry_table_offset + entry_count * _ENTRY_SIZE
    if data_start % 0x10:
        data_start += 0x10 - (data_start % 0x10)

    table = bytearray()
    data_region = bytearray()
    cursor = data_start
    for eid, data in entries_data:
        table += struct.pack(">IIIIII", eid, 0, 0, 0, cursor, len(data))
        table += b"\x00" * 8
        data_region += data
        cursor += len(data)

    total = cursor
    buf = bytearray(b"\x00" * max(0x600, total))
    buf[0:4] = b"\x7fCNT"
    struct.pack_into(">I", buf, 0x10, entry_count)
    struct.pack_into(">I", buf, 0x18, entry_table_offset)
    cid = content_id.encode("ascii")
    buf[0x40 : 0x40 + len(cid)] = cid
    struct.pack_into(">I", buf, 0x70, 0xF)
    struct.pack_into(">I", buf, 0x74, content_type)
    struct.pack_into(">I", buf, 0x78, content_flags)
    struct.pack_into(">Q", buf, 0x430, package_size)  # declared package size
    buf[entry_table_offset : entry_table_offset + len(table)] = table
    buf[data_start : data_start + len(data_region)] = data_region
    return bytes(buf)


def build_ps5_pkg(param_obj, icon0=b"", content_id="UP4433-PPSA19639_00-CPREVIEW00000000",
                  content_flags=0, signed=0x80, content_type=0x20):
    """Build a PS5 \\x7FFIH image wrapping a CNT with param.json (0x2000).

    signed: FIH signed byte (0x80 = retail, 0x00 = debug).
    content_type: 0x20 full app, 0x23 delta patch.
    """
    import json

    entries = [(0x2000, json.dumps(param_obj).encode("utf-8"))]
    if icon0:
        entries.append((0x1200, icon0))
    cnt = build_cnt(entries, content_id, content_type=content_type, content_flags=content_flags)

    cnt_base = 0x10000  # FIH header region, then the embedded CNT
    fih = bytearray(b"\x00" * cnt_base)
    fih[0:4] = b"\x7fFIH"
    fih[0x05] = signed
    struct.pack_into("<Q", fih, 0x58, cnt_base)  # embedded CNT offset
    return bytes(fih) + cnt


def test_ps5_pkg_parse():
    icon = b"\x89PNG\r\n\x1a\nPS5ICONDATA"
    param = {
        "titleId": "PPSA19639",
        "contentId": "UP4433-PPSA19639_00-CPREVIEW00000000",
        "masterVersion": "01.00",
        "contentVersion": "01.024.000",
        "applicationCategoryType": 0,
        "localizedParameters": {
            "defaultLanguage": "en-US",
            "en-US": {"titleName": "Minecraft Preview"},
        },
    }
    img = build_ps5_pkg(param, icon0=icon)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.platform == "PS5"
        assert pkg.title == "Minecraft Preview"
        assert pkg.title_id == "PPSA19639"
        # contentVersion is preferred over masterVersion.
        assert pkg.version == "01.024.000"
        # content_type 0x20 = full application -> Base Game, despite any patch flags.
        assert pkg.kind == "Base Game"
        assert pkg.region == "US"
        assert pkg.content_id == "UP4433-PPSA19639_00-CPREVIEW00000000"
        assert pkg.edition == "Retail"  # FIH signed byte 0x80
        assert pkg.has_icon0()
        assert pkg.read_icon0() == icon


def build_ps4_marriage_pkg(kind, digest, content_id="UP0700-CUSA03388_00-DARKSOULS3000000", content_type=0x1A):
    """Build a PS4 CNT with a DIGESTS table so marriage_digest() can be tested.

    kind: "base" (content_flags 0x0A..., playgo entry 0x1001) or
          "update" (content_flags 0x02..., playgo entry 0x1008).
    The DIGESTS entry (0x0001) is at index 0; the playgo entry sits at index 2,
    so its 32-byte digest lives at digests_base + 2*32.
    """
    assert len(digest) == 32
    if kind == "base":
        content_flags, playgo_id, category = 0x0A000000, 0x1001, "gd"
    else:
        content_flags, playgo_id, category = 0x02000000, 0x1008, "gp"

    sfo = build_sfo({"TITLE": "Test", "TITLE_ID": "CUSA03388", "VERSION": "01.00", "CATEGORY": category})
    digest_table = bytearray(3 * 32)  # one slot per entry (indices 0,1,2)
    digest_table[2 * 32 : 3 * 32] = digest  # slot for the playgo entry at index 2
    entries = [(0x0001, bytes(digest_table)), (0x1000, sfo), (playgo_id, b"playgo")]
    return build_cnt(entries, content_id, content_type=content_type, content_flags=content_flags)


def test_concat_source():
    import os
    import tempfile
    from pkgtool.pkg import ConcatSource

    data = bytes(range(256)) * 40  # 10240 bytes
    with tempfile.TemporaryDirectory() as d:
        # Split into uneven parts to exercise boundary math.
        offsets = [0, 3000, 7000, len(data)]
        parts = []
        for i in range(len(offsets) - 1):
            p = os.path.join(d, f"part_{i}.bin")
            with open(p, "wb") as f:
                f.write(data[offsets[i]:offsets[i + 1]])
            parts.append((p, offsets[i + 1] - offsets[i]))

        src = ConcatSource(parts)
        assert src.total_size == len(data)
        # Whole thing.
        assert src.read(0, len(data)) == data
        # A read spanning all three boundaries.
        assert src.read(2500, 5000) == data[2500:7500]
        # Exactly at a boundary.
        assert src.read(3000, 10) == data[3000:3010]
        # Tail.
        assert src.read(10000, 240) == data[10000:10240]
        src.close()


def test_marriage_digest():
    d = bytes(range(32))
    with Pkg.from_source(BytesSource(build_ps4_marriage_pkg("base", d))) as p:
        assert p.platform == "PS4"
        assert p.kind == "Base Game"
        assert p.marriage_digest() == d.hex().upper()
    with Pkg.from_source(BytesSource(build_ps4_marriage_pkg("update", d))) as p:
        assert p.kind == "Update"
        assert p.marriage_digest() == d.hex().upper()
    with Pkg.from_source(BytesSource(build_ps4_marriage_pkg("update", b"\xff" * 32))) as p:
        assert p.marriage_digest() == ("FF" * 32)


def test_marriage_digest_excluded_cases():
    d = bytes(range(32))
    # DLC (content_type 0x1B) has no marriage digest.
    with Pkg.from_source(BytesSource(build_ps4_marriage_pkg("base", d, content_type=0x1B))) as p:
        assert p.marriage_digest() is None
    # PS5 packages use a different model -> None.
    param = {"titleId": "PPSA1", "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "X"}}}
    with Pkg.from_source(BytesSource(build_ps5_pkg(param))) as p:
        assert p.marriage_digest() is None


def test_edition_detection():
    from pkgtool.pkg import detect_edition

    # PS5 finalized image: signed byte is definitive.
    assert detect_edition(0x80, 0) == "Retail"
    assert detect_edition(0x00, 0) == "Debug"
    # Bare CNT / PS4: FINALIZED flag (bit 31) at header 0x04.
    assert detect_edition(None, 0x83020001) == "Retail"
    assert detect_edition(None, 0x00000001) == "fpkg"
    assert detect_edition(None, 0x40000001) == "fpkg"


def test_ps5_debug_edition():
    param = {"titleId": "PPSA00001", "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "Dbg"}}}
    img = build_ps5_pkg(param, signed=0x00)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.edition == "Debug"


def test_ps5_delta_patch_kind():
    # A delta patch has the DELTA_PATCH content flag (0x41000000 subset).
    param = {
        "titleId": "PPSA19639",
        "masterVersion": "01.00",
        "contentVersion": "01.905.000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Patch"}},
    }
    img = build_ps5_pkg(param, content_flags=0x43400000, content_type=0x23)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.platform == "PS5"
        assert pkg.kind == "Update"
        # The version the patch produces is contentVersion.
        assert pkg.version == "01.905.000"
        # Delta patches are detected for hiding.
        assert pkg.is_delta_patch is True


def test_ps5_full_app_with_patch_flags_is_base():
    # A cumulative full app (content_type 0x20) has patch bits set but is NOT an
    # update -- this is the Astro's Playroom / Balatro case.
    param = {
        "titleId": "PPSA01325",
        "masterVersion": "01.00",
        "contentVersion": "01.905.000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "ASTRO"}},
    }
    img = build_ps5_pkg(param, content_flags=0x42420000, content_type=0x20)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.kind == "Base Game"
        assert pkg.version == "01.905.000"


def test_ps5_title_fallback_any_language():
    # No default-language entry: fall back to any language's titleName.
    param = {
        "titleId": "PPSA00001",
        "localizedParameters": {"defaultLanguage": "ja-JP", "en-US": {"titleName": "Only English"}},
    }
    img = build_ps5_pkg(param)
    with Pkg.from_source(BytesSource(img)) as pkg:
        assert pkg.title == "Only English"


def test_title_id_from_content_id():
    from pkgtool.pkg import title_id_from_content_id

    assert title_id_from_content_id("UP4433-PPSA19639_00-CPREVIEW00000000") == "PPSA19639"
    assert title_id_from_content_id("EP0006-CUSA00001_00-XXXX") == "CUSA00001"
    assert title_id_from_content_id("") == ""


def test_kind_classification():
    from pkgtool.pkg import classify_kind

    # CATEGORY-driven
    assert classify_kind("gd", 0x1A) == "Base Game"
    assert classify_kind("gp", 0x1A) == "Update"  # patch shares content_type GD
    assert classify_kind("gpc", 0x1A) == "Update"
    assert classify_kind("ac", 0x1B) == "DLC"
    assert classify_kind("gdc", 0x1A) == "App"
    # content_type fallback when CATEGORY missing
    assert classify_kind(None, 0x1B) == "DLC"
    assert classify_kind(None, 0x1E) == "Update"
    assert classify_kind(None, 0x1A) == "Base Game"
    assert classify_kind("", 0x99) == "Other"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
