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
            assert "PS PKG Server" in r.text

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


def test_push_etahen_v1():
    """etaHEN DPI v1: server sends a JSON object over raw TCP, reads {"res":"N"}."""
    import json
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

            received = {}
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def accept():
                conn, _ = srv.accept()
                conn.settimeout(3)
                data = b""
                # etaHEN v1 does a single <=1024-byte read; grab the JSON payload.
                try:
                    while True:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                        try:
                            json.loads(data.decode("utf-8"))
                            break  # got a complete object
                        except ValueError:
                            continue
                except (OSError, socket.timeout):
                    pass
                received["data"] = data.decode("utf-8", "replace")
                conn.sendall(b'{"res":"0"}')
                conn.close()

            t = threading.Thread(target=accept)
            t.start()

            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": port,
                    "pkg_id": pkg_id,
                    "protocol": "etahen_v1",
                },
            )
            t.join(timeout=5)
            srv.close()

            body = resp.json()
            assert body["ok"] is True, body
            assert body["protocol"] == "etahen_v1"
            assert body["code"] == 0
            assert body["code_hex"] == "0x00000000"

            # The console received a JSON object with the etaHEN field names.
            sent = json.loads(received["data"])
            assert sent["url"] == f"http://{body['server_ip']}:80/download/{pkg_id}"
            assert "?" not in sent["url"]  # metadata is in fields, not query
            assert sent["content_id"] == alpha["content_id"]
            assert sent["content_name"] == "Alpha"
            assert sent["icon_url"].startswith("http://")
            assert "/icons/" in sent["icon_url"]


def test_push_etahen_v2():
    """etaHEN DPI v2: server POSTs form fields to /upload, reads status text."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                received["path"] = self.path
                received["fields"] = {k: v[0] for k, v in parse_qs(body).items()}
                msg = b"SUCCESS: PKG installation started"
                self.send_response(200)
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

            def log_message(self, *a):  # silence
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.handle_request)
        t.start()

        with TestClient(app_module.app) as client:
            recs = client.get("/api/pkgs").json()["records"]
            alpha = next(x for x in recs if x["title"] == "Alpha")
            pkg_id = alpha["id"]

            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": port,
                    "pkg_id": pkg_id,
                    "protocol": "etahen_v2",
                },
            )
            t.join(timeout=5)
            httpd.server_close()

            body = resp.json()
            assert body["ok"] is True, body
            assert body["protocol"] == "etahen_v2"
            assert body["code"] == 0  # SUCCESS -> 0
            assert body["response"].startswith("SUCCESS")

            assert received["path"] == "/upload"
            fields = received["fields"]
            assert fields["url"] == f"http://{body['server_ip']}:80/download/{pkg_id}"
            assert fields["content_id"] == alpha["content_id"]
            assert fields["content_name"] == "Alpha"
            assert fields["icon_url"].startswith("http://")


def test_push_unknown_protocol():
    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            pkg_id = client.get("/api/pkgs").json()["records"][0]["id"]
            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": 9999,
                    "pkg_id": pkg_id,
                    "protocol": "bogus",
                },
            )
            assert resp.status_code == 400
            assert resp.json()["ok"] is False


def test_push_remote_pkg():
    """flatz ps4_remote_pkg_installer: POST /api/install {type:direct,packages:[url]}."""
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    with tempfile.TemporaryDirectory() as root:
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                received["path"] = self.path
                received["json"] = json.loads(body)
                msg = b'{ "status": "success", "task_id": 5, "title": "Alpha" }'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

            def log_message(self, *a):
                pass

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.handle_request)
        t.start()

        with TestClient(app_module.app) as client:
            recs = client.get("/api/pkgs").json()["records"]
            alpha = next(x for x in recs if x["title"] == "Alpha")
            pkg_id = alpha["id"]

            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": port,
                    "pkg_id": pkg_id,
                    "protocol": "remote_pkg",
                },
            )
            t.join(timeout=5)
            httpd.server_close()

            body = resp.json()
            assert body["ok"] is True, body
            assert body["protocol"] == "remote_pkg"
            assert body["code"] == 0  # success

            assert received["path"] == "/api/install"
            sent = received["json"]
            assert sent["type"] == "direct"
            assert sent["packages"] == [f"http://{body['server_ip']}:80/download/{pkg_id}"]
            assert "?" not in sent["packages"][0]  # clean URL, no query params


def test_push_remote_pkg_rejects_ps5():
    """The PS4 installer can't handle PS5 packages -> server rejects with 400."""
    from tests.test_pkg import build_ps5_pkg

    with tempfile.TemporaryDirectory() as root:
        param = {
            "titleId": "PPSA00001",
            "contentVersion": "01.00.000",
            "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "PS5 Game"}},
        }
        with open(os.path.join(root, "PS5-GAME.pkg"), "wb") as f:
            f.write(build_ps5_pkg(param))

        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")

        import importlib
        import app as app_module
        importlib.reload(app_module)

        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            recs = client.get("/api/pkgs").json()["records"]
            assert recs and recs[0]["platform"] == "PS5"
            pkg_id = recs[0]["id"]

            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": 12800,
                    "pkg_id": pkg_id,
                    "protocol": "remote_pkg",
                },
            )
            assert resp.status_code == 400
            body = resp.json()
            assert body["ok"] is False
            assert "PS5" in body["error"]


