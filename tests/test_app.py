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


def test_push_goldhen():
    """GoldHEN installer: JSON {id,contentUrl,contentName,iconPath} over raw TCP."""
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
                conn.sendall(b'{"status":"success"}')
                conn.close()

            t = threading.Thread(target=accept)
            t.start()

            resp = client.post(
                "/api/push",
                json={
                    "console_ip": "127.0.0.1",
                    "console_port": port,
                    "pkg_id": pkg_id,
                    "protocol": "goldhen",
                },
            )
            t.join(timeout=5)
            srv.close()

            body = resp.json()
            assert body["ok"] is True, body
            assert body["protocol"] == "goldhen"
            assert body["code"] == 0  # {"status":"success"} -> 0

            sent = json.loads(received["data"])
            # GoldHEN's BGFT-style field names (not the etaHEN shape).
            assert sent["contentUrl"] == f"http://{body['server_ip']}:80/download/{pkg_id}"
            assert "?" not in sent["contentUrl"]
            assert sent["id"] == alpha["content_id"]
            assert sent["contentName"] == "Alpha"
            assert sent["iconPath"].startswith("http://")
            assert "/icons/" in sent["iconPath"]


def test_parse_goldhen_responses():
    import importlib
    import app as app_module
    importlib.reload(app_module)

    assert app_module._parse_goldhen_response('{"status":"success"}') == (0, "0x00000000")
    assert app_module._parse_goldhen_response('{"res":"0"}') == (0, "0x00000000")
    assert app_module._parse_goldhen_response('{"error_code":2157510663}') == (
        -2137456633,
        "0x80990007",
    )
    # Bare int fallback and unknown shapes.
    assert app_module._parse_goldhen_response("0") == (0, "0x00000000")
    assert app_module._parse_goldhen_response('{"whatever":1}') == (None, None)
    assert app_module._parse_goldhen_response("") == (None, None)


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
