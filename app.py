"""FastAPI web app: scan directories for PS4 PKGs and serve a metadata listing.

Configuration (environment variables):
  PKG_DIRS   - os-path-separated list of directories to scan (required)
  ICON_DIR   - where to cache extracted icons (default: ./cache/icons)
  SCAN_WORKERS - parallel parse workers (default: 8)

Run:
  set PKG_DIRS=C:\\path\\to\\pkgs
  uvicorn app:app --reload
"""

from __future__ import annotations

import html
import os
import socket
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pkgtool import ConcatSource
from pkgtool.scan import PkgRecord, ScanResult, scan, group_by_title_id

ICON_DIR = os.environ.get("ICON_DIR", os.path.join("cache", "icons"))
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))
# Optional override for the host:port the console downloads from. Useful when
# the auto-detected address is wrong (e.g. docker bridge networking or a proxy).
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
# How long to wait (seconds) for the console to reply with the install result code.
PUSH_RESPONSE_TIMEOUT = float(os.environ.get("PUSH_RESPONSE_TIMEOUT", "10"))


def _configured_dirs() -> List[str]:
    raw = os.environ.get("PKG_DIRS", "").strip()
    if not raw:
        return []
    return [d for d in raw.split(os.pathsep) if d.strip()]


class AppState:
    def __init__(self) -> None:
        self.dirs: List[str] = _configured_dirs()
        self.result: Optional[ScanResult] = None
        self.index: Dict[str, PkgRecord] = {}

    def rescan(self) -> ScanResult:
        self.result = scan(self.dirs, icon_dir=ICON_DIR, workers=SCAN_WORKERS)
        # Build id -> record lookup for downloads/pushes. Only ids present here
        # are downloadable, which prevents arbitrary path access.
        self.index = {r.id: r for r in self.result.records if r.id}
        return self.result


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(ICON_DIR, exist_ok=True)
    if state.dirs:
        state.rescan()
    yield


app = FastAPI(title="PS PKG Server", lifespan=lifespan)
# Ensure the cache dir exists before mounting (StaticFiles validates at init).
os.makedirs(ICON_DIR, exist_ok=True)
app.mount("/icons", StaticFiles(directory=ICON_DIR), name="icons")


def _icon_tag(icon: Optional[str], small: bool = False) -> str:
    if icon:
        return f"<img loading='lazy' src='/icons/{html.escape(icon)}' alt=''>"
    label = "" if small else "no icon"
    return f"<div class='noicon'>{label}</div>"


def _badge(kind: Optional[str]) -> str:
    if not kind:
        return ""
    kind_class = "kind-" + kind.lower().replace(" ", "")
    return f"<span class='badge {kind_class}'>{html.escape(kind)}</span>"


def _edition_badge(edition: Optional[str]) -> str:
    if not edition:
        return ""
    return f"<span class='badge ed-{edition.lower()}'>{html.escape(edition)}</span>"


