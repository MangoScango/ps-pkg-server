"""Tests for the entitlement catalogue and version.xml resolution."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pkgtool import entitlements as ent  # noqa: E402


CSV = """entitlement_id,title,title_id,package_url,platform,content_type
UP0700-PPSA04610_00-ELDENRING0000000, ELDEN RING™,PPSA04610,https://sgst.example/np/PPSA04610_00/abc-version.xml,ps5,game
UP0700-CUSA28863_00-ELDENRING0000000,ELDEN RING™,CUSA28863,http://gs2.example/appkgo/CUSA28863_00/UP0700-CUSA28863_00-ELDENRING0000000.json,ps4,game
HP4497-CUSA01490_00-00000000000DLC12,Skellige Contract,CUSA01490,http://gs2.example/acpkgo/CUSA01490_00/HP4497-CUSA01490_00-00000000000DLC12.json,ps4,
UP9999-PPSA00001_00-NOURL00000000000,No URL Title,PPSA00001,,ps5,game
"""

VERSION_XML = b"""<?xml version="1.0" encoding="UTF-8"?><title_patch ac_set_rev="1" nptitleid="PPSA04610_00" schema_ver="1.0">
    <app_tag content_id="UP0700-PPSA04610_00-ELDENRING0000000" revision="55">
        <package content_ver="01.017.000" manifest_url="https://sgst.example/old.json" system_ver="325058567"/>
        <package content_ver="01.018.001" manifest_url="https://sgst.example/app.json" system_ver="325058567"/>
    </app_tag>
    <ac_tag content_id="UP0700-PPSA04610_00-ELDENRINGDLC0000" revision="39">
        <package content_ver="01.000.000" manifest_url="https://sgst.example/dlc.json" system_ver="153092101"/>
    </ac_tag>
</title_patch>
"""


def _catalogue():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "entitlements.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write(CSV)
    return p


def test_load_strips_and_skips_urlless_rows():
    rows = ent.load(_catalogue())
    assert len(rows) == 3  # the empty-package_url row is dropped
    first = rows[0]
    assert first.title == "ELDEN RING™"  # leading space stripped
    assert first.url_kind == "xml"
    assert rows[1].url_kind == "json"


def test_load_text_matches_load():
    path = _catalogue()
    from_file = ent.load(path)
    from_text = ent.load_text(open(path, encoding="utf-8").read())
    assert [e.entitlement_id for e in from_text] == [e.entitlement_id for e in from_file]
    assert ent.load_text("entitlement_id,package_url\n") == []
    assert ent.load_text("") == []


def test_load_missing_file():
    assert ent.load("/nonexistent/entitlements.csv") == []


def test_search_matches_title_titleid_and_entitlement_id():
    rows = ent.load(_catalogue())
    assert len(ent.search(rows, "elden")) == 2
    # Terms may appear across the title and ids.
    assert [e.title_id for e in ent.search(rows, "elden ppsa04610")] == ["PPSA04610"]
    assert [e.title_id for e in ent.search(rows, "cusa01490")] == ["CUSA01490"]
    # Every term has to match.
    assert ent.search(rows, "elden nomatch") == []
    assert ent.search(rows, "") == []


def test_search_ranks_prefix_matches_first():
    rows = ent.load(_catalogue())
    hits = ent.search(rows, "skellige")
    assert hits[0].title == "Skellige Contract"


def test_search_platform_filter():
    rows = ent.load(_catalogue())
    assert [e.platform for e in ent.search(rows, "elden", platform="ps5")] == ["ps5"]
    assert [e.platform for e in ent.search(rows, "elden", platform="ps4")] == ["ps4"]
    # Case and surrounding space are tolerated.
    assert len(ent.search(rows, "elden", platform=" PS5 ")) == 1
    # No filter keeps both.
    assert len(ent.search(rows, "elden")) == 2
    # A platform nothing matches yields nothing.
    assert ent.search(rows, "elden", platform="ps3") == []


def test_search_limit():
    rows = ent.load(_catalogue())
    assert len(ent.search(rows, "elden", limit=1)) == 1


def test_parse_version_xml_keeps_highest_version_per_content():
    packages = ent.parse_version_xml(VERSION_XML)
    assert [(p.kind, p.content_id, p.content_ver) for p in packages] == [
        ("Game", "UP0700-PPSA04610_00-ELDENRING0000000", "01.018.001"),
        ("DLC", "UP0700-PPSA04610_00-ELDENRINGDLC0000", "01.000.000"),
    ]
    assert packages[0].manifest_url == "https://sgst.example/app.json"


def test_parse_version_xml_ignores_packages_without_manifest():
    xml = b"""<title_patch><app_tag content_id="X"><package content_ver="01.000.000"/></app_tag></title_patch>"""
    assert ent.parse_version_xml(xml) == []


def test_resolve_json_row_needs_no_fetch():
    rows = ent.load(_catalogue())
    row = next(r for r in rows if r.url_kind == "json")
    packages = ent.resolve(row)
    assert len(packages) == 1
    assert packages[0].manifest_url == row.package_url
    assert packages[0].name == row.title


def test_resolve_xml_row_parses_fetched_document():
    rows = ent.load(_catalogue())
    row = next(r for r in rows if r.url_kind == "xml")
    original = ent.fetch
    ent.fetch = lambda url, timeout=20: VERSION_XML
    try:
        packages = ent.resolve(row)
    finally:
        ent.fetch = original
    assert [p.kind for p in packages] == ["Game", "DLC"]
    # The catalogue title is carried onto every resolved package.
    assert {p.name for p in packages} == {"ELDEN RING™"}


def test_from_url():
    e = ent.from_url("  https://example.com/a/b/THING.json  ")
    assert e.url_kind == "json"
    assert e.package_url == "https://example.com/a/b/THING.json"
    assert e.title == "THING.json"
    assert ent.from_url("https://e/x/abc-version.xml").url_kind == "xml"
    assert ent.from_url("https://e/x/GAME.pkg").url_kind == "pkg"
    assert ent.from_url("https://e/x/readme.txt").url_kind == ""


def test_resolve_pkg_url_needs_no_fetch():
    row = ent.from_url("https://example.com/a/UP1234-PPSA00001_00-GAME.pkg")
    packages = ent.resolve(row)
    assert len(packages) == 1
    assert packages[0].manifest_url == row.package_url
    assert packages[0].name == "UP1234-PPSA00001_00-GAME.pkg"


def test_url_kind_ignores_query_and_fragment():
    assert ent.from_url("https://e/x/THING.json?token=abc").url_kind == "json"
    assert ent.from_url("https://e/x/v-version.xml#frag").url_kind == "xml"


def test_find():
    rows = ent.load(_catalogue())
    assert ent.find(rows, "HP4497-CUSA01490_00-00000000000DLC12").title_id == "CUSA01490"
    assert ent.find(rows, "nope") is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
