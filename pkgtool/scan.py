"""Recursive scanner for PS4 PKG files.

Walks one or more directories, parses metadata from every ``*.pkg`` found, and
extracts icons to a cache directory. Parsing is I/O-light (only the metadata
region of each file is read), and files are processed in parallel to keep large
libraries fast.
"""

from __future__ import annotations

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

from .pkg import ConcatSource, Pkg, PkgError


@dataclass
class PkgRecord:
    """Flattened metadata for one scanned PKG."""

    path: str
    filename: str
    size: int
    platform: str
    edition: str
    content_id: str
    title: Optional[str]
    title_id: Optional[str]
    version: Optional[str]
    category: Optional[str]
    content_type: str
    kind: str
    region: str
    icon: Optional[str] = None  # cache key / filename for the icon, if extracted
    error: Optional[str] = None
    id: str = ""  # stable id (hash of path) used for download/push routing
    marriage: Optional[str] = None  # PS4 base<->update compatibility digest
    compat: str = ""  # per-update: "married" / "mismatch" (set during grouping)
    parts: List[str] = field(default_factory=list)  # ordered files (>1 => split pkg)
    # Not shown in the listing (delta patch / metadata fragment). See hidden_reason.
    hidden: bool = False
    hidden_reason: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


@dataclass
class ScanResult:
    records: List[PkgRecord] = field(default_factory=list)
    errors: List[PkgRecord] = field(default_factory=list)
    hidden: List[PkgRecord] = field(default_factory=list)  # delta patches / SC fragments

    @property
    def total(self) -> int:
        return len(self.records) + len(self.errors)


# Ordering used to pick a group's representative and to sort members within a
# group: base game first, then update, then DLC, then everything else.
_KIND_PRIORITY = {"Game": 0, "App": 1, "Update": 2, "DLC": 3}


def _kind_rank(kind: str) -> int:
    return _KIND_PRIORITY.get(kind, 99)


@dataclass
class PkgGroup:
    """A set of packages sharing a Title ID.

    The displayed fields (title, region, icon) are derived from the members in
    priority order (base game > update > dlc > other), taking the first member
    that actually has a value.
    """

    title_id: str
    title: Optional[str]
    region: str
    icon: Optional[str]
    kind: str  # kind of the representative (highest-priority) member
    platform: str = ""
    edition: str = ""
    compat: str = ""  # "married" / "mismatch" / "" (base<->update compatibility)
    build: str = ""  # short marriage-digest tag when a title is split by build
    members: List[PkgRecord] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.members)

    @property
    def kinds(self) -> List[str]:
        """Distinct kinds present, in priority order."""
        seen = []
        for m in self.members:
            if m.kind and m.kind not in seen:
                seen.append(m.kind)
        return sorted(seen, key=_kind_rank)

    def to_dict(self) -> Dict[str, object]:
        return {
            "title_id": self.title_id,
            "title": self.title,
            "region": self.region,
            "icon": self.icon,
            "kind": self.kind,
            "platform": self.platform,
            "edition": self.edition,
            "compat": self.compat,
            "build": self.build,
            "count": self.count,
            "kinds": self.kinds,
            "members": [m.to_dict() for m in self.members],
        }


def _first(members: List[PkgRecord], getter) -> Optional[str]:
    """First truthy value from members (already sorted by priority)."""
    for m in members:
        v = getter(m)
        if v:
            return v
    return None


def _mark_compat(members: List[PkgRecord]) -> None:
    """Set each update's compat vs the base games in the whole title.

    Done at the title level (before any build split) so an update whose digest
    matches no present base is still flagged "mismatch" even after it lands in
    its own build group. "married" when the digest matches a base; "" when the
    title has no base game to compare against.
    """
    base_digests = {
        m.marriage for m in members if m.kind == "Game" and m.marriage
    }
    if not base_digests:
        return
    for m in members:
        if m.kind == "Update" and m.marriage:
            m.compat = "married" if m.marriage in base_digests else "mismatch"


def _group_compat(members: List[PkgRecord]) -> str:
    """Summarize a group's compatibility from its members' already-set compat."""
    statuses = {m.compat for m in members if m.compat}
    if "mismatch" in statuses:
        return "mismatch"
    if "married" in statuses:
        return "married"
    return ""


def _make_group(key: str, members: List[PkgRecord], build: str = "") -> "PkgGroup":
    """Build a PkgGroup from a set of members (already a coherent set)."""
    # Sort members by kind priority (base game > update > dlc > other), then id.
    members.sort(key=lambda m: (_kind_rank(m.kind), (m.content_id or "").lower()))
    rep = members[0]
    title = _first(members, lambda m: m.title) or rep.filename
    region = _first(members, lambda m: m.region if m.region != "Unknown" else None) or "Unknown"
    icon = _first(members, lambda m: m.icon)
    platform = _first(members, lambda m: m.platform) or ""
    # Only show a group-level edition when all members agree.
    member_editions = {m.edition for m in members if m.edition}
    edition = member_editions.pop() if len(member_editions) == 1 else ""
    compat = _group_compat(members)
    return PkgGroup(
        title_id=key,
        title=title,
        region=region,
        icon=icon,
        kind=rep.kind,
        platform=platform,
        edition=edition,
        compat=compat,
        build=build,
        members=members,
    )


