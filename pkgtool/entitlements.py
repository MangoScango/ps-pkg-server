"""Entitlement catalogue lookup and Sony version.xml resolution.

The catalogue is a CSV of entitlement_id -> package_url. A package_url is a
``.json`` manifest or a ``.pkg`` the console installs directly, or a ``.xml``
title-patch document listing one or more manifests (the app plus any additional
content).
"""

from __future__ import annotations

import csv
import io
import os
import re
import ssl
import urllib.request
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional
from urllib.parse import SplitResult, urlsplit, urlunsplit
from xml.etree import ElementTree


@dataclass
class Entitlement:
    """One catalogue row."""

    entitlement_id: str
    title: str
    title_id: str
    package_url: str
    platform: str
    content_type: str

    @property
    def url_kind(self) -> str:
        """"json", "pkg", "xml", or "" when the row carries no usable URL."""
        path = self.package_url.lower().split("?", 1)[0].split("#", 1)[0]
        for ext in ("json", "pkg", "xml"):
            if path.endswith("." + ext):
                return ext
        return ""

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["url_kind"] = self.url_kind
        return d


@dataclass
class CloudPackage:
    """One installable manifest resolved from a catalogue row."""

    content_id: str
    kind: str          # "Game" / "DLC" / "Other"
    content_ver: str
    manifest_url: str
    name: str = ""
    title: str = ""
    title_id: str = ""
    platform: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


# version.xml wraps each package in a per-content element; the tag states what
# the content is.
_TAG_KINDS = {"app_tag": "Game", "ac_tag": "DLC", "patch_tag": "Update"}


def _rows(reader) -> List[Entitlement]:
    out: List[Entitlement] = []
    for row in reader:
        url = (row.get("package_url") or "").strip()
        if not url:
            continue
        out.append(
            Entitlement(
                entitlement_id=(row.get("entitlement_id") or "").strip(),
                title=(row.get("title") or "").strip(),
                title_id=(row.get("title_id") or "").strip(),
                package_url=url,
                platform=(row.get("platform") or "").strip(),
                content_type=(row.get("content_type") or "").strip(),
            )
        )
    return out


def load(path: str) -> List[Entitlement]:
    """Read the catalogue CSV. Returns [] when the file is absent."""
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return _rows(csv.DictReader(f))


def load_text(text: str) -> List[Entitlement]:
    """Parse a catalogue from CSV text, for validating one before storing it."""
    return _rows(csv.DictReader(io.StringIO(text)))