def test_parse_remote_pkg_responses():
    import importlib
    import app as app_module
    importlib.reload(app_module)

    # Success -> 0.
    assert app_module._parse_remote_pkg_response(
        '{ "status": "success", "task_id": 3, "title": "X" }'
    ) == (0, "0x00000000")
    # Register fail: hex error_code (invalid JSON) -> signed int + hex.
    assert app_module._parse_remote_pkg_response(
        '{ "status": "fail", "error_code": 0x80B21106 }'
    ) == (-2135813882, "0x80B21106")
    # Param/prereq fail: valid JSON with an error string, no numeric code.
    assert app_module._parse_remote_pkg_response(
        '{ "status": "fail", "error": "Unsupported content type." }'
    ) == (None, None)
    assert app_module._parse_remote_pkg_response("") == (None, None)


def test_parse_etahen_responses():
    import importlib
    import app as app_module
    importlib.reload(app_module)

    # v1: {"res":"N"} -> signed int + 32-bit hex.
    assert app_module._parse_etahen_v1_response('{"res":"0"}') == (0, "0x00000000")
    assert app_module._parse_etahen_v1_response('{"res":"-2135813882"}') == (
        -2135813882,
        "0x80B21106",
    )
    assert app_module._parse_etahen_v1_response("") == (None, None)
    # Bare-int fallback if the reply isn't the expected JSON.
    assert app_module._parse_etahen_v1_response("0") == (0, "0x00000000")

    # v2: SUCCESS -> 0; FAILED text embeds the numeric code.
    assert app_module._parse_etahen_v2_response("SUCCESS: started") == (0, "0x00000000")
    assert app_module._parse_etahen_v2_response(
        "FAILED: Install failed with error X, code -2135813882 (0x80B21106) for URL: y"
    ) == (-2135813882, "0x80B21106")
    assert app_module._parse_etahen_v2_response("") == (None, None)


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


def test_hide_delta_and_error_on_orphan_fragment():
    """A delta patch is hidden; an orphaned tail-less CNT is a visible error."""
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

        # An orphaned tail-less CNT: declares a huge package_size but is a tiny
        # file, with no main to complete it. This is broken/incomplete and must
        # surface as an ERROR (not silently hidden). (Named so it isn't grouped as
        # a split part.)
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
        # The delta patch is hidden.
        assert hidden == {"delta patch"}
        assert len(res.hidden) == 1
        # The orphaned tail-less fragment is a visible error, not hidden.
        assert len(res.errors) == 1
        err = res.errors[0]
        assert err.filename == "FRAGMENT-META.pkg"
        assert "incomplete package" in (err.error or "")


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
            min_sdk=None,
            min_ps5_fw=None,
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
        rec("Game", "Game", icon=None),
    ]
    groups = group_by_title_id(records)
    assert len(groups) == 1
    g = groups[0]
    # Representative kind is the base game (highest priority).
    assert g.kind == "Game"
    # Title derived from highest-priority member that has one.
    assert g.title == "Game"
    # Icon falls back to first member (in priority order) that has one -> update.
    assert g.icon == "update.png"
    # Members sorted base > update > dlc.
    assert [m.kind for m in g.members] == ["Game", "Update", "DLC"]
    assert g.kinds == ["Game", "Update", "DLC"]


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
            min_sdk=None,
            min_ps5_fw=None,
            category=None,
            content_type="GD",
            kind=kind,
            region="US",
            marriage=marriage,
        )

    a = "AA" * 32
    b = "BB" * 32

    # Matching digests -> single group, update married.
    gs = group_by_title_id([rec("Game", a), rec("Update", a)])
    assert len(gs) == 1
    assert next(m for m in gs[0].members if m.kind == "Update").compat == "married"

    # Base + non-matching update (DS3 symptom): the title splits into two build
    # groups, but the orphan update is still flagged mismatch.
    gs = group_by_title_id([rec("Game", a), rec("Update", b)])
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
            min_sdk=None,
            min_ps5_fw=None,
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
    gs = group_by_title_id([rec("Game", a, cid), rec("Update", a, cid), rec("DLC", None, cid)])
    assert len(gs) == 1
    assert gs[0].count == 3
    assert gs[0].build == "AAAAAAA"

    # Two builds (A, B) + a DLC -> two build groups; the DLC attaches to BOTH
    # (each has a base), so it's duplicated and there is no separate group.
    recs = [
        rec("Game", a, cid),
        rec("Update", a, cid),
        rec("Game", b, cid),
        rec("Update", b, cid),
        rec("DLC", None, cid),
    ]
    gs = group_by_title_id(recs)
    assert len(gs) == 2
    assert sorted(g.build for g in gs) == ["AAAAAAA", "BBBBBBB"]
    for g in gs:
        assert sorted(m.kind for m in g.members) == ["DLC", "Game", "Update"]
        digs = {m.marriage for m in g.members if m.kind in ("Game", "Update")}
        assert len(digs) == 1  # one coherent marriage per group
        assert next(m for m in g.members if m.kind == "Update").compat == "married"

    # Base A + orphan update B + DLC: A gets the DLC; B is an update-only group,
    # still flagged mismatch, with its build tag shown.
    gs = group_by_title_id([rec("Game", a, cid), rec("Update", b, cid), rec("DLC", None, cid)])
    assert len(gs) == 2
    ga = next(g for g in gs if g.build == "AAAAAAA")
    gb = next(g for g in gs if g.build == "BBBBBBB")
    assert sorted(m.kind for m in ga.members) == ["DLC", "Game"]
    assert [m.kind for m in gb.members] == ["Update"]
    assert gb.members[0].compat == "mismatch"

    # No base anywhere: two orphan update builds + a shared DLC group.
    gs = group_by_title_id([rec("Update", a, cid), rec("Update", b, cid), rec("DLC", None, cid)])
    assert len(gs) == 3
    shared = next(g for g in gs if g.build == "")
    assert [m.kind for m in shared.members] == ["DLC"]