def _split_title(key: str, members: List[PkgRecord]) -> List["PkgGroup"]:
    """Split one title id into groups so no group mixes incompatible builds.

    A base game and an update only belong together when their marriage digests
    match. If a title has more than one distinct base/update digest (multiple
    bases and/or updates for different builds), spin each digest off into its
    own group; digest-less members (DLC, PS5, unreadable) go to a shared group.
    Titles with a single build (or none) stay as one group.
    """
    # Mark update compat against all bases in the title before splitting, so an
    # orphan update (no matching base present) still reads "mismatch".
    _mark_compat(members)

    # Partition into build-specific members (base/update carrying a digest) and
    # shared members (DLC, PS5, unreadable) that aren't tied to a build.
    def _build_key(m: PkgRecord):
        return m.marriage if (m.marriage and m.kind in ("Game", "Update")) else None

    by_digest: Dict[str, List[PkgRecord]] = {}
    shared: List[PkgRecord] = []
    for m in members:
        d = _build_key(m)
        if d is None:
            shared.append(m)
        else:
            by_digest.setdefault(d, []).append(m)

    # Single build (or none): everything stays together. Tag it with the build
    # digest when there's a base/update present.
    if len(by_digest) <= 1:
        build = next(iter(by_digest))[:7] if by_digest else ""
        return [_make_group(key, members, build=build)]

    # Multiple builds: one group per digest. Shared members (DLC, etc.) attach to
    # every build group that has a base game (duplicated in the UI, which is fine).
    groups: List[PkgGroup] = []
    attached_shared = False
    for digest, subm in by_digest.items():
        group_members = list(subm)
        if any(m.kind == "Game" for m in subm):
            group_members += shared
            attached_shared = True
        groups.append(_make_group(key, group_members, build=digest[:7]))

    # If no build had a base to attach the shared members to, keep them together.
    if shared and not attached_shared:
        groups.append(_make_group(key, list(shared)))
    return groups


def group_by_title_id(records: List[PkgRecord]) -> List["PkgGroup"]:
    """Group records by Title ID, then split each title by build so a group
    never contains two incompatible base/update marriages."""
    buckets: Dict[str, List[PkgRecord]] = {}
    for r in records:
        key = r.title_id or r.content_id or r.filename
        buckets.setdefault(key, []).append(r)

    groups: List[PkgGroup] = []
    for key, members in buckets.items():
        groups.extend(_split_title(key, members))

    groups.sort(key=lambda g: ((g.title or g.title_id).lower(), g.build))
    return groups


def find_pkgs(paths: List[str]) -> List[str]:
    """Return all ``*.pkg`` file paths under the given directories/files."""
    found: List[str] = []
    for p in paths:
        if os.path.isfile(p):
            if p.lower().endswith(".pkg"):
                found.append(os.path.abspath(p))
            continue
        for root, _dirs, files in os.walk(p):
            for name in files:
                if name.lower().endswith(".pkg"):
                    found.append(os.path.abspath(os.path.join(root, name)))
    return found


def _icon_key(content_id: str, path: str) -> str:
    """Stable icon cache filename. Falls back to a path hash if no content id."""
    base = content_id or hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
    # Sanitize for use as a filename.
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in base)
    return f"{safe}.png"


def pkg_id_for(key: str) -> str:
    """Stable id for download/push routing (hash of the part list / path)."""
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


_NUM_SUFFIX_RE = re.compile(r"^(.*)_(\d+)$")


def _classify_piece(path: str):
    """Classify a .pkg filename as ('num'|'sc'|'base', stem, index)."""
    name = os.path.basename(path)
    stem = name[:-4] if name.lower().endswith(".pkg") else name
    m = _NUM_SUFFIX_RE.match(stem)
    if m:
        return "num", m.group(1), int(m.group(2))
    if stem.endswith("_sc"):
        return "sc", stem[:-3], None
    return "base", stem, None


