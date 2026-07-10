"""End-to-end test: build synthetic PKGs on disk, scan, and exercise the app."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_pkg import build_pkg  # noqa: E402
from pkgtool.scan import scan, find_pkgs  # noqa: E402


def _write_pkgs(root: str):
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    icon = b"\x89PNG\r\n\x1a\nFAKEICON"
    p1 = os.path.join(root, "game.pkg")
    with open(p1, "wb") as f:
        f.write(build_pkg({"TITLE": "Alpha", "TITLE_ID": "CUSA00001", "VERSION": "01.00", "CATEGORY": "gd"}, icon0=icon))
    p2 = os.path.join(root, "sub", "patch.pkg")
    with open(p2, "wb") as f:
        f.write(build_pkg({"TITLE": "Beta", "TITLE_ID": "CUSA00002", "VERSION": "01.00", "APP_VER": "01.03", "CATEGORY": "gp"}))
    # A junk file that looks like a pkg but isn't.
    p3 = os.path.join(root, "broken.pkg")
    with open(p3, "wb") as f:
        f.write(b"\x00" * 4096)


def test_find_and_scan():
    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        assert len(find_pkgs([root])) == 3

        icon_dir = os.path.join(root, "_icons")
        result = scan([root], icon_dir=icon_dir, workers=4)
        assert len(result.records) == 2
        assert len(result.errors) == 1
        titles = {r.title for r in result.records}
        assert titles == {"Alpha", "Beta"}
        alpha = next(r for r in result.records if r.title == "Alpha")
        assert alpha.icon is not None
        assert os.path.exists(os.path.join(icon_dir, alpha.icon))
        beta = next(r for r in result.records if r.title == "Beta")
        assert beta.version == "01.03"  # APP_VER preferred


def test_app_endpoints():
    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        # Import after env is set so module-level config picks it up.
        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            r = client.get("/")
            assert r.status_code == 200
            assert "Alpha" in r.text
            assert "title(s)" in r.text

            # Grouped API
            groups = client.get("/api/groups").json()
            assert groups["total"] == 2  # Alpha and Beta have distinct title ids

            j = client.get("/api/pkgs").json()
            assert j["total"] == 3
            assert len(j["records"]) == 2

            # Icon should be served.
            alpha = next(x for x in j["records"] if x["title"] == "Alpha")
            assert alpha["icon"]
            ico = client.get(f"/icons/{alpha['icon']}")
            assert ico.status_code == 200
            assert ico.content.startswith(b"\x89PNG")

            rescan = client.post("/api/rescan").json()
            assert rescan["total"] == 3


def test_download_and_push():
    import socket
    import threading

    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            recs = client.get("/api/pkgs").json()["records"]
            alpha = next(x for x in recs if x["title"] == "Alpha")
            pkg_id = alpha["id"]
            assert pkg_id

            # Full download
            r = client.get(f"/download/{pkg_id}")
            assert r.status_code == 200
            assert r.content[:4] == b"\x7fCNT"
            full_len = len(r.content)

            # Range request -> 206 partial content
            r2 = client.get(f"/download/{pkg_id}", headers={"Range": "bytes=0-15"})
            assert r2.status_code == 206
            assert len(r2.content) == 16
            assert r2.headers["content-range"].startswith("bytes 0-15/")

            # Unknown id -> 404
            assert client.get("/download/deadbeef").status_code == 404

            # Push: stand up a fake console TCP listener and capture the line.
            received = {}
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def accept():
                conn, _ = srv.accept()
                data = b""
                while b"\n" not in data:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                received["line"] = data.decode("utf-8").strip()
                # Emulate ezremote-dpi: reply with the install result code, then close.
                conn.sendall(b"-2135813882")
                conn.close()

            t = threading.Thread(target=accept)
            t.start()

            resp = client.post(
                "/api/push",
                json={"console_ip": "127.0.0.1", "console_port": port, "pkg_id": pkg_id},
            )
            t.join(timeout=5)
            srv.close()

            body = resp.json()
            assert body["ok"] is True, body
            line = received["line"]
            assert f"/download/{pkg_id}?" in line
            assert "content_id=" in line
            assert "name=" in line
            assert "icon=" in line
            # Icon param is the relative icon url.
            assert "%2Ficons%2F" in line  # url-encoded "/icons/"

            # Console result code is returned as int + 32-bit hex.
            assert body["code"] == -2135813882
            assert body["code_hex"] == "0x80B21106"
            assert body["response"] == "-2135813882"


def test_parse_console_code():
    import importlib
    import app as app_module
    importlib.reload(app_module)

    # Signed int -> unsigned two's-complement hex.
    assert app_module._parse_console_code("-2135813882") == (-2135813882, "0x80B21106")
    # The 0x80B22416 the console reports corresponds to this signed value.
    assert app_module._parse_console_code("-2135809002") == (-2135809002, "0x80B22416")
    assert app_module._parse_console_code("0") == (0, "0x00000000")
    assert app_module._parse_console_code("") == (None, None)
    assert app_module._parse_console_code("garbage") == (None, None)


def test_split_scan_and_download():
    with tempfile.TemporaryDirectory() as root:
        # Build a valid PS4 pkg, then split it into two numbered parts on disk.
        img = build_pkg(
            {"TITLE": "Split Game", "TITLE_ID": "CUSA55555", "VERSION": "01.00", "CATEGORY": "gd"},
            icon0=b"\x89PNG\r\n\x1a\nSPLITICON",
        )
        cut = len(img) // 2
        with open(os.path.join(root, "GAME-CUSA55555_0.pkg"), "wb") as f:
            f.write(img[:cut])
        with open(os.path.join(root, "GAME-CUSA55555_1.pkg"), "wb") as f:
            f.write(img[cut:])

        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)
        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            recs = client.get("/api/pkgs").json()["records"]
            assert len(recs) == 1  # the two parts form ONE logical package
            rec = recs[0]
            assert rec["title"] == "Split Game"
            assert len(rec["parts"]) == 2
            assert rec["size"] == len(img)
            pkg_id = rec["id"]

            # Full download reassembles the original bytes.
            r = client.get(f"/download/{pkg_id}")
            assert r.status_code == 200
            assert r.content == img
            assert r.headers["accept-ranges"] == "bytes"

            # A range spanning the part boundary returns correct bytes (206).
            lo, hi = cut - 20, cut + 20
            r2 = client.get(f"/download/{pkg_id}", headers={"Range": f"bytes={lo}-{hi}"})
            assert r2.status_code == 206
            assert r2.content == img[lo:hi + 1]
            assert r2.headers["content-range"] == f"bytes {lo}-{hi}/{len(img)}"
            assert r2.headers["content-length"] == str(hi - lo + 1)


def test_hide_delta_and_sc():
    import json
    from pkgtool.scan import scan
    from tests.test_pkg import build_cnt, build_ps5_pkg, build_pkg

    with tempfile.TemporaryDirectory() as root:
        # A normal PS4 full game -> shown.
        with open(os.path.join(root, "GAME-CUSA00001.pkg"), "wb") as f:
            f.write(build_pkg({"TITLE": "Game", "TITLE_ID": "CUSA00001", "VERSION": "01.00", "CATEGORY": "gd"}))

        # A PS5 delta patch (content_type 0x23) -> hidden.
        param = {"titleId": "PPSA00002", "contentVersion": "01.905.000",
                 "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "Delta"}}}
        with open(os.path.join(root, "DELTA-DP.pkg"), "wb") as f:
            f.write(build_ps5_pkg(param, content_flags=0x43400000, content_type=0x23))

        # An orphaned SC fragment: bare CNT declaring a huge package_size but a
        # tiny file -> hidden. (Named so it isn't grouped as a split part.)
        pj = json.dumps({"titleId": "PPSA00003",
                         "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "Frag"}}}).encode()
        cnt = build_cnt([(0x2000, pj)], "IP9100-PPSA00003_00-XXXX",
                        content_type=0x20, package_size=11_000_000_000)
        with open(os.path.join(root, "FRAGMENT-META.pkg"), "wb") as f:
            f.write(cnt)

        res = scan([root], icon_dir=None, workers=4)
        shown = {r.title_id for r in res.records}
        hidden = {r.hidden_reason for r in res.hidden}
        assert shown == {"CUSA00001"}, shown
        assert len(res.records) == 1
        assert "delta patch" in hidden
        assert "metadata fragment (sc)" in hidden
        assert len(res.hidden) == 2


def test_grouping_representative():
    from pkgtool.scan import group_by_title_id, PkgRecord

    def rec(title, kind, icon=None, region="US", version="01.00"):
        return PkgRecord(
            path=f"/x/{title}.pkg",
            filename=f"{title}.pkg",
            size=1000,
            platform="PS4",
            edition="fpkg",
            content_id=f"UP0000-CUSA09999_00-{title}",
            title=title,
            title_id="CUSA09999",
            version=version,
            category=None,
            content_type="",
            kind=kind,
            region=region,
            icon=icon,
        )

    # Intentionally out of priority order; update has an icon, base game does not.
    records = [
        rec("Game DLC Pack", "DLC", icon="dlc.png"),
        rec("Game Update", "Update", icon="update.png"),
        rec("Game", "Base Game", icon=None),
    ]
    groups = group_by_title_id(records)
    assert len(groups) == 1
    g = groups[0]
    # Representative kind is the base game (highest priority).
    assert g.kind == "Base Game"
    # Title derived from highest-priority member that has one.
    assert g.title == "Game"
    # Icon falls back to first member (in priority order) that has one -> update.
    assert g.icon == "update.png"
    # Members sorted base > update > dlc.
    assert [m.kind for m in g.members] == ["Base Game", "Update", "DLC"]
    assert g.kinds == ["Base Game", "Update", "DLC"]


def test_group_compat():
    from pkgtool.scan import group_by_title_id, PkgRecord

    def rec(kind, marriage):
        return PkgRecord(
            path=f"/x/{kind}.pkg",
            filename=f"{kind}.pkg",
            size=1,
            platform="PS4",
            edition="fpkg",
            content_id="UP0700-CUSA03388_00-DARKSOULS3000000",
            title="Dark Souls III",
            title_id="CUSA03388",
            version="01.00",
            category=None,
            content_type="GD",
            kind=kind,
            region="US",
            marriage=marriage,
        )

    a = "AA" * 32
    b = "BB" * 32

    # Matching digests -> single group, update married.
    gs = group_by_title_id([rec("Base Game", a), rec("Update", a)])
    assert len(gs) == 1
    assert next(m for m in gs[0].members if m.kind == "Update").compat == "married"

    # Base + non-matching update (DS3 symptom): the title splits into two build
    # groups, but the orphan update is still flagged mismatch.
    gs = group_by_title_id([rec("Base Game", a), rec("Update", b)])
    assert len(gs) == 2
    upd = next(m for g in gs for m in g.members if m.kind == "Update")
    assert upd.compat == "mismatch"

    # No base game to compare against -> no verdict.
    gs = group_by_title_id([rec("Update", a)])
    assert len(gs) == 1
    assert gs[0].members[0].compat == ""


def test_group_split_by_build():
    from pkgtool.scan import group_by_title_id, PkgRecord

    def rec(kind, marriage, cid):
        return PkgRecord(
            path=f"/x/{cid}-{kind}.pkg",
            filename=f"{cid}-{kind}.pkg",
            size=1,
            platform="PS4",
            edition="fpkg",
            content_id=cid,
            title="DS3",
            title_id="CUSA03388",
            version="01.00",
            category=None,
            content_type="GD",
            kind=kind,
            region="US",
            marriage=marriage,
        )

    a = "AA" * 32
    b = "BB" * 32
    cid = "UP0700-CUSA03388_00-DARKSOULS3000000"

    # Single build + DLC -> one combined group, build tag shown (there's a base).
    gs = group_by_title_id([rec("Base Game", a, cid), rec("Update", a, cid), rec("DLC", None, cid)])
    assert len(gs) == 1
    assert gs[0].count == 3
    assert gs[0].build == "AAAAAAA"

    # Two builds (A, B) + a DLC -> two build groups; the DLC attaches to BOTH
    # (each has a base), so it's duplicated and there is no separate group.
    recs = [
        rec("Base Game", a, cid),
        rec("Update", a, cid),
        rec("Base Game", b, cid),
        rec("Update", b, cid),
        rec("DLC", None, cid),
    ]
    gs = group_by_title_id(recs)
    assert len(gs) == 2
    assert sorted(g.build for g in gs) == ["AAAAAAA", "BBBBBBB"]
    for g in gs:
        assert sorted(m.kind for m in g.members) == ["Base Game", "DLC", "Update"]
        digs = {m.marriage for m in g.members if m.kind in ("Base Game", "Update")}
        assert len(digs) == 1  # one coherent marriage per group
        assert next(m for m in g.members if m.kind == "Update").compat == "married"

    # Base A + orphan update B + DLC: A gets the DLC; B is an update-only group,
    # still flagged mismatch, with its build tag shown.
    gs = group_by_title_id([rec("Base Game", a, cid), rec("Update", b, cid), rec("DLC", None, cid)])
    assert len(gs) == 2
    ga = next(g for g in gs if g.build == "AAAAAAA")
    gb = next(g for g in gs if g.build == "BBBBBBB")
    assert sorted(m.kind for m in ga.members) == ["Base Game", "DLC"]
    assert [m.kind for m in gb.members] == ["Update"]
    assert gb.members[0].compat == "mismatch"

    # No base anywhere: two orphan update builds + a shared DLC group.
    gs = group_by_title_id([rec("Update", a, cid), rec("Update", b, cid), rec("DLC", None, cid)])
    assert len(gs) == 3
    shared = next(g for g in gs if g.build == "")
    assert [m.kind for m in shared.members] == ["DLC"]


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