def _write_headless_main(root, name, *, pfs_image_size):
    """Write a headless \\x7FFIH main image (its embedded CNT split out).

    Built from a full PS5 image with its FIH outer-PFS image size (0x18) patched
    to ``pfs_image_size`` and cut at the embedded-CNT boundary, so the file's CNT
    offset (0x58) equals its own size -> headless.
    """
    import struct
    from tests.test_pkg import build_ps5_pkg

    param = {"titleId": "PPSA00000",
             "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": "main"}}}
    img = bytearray(build_ps5_pkg(param, content_type=0x20))
    cnt_base = struct.unpack_from("<Q", img, 0x58)[0]
    struct.pack_into("<Q", img, 0x18, pfs_image_size)
    with open(os.path.join(root, name), "wb") as f:
        f.write(bytes(img[:cnt_base]))


def _write_sc(root, name, *, content_id, title, pfs_image_size):
    """Write a bare \\x7FCNT SC tail with a given content id and PFS image size.

    The SC's CNT PFS image size (0x418) is the structural key that matches it to a
    headless main written with the same size.
    """
    import struct
    from tests.test_pkg import build_ps5_pkg

    param = {"titleId": "PPSA00000",
             "localizedParameters": {"defaultLanguage": "en", "en": {"titleName": title}}}
    img = bytearray(build_ps5_pkg(param, content_id=content_id, content_type=0x20))
    cnt_base = struct.unpack_from("<Q", img, 0x58)[0]
    struct.pack_into(">Q", img, cnt_base + 0x410 + 0x08, pfs_image_size)
    with open(os.path.join(root, name), "wb") as f:
        f.write(bytes(img[cnt_base:]))  # bare CNT tail