def group_sources(paths: List[str]) -> List[dict]:
    """Group split parts into logical packages.

    Split naming conventions handled:
      - numbered 4 GiB chunks: ``<stem>_0.pkg, _1.pkg, ...`` (+ optional ``_sc.pkg``)
      - disc-backup pair:      ``<stem>.pkg`` + ``<stem>_sc.pkg``
    Everything else (including ``-DP`` delta patches and merged files) is a
    standalone single-file package. Returns dicts: {name, parts, split}.
    """
    groups: Dict[tuple, dict] = {}
    order: List[tuple] = []
    for p in paths:
        key = (os.path.dirname(p), _classify_piece(p)[1])
        if key not in groups:
            groups[key] = {"num": {}, "sc": None, "base": []}
            order.append(key)
        kind, _stem, idx = _classify_piece(p)
        g = groups[key]
        if kind == "num":
            g["num"][idx] = p
        elif kind == "sc":
            g["sc"] = p
        else:
            g["base"].append(p)

    sources: List[dict] = []
    for key in order:
        _dir, stem = key
        g = groups[key]
        numbered = [g["num"][i] for i in sorted(g["num"])]
        sc = g["sc"]
        bases = sorted(g["base"])

        if numbered:
            parts = numbered + ([sc] if sc else [])
            sources.append({"name": stem + ".pkg", "parts": parts, "split": True})
            # A stray plain <stem>.pkg alongside numbered parts is standalone.
            sources += [{"name": os.path.basename(b), "parts": [b], "split": False} for b in bases]
        elif bases and sc:
            # Disc-backup pair: base image + split-out CNT tail.
            sources.append({"name": stem + ".pkg", "parts": bases + [sc], "split": True})
        else:
            sources += [{"name": os.path.basename(b), "parts": [b], "split": False} for b in bases]
            if sc and not bases:
                sources.append({"name": os.path.basename(sc), "parts": [sc], "split": False})
    return sources


def scan_one(source: dict, icon_dir: Optional[str]) -> PkgRecord:
    """Parse one logical package (single file or split set) and extract its icon."""
    parts: List[str] = source["parts"]
    split: bool = source["split"]
    name: str = source["name"]
    primary = parts[0]
    pkg_id = pkg_id_for("\n".join(parts))
    try:
        sizes = [os.path.getsize(p) for p in parts]
        total = sum(sizes)
        pkg = Pkg.open_split(list(zip(parts, sizes))) if split else Pkg.open(primary)
        with pkg:
            marriage = pkg.marriage_digest()
            # Delta patches carry only a chunk-copy map (not installable here);
            # SC segments are metadata-only tails. Both are hidden from the UI.
            hidden_reason = ""
            if pkg.is_delta_patch:
                hidden_reason = "delta patch"
            elif pkg.is_metadata_fragment(total):
                hidden_reason = "metadata fragment (sc)"
            icon_name: Optional[str] = None
            if icon_dir is not None and pkg.has_icon0():
                try:
                    data = pkg.read_icon0()
                    if data:
                        icon_name = _icon_key(pkg.content_id, primary)
                        out = os.path.join(icon_dir, icon_name)
                        if not os.path.exists(out):
                            with open(out, "wb") as f:
                                f.write(data)
                except (PkgError, OSError):
                    icon_name = None

            return PkgRecord(
                path=primary,
                filename=name,
                size=total,
                platform=pkg.platform,
                edition=pkg.edition,
                content_id=pkg.content_id,
                title=pkg.title,
                title_id=pkg.title_id,
                version=pkg.version,
                category=pkg.category,
                content_type=pkg.content_type_name,
                kind=pkg.kind,
                region=pkg.region,
                icon=icon_name,
                id=pkg_id,
                marriage=marriage,
                parts=parts,
                hidden=bool(hidden_reason),
                hidden_reason=hidden_reason,
            )
    except (PkgError, OSError) as e:
        return PkgRecord(
            path=primary,
            filename=name,
            size=sum(os.path.getsize(p) for p in parts if os.path.exists(p)),
            platform="",
            edition="",
            content_id="",
            title=None,
            title_id=None,
            version=None,
            category=None,
            content_type="",
            kind="",
            region="Unknown",
            error=str(e),
            id=pkg_id,
            parts=parts,
        )


def scan(
    paths: List[str],
    icon_dir: Optional[str] = None,
    workers: int = 8,
) -> ScanResult:
    """Scan directories for PKGs and return parsed metadata.

    Split packages (numbered parts and disc-backup pairs) are grouped into one
    logical package before parsing.

    :param paths: directories or files to scan.
    :param icon_dir: if given, icons are extracted here (created if missing).
    :param workers: thread pool size (parsing is I/O-bound, so threads help).
    """
    if icon_dir is not None:
        os.makedirs(icon_dir, exist_ok=True)

    sources = group_sources(find_pkgs(paths))
    result = ScanResult()
    if not sources:
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(scan_one, s, icon_dir): s for s in sources}
        for fut in as_completed(futures):
            record = fut.result()
            if record.hidden:
                result.hidden.append(record)
            elif record.error:
                result.errors.append(record)
            else:
                result.records.append(record)

    result.records.sort(key=lambda r: (r.title or r.filename).lower())
    result.errors.sort(key=lambda r: r.filename.lower())
    return result
