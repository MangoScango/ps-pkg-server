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
import json
import os
import re
import socket
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from xml.etree import ElementTree

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pkgtool import ConcatSource
from pkgtool import entitlements
from pkgtool.scan import PkgRecord, ScanResult, scan, group_by_title_id

ICON_DIR = os.environ.get("ICON_DIR", os.path.join("cache", "icons"))
SCAN_WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))
# Optional override for the host:port the console downloads from. Useful when
# the auto-detected address is wrong (e.g. docker bridge networking or a proxy).
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()
# How long to wait (seconds) for the console to reply with the install result code.
PUSH_RESPONSE_TIMEOUT = float(os.environ.get("PUSH_RESPONSE_TIMEOUT", "10"))
# Entitlement catalogue backing cloud push.
ENTITLEMENTS_CSV = os.environ.get("ENTITLEMENTS_CSV", "entitlements_all.csv")
# Where users export their own catalogue from.
ENTITLEMENTS_SOURCE_URL = "https://garlicsaves.com/tools/entitlements"
IN_CONTAINER = os.path.exists("/.dockerenv")
ENTITLEMENTS_MAX_UPLOAD = 64 * 1024 * 1024


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
        self._entitlements: Optional[List[entitlements.Entitlement]] = None

    @property
    def entitlements(self) -> List[entitlements.Entitlement]:
        if self._entitlements is None:
            self._entitlements = entitlements.load(ENTITLEMENTS_CSV)
        return self._entitlements

    def reload_entitlements(self) -> List[entitlements.Entitlement]:
        self._entitlements = None
        return self.entitlements

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