def test_group_sources_divergent_name_sc_pairing():
    """CDN packages whose main and _sc filenames diverge are paired structurally
    (equal PFS image size, then content-label tiebreak), not by filename stem.

    Two DLC share one PFS image size (0x90000) with distinct mains -> each is a
    1:1 pairing resolved by content label; a third app has a unique size."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        for label in ("AC00000000000008", "AR00000000000004"):
            _write_headless_main(root, f"JP0082-PPSA08664_00-{label}.pkg", pfs_image_size=0x90000)
            _write_sc(root, f"EP0082-PPSA08668_00-{label}_sc.pkg",
                      content_id=f"EP0082-PPSA08668_00-{label}", title="DLC", pfs_image_size=0x90000)
        _write_headless_main(root, "JP0082-PPSA08664_00-YOUTUBESIEA00000.pkg", pfs_image_size=0x4e00000)
        _write_sc(root, "EP4381-PPSA01651_00-YOUTUBESIEE00000_sc.pkg",
                  content_id="EP4381-PPSA01651_00-YOUTUBESIEE00000", title="YouTube", pfs_image_size=0x4e00000)

        sources = group_sources(find_pkgs([root]))
        # One combined package per SC (SC-driven identity), keyed on the SC file.
        assert len(sources) == 3
        by_sc = {os.path.basename(s["parts"][1]): s for s in sources if len(s["parts"]) == 2}
        expected = {
            "EP0082-PPSA08668_00-AC00000000000008_sc.pkg": "JP0082-PPSA08664_00-AC00000000000008.pkg",
            "EP0082-PPSA08668_00-AR00000000000004_sc.pkg": "JP0082-PPSA08664_00-AR00000000000004.pkg",
            "EP4381-PPSA01651_00-YOUTUBESIEE00000_sc.pkg": "JP0082-PPSA08664_00-YOUTUBESIEA00000.pkg",
        }
        assert set(by_sc) == set(expected)
        for sc_name, main_name in expected.items():
            s = by_sc[sc_name]
            assert s["split"] is True
            assert os.path.basename(s["parts"][0]) == main_name, sc_name
            # Package identity comes from the SC (its content id), not the main.
            assert s["name"] == sc_name.replace("_sc.pkg", ".pkg")


def test_group_sources_shared_main_fans_out_to_region_scs():
    """One shared main body + several region SCs -> one package per SC, all
    reusing the single main. The SC alone distinguishes the region editions."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        # A single YouTube main body...
        _write_headless_main(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg", pfs_image_size=0x4e00000)
        # ...backing three region SCs (US same-stem, EU, JP), all same PFS size.
        region_scs = {
            "UP4381-PPSA01650_00-YOUTUBESIEA00000_sc.pkg": "UP4381-PPSA01650_00-YOUTUBESIEA00000",
            "EP4381-PPSA01651_00-YOUTUBESIEE00000_sc.pkg": "EP4381-PPSA01651_00-YOUTUBESIEE00000",
            "JA0004-PPSA01652_00-YOUTUBESIEJA0000_sc.pkg": "JA0004-PPSA01652_00-YOUTUBESIEJA0000",
        }
        for sc_name, cid in region_scs.items():
            _write_sc(root, sc_name, content_id=cid, title="YouTube", pfs_image_size=0x4e00000)

        sources = group_sources(find_pkgs([root]))
        # Three packages, one per SC, each reusing the single shared main.
        assert len(sources) == 3
        main_name = "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg"
        seen_scs = set()
        for s in sources:
            assert s["split"] is True
            assert len(s["parts"]) == 2
            assert os.path.basename(s["parts"][0]) == main_name  # shared main
            seen_scs.add(os.path.basename(s["parts"][1]))
        assert seen_scs == set(region_scs)


def test_group_sources_two_bodies_matched_by_content_id_similarity():
    """Two distinct same-size main bodies + two SCs must each pair to the RIGHT
    body via content-id similarity (publisher code + label), not greedily collapse
    onto one main and orphan the other."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        # Two regional bodies of one title: publisher 4381, regions UP / EP.
        _write_headless_main(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg", pfs_image_size=0x300000)
        _write_headless_main(root, "EP4381-PPSA01651_00-YOUTUBESIEE00000.pkg", pfs_image_size=0x300000)
        _write_sc(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000_sc.pkg",
                  content_id="UP4381-PPSA01650_00-YOUTUBESIEA00000", title="US", pfs_image_size=0x300000)
        _write_sc(root, "EP4381-PPSA01651_00-YOUTUBESIEE00000_sc.pkg",
                  content_id="EP4381-PPSA01651_00-YOUTUBESIEE00000", title="EU", pfs_image_size=0x300000)

        sources = group_sources(find_pkgs([root]))
        # Two two-part packages; each SC paired to its own matching body.
        pairs = {os.path.basename(s["parts"][1]): os.path.basename(s["parts"][0])
                 for s in sources if len(s["parts"]) == 2}
        assert pairs == {
            "UP4381-PPSA01650_00-YOUTUBESIEA00000_sc.pkg": "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg",
            "EP4381-PPSA01651_00-YOUTUBESIEE00000_sc.pkg": "EP4381-PPSA01651_00-YOUTUBESIEE00000.pkg",
        }
        # No main left orphaned as a standalone.
        assert all(len(s["parts"]) == 2 for s in sources)


def test_content_id_affinity_weights_publisher_code():
    from pkgtool.scan import _content_id_affinity

    us = "UP4381-PPSA01650_00-YOUTUBESIEA00000"
    eu = "EP4381-PPSA01651_00-YOUTUBESIEE00000"
    unrelated = "JP0506-PPSA03169_00-2465683171993968"
    # Exact id beats a cross-region (publisher-only) match, which beats unrelated.
    assert _content_id_affinity(us, us) > _content_id_affinity(us, eu)
    assert _content_id_affinity(us, eu) > _content_id_affinity(us, unrelated)
    # A shared publisher code alone (regions differ) is a strong positive signal.
    assert _content_id_affinity(us, eu) >= 1_000_000
    # No publisher match and no label match -> no bonus.
    assert _content_id_affinity(us, unrelated) < 1_000_000


def test_group_sources_orphan_sc_and_main_left_alone():
    """An SC with no same-size main, and a main with no SC, are not paired; they
    fall back to standalone single-file sources."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        _write_headless_main(root, "UP0000-PPSA00001_00-LONELYMAIN0000000.pkg", pfs_image_size=0x50000)
        _write_sc(root, "EP0000-PPSA00002_00-LONELYSC000000000_sc.pkg",
                  content_id="EP0000-PPSA00002_00-LONELYSC000000000", title="Orphan", pfs_image_size=0x999000)

        sources = group_sources(find_pkgs([root]))
        # No size match -> no pairing; both remain single-file sources.
        assert all(len(s["parts"]) == 1 for s in sources)
        names = {os.path.basename(s["parts"][0]) for s in sources}
        assert names == {
            "UP0000-PPSA00001_00-LONELYMAIN0000000.pkg",
            "EP0000-PPSA00002_00-LONELYSC000000000_sc.pkg",
        }


CLOUD_CSV = """entitlement_id,title,title_id,package_url,platform,content_type
UP0700-PPSA04610_00-ELDENRING0000000,ELDEN RING,PPSA04610,https://sgst.example/abc-version.xml,ps5,game
UP0700-CUSA28863_00-ELDENRING0000000,ELDEN RING PS4,CUSA28863,http://gs2.example/UP0700-CUSA28863_00-ELDENRING0000000.json,ps4,game
"""

CLOUD_XML = b"""<?xml version="1.0" encoding="UTF-8"?><title_patch nptitleid="PPSA04610_00">
    <app_tag content_id="UP0700-PPSA04610_00-ELDENRING0000000">
        <package content_ver="01.018.001" manifest_url="https://sgst.example/app.json"/>
    </app_tag>
    <ac_tag content_id="UP0700-PPSA04610_00-ELDENRINGDLC0000">
        <package content_ver="01.000.000" manifest_url="https://sgst.example/dlc.json"/>
    </ac_tag>
</title_patch>
"""


def _cloud_client(root):
    """Reload the app with a cloud catalogue in place and return a TestClient."""
    import importlib

    csv_path = os.path.join(root, "cloud.csv")
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write(CLOUD_CSV)
    _write_pkgs(root)
    os.environ["PKG_DIRS"] = root
    os.environ["ICON_DIR"] = os.path.join(root, "_icons")
    os.environ["ENTITLEMENTS_CSV"] = csv_path

    import app as app_module
    importlib.reload(app_module)
    from fastapi.testclient import TestClient
    return app_module, TestClient(app_module.app)


def test_cloud_search_and_resolve():
    with tempfile.TemporaryDirectory() as root:
        app_module, client = _cloud_client(root)
        with client:
            j = client.get("/api/cloud/search?q=elden").json()
            assert j["ok"] is True
            assert j["total"] == 2
            assert {r["url_kind"] for r in j["results"]} == {"xml", "json"}

            # Platform filter narrows the same query.
            j = client.get("/api/cloud/search?q=elden&platform=ps5").json()
            assert [r["platform"] for r in j["results"]] == ["ps5"]
            j = client.get("/api/cloud/search?q=elden&platform=ps4").json()
            assert [r["platform"] for r in j["results"]] == ["ps4"]
            assert client.get("/api/cloud/search?q=elden&platform=ps3").status_code == 400

            # A json row resolves without any network access.
            j = client.post(
                "/api/cloud/resolve",
                json={"entitlement_id": "UP0700-CUSA28863_00-ELDENRING0000000"},
            ).json()
            assert j["ok"] is True and j["url_kind"] == "json"
            assert len(j["packages"]) == 1

            # An xml row resolves to the app plus its additional content.
            app_module.entitlements.fetch = lambda url, timeout=20: CLOUD_XML
            j = client.post(
                "/api/cloud/resolve",
                json={"entitlement_id": "UP0700-PPSA04610_00-ELDENRING0000000"},
            ).json()
            assert j["ok"] is True and j["url_kind"] == "xml"
            assert [(p["kind"], p["content_ver"]) for p in j["packages"]] == [
                ("Game", "01.018.001"), ("DLC", "01.000.000")
            ]

            assert client.post(
                "/api/cloud/resolve", json={"entitlement_id": "nope"}
            ).status_code == 404


def test_cloud_push_hands_manifest_url_to_console():
    """The console is given the Sony manifest URL, not a local download URL."""
    import json
    import socket
    import threading

    with tempfile.TemporaryDirectory() as root:
        _app_module, client = _cloud_client(root)
        with client:
            received = {}
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]

            def accept():
                conn, _ = srv.accept()
                conn.settimeout(3)
                data = b""
                try:
                    while True:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        data += chunk
                        try:
                            json.loads(data.decode("utf-8"))
                            break
                        except ValueError:
                            continue
                except (OSError, socket.timeout):
                    pass
                received["data"] = data.decode("utf-8", "replace")
                conn.sendall(b'{"res":"0"}')
                conn.close()

            t = threading.Thread(target=accept)
            t.start()
            resp = client.post(
                "/api/cloud/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": port,
                    "protocol": "etahen_v1",
                    "manifest_url": "https://sgst.example/app.json",
                    "content_id": "UP0700-PPSA04610_00-ELDENRING0000000",
                    "name": "ELDEN RING",
                },
            )
            t.join(timeout=5)
            srv.close()

            body = resp.json()
            assert body["ok"] is True, body
            assert body["url"] == "https://sgst.example/app.json"
            sent = json.loads(received["data"])
            assert sent["url"] == "https://sgst.example/app.json"
            assert sent["content_id"] == "UP0700-PPSA04610_00-ELDENRING0000000"