def _compat_badge(compat: Optional[str]) -> str:
    """Base<->update compatibility badge (PS4 marriage check)."""
    if compat == "married":
        return "<span class='badge compat-married' title='Update is compatible with the base game'>&#10084; married</span>"
    if compat == "mismatch":
        return "<span class='badge compat-mismatch' title='Update will NOT install: its playgo digest does not match the base game'>&#10008; mismatch</span>"
    return ""


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _render_index(result: Optional[ScanResult]) -> str:
    if result is None:
        body = "<p class='empty'>No scan has run yet. Set <code>PKG_DIRS</code> and rescan.</p>"
        count = 0
    elif result.total == 0:
        dirs = ", ".join(html.escape(d) for d in state.dirs) or "(none configured)"
        body = f"<p class='empty'>No .pkg files found in: {dirs}</p>"
        count = 0
    else:
        groups = group_by_title_id(result.records)
        count = len(groups)

        group_html = []
        for g in groups:
            icon_html = _icon_tag(g.icon)
            kind_badges = "".join(_badge(k) for k in g.kinds)

            member_rows = []
            for m in g.members:
                member_rows.append(
                    f"""
                    <div class="member" data-id="{html.escape(m.id)}">
                      <div class="micon">{_icon_tag(m.icon, small=True)}</div>
                      <div class="minfo">
                        <div class="mtitle"><span class="mname">{html.escape(m.title or m.filename)}</span>{_badge(m.kind)}{_edition_badge(m.edition)}{_compat_badge(m.compat)}</div>
                        <div class="msub">v{html.escape(m.version or '-')} &middot; {_fmt_size(m.size)} &middot; {html.escape(m.content_id or '-')}</div>
                        <div class="path" title="{html.escape(m.path)}">{html.escape(m.filename)}</div>
                      </div>
                      <div class="mstatus"></div>
                      <div class="mactions">
                        <a class="btn dl" href="/download/{html.escape(m.id)}" title="Download" download>&#8681;</a>
                        <button class="btn push" onclick="push('{html.escape(m.id)}', this)" title="Send to console">&#10132;</button>
                      </div>
                    </div>"""
                )

            group_html.append(
                f"""
                <details class="group">
                  <summary>
                    <div class="icon">{icon_html}</div>
                    <div class="ginfo">
                      <div class="gtitle">{html.escape(g.title or g.title_id)}<span class="gcount">{g.count}</span></div>
                      <div class="gsub">{html.escape(g.platform or '?')} &middot; {html.escape(g.title_id)} &middot; {html.escape(g.region)}{(' &middot; build ' + html.escape(g.build)) if g.build else ''}</div>
                      <div class="gkinds">{_edition_badge(g.edition)}{kind_badges}</div>
                    </div>
                    <button class="btn sendall" onclick="sendAll(event, this)" title="Send all to console">Send all</button>
                    <div class="chevron">&#9656;</div>
                  </summary>
                  <div class="members">{''.join(member_rows)}</div>
                </details>"""
            )

        body = f"<div class='groups'>{''.join(group_html)}</div>"

        if result.errors:
            err_rows = "".join(
                f"<li>{html.escape(e.filename)} — {html.escape(e.error or '')}</li>"
                for e in result.errors
            )
            body += f"<details class='errors'><summary>{len(result.errors)} file(s) failed to parse</summary><ul>{err_rows}</ul></details>"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PS PKG Server</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ font-family: system-ui, sans-serif; margin: 0; background: #14161a; color: #e8eaed; }}
  header {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 12px 16px; border-bottom: 1px solid #2a2e35; position: sticky; top: 0; background: #14161a; z-index: 5; }}
  .hleft {{ display: flex; align-items: baseline; gap: 10px; min-width: 0; }}
  .hleft h1 {{ font-size: 18px; margin: 0; white-space: nowrap; }}
  .count {{ color: #9aa0a6; font-size: 13px; white-space: nowrap; }}
  .hright {{ display: flex; align-items: center; gap: 8px; margin-left: auto; }}
  button {{ background: #3b82f6; color: white; border: 0; padding: 9px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }}
  button:hover {{ background: #2563eb; }}
  button.rescan {{ margin: 0; flex: 0 0 auto; white-space: nowrap; }}
  .groups {{ display: flex; flex-direction: column; gap: 10px; padding: 16px 24px 24px; max-width: 900px; }}
  .group {{ background: #1c1f26; border: 1px solid #2a2e35; border-radius: 10px; overflow: hidden; }}
  .group summary {{ display: flex; align-items: center; gap: 14px; padding: 12px 14px; cursor: pointer; list-style: none; }}
  .group summary::-webkit-details-marker {{ display: none; }}
  .group summary:hover {{ background: #21252d; }}
  .icon img, .icon .noicon {{ width: 72px; height: 72px; border-radius: 8px; object-fit: cover; background: #2a2e35; }}
  .noicon {{ display: flex; align-items: center; justify-content: center; color: #6b7280; font-size: 11px; }}
  .ginfo {{ min-width: 0; flex: 1; }}
  .gtitle {{ font-weight: 600; font-size: 15px; display: flex; align-items: center; gap: 8px; }}
  .gcount {{ font-size: 11px; color: #9aa0a6; background: #2a2e35; border-radius: 10px; padding: 1px 8px; }}
  .gsub {{ font-size: 12px; color: #7c828a; margin: 3px 0 6px; }}
  .gkinds {{ display: flex; gap: 6px; flex-wrap: wrap; }}
  .chevron {{ color: #6b7280; transition: transform .15s ease; }}
  .group[open] .chevron {{ transform: rotate(90deg); }}
  .members {{ border-top: 1px solid #2a2e35; padding: 6px 14px 10px; display: flex; flex-direction: column; }}
  .member {{ display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid #23272f; align-items: center; border-radius: 6px; transition: background .15s ease; }}
  .member:last-child {{ border-bottom: 0; }}
  .member.pushing {{ background: rgba(59,130,246,0.12); }}
  .mstatus {{ font-size: 11px; flex: 0 0 auto; text-align: right; color: #7c828a; font-variant-numeric: tabular-nums; }}
  .mstatus.pushing {{ color: #93c5fd; }}
  .mstatus.sent {{ color: #86efac; }}
  .mstatus.failed {{ color: #f87171; }}
  .micon img, .micon .noicon {{ width: 40px; height: 40px; border-radius: 6px; object-fit: cover; background: #2a2e35; }}
  .minfo {{ min-width: 0; flex: 1; }}
  .mtitle {{ font-size: 13px; display: flex; align-items: center; gap: 8px; min-width: 0; }}
  .mname {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }}
  .mtitle .badge {{ flex: 0 0 auto; }}
  .msub {{ font-size: 11px; color: #7c828a; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .mactions {{ display: flex; align-items: center; gap: 6px; flex: 0 0 auto; }}
  .btn {{ margin: 0; width: 34px; height: 34px; padding: 0; display: inline-flex; align-items: center; justify-content: center; font-size: 16px; border-radius: 8px; text-decoration: none; }}
  .btn.dl {{ background: #374151; color: #e8eaed; }}
  .btn.dl:hover {{ background: #4b5563; }}
  .btn.push {{ background: #059669; color: white; }}
  .btn.push:hover {{ background: #047857; }}
  .btn.sendall {{ width: auto; height: 32px; padding: 0 12px; font-size: 12px; background: #059669; color: white; }}
  .btn.sendall:hover {{ background: #047857; }}
  .btn.sendall:disabled {{ background: #374151; color: #9aa0a6; cursor: default; }}
  .console {{ display: flex; align-items: center; gap: 6px; }}
  /* 16px font keeps iOS from zooming when focusing an input. */
  .console input {{ background: #1c1f26; border: 1px solid #2a2e35; color: #e8eaed; border-radius: 6px; padding: 8px 10px; font-size: 16px; min-width: 0; }}
  .console #cip {{ width: 130px; }}
  .console #cport {{ width: 72px; }}
  .badge {{ font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 10px; white-space: nowrap; text-transform: uppercase; letter-spacing: .03em; background: #374151; color: #d1d5db; }}
  .badge.kind-basegame {{ background: #14532d; color: #86efac; }}
  .badge.kind-update {{ background: #1e3a5f; color: #93c5fd; }}
  .badge.kind-dlc {{ background: #4c1d95; color: #c4b5fd; }}
  .badge.kind-app {{ background: #78350f; color: #fcd34d; }}
  .badge.ed-retail {{ background: #134e4a; color: #5eead4; }}
  .badge.ed-debug {{ background: #3f3f46; color: #e4e4e7; }}
  .badge.ed-fpkg {{ background: #7f1d1d; color: #fca5a5; }}
  .badge.compat-married {{ background: #14532d; color: #86efac; }}
  .badge.compat-mismatch {{ background: #7f1d1d; color: #fca5a5; }}
  .btn.sendall {{ flex: 0 0 auto; }}
  .row {{ font-size: 12px; color: #c3c7cc; display: flex; gap: 8px; }}
  .row span {{ color: #7c828a; min-width: 78px; display: inline-block; }}
  .path {{ font-size: 11px; color: #6b7280; margin-top: 6px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .empty {{ padding: 40px 24px; color: #9aa0a6; }}
  .errors {{ margin: 0 24px 24px; color: #f59e0b; font-size: 13px; }}
  .errors ul {{ color: #c3c7cc; }}

  /* Narrow screens: the console + rescan drop to their own full-width row,
     the IP field grows to fill, and the group padding tightens. */
  @media (max-width: 600px) {{
    .hright {{ width: 100%; margin-left: 0; flex-wrap: wrap; }}
    .console {{ flex: 1 1 auto; }}
    .console #cip {{ flex: 1; width: auto; }}
    .groups {{ padding: 12px 12px 24px; }}
    .group summary {{ gap: 10px; padding: 10px; }}
    .icon img, .icon .noicon {{ width: 56px; height: 56px; }}
    .gtitle {{ font-size: 14px; }}
    .members {{ padding: 4px 10px 8px; }}
  }}

  /* Very narrow (e.g. folding-phone cover screens): stack the console controls,
     make Rescan full-width, and shrink the rows so nothing overflows. */
  @media (max-width: 400px) {{
    .console {{ flex: 1 1 100%; }}
    .rescan {{ width: 100%; }}
    .group summary {{ gap: 8px; padding: 9px; }}
    .icon img, .icon .noicon {{ width: 46px; height: 46px; }}
    .gcount {{ display: none; }}
    .member {{ gap: 8px; }}
    .micon img, .micon .noicon {{ width: 32px; height: 32px; }}
    .mactions {{ gap: 4px; }}
    .btn {{ width: 30px; height: 30px; font-size: 14px; }}
    .btn.sendall {{ padding: 0 8px; font-size: 11px; height: 30px; }}
    .path {{ display: none; }}
    .mstatus {{ font-size: 10px; }}
  }}
</style>
</head>
<body>
<header>
  <div class="hleft">
    <h1>PS PKG Server</h1>
    <span class="count">{count} title(s)</span>
  </div>
  <div class="hright">
    <div class="console">
      <input id="cip" placeholder="Console IP" autocomplete="off" inputmode="decimal">
      <input id="cport" placeholder="Port" value="9040" autocomplete="off" inputmode="numeric">
    </div>
    <button class="rescan" onclick="rescan(this)">Rescan</button>
  </div>
</header>
{body}
<script>
async function rescan(btn) {{
  btn.disabled = true; btn.textContent = 'Scanning...';
  try {{ await fetch('/api/rescan', {{ method: 'POST' }}); location.reload(); }}
  finally {{ btn.disabled = false; btn.textContent = 'Rescan'; }}
}}
// Persist console IP/port across reloads.
['cip', 'cport'].forEach(id => {{
  const el = document.getElementById(id);
  const key = 'pkgserver_' + id;
  const saved = localStorage.getItem(key);
  if (saved) el.value = saved;
  el.addEventListener('change', () => localStorage.setItem(key, el.value));
}});

const PUSH_DELAY_MS = 1000;  // gap AFTER the console responds, before the next push
const sleep = ms => new Promise(r => setTimeout(r, ms));

function getConsole() {{
  const ip = document.getElementById('cip').value.trim();
  const port = parseInt(document.getElementById('cport').value, 10);
  if (!ip || !port) {{ alert('Enter the console IP and port first.'); return null; }}
  return {{ ip, port }};
}}

async function doPush(id, ip, port) {{
  try {{
    const r = await fetch('/api/push', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ console_ip: ip, console_port: port, pkg_id: id }})
    }});
    return await r.json();
  }} catch (e) {{
    return {{ ok: false, error: String(e) }};
  }}
}}

function setStatus(member, state, text, title) {{
  if (!member) return;
  member.classList.toggle('pushing', state === 'pushing');
  const el = member.querySelector('.mstatus');
  if (!el) return;
  el.className = 'mstatus ' + state;
  el.textContent = text || '';
  el.title = title || '';
}}

// Turn a /api/push result into a status label. The console returns an install
// result code: 0 = accepted, non-zero = error (shown as hex, e.g. 0x80B22416).
function resultLabel(j) {{
  if (!j.ok) return {{ state: 'failed', text: 'Failed', title: j.error || 'connection failed' }};
  if (j.code === 0) return {{ state: 'sent', text: 'OK', title: 'Install accepted (0)' }};
  if (j.code === null || j.code === undefined)
    return {{ state: 'sent', text: 'Sent', title: j.response ? ('console reply: ' + j.response) : 'no reply from console' }};
  return {{
    state: 'failed',
    text: j.code_hex || String(j.code),
    title: 'Console returned ' + j.code + ' (' + j.code_hex + ')',
  }};
}}

async function push(id, btn) {{
  const c = getConsole(); if (!c) return;
  const member = btn.closest('.member');
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '&hellip;';
  setStatus(member, 'pushing', 'Sending\u2026');
  const j = await doPush(id, c.ip, c.port);
  const r = resultLabel(j);
  setStatus(member, r.state, r.text, r.title);
  btn.innerHTML = old; btn.disabled = false;
  if (r.state === 'failed') alert('Push result: ' + r.text + '\\n' + r.title);
}}

async function sendAll(ev, btn) {{
  ev.preventDefault(); ev.stopPropagation();
  const c = getConsole(); if (!c) return;
  const group = btn.closest('.group');
  group.open = true;  // expand so status is visible
  const members = Array.from(group.querySelectorAll('.member'));
  const old = btn.textContent; btn.disabled = true;
  for (let i = 0; i < members.length; i++) {{
    const m = members[i];
    btn.textContent = 'Sending ' + (i + 1) + '/' + members.length;
    setStatus(m, 'pushing', 'Sending\u2026');
    const j = await doPush(m.dataset.id, c.ip, c.port);
    const r = resultLabel(j);
    setStatus(m, r.state, r.text, r.title);
    if (i < members.length - 1) await sleep(PUSH_DELAY_MS);
  }}
  btn.textContent = old; btn.disabled = false;
}}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_render_index(state.result))


@app.get("/api/pkgs")
def api_pkgs() -> JSONResponse:
    result = state.result
    if result is None:
        return JSONResponse({"total": 0, "records": [], "errors": []})
    return JSONResponse(
        {
            "total": result.total,
            "records": [r.to_dict() for r in result.records],
            "errors": [r.to_dict() for r in result.errors],
        }
    )


@app.get("/api/groups")
def api_groups() -> JSONResponse:
    result = state.result
    if result is None:
        return JSONResponse({"total": 0, "groups": []})
    groups = group_by_title_id(result.records)
    return JSONResponse(
        {"total": len(groups), "groups": [g.to_dict() for g in groups]}
    )


@app.post("/api/rescan")
def api_rescan() -> JSONResponse:
    result = state.rescan()
    return JSONResponse({"total": result.total, "scanned_dirs": state.dirs})


@app.get("/download/{pkg_id}")
def download(pkg_id: str, request: Request):
    """Serve a scanned PKG. Single files use FileResponse; split sets are served
    as one contiguous stream with HTTP range support (resumable console installs)."""
    record = state.index.get(pkg_id)
    if record is None:
        return JSONResponse({"error": "package not found"}, status_code=404)

    parts = record.parts or [record.path]
    if len(parts) == 1:
        if not os.path.isfile(parts[0]):
            return JSONResponse({"error": "package not found"}, status_code=404)
        return FileResponse(
            parts[0], media_type="application/octet-stream", filename=record.filename
        )

    return _serve_split(parts, record.filename, request)


def _serve_split(parts, filename: str, request: Request):
    """Serve an ordered set of split part files as one contiguous, range-capable
    download."""
    for p in parts:
        if not os.path.isfile(p):
            return JSONResponse({"error": "package part missing"}, status_code=404)

    sizes = [os.path.getsize(p) for p in parts]
    total = sum(sizes)

    start, end = 0, total - 1
    status = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        first, _, last = range_header[6:].split(",")[0].strip().partition("-")
        if first == "":  # suffix range: last N bytes
            start = max(0, total - int(last))
        else:
            start = int(first)
            end = int(last) if last else total - 1
        end = min(end, total - 1)
        if start > end or start >= total:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    length = end - start + 1
    headers["Content-Length"] = str(length)

    # HEAD: headers only, don't stream the body.
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type="application/octet-stream")

    def body():
        src = ConcatSource(list(zip(parts, sizes)))
        try:
            remaining = length
            pos = start
            chunk = 1024 * 1024
            while remaining > 0:
                take = min(chunk, remaining)
                yield src.read(pos, take)
                pos += take
                remaining -= take
        finally:
            src.close()

    return StreamingResponse(
        body(), status_code=status, media_type="application/octet-stream", headers=headers
    )


class PushRequest(BaseModel):
    console_ip: str
    console_port: int
    pkg_id: str


@app.post("/api/push")
def api_push(req: PushRequest, request: Request) -> JSONResponse:
    """Send a package's download URL to a console over a raw TCP connection.

    This behaves like echoing the URL to the console's listening port with
    netcat. The URL carries the content id, name, and relative icon url as
    query parameters.
    """
    record = state.index.get(req.pkg_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "unknown package"}, status_code=404)

    # Port the HTTP server is reachable on (as seen by this request).
    http_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    icon_rel = f"/icons/{record.icon}" if record.icon else ""

    try:
        with socket.create_connection((req.console_ip, req.console_port), timeout=5) as sock:
            # The local address used to reach the console is the server address
            # the console can reach us on. PUBLIC_HOST overrides this when needed.
            server_ip = sock.getsockname()[0]
            authority = PUBLIC_HOST or f"{server_ip}:{http_port}"
            params = urlencode(
                {
                    "content_id": record.content_id or "",
                    "name": record.title or record.filename,
                    "icon": icon_rel,
                }
            )
            url = f"http://{authority}/download/{req.pkg_id}?{params}"
            sock.sendall((url + "\n").encode("utf-8"))
            response = _read_console_response(sock)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)

    code, code_hex = _parse_console_code(response)
    return JSONResponse(
        {
            "ok": True,
            "url": url,
            "server_ip": server_ip,
            "response": response,
            "code": code,
            "code_hex": code_hex,
        }
    )


def _read_console_response(sock: socket.socket) -> str:
    """Read the console's reply (the install result code as a decimal string).

    The ezremote-dpi payload sends the sceAppInstUtil return code, then closes.
    We read until the peer closes or the timeout elapses.
    """
    sock.settimeout(PUSH_RESPONSE_TIMEOUT)
    chunks: List[bytes] = []
    try:
        while True:
            chunk = sock.recv(256)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 256:
                break
    except (OSError, socket.timeout):
        pass
    return b"".join(chunks).decode("utf-8", "replace").strip().strip("\x00").strip()


def _parse_console_code(response: str):
    """Return (int_code, hex_string) from the console reply, or (None, None).

    The console reports a signed 32-bit integer; the hex form is its unsigned
    two's-complement representation, e.g. -2135813882 -> 0x80B22416.
    """
    if not response:
        return None, None
    try:
        code = int(response)
    except ValueError:
        return None, None
    return code, f"0x{code & 0xFFFFFFFF:08X}"