def search(
    entries: List[Entitlement],
    query: str,
    limit: int = 50,
    platform: str = "",
) -> List[Entitlement]:
    """Case-insensitive match on title, title id and entitlement id.

    ``platform`` restricts results to "ps4" or "ps5" when given.

    Results are ordered by how early every term appears in the title, so exact
    and prefix matches surface above incidental substring hits.
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    want_platform = platform.strip().lower()

    scored = []
    for e in entries:
        if want_platform and e.platform.lower() != want_platform:
            continue
        title = e.title.lower()
        haystack = f"{title} {e.title_id.lower()} {e.entitlement_id.lower()}"
        if not all(t in haystack for t in terms):
            continue
        pos = title.find(terms[0])
        scored.append(((0, pos) if pos >= 0 else (1, 0), len(e.title), e))
    scored.sort(key=lambda s: (s[0], s[1], s[2].title.lower(), s[2].entitlement_id))
    return [e for _rank, _len, e in scored[:limit]]


_NUMBERED_PIECE = re.compile(r"^(.+)_\d+\.pkg$", re.IGNORECASE)
_SC_PIECE = re.compile(r"^(.+)_sc\.pkg$", re.IGNORECASE)


def _is_ps4_cdn(parts: SplitResult) -> bool:
    host = (parts.hostname or "").lower()
    return host.startswith("gs2.") and parts.path.startswith("/gs2/")


def _is_ps5_cdn(parts: SplitResult) -> bool:
    host = (parts.hostname or "").lower()
    return host.startswith("sgst.") and parts.path.startswith("/sgst/")


def manifest_url_for(url: str) -> str:
    """Derive the .json manifest URL for one piece of a split package.

    Returns ``url`` unchanged when no manifest can be derived from it.

    PS5 signs the numbered pieces' directory (``/app/pkg/``) separately from the
    manifest's (``/app/info/``), so only its ``_sc`` piece, which sits beside the
    manifest, can be mapped; PS4 keeps both in one directory. Either suffix is
    also an ordinary local filename, hence the check against the known CDNs.
    """
    url = url.strip()
    parts = urlsplit(url)
    head, slash, filename = parts.path.rpartition("/")
    if not slash or not filename:
        return url
    match = _SC_PIECE.match(filename)
    if match is not None:
        if not _is_ps5_cdn(parts):
            return url
    else:
        match = _NUMBERED_PIECE.match(filename)
        if match is None or not _is_ps4_cdn(parts):
            return url
    path = f"{head}/{match.group(1)}.json"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def from_url(url: str) -> Entitlement:
    """Wrap a bare URL as a catalogue row so it resolves the same way."""
    url = url.strip()
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    return Entitlement(
        entitlement_id="",
        title=name or url,
        title_id="",
        package_url=url,
        platform="",
        content_type="",
    )


def find(entries: List[Entitlement], entitlement_id: str) -> Optional[Entitlement]:
    for e in entries:
        if e.entitlement_id == entitlement_id:
            return e
    return None


def parse_version_xml(data: bytes) -> List[CloudPackage]:
    """Extract the installable manifests from a title-patch document.

    Each ``*_tag`` element carries the content id and holds ``package`` children
    whose ``manifest_url`` is the JSON to install. Only the highest
    ``content_ver`` is kept per content id.
    """
    root = ElementTree.fromstring(data)
    best: Dict[str, CloudPackage] = {}
    for tag in root:
        kind = _TAG_KINDS.get(tag.tag, "Other")
        content_id = tag.get("content_id") or ""
        for package in tag.findall("package"):
            url = package.get("manifest_url")
            if not url:
                continue
            ver = package.get("content_ver") or ""
            existing = best.get(content_id)
            if existing is None or ver > existing.content_ver:
                best[content_id] = CloudPackage(
                    content_id=content_id,
                    kind=kind,
                    content_ver=ver,
                    manifest_url=url,
                )
    order = {"Game": 0, "Update": 1, "DLC": 2}
    return sorted(best.values(), key=lambda p: (order.get(p.kind, 9), p.content_id))


def fetch(url: str, timeout: int = 20) -> bytes:
    """GET a catalogue URL.

    Certificate verification is disabled: the Sony content servers present
    chains this does not validate against a normal trust store.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "ps-pkg-server"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def resolve(entry: Entitlement) -> List[CloudPackage]:
    """Return the installable manifests for a catalogue row.

    A ``.json`` manifest or a direct ``.pkg`` needs no lookup; a ``.xml`` row is
    fetched and parsed.
    """
    if entry.url_kind in ("json", "pkg"):
        return [
            CloudPackage(
                content_id=entry.entitlement_id,
                kind="Game" if entry.content_type.endswith("game") else "",
                content_ver="",
                manifest_url=entry.package_url,
                name=entry.title,
                title=entry.title,
                title_id=entry.title_id,
                platform=entry.platform,
            )
        ]
    if entry.url_kind == "xml":
        packages = parse_version_xml(fetch(entry.package_url))
        for p in packages:
            # A document yields the app plus its update and additional content
            # under one title; qualify all but the base game to tell them apart.
            qualifier = " ".join(x for x in (p.kind, p.content_ver) if x)
            p.title = entry.title
            p.name = entry.title
            if p.kind != "Game" and qualifier:
                p.name = f"{entry.title} ({qualifier})"
            p.title_id = entry.title_id
            p.platform = entry.platform
        return packages
    return []