def test_cloud_status_and_reload_without_catalogue():
    """An absent catalogue reports itself rather than erroring, and reload picks
    one up without a restart. A direct URL still works meanwhile."""
    import importlib

    with tempfile.TemporaryDirectory() as root:
        csv_path = os.path.join(root, "later.csv")
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")
        os.environ["ENTITLEMENTS_CSV"] = csv_path

        import app as app_module
        importlib.reload(app_module)
        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            s = client.get("/api/cloud/status").json()
            assert s["available"] is False and s["count"] == 0
            assert s["path"] == os.path.abspath(csv_path)
            assert s["source_url"].startswith("https://")
            # Search says "not configured", not "failed".
            assert client.get("/api/cloud/search?q=elden").status_code == 503
            # A pasted URL needs no catalogue.
            assert client.post(
                "/api/cloud/resolve", json={"url": "https://example.com/a/GAME.pkg"}
            ).json()["ok"] is True

            with open(csv_path, "w", encoding="utf-8") as f:
                f.write(CLOUD_CSV)
            s = client.post("/api/cloud/reload").json()
            assert s["available"] is True and s["count"] == 2
            assert client.get("/api/cloud/search?q=elden").json()["total"] == 2


def test_cloud_upload():
    """Uploading a CSV stores it at the configured path and loads it."""
    import importlib

    with tempfile.TemporaryDirectory() as root:
        csv_path = os.path.join(root, "nested", "cat.csv")
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")
        os.environ["ENTITLEMENTS_CSV"] = csv_path

        import app as app_module
        importlib.reload(app_module)
        from fastapi.testclient import TestClient

        with TestClient(app_module.app) as client:
            assert client.get("/api/cloud/status").json()["available"] is False
            r = client.post(
                "/api/cloud/upload",
                content=CLOUD_CSV.encode("utf-8"),
                headers={"Content-Type": "text/csv"},
            )
            body = r.json()
            assert r.status_code == 200 and body["ok"] is True
            assert body["count"] == 2
            # Written to the configured path, creating parent dirs as needed.
            assert os.path.exists(csv_path)
            assert client.get("/api/cloud/search?q=elden").json()["total"] == 2
            # No temp file left behind.
            assert not os.path.exists(csv_path + ".part")