def _firmware_html(platform: Optional[str], min_sdk: Optional[str],
                   min_ps5_fw: Optional[str]) -> str:
    """Minimum-firmware segments for a member's detail line.

    A PS5 package shows its own requirement; a PS4 package shows the PS4 level it
    needs plus the lowest PS5 system software that can host it.
    """
    out = ""
    if platform == "PS4":
        if min_sdk:
            out += (
                " &middot; <span title='Minimum PS4 system software version'>"
                f"PS4 {html.escape(min_sdk)}</span>"
            )
        if min_ps5_fw:
            out += (
                " &middot; <span title='Lowest PS5 system software that can run this'>"
                f"PS5 {html.escape(min_ps5_fw)}</span>"
            )
    elif min_sdk:
        out += (
            " &middot; <span title='Minimum system software version'>"
            f"FW {html.escape(min_sdk)}</span>"
        )
    return out


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
                        <div class="msub">v{html.escape(m.version or '-')}{_firmware_html(m.platform, m.min_sdk, m.min_ps5_fw)} &middot; {_fmt_size(m.size)} &middot; {html.escape(m.content_id or '-')}</div>
                        <div class="path" title="{html.escape(m.path)}">{html.escape(m.filename)}</div>
                      </div>
                      <div class="mstatus"></div>
                      <div class="mactions">
                        <a class="btn dl" href="/download/{html.escape(m.id)}" title="Download" download>&#8681;</a>
                        <button class="btn push" onclick="push('{html.escape(m.id)}', this)" title="Install" aria-label="Install"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8h9M8 4l4 4-4 4"/></svg></button>
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
                    <button class="btn sendall" onclick="sendAll(event, this)" title="Install all">Install all</button>
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
  .hright {{ display: flex; align-items: center; gap: 8px; margin-left: auto; }}
  button {{ background: #3b82f6; color: white; border: 0; padding: 9px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }}
  button:hover {{ background: #2563eb; }}
  button.rescan {{ margin: 0; flex: 0 0 auto; white-space: nowrap; }}
  button.cloudbtn {{ margin: 0; flex: 0 0 auto; width: 38px; height: 36px; padding: 0; display: inline-flex; align-items: center; justify-content: center; background: #374151; }}
  button.cloudbtn:hover {{ background: #4b5563; }}
  .modal {{ position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 20; display: flex; align-items: flex-start; justify-content: center; padding: 60px 16px 16px; }}
  .modal[hidden] {{ display: none; }}
  .sheet {{ background: #1a1d22; border: 1px solid #2a2e35; border-radius: 12px; width: 100%; max-width: 680px; display: flex; flex-direction: column; max-height: 80vh; overflow: hidden; }}
  .sheethead {{ display: flex; gap: 8px; padding: 12px; border-bottom: 1px solid #2a2e35; }}
  .sheethead input {{ flex: 1 1 auto; min-width: 0; }}
  .sheethead select {{ flex: 0 0 auto; }}
  .btn.close {{ background: #374151; color: #e8eaed; font-size: 22px; line-height: 1; }}
  .btn.close:hover {{ background: #4b5563; }}
  .sheetbody {{ overflow-y: auto; padding: 6px; }}
  .crow {{ display: flex; align-items: center; gap: 10px; padding: 9px 10px; border-radius: 8px; cursor: pointer; }}
  .crow:hover {{ background: #22262c; }}
  .crow .cmain {{ min-width: 0; flex: 1 1 auto; }}
  .crow .cname {{ font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .crow .csub {{ font-size: 11px; color: #7c828a; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .cnote {{ padding: 14px 12px; color: #7c828a; font-size: 12px; }}
  .csetup {{ padding: 14px 14px 18px; font-size: 12.5px; line-height: 1.55; color: #c4c8ce; }}
  .csetup h2 {{ font-size: 13px; margin: 0 0 8px; color: #e8eaed; }}
  .csetup ol {{ margin: 8px 0 12px; padding-left: 20px; }}
  .csetup li {{ margin: 4px 0; }}
  .csetup a {{ color: #93c5fd; }}
  .csetup code {{ background: #101216; border: 1px solid #2a2e35; border-radius: 4px; padding: 1px 5px; font-size: 11.5px; word-break: break-all; }}
  .cback {{ background: none; color: #93c5fd; padding: 6px 10px; font-size: 12px; }}
  .cback:hover {{ background: #22262c; }}
  /* Base = single column stack (also the no-JS fallback). JS turns this into a
     row of independent column stacks (.cols + .gcol) so expanding one card only
     grows its own column instead of leaving gaps across a shared grid row. */
  .groups {{ display: flex; flex-direction: column; gap: 10px; padding: 16px 24px 24px; max-width: 900px; margin: 0 auto; }}
  .groups.cols {{ flex-direction: row; align-items: flex-start; }}
  .gcol {{ display: flex; flex-direction: column; gap: 10px; flex: 1 1 0; min-width: 0; }}
  .group {{ background: #1c1f26; border: 1px solid #2a2e35; border-radius: 10px; overflow: hidden; min-width: 0; }}
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
  .console input, .console select {{ background: #1c1f26; border: 1px solid #2a2e35; color: #e8eaed; border-radius: 6px; padding: 8px 10px; font-size: 16px; min-width: 0; }}
  .console select {{ cursor: pointer; max-width: 100%; }}
  .console #cip {{ width: 130px; }}
  .console #cport {{ width: 72px; }}
  .badge {{ font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 10px; white-space: nowrap; text-transform: uppercase; letter-spacing: .03em; background: #374151; color: #d1d5db; }}
  .badge.kind-game {{ background: #14532d; color: #86efac; }}
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
    /* Stack the console: protocol on its own row, IP + port share the next.
       Keeps the wide protocol <select> from forcing horizontal scroll. */
    .console {{ flex: 1 1 100%; flex-wrap: wrap; }}
    .console select {{ flex: 1 1 100%; }}
    .console #cip {{ flex: 1 1 120px; width: auto; }}
    .console #cport {{ flex: 0 0 80px; width: auto; }}
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

  /* Wider viewports get more room; the JS picks 2 then 3 columns to match. */
  @media (min-width: 900px) {{
    .groups {{ max-width: 1280px; }}
  }}
  @media (min-width: 1320px) {{
    .groups {{ max-width: 1860px; }}
  }}
</style>
</head>
<body>
<header>
  <div class="hleft">
    <h1>PS PKG Server</h1>
  </div>
  <div class="hright">
    <div class="console">
      <select id="cproto" title="Console install protocol">
        <option value="ezremote">ezremote-dpi</option>
        <option value="etahen_v1">etaHEN DPI v1</option>
        <option value="etahen_v2">etaHEN DPI v2</option>
        <option value="remote_pkg">PS4 Remote PKG Installer</option>
      </select>
      <input id="cip" placeholder="Console IP" autocomplete="off" inputmode="decimal">
      <input id="cport" placeholder="Port" value="9040" autocomplete="off" inputmode="numeric">
    </div>
    <button class="cloudbtn" onclick="cloudOpen()" title="Install from the cloud" aria-label="Install from the cloud"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/></svg></button>
    <button class="rescan" onclick="rescan(this)">Rescan</button>
  </div>
</header>
<div id="cloud" class="modal" hidden>
  <div class="sheet" role="dialog" aria-modal="true" aria-label="Install from the cloud">
    <div class="sheethead">
      <input id="cloudq" placeholder="Search titles..." autocomplete="off" spellcheck="false">
      <select id="cloudplat" title="Filter by platform">
        <option value="">All</option>
        <option value="ps5">PS5</option>
        <option value="ps4">PS4</option>
      </select>
      <button class="btn close" onclick="cloudClose()" aria-label="Close">&times;</button>
    </div>
    <div id="cloudbody" class="sheetbody"></div>
  </div>
</div>
{body}
<script>
async function rescan(btn) {{
  btn.disabled = true; btn.textContent = 'Scanning...';
  try {{ await fetch('/api/rescan', {{ method: 'POST' }}); location.reload(); }}
  finally {{ btn.disabled = false; btn.textContent = 'Rescan'; }}
}}
// Persist console protocol/IP/port across reloads.
['cproto', 'cip', 'cport'].forEach(id => {{
  const el = document.getElementById(id);
  const key = 'pkgserver_' + id;
  const saved = localStorage.getItem(key);
  if (saved) el.value = saved;
  el.addEventListener('change', () => localStorage.setItem(key, el.value));
}});

// Default listen ports per protocol. Switching protocol updates the port only
// when the user hasn't set a custom one (empty or still a known default).
const PROTO_PORTS = {{ ezremote: 9040, etahen_v1: 9090, etahen_v2: 12800, remote_pkg: 12800 }};
(() => {{
  const proto = document.getElementById('cproto');
  const port = document.getElementById('cport');
  proto.addEventListener('change', () => {{
    const defaults = Object.values(PROTO_PORTS).map(String);
    const cur = port.value.trim();
    if (!cur || defaults.includes(cur)) {{
      port.value = PROTO_PORTS[proto.value] || cur;
      localStorage.setItem('pkgserver_cport', port.value);
    }}
  }});
}})();

const PUSH_DELAY_MS = 1000;  // gap AFTER the console responds, before the next push
const sleep = ms => new Promise(r => setTimeout(r, ms));

function getConsole() {{
  const ip = document.getElementById('cip').value.trim();
  const port = parseInt(document.getElementById('cport').value, 10);
  const proto = document.getElementById('cproto').value;
  if (!ip || !port) {{ alert('Enter the console IP and port first.'); return null; }}
  return {{ ip, port, proto }};
}}

async function doPush(id, ip, port, proto) {{
  try {{
    const r = await fetch('/api/push', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ console_ip: ip, console_port: port, pkg_id: id, protocol: proto }})
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

// --- cloud push ---------------------------------------------------------
const cloudEl = () => document.getElementById('cloud');
const cloudBodyEl = () => document.getElementById('cloudbody');
let cloudSeq = 0;  // discards responses from superseded searches

const CLOUD_HINT = 'Search the catalogue, or paste a .pkg / .json / .xml URL to install from it.';

// The catalogue is community-sourced and not shipped, so an absent one is a
// normal first-run state rather than an error.
function cloudSetup(status) {{
  const body = cloudBodyEl();
  body.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'csetup';

  const h = document.createElement('h2');
  h.textContent = 'No entitlement catalogue loaded';
  const p = document.createElement('p');
  p.style.margin = '0';
  p.textContent = 'Cloud install needs a catalogue of your own entitlements. '
    + 'It is community tooling, so you export it yourself:';

  const ol = document.createElement('ol');
  const saveStep = status.volume
    ? ['Save it into the ' + status.volume + ' volume, so it lands at ', null, null, status.path]
    : ['Save it to ', null, null, status.path];
  const steps = [
    ['Open ', status.source_url, ' and follow the instructions to ingest your console\u2019s entitlement database.'],
    ['Export the result as CSV.'],
    saveStep,
    ['Reload below (no restart needed).'],
  ];
  for (const [lead, href, tail, code] of steps) {{
    const li = document.createElement('li');
    li.append(document.createTextNode(lead));
    if (href) {{
      const a = document.createElement('a');
      a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.textContent = href;
      li.append(a);
    }}
    if (tail) li.append(document.createTextNode(tail));
    if (code) {{
      const c = document.createElement('code');
      c.textContent = code;
      li.append(c);
    }}
    ol.append(li);
  }}

  const note = document.createElement('p');
  note.style.cssText = 'margin:0 0 12px; color:#7c828a;';
  note.textContent = (status.volume
      ? 'That is the path inside the container; put the file wherever ' + status.volume
        + ' is mounted on the host. '
      : '')
    + 'You can still paste a direct .pkg / .json / .xml URL into the search box without a catalogue.';

  const row = document.createElement('div');
  row.style.cssText = 'display:flex; gap:8px; align-items:center;';

  const upload = document.createElement('button');
  upload.textContent = 'Upload CSV\u2026';
  upload.addEventListener('click', () => cloudPickFile());

  const reload = document.createElement('button');
  reload.textContent = 'Reload from disk';
  reload.style.cssText = 'background:#374151;';
  reload.addEventListener('click', async () => {{
    reload.disabled = true; reload.textContent = 'Reloading\u2026';
    try {{
      const s = await (await fetch('/api/cloud/reload', {{ method: 'POST' }})).json();
      if (s.available) cloudReady(s);
      else {{ cloudSetup(s); cloudFlash('Still nothing at that path.'); }}
    }} catch (e) {{
      reload.disabled = false; reload.textContent = 'Reload from disk';
    }}
  }});

  row.append(upload, reload);
  wrap.append(h, p, ol, note, row);
  body.append(wrap);
}}

// Hand the chosen file to the server as the raw request body.
function cloudPickFile() {{
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.csv,text/csv';
  input.addEventListener('change', async () => {{
    const file = input.files && input.files[0];
    if (!file) return;
    cloudNote('Uploading ' + file.name + '\u2026');
    let j;
    try {{
      const r = await fetch('/api/cloud/upload', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'text/csv' }},
        body: file
      }});
      j = await r.json();
    }} catch (e) {{ j = {{ ok: false, error: String(e) }}; }}
    if (j.ok) cloudReady(j);
    else {{
      cloudSetup(j.path ? j : await (await fetch('/api/cloud/status')).json());
      cloudFlash('Upload failed: ' + (j.error || 'unknown error'));
    }}
  }});
  input.click();
}}

// Catalogue is usable: show the hint plus how to swap it out.
function cloudReady(status) {{
  cloudNote(CLOUD_HINT);
  const line = document.createElement('div');
  line.className = 'cnote';
  line.style.paddingTop = '0';
  line.append(document.createTextNode(
    (status && status.count ? status.count.toLocaleString() + ' entries \u00b7 ' : '')));
  const swap = document.createElement('a');
  swap.href = '#'; swap.textContent = 'replace catalogue';
  swap.style.color = '#93c5fd';
  swap.addEventListener('click', ev => {{ ev.preventDefault(); cloudPickFile(); }});
  line.append(swap);
  cloudBodyEl().append(line);
  document.getElementById('cloudq').focus();
}}

function cloudFlash(msg) {{
  const n = document.createElement('div');
  n.className = 'cnote';
  n.textContent = msg;
  cloudBodyEl().append(n);
}}

async function cloudOpen() {{
  cloudEl().hidden = false;
  const q = document.getElementById('cloudq');
  q.focus(); q.select();
  let status = {{ available: true }};
  try {{ status = await (await fetch('/api/cloud/status')).json(); }} catch (e) {{}}
  if (!status.available) {{ cloudSetup(status); return; }}
  if (!cloudBodyEl().innerHTML) cloudReady(status);
}}

function cloudClose() {{ cloudEl().hidden = true; }}

function cloudNote(msg) {{
  cloudBodyEl().innerHTML = '<div class="cnote"></div>';
  cloudBodyEl().firstChild.textContent = msg;
}}

function cloudRow(onClick, name, sub) {{
  const row = document.createElement('div');
  row.className = 'crow';
  const main = document.createElement('div');
  main.className = 'cmain';
  const n = document.createElement('div'); n.className = 'cname'; n.textContent = name;
  const s = document.createElement('div'); s.className = 'csub'; s.textContent = sub;
  main.append(n, s);
  row.append(main);
  row.addEventListener('click', onClick);
  return row;
}}

// Pasting a URL offers a direct install instead of a catalogue search.
function cloudUrlOption(url) {{
  const body = cloudBodyEl();
  body.innerHTML = '';
  const m = url.split('?')[0].split('#')[0].match(/\\.(pkg|json|xml)$/i);
  if (!m) {{ cloudNote('A URL must end in .pkg, .json or .xml to install from it.'); return; }}
  const kind = m[1].toLowerCase();
  const label = {{
    pkg: 'Install this .pkg directly',
    json: 'Install from this manifest',
    xml: 'Install from this version.xml',
  }}[kind];
  body.append(cloudRow(() => cloudResolve({{ url: url }}), label, url));
}}

async function cloudSearch(q) {{
  const seq = ++cloudSeq;
  const plat = document.getElementById('cloudplat').value;
  const r = await fetch('/api/cloud/search?limit=40&q=' + encodeURIComponent(q)
    + (plat ? '&platform=' + encodeURIComponent(plat) : ''));
  const j = await r.json();
  if (seq !== cloudSeq) return;
  if (r.status === 503) {{
    cloudSetup(await (await fetch('/api/cloud/status')).json());
    return;
  }}
  if (!j.ok) {{ cloudNote(j.error || 'search failed'); return; }}
  if (!j.results.length) {{ cloudNote('No matches.'); return; }}
  const body = cloudBodyEl();
  body.innerHTML = '';
  for (const e of j.results) {{
    body.append(cloudRow(
      () => cloudResolve({{ entitlement_id: e.entitlement_id }}),
      e.title || e.entitlement_id,
      [e.platform.toUpperCase(), e.title_id, e.entitlement_id].filter(Boolean).join(' \u00b7 ')
    ));
  }}
}}

// Re-render whatever the current input implies.
function cloudRefresh() {{
  const v = document.getElementById('cloudq').value.trim();
  if (!v) {{ cloudNote(CLOUD_HINT); return; }}
  if (/^https?:\\/\\//i.test(v)) cloudUrlOption(v); else cloudSearch(v);
}}

// A single manifest installs straight away; several are offered as a choice.
async function cloudResolve(payload) {{
  const c = getConsole(); if (!c) return;
  cloudNote('Resolving\u2026');
  let j;
  try {{
    const r = await fetch('/api/cloud/resolve', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(payload)
    }});
    j = await r.json();
  }} catch (e) {{ cloudNote(String(e)); return; }}
  if (!j.ok) {{ cloudNote(j.error || 'could not resolve'); return; }}
  if (j.packages.length === 1) {{ cloudPush(j.packages[0]); return; }}

  const body = cloudBodyEl();
  body.innerHTML = '';
  const back = document.createElement('button');
  back.className = 'cback'; back.textContent = '\u2190 back';
  back.addEventListener('click', cloudRefresh);
  body.append(back);
  for (const p of j.packages) {{
    body.append(cloudRow(
      () => cloudPush(p),
      [p.kind, j.title].filter(Boolean).join(' \u00b7 '),
      [p.content_id, p.content_ver && 'v' + p.content_ver].filter(Boolean).join(' \u00b7 ')
    ));
  }}
}}

async function cloudPush(pkg) {{
  const c = getConsole(); if (!c) return;
  cloudNote('Installing ' + (pkg.content_id || pkg.name) + '\u2026');
  let j;
  try {{
    const r = await fetch('/api/cloud/push', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{
        console_ip: c.ip, console_port: c.port, protocol: c.proto,
        manifest_url: pkg.manifest_url, content_id: pkg.content_id, name: pkg.name || ''
      }})
    }});
    j = await r.json();
  }} catch (e) {{ j = {{ ok: false, error: String(e) }}; }}
  const res = resultLabel(j);
  cloudNote(res.text + ' \u2014 ' + res.title);
}}

(() => {{
  const q = document.getElementById('cloudq');
  let t;
  q.addEventListener('input', () => {{
    clearTimeout(t);
    const v = q.value.trim();
    if (!v) {{ cloudNote(CLOUD_HINT); return; }}
    // A URL needs no lookup, so render its install option immediately.
    if (/^https?:\\/\\//i.test(v)) {{ cloudUrlOption(v); return; }}
    t = setTimeout(() => cloudSearch(v), 200);
  }});
  const plat = document.getElementById('cloudplat');
  const platKey = 'pkgserver_cloudplat';
  const savedPlat = localStorage.getItem(platKey);
  if (savedPlat !== null) plat.value = savedPlat;
  plat.addEventListener('change', () => {{
    localStorage.setItem(platKey, plat.value);
    cloudRefresh();
  }});
  cloudEl().addEventListener('click', ev => {{ if (ev.target === cloudEl()) cloudClose(); }});
  document.addEventListener('keydown', ev => {{
    if (ev.key === 'Escape' && !cloudEl().hidden) cloudClose();
  }});
}})();

async function push(id, btn) {{
  const c = getConsole(); if (!c) return;
  const member = btn.closest('.member');
  const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '&hellip;';
  setStatus(member, 'pushing', 'Installing\u2026');
  const j = await doPush(id, c.ip, c.port, c.proto);
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
    btn.textContent = 'Installing ' + (i + 1) + '/' + members.length;
    setStatus(m, 'pushing', 'Installing\u2026');
    const j = await doPush(m.dataset.id, c.ip, c.port, c.proto);
    const r = resultLabel(j);
    setStatus(m, r.state, r.text, r.title);
    if (i < members.length - 1) await sleep(PUSH_DELAY_MS);
  }}
  btn.textContent = old; btn.disabled = false;
}}

// Masonry-ish columns: distribute the group cards round-robin into independent
// column stacks so expanding one card only grows its own column. Cards keep
// left-to-right reading order across the top. Column count tracks the same
// breakpoints as the CSS max-width (1 / 2 / 3). Cards are moved (not recreated),
// so open state and push status are preserved across re-layouts.
let _groupCards = null;
function layoutGroups() {{
  const c = document.querySelector('.groups');
  if (!c) return;
  if (!_groupCards) _groupCards = Array.from(c.querySelectorAll('.group'));
  const w = c.clientWidth || window.innerWidth;
  const n = w >= 1320 ? 3 : (w >= 900 ? 2 : 1);
  if (c.dataset.cols === String(n)) return;  // same column count -> leave as is
  c.dataset.cols = String(n);
  c.classList.add('cols');
  c.innerHTML = '';
  const cols = [];
  for (let i = 0; i < n; i++) {{
    const d = document.createElement('div');
    d.className = 'gcol';
    c.appendChild(d);
    cols.push(d);
  }}
  _groupCards.forEach((card, i) => cols[i % n].appendChild(card));
}}
// Cheap on every event: it early-returns unless the column count actually
// changed, so the layout snaps at breakpoints during a live drag.
window.addEventListener('resize', layoutGroups);
layoutGroups();
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


# Supported console push protocols. "ezremote" is the original MangoScango/
# ps5-ezremote-dpi dialect (raw TCP, plain URL line, bare-integer reply).
# "etahen"/"etahen_v1" and "etahen_v2" target etaHEN's DirectPKGInstaller:
#   v1  raw TCP :9090, JSON payload, {"res":"N"} reply
#   v2  HTTP    :12800, POST /upload form fields, "SUCCESS:"/"FAILED:" text reply
# "remote_pkg" is flatz's ps4_remote_pkg_installer (and the OOP fork):
#   HTTP :12800, POST /api/install {"type":"direct","packages":[url]}, JSON reply.
#   PS4-only: it fetches param.sfo/icon0 by range and rejects PS5 param.json.
# All of them drive the console's own HTTP downloader against /download/{id}.
PUSH_PROTOCOLS = (
    "ezremote",
    "etahen",
    "etahen_v1",
    "etahen_v2",
    "remote_pkg",
)


class PushRequest(BaseModel):
    console_ip: str
    console_port: int
    pkg_id: str
    protocol: str = "ezremote"


@app.post("/api/push")
def api_push(req: PushRequest, request: Request) -> JSONResponse:
    """Tell a console to download and install a package over HTTP.

    Every supported protocol hands the console a ``/download/{id}`` URL that it
    fetches itself (with range support, so installs resume). They differ only in
    how the request is framed and how the reply is read -- see PUSH_PROTOCOLS.
    """
    record = state.index.get(req.pkg_id)
    if record is None:
        return JSONResponse({"ok": False, "error": "unknown package"}, status_code=404)

    protocol = (req.protocol or "ezremote").lower()
    if protocol not in PUSH_PROTOCOLS:
        return JSONResponse(
            {"ok": False, "error": f"unknown protocol {protocol!r}"}, status_code=400
        )
    if protocol == "remote_pkg" and record.platform == "PS5":
        return JSONResponse(
            {
                "ok": False,
                "error": "the PS4 remote pkg installer does not support PS5 packages",
            },
            status_code=400,
        )

    # Port the HTTP server is reachable on (as seen by this request).
    http_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    authority, server_ip = _server_authority(req.console_ip, req.console_port, http_port)
    icon_rel = f"/icons/{record.icon}" if record.icon else ""
    icon_abs = f"http://{authority}{icon_rel}" if icon_rel else ""
    name = record.title or record.filename

    try:
        if protocol == "ezremote":
            # Metadata rides in the URL query string; icon stays relative.
            params = urlencode(
                {"content_id": record.content_id or "", "name": name, "icon": icon_rel}
            )
            url = f"http://{authority}/download/{req.pkg_id}?{params}"
            result = _push_ezremote(req.console_ip, req.console_port, url)
        elif protocol == "remote_pkg":
            # flatz installer takes an array of piece URLs; our /download/{id}
            # already serves a (possibly split) package as one contiguous,
            # range-capable stream, so a single-element array is enough.
            url = f"http://{authority}/download/{req.pkg_id}"
            result = _push_remote_pkg(req.console_ip, req.console_port, url)
        else:
            # etaHEN carries metadata in dedicated fields, so the URL stays clean
            # and the icon must be absolute for the console to fetch it.
            url = f"http://{authority}/download/{req.pkg_id}"
            fields = {
                "url": url,
                "content_id": record.content_id or "",
                "content_name": name,
                "icon_url": icon_abs,
            }
            if protocol == "etahen_v2":
                result = _push_etahen_v2(req.console_ip, req.console_port, fields)
            else:  # etahen / etahen_v1
                result = _push_etahen_v1(req.console_ip, req.console_port, fields)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except urllib.error.URLError as e:
        return JSONResponse({"ok": False, "error": str(e.reason)}, status_code=502)

    return JSONResponse(
        {
            "ok": True,
            "protocol": protocol,
            "url": url,
            "server_ip": server_ip,
            **result,
        }
    )


def _cloud_status() -> dict:
    entries = state.entitlements
    path = os.path.abspath(ENTITLEMENTS_CSV)
    # In a container the path is only reachable through a mounted volume, so the
    # setup instructions have to say which one.
    volume = ""
    if IN_CONTAINER:
        for mount in ("/data",):
            if path == mount or path.startswith(mount + os.sep):
                volume = mount
                break
    return {
        "available": bool(entries),
        "count": len(entries),
        "path": path,
        "volume": volume,
        "in_container": IN_CONTAINER,
        "source_url": ENTITLEMENTS_SOURCE_URL,
    }


@app.get("/api/cloud/status")
def api_cloud_status() -> JSONResponse:
    """Whether an entitlement catalogue is loaded, and where it is expected."""
    return JSONResponse(_cloud_status())


@app.post("/api/cloud/reload")
def api_cloud_reload() -> JSONResponse:
    """Re-read the catalogue, picking up a file added since startup."""
    state.reload_entitlements()
    return JSONResponse(_cloud_status())


@app.post("/api/cloud/upload")
async def api_cloud_upload(request: Request) -> JSONResponse:
    """Write a catalogue CSV sent as the request body, then load it.

    The destination is always ENTITLEMENTS_CSV, never anything the caller names.
    The body is validated as an entitlement CSV before it replaces an existing
    catalogue, and is swapped in by rename so a failure cannot leave a partial
    file behind.
    """
    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "error": "empty upload"}, status_code=400)
    if len(body) > ENTITLEMENTS_MAX_UPLOAD:
        return JSONResponse(
            {
                "ok": False,
                "error": f"too large (limit {ENTITLEMENTS_MAX_UPLOAD // (1024 * 1024)} MB)",
            },
            status_code=413,
        )
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        return JSONResponse({"ok": False, "error": "not UTF-8 text"}, status_code=400)

    header = text.lstrip().split("\n", 1)[0].strip()
    columns = {c.strip().lower() for c in header.split(",")}
    missing = {"entitlement_id", "package_url"} - columns
    if missing:
        return JSONResponse(
            {"ok": False, "error": f"missing column(s): {', '.join(sorted(missing))}"},
            status_code=400,
        )
    # Parsed up front so an unusable file is rejected before it can replace a
    # working catalogue.
    parsed = entitlements.load_text(text)
    if not parsed:
        return JSONResponse(
            {"ok": False, "error": "no usable rows in that CSV"}, status_code=400
        )

    path = os.path.abspath(ENTITLEMENTS_CSV)
    tmp = path + ".part"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    state.reload_entitlements()
    return JSONResponse({"ok": True, **_cloud_status()})


@app.get("/api/cloud/search")
def api_cloud_search(q: str = "", limit: int = 40, platform: str = "") -> JSONResponse:
    """Search the entitlement catalogue by title, title id or entitlement id.

    ``platform`` optionally restricts results to one of ps4 / ps5.
    """
    entries = state.entitlements
    if not entries:
        return JSONResponse(
            {"ok": False, "error": f"no catalogue at {ENTITLEMENTS_CSV}", "results": []},
            status_code=503,
        )
    platform = platform.strip().lower()
    if platform and platform not in ("ps4", "ps5"):
        return JSONResponse(
            {"ok": False, "error": f"unknown platform {platform!r}", "results": []},
            status_code=400,
        )
    hits = entitlements.search(
        entries, q, limit=max(1, min(limit, 200)), platform=platform
    )
    return JSONResponse({"ok": True, "total": len(hits), "results": [h.to_dict() for h in hits]})


class CloudResolveRequest(BaseModel):
    entitlement_id: str = ""
    url: str = ""


@app.post("/api/cloud/resolve")
def api_cloud_resolve(req: CloudResolveRequest) -> JSONResponse:
    """List the installable targets behind a catalogue row or a bare URL.

    A ``.json`` or ``.pkg`` target yields one; a ``.xml`` target is fetched and
    may yield several (the app plus its additional content) to pick from.
    """
    if req.url.strip():
        url = req.url.strip()
        if not url.lower().startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": "url must be http(s)"}, status_code=400)
        entry = entitlements.from_url(url)
        if not entry.url_kind:
            return JSONResponse(
                {"ok": False, "error": "url must end in .pkg, .json or .xml"},
                status_code=400,
            )
    else:
        entry = entitlements.find(state.entitlements, req.entitlement_id)
    if entry is None:
        return JSONResponse({"ok": False, "error": "unknown entitlement"}, status_code=404)
    try:
        packages = entitlements.resolve(entry)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except ElementTree.ParseError as e:
        return JSONResponse({"ok": False, "error": f"bad version.xml: {e}"}, status_code=502)
    if not packages:
        return JSONResponse({"ok": False, "error": "no installable manifest"}, status_code=404)
    return JSONResponse(
        {
            "ok": True,
            "title": entry.title,
            "url_kind": entry.url_kind,
            "packages": [p.to_dict() for p in packages],
        }
    )


class CloudPushRequest(BaseModel):
    console_ip: str
    console_port: int
    manifest_url: str
    content_id: str = ""
    name: str = ""
    protocol: str = "ezremote"


@app.post("/api/cloud/push")
def api_cloud_push(req: CloudPushRequest) -> JSONResponse:
    """Hand a console a Sony manifest URL to install from.

    Unlike ``/api/push`` the console downloads straight from Sony, so no local
    package or download URL is involved.
    """
    protocol = (req.protocol or "ezremote").lower()
    if protocol not in PUSH_PROTOCOLS:
        return JSONResponse(
            {"ok": False, "error": f"unknown protocol {protocol!r}"}, status_code=400
        )
    url = req.manifest_url.strip()
    if not url.lower().startswith(("http://", "https://")):
        return JSONResponse({"ok": False, "error": "manifest_url must be http(s)"}, status_code=400)

    name = req.name or req.content_id or url.rsplit("/", 1)[-1]
    try:
        if protocol == "ezremote":
            result = _push_ezremote(req.console_ip, req.console_port, url)
        elif protocol == "remote_pkg":
            result = _push_remote_pkg(req.console_ip, req.console_port, url)
        else:
            fields = {
                "url": url,
                "content_id": req.content_id,
                "content_name": name,
                "icon_url": "",
            }
            if protocol == "etahen_v2":
                result = _push_etahen_v2(req.console_ip, req.console_port, fields)
            else:
                result = _push_etahen_v1(req.console_ip, req.console_port, fields)
    except OSError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    except urllib.error.URLError as e:
        return JSONResponse({"ok": False, "error": str(e.reason)}, status_code=502)

    return JSONResponse({"ok": True, "protocol": protocol, "url": url, **result})


def _server_authority(
    console_ip: str, console_port: int, http_port: int
) -> Tuple[str, str]:
    """Return (authority, server_ip): the ``host:port`` the console should
    download from, and the bare server IP.

    PUBLIC_HOST wins when set. Otherwise we discover the local address that
    routes toward the console using a connected UDP socket (no packets are
    actually sent), which is the address the console can reach us on.
    """
    if PUBLIC_HOST:
        return PUBLIC_HOST, PUBLIC_HOST.split(":", 1)[0]
    server_ip = "127.0.0.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((console_ip, console_port))
            server_ip = s.getsockname()[0]
    except OSError:
        pass
    return f"{server_ip}:{http_port}", server_ip


def _push_ezremote(console_ip: str, console_port: int, url: str) -> dict:
    """ps5-ezremote-dpi: send the URL line over raw TCP, read a bare int code."""
    with socket.create_connection((console_ip, console_port), timeout=5) as sock:
        sock.sendall((url + "\n").encode("utf-8"))
        response = _read_console_response(sock)
    code, code_hex = _parse_console_code(response)
    return {"response": response, "code": code, "code_hex": code_hex}


def _push_etahen_v1(console_ip: str, console_port: int, fields: dict) -> dict:
    """etaHEN DPI v1: send a JSON object over raw TCP (:9090), read {"res":"N"}.

    The console's reader takes a single <=1024-byte read, so the payload is sent
    in one write and stays compact.
    """
    payload = json.dumps(fields, separators=(",", ":"))
    with socket.create_connection((console_ip, console_port), timeout=5) as sock:
        sock.sendall(payload.encode("utf-8"))
        response = _read_console_response(sock)
    code, code_hex = _parse_etahen_v1_response(response)
    return {"response": response, "code": code, "code_hex": code_hex}


def _push_etahen_v2(console_ip: str, console_port: int, fields: dict) -> dict:
    """etaHEN DPI v2: POST the fields to ``/upload`` (:12800), read status text.

    The console's libmicrohttpd post processor accepts application/x-www-form-
    urlencoded for the non-file fields, so no multipart framing is needed.
    """
    endpoint = f"http://{console_ip}:{console_port}/upload"
    data = urlencode(fields).encode("utf-8")
    http_req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(http_req, timeout=PUSH_RESPONSE_TIMEOUT) as resp:
        response = resp.read().decode("utf-8", "replace").strip()
    code, code_hex = _parse_etahen_v2_response(response)
    return {"response": response, "code": code, "code_hex": code_hex}


def _push_remote_pkg(console_ip: str, console_port: int, download_url: str) -> dict:
    """flatz ps4_remote_pkg_installer: POST /api/install with a direct package.

    The installer reads the pkg header/param.sfo/icon0 from the URL by range
    request, then registers a background download. A register failure returns
    HTTP 200 with a hex ``error_code``; bad params/prerequisites return HTTP 500
    with a JSON ``error`` string -- both are read and normalized here.
    """
    endpoint = f"http://{console_ip}:{console_port}/api/install"
    payload = json.dumps({"type": "direct", "packages": [download_url]})
    http_req = urllib.request.Request(
        endpoint,
        data=payload.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(http_req, timeout=PUSH_RESPONSE_TIMEOUT) as resp:
            response = resp.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        # 500 carries a JSON error body we still want to surface, not a transport
        # failure. (A real connection failure is a plain URLError -> handled by
        # the caller as 502.)
        response = e.read().decode("utf-8", "replace").strip()
    code, code_hex = _parse_remote_pkg_response(response)
    return {"response": response, "code": code, "code_hex": code_hex}


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


def _parse_etahen_v1_response(response: str):
    """Parse etaHEN v1's ``{"res":"N"}`` reply into (int_code, hex_string).

    Falls back to a bare-integer parse if the reply isn't the expected JSON.
    """
    if not response:
        return None, None
    try:
        res = json.loads(response).get("res")
    except (ValueError, AttributeError):
        return _parse_console_code(response)
    if res is None:
        return None, None
    try:
        code = int(res)
    except (ValueError, TypeError):
        return None, None
    return code, f"0x{code & 0xFFFFFFFF:08X}"


def _parse_etahen_v2_response(response: str):
    """Map etaHEN v2's status text to (int_code, hex_string).

    A "SUCCESS" reply is normalized to code 0. A "FAILED" reply embeds the
    numeric install error ("... code -2135813882 (0x80B22416) ..."), which we
    extract so the UI can show it like the other protocols.
    """
    if not response:
        return None, None
    if response.startswith("SUCCESS"):
        return 0, "0x00000000"
    m = re.search(r"code (-?\d+)", response)
    if m:
        code = int(m.group(1))
        return code, f"0x{code & 0xFFFFFFFF:08X}"
    return None, None


def _parse_remote_pkg_response(response: str):
    """Map a ps4_remote_pkg_installer reply to (int_code, hex_string).

    Three shapes are possible:
      - success: ``{"status":"success","task_id":N,...}`` (valid JSON) -> 0
      - register fail: ``{ "status": "fail", "error_code": 0x80B21106 }`` -- note
        the hex literal makes this *invalid* JSON, so the code is pulled out with
        a regex and normalized to a signed 32-bit int.
      - param/prereq fail: ``{"status":"fail","error":"..."}`` (valid JSON) -> no
        numeric code, the message travels in ``response``.
    """
    if not response:
        return None, None
    m = re.search(r'"error_code"\s*:\s*(0x[0-9A-Fa-f]+|-?\d+)', response)
    if m:
        raw = m.group(1)
        code = int(raw, 16) if raw.lower().startswith("0x") else int(raw)
        if code >= 0x80000000:  # unsigned 32-bit -> signed, matches other codes
            code -= 0x100000000
        return code, f"0x{code & 0xFFFFFFFF:08X}"
    try:
        obj = json.loads(response)
    except ValueError:
        return None, None
    if isinstance(obj, dict) and obj.get("status") == "success":
        return 0, "0x00000000"
    return None, None