def test_cloud_upload_rejects_bad_csv_without_clobbering():
    """A rejected upload leaves an existing catalogue untouched."""
    import importlib

    with tempfile.TemporaryDirectory() as root:
        csv_path = os.path.join(root, "cat.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(CLOUD_CSV)
        _write_pkgs(root)
        os.environ["PKG_DIRS"] = root
        os.environ["ICON_DIR"] = os.path.join(root, "_icons")
        os.environ["ENTITLEMENTS_CSV"] = csv_path

        import app as app_module
        importlib.reload(app_module)
        from fastapi.testclient import TestClient

        original = open(csv_path, "rb").read()
        with TestClient(app_module.app) as client:
            assert client.get("/api/cloud/status").json()["count"] == 2
            cases = [
                (b"", 400),                                        # empty
                (b"a,b\n1,2\n", 400),                              # wrong columns
                (b"entitlement_id,package_url\n", 400),            # header only
                (b"entitlement_id,package_url\nX,\n", 400),        # no usable rows
                (b"\xff\xfe\x00nope", 400),                        # not UTF-8
            ]
            for payload, expected in cases:
                r = client.post(
                    "/api/cloud/upload",
                    content=payload,
                    headers={"Content-Type": "text/csv"},
                )
                assert r.status_code == expected, (payload[:20], r.status_code)
                assert r.json()["ok"] is False
            # Catalogue and file both survived every rejection.
            assert client.get("/api/cloud/status").json()["count"] == 2
            assert open(csv_path, "rb").read() == original


def test_cloud_resolve_arbitrary_url():
    """A pasted URL resolves without being in the catalogue."""
    with tempfile.TemporaryDirectory() as root:
        app_module, client = _cloud_client(root)
        with client:
            # A .json URL is already a manifest.
            j = client.post(
                "/api/cloud/resolve", json={"url": "https://example.com/x/MYPKG.json"}
            ).json()
            assert j["ok"] is True and j["url_kind"] == "json"
            assert j["packages"][0]["manifest_url"] == "https://example.com/x/MYPKG.json"
            assert j["packages"][0]["name"] == "MYPKG.json"

            # A .xml URL is fetched and may offer several packages.
            app_module.entitlements.fetch = lambda url, timeout=20: CLOUD_XML
            j = client.post(
                "/api/cloud/resolve", json={"url": "https://example.com/abc-version.xml"}
            ).json()
            assert j["ok"] is True and j["url_kind"] == "xml"
            assert [p["kind"] for p in j["packages"]] == ["Game", "DLC"]

            # A direct .pkg is installable as-is.
            j = client.post(
                "/api/cloud/resolve", json={"url": "https://example.com/a/GAME.pkg"}
            ).json()
            assert j["ok"] is True and j["url_kind"] == "pkg"
            assert j["packages"][0]["manifest_url"] == "https://example.com/a/GAME.pkg"

            for bad in ("ftp://x/y.json", "https://x/y.txt", "notaurl"):
                assert client.post("/api/cloud/resolve", json={"url": bad}).status_code == 400


def test_cloud_push_rejects_bad_input():
    with tempfile.TemporaryDirectory() as root:
        _app_module, client = _cloud_client(root)
        with client:
            base = {"console_ip": "127.0.0.1", "console_port": 9040}
            r = client.post("/api/cloud/push", json={**base, "manifest_url": "ftp://x/y.json"})
            assert r.status_code == 400
            r = client.post(
                "/api/cloud/push",
                json={**base, "manifest_url": "https://x/y.json", "protocol": "bogus"},
            )
            assert r.status_code == 400


def _capture_pushes(app_module):
    """Replace the console push helpers with recorders. Returns the call log."""
    calls = []
    ok = {"response": "0", "code": 0, "code_hex": "0x0"}

    def ez(ip, port, url):
        calls.append(("ezremote", url))
        return ok

    def etahen(ip, port, fields):
        calls.append(("etahen", dict(fields)))
        return ok

    def remote(ip, port, url):
        calls.append(("remote_pkg", url))
        return ok

    app_module._push_ezremote = ez
    app_module._push_etahen_v1 = etahen
    app_module._push_etahen_v2 = etahen
    app_module._push_remote_pkg = remote
    return calls


def test_cloud_push_passes_catalogue_metadata_to_etahen():
    """etaHEN gets the catalogue title and content id in its own fields."""
    with tempfile.TemporaryDirectory() as root:
        app_module, client = _cloud_client(root)
        calls = _capture_pushes(app_module)
        with client:
            j = client.post(
                "/api/cloud/resolve",
                json={"entitlement_id": "UP0700-CUSA28863_00-ELDENRING0000000"},
            ).json()
            pkg = j["packages"][0]
            assert pkg["title"] == "ELDEN RING PS4"
            assert pkg["title_id"] == "CUSA28863"
            assert pkg["platform"] == "ps4"

            for protocol in ("etahen_v1", "etahen_v2"):
                del calls[:]
                r = client.post(
                    "/api/cloud/push",
                    json={
                        "console_ip": "127.0.0.1",
                        "console_port": 9090,
                        "protocol": protocol,
                        "manifest_url": pkg["manifest_url"],
                        "content_id": pkg["content_id"],
                        "name": pkg["name"],
                        "title": pkg["title"],
                    },
                )
                assert r.json()["ok"] is True
                kind, fields = calls[0]
                assert kind == "etahen"
                assert fields["url"] == pkg["manifest_url"]
                assert fields["content_id"] == "UP0700-CUSA28863_00-ELDENRING0000000"
                assert fields["content_name"] == "ELDEN RING PS4"
                assert fields["icon_url"] == ""


def test_cloud_push_leaves_the_cdn_url_undecorated():
    """No query string is appended to a signed Sony URL for any protocol."""
    url = "http://gs2.example/UP0700-CUSA28863_00-ELDENRING0000000.json"
    with tempfile.TemporaryDirectory() as root:
        app_module, client = _cloud_client(root)
        calls = _capture_pushes(app_module)
        with client:
            for protocol in ("ezremote", "remote_pkg"):
                del calls[:]
                r = client.post(
                    "/api/cloud/push",
                    json={
                        "console_ip": "127.0.0.1",
                        "console_port": 9090,
                        "protocol": protocol,
                        "manifest_url": url,
                        "content_id": "UP0700-CUSA28863_00-ELDENRING0000000",
                        "title": "ELDEN RING PS4",
                    },
                )
                assert r.json()["ok"] is True
                assert calls[0] == (protocol, url)


def test_cloud_push_swaps_a_split_piece_for_its_manifest():
    """A pasted PS4 piece URL installs from the manifest beside it; a PS5
    numbered piece has no derivable manifest and goes through untouched."""
    piece = (
        "http://gs2.ww.prod.dl.playstation.net/gs2/ppkgo/prod/CUSA03041_00/48/"
        "f_756e60f4ca0dd7575e21603b66f1d0b49885bf551ba36913e7ad17355b12a8d2/f/"
        "UP1004-CUSA03041_00-REDEMPTION000002-A0132-V0100_2.pkg"
    )
    manifest = piece.replace("_2.pkg", ".json")
    ps5_piece = (
        "http://gst.prod.dl.playstation.net/gst/prod/00/PPSA30449_00/app/pkg/24/"
        "f_f509835c1f6b63c73607d0f09dd40afcc0666aae07ba827fafb75f7a65e96733/"
        "EP4638-PPSA30449_00-XXXXXXXXXXXXXXXX_4.pkg"
    )
    with tempfile.TemporaryDirectory() as root:
        app_module, client = _cloud_client(root)
        calls = _capture_pushes(app_module)
        with client:
            # Resolution hands the piece on untouched; the swap belongs to the
            # push, which is the only step that knows the protocol.
            j = client.post("/api/cloud/resolve", json={"url": piece}).json()
            assert j["ok"] is True and j["url_kind"] == "pkg"
            resolved = j["packages"][0]["manifest_url"]
            assert resolved == piece

            base = {"console_ip": "127.0.0.1", "console_port": 9090}
            r = client.post("/api/cloud/push", json={**base, "manifest_url": resolved})
            assert r.json()["url"] == manifest
            assert calls[-1] == ("ezremote", manifest)

            r = client.post("/api/cloud/push", json={**base, "manifest_url": ps5_piece})
            assert r.json()["url"] == ps5_piece
            assert calls[-1] == ("ezremote", ps5_piece)

            # remote_pkg installs from a pkg header, so it keeps the piece URL
            # even when the browser reached the endpoint the same way.
            r = client.post(
                "/api/cloud/push",
                json={**base, "protocol": "remote_pkg", "manifest_url": resolved},
            )
            assert r.json()["url"] == piece
            assert calls[-1] == ("remote_pkg", piece)


def test_group_sources_numbered_split_with_divergent_sc_name():
    """A numbered chunk set whose SC carries a different content id is paired
    structurally, in chunk order, with the SC last."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        _write_headless_main(root, "JP0005-PPSA03805_00-P5RGAME000000000.pkg",
                             pfs_image_size=0x300000)
        # Cut the main into numbered chunks, as a download splits it.
        whole = os.path.join(root, "JP0005-PPSA03805_00-P5RGAME000000000.pkg")
        data = open(whole, "rb").read()
        os.remove(whole)
        step = len(data) // 3 + 1
        for i in range(3):
            with open(os.path.join(root, f"JP0005-PPSA03805_00-P5RGAME000000000_{i}.pkg"), "wb") as f:
                f.write(data[i * step:(i + 1) * step])
        _write_sc(root, "UP0177-PPSA05109_00-P5RGAME000000000_sc.pkg",
                  content_id="UP0177-PPSA05109_00-P5RGAME000000000", title="P5R",
                  pfs_image_size=0x300000)

        sources = group_sources(find_pkgs([root]))
        assert len(sources) == 1
        s = sources[0]
        assert s["split"] is True
        assert [os.path.basename(p) for p in s["parts"]] == [
            "JP0005-PPSA03805_00-P5RGAME000000000_0.pkg",
            "JP0005-PPSA03805_00-P5RGAME000000000_1.pkg",
            "JP0005-PPSA03805_00-P5RGAME000000000_2.pkg",
            "UP0177-PPSA05109_00-P5RGAME000000000_sc.pkg",
        ]


def test_group_sources_numbered_split_same_stem_sc():
    """A same-stem SC keeps its filename-based pairing."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        for i in range(2):
            with open(os.path.join(root, f"GAME-CUSA1_{i}.pkg"), "wb") as f:
                f.write(b"\x00" * 64)
        with open(os.path.join(root, "GAME-CUSA1_sc.pkg"), "wb") as f:
            f.write(b"\x00" * 64)

        sources = group_sources(find_pkgs([root]))
        assert len(sources) == 1
        assert [os.path.basename(p) for p in sources[0]["parts"]] == [
            "GAME-CUSA1_0.pkg", "GAME-CUSA1_1.pkg", "GAME-CUSA1_sc.pkg",
        ]


def test_group_sources_standalone_cnt_not_paired_as_sc():
    """A bare-CNT package NOT named ``_sc.pkg`` (e.g. a -MERGED standalone) is
    never treated as an SC tail, even when a same-size headless main is present.
    It stays a standalone single-file source, and the real ``_sc.pkg`` pairs with
    the main."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        _write_headless_main(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg", pfs_image_size=0x4e00000)
        # Its true SC tail (same stem, _sc.pkg) -> pairs with the main.
        _write_sc(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000_sc.pkg",
                  content_id="UP4381-PPSA01650_00-YOUTUBESIEA00000", title="YouTube",
                  pfs_image_size=0x4e00000)
        # A -MERGED standalone (same content, same PFS size, but NOT _sc.pkg).
        _write_sc(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000-MERGED.pkg",
                  content_id="UP4381-PPSA01650_00-YOUTUBESIEA00000", title="YouTube",
                  pfs_image_size=0x4e00000)

        sources = group_sources(find_pkgs([root]))
        pkg_by_name = {s["name"]: s for s in sources}
        # The main + its _sc pair into one two-part package.
        pair = next(s for s in sources if len(s["parts"]) == 2)
        assert os.path.basename(pair["parts"][0]) == "UP4381-PPSA01650_00-YOUTUBESIEA00000.pkg"
        assert os.path.basename(pair["parts"][1]) == "UP4381-PPSA01650_00-YOUTUBESIEA00000_sc.pkg"
        # The -MERGED file stands alone, paired with nothing.
        merged = pkg_by_name["UP4381-PPSA01650_00-YOUTUBESIEA00000-MERGED.pkg"]
        assert merged["parts"] == [
            os.path.join(root, "UP4381-PPSA01650_00-YOUTUBESIEA00000-MERGED.pkg")
        ]
        assert merged["split"] is False


def test_group_sources_sc_not_paired_across_directories():
    """A main and an SC in different directories are never paired -- a split
    package is always delivered with its SC alongside the main."""
    from pkgtool.scan import group_sources, find_pkgs

    with tempfile.TemporaryDirectory() as root:
        lib_a = os.path.join(root, "libA")
        lib_b = os.path.join(root, "libB")
        os.makedirs(lib_a)
        os.makedirs(lib_b)
        _write_headless_main(lib_a, "UP0000-PPSA00001_00-APP0000000000000.pkg", pfs_image_size=0x300000)
        # Same PFS size, but the SC lives in a different directory -> no pairing.
        _write_sc(lib_b, "EP0000-PPSA00002_00-APP0000000000000_sc.pkg",
                  content_id="EP0000-PPSA00002_00-APP0000000000000", title="App",
                  pfs_image_size=0x300000)

        sources = group_sources(find_pkgs([root]))
        assert all(len(s["parts"]) == 1 for s in sources)
        assert {os.path.basename(s["parts"][0]) for s in sources} == {
            "UP0000-PPSA00001_00-APP0000000000000.pkg",
            "EP0000-PPSA00002_00-APP0000000000000_sc.pkg",
        }


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
