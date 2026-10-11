"""HTTP proxy support for cloud push.

When the console cannot reach Sony's CDN directly (DNS blocking, no internet
route), the server can stand in: a manifest URL on ``*.playstation.net`` is
traded for a local ``/proxy/{token}`` URL whose token is the base64url-encoded
original. The proxy endpoint decodes, re-validates, and relays the request
(range-capable, streaming). JSON manifests get their embedded piece URLs
rewritten back through the proxy so the whole install stays local.
"""

from __future__ import annotations

import base64
import bisect
import http.client
import json
import logging
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("uvicorn.error")

ALLOWED_HOST = "playstation.net"


def is_proxiable(url: str) -> bool:
    """Only http(s) URLs on ``playstation.net`` or its subdomains may be proxied.

    This bounds the proxy endpoint to Sony's CDN instead of being an open relay.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (
        host == ALLOWED_HOST or host.endswith("." + ALLOWED_HOST)
    )


def encode_url(url: str) -> str:
    """Pack a URL into a path-safe token (urlsafe base64, no padding)."""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def decode_url(token: str) -> Optional[str]:
    """Unpack a token back into its URL, or None when it is not a proxiable URL.

    Validation runs again at decode time: the endpoint is network-exposed, so a
    hand-crafted token must not turn the server into a relay for arbitrary URLs.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        url = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True
        ).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return url if is_proxiable(url) else None


def rewrite_manifest(data: bytes, authority: str) -> bytes:
    """Point every playstation.net URL inside a JSON manifest at the proxy.

    ``authority`` is the ``host:port`` the console used to reach us (its Host
    header), which is exactly what the rewritten piece URLs must target.
    Unparseable input is returned unchanged.
    """
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return data

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and is_proxiable(node):
            # Keep the original filename last in the path: installers derive a
            # display name from it, and a bare base64 token is far over the
            # title-name length limit.
            filename = urlsplit(node).path.rsplit("/", 1)[-1]
            return f"http://{authority}/proxy/{encode_url(node)}/{filename}"
        return node
    return json.dumps(walk(doc)).encode("utf-8")


# Certificate verification is disabled for the same reason as
# ``entitlements.fetch``: the Sony content servers present chains this does
# not validate against a normal trust store.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE


def open_upstream(url: str, range_header: Optional[str] = None, timeout: int = 30):
    """GET an upstream URL, forwarding an optional Range header."""
    headers = {"User-Agent": "ps-pkg-server"}
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=_UNVERIFIED)


def _target_of(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def _connect(url: str, timeout: int):
    """Open a persistent connection to a URL's host.

    Workers keep one of these each and issue ranged GETs back-to-back on it,
    instead of paying a fresh connect (and a CDN redirect) per block.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return http.client.HTTPSConnection(
            parts.hostname, parts.port, timeout=timeout, context=_UNVERIFIED
        )
    return http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)


# The CDN throttles per connection, so a binary proxy
# content is prefetched: workers pull fixed-size blocks over separate
# connections ahead of the console's sequential reads.
BLOCK_SIZE = int(os.environ.get("PROXY_BLOCK_MB", "8")) * 1024 * 1024
PREFETCH_WORKERS = int(os.environ.get("PROXY_WORKERS", "16"))
MAX_CACHED_BYTES = int(os.environ.get("PROXY_CACHE_MB", "1024")) * 1024 * 1024
MAX_BLOCK_ATTEMPTS = 5
STALE_AFTER = 300  # seconds without a reader before an entry is dropped
# A silent upstream connection is abandoned and the block's remaining bytes
# resume on a fresh one. This is the max seconds without bytes before
# reconnecting, not a whole-transfer deadline.
BLOCK_FETCH_TIMEOUT = int(os.environ.get("PROXY_FETCH_TIMEOUT", "4"))
STATS_INTERVAL = 10  # seconds between per-download throughput log lines


class _BadStatus(Exception):
    """The upstream answered a ranged GET with a non-206 status."""


class Download:
    """Whole-file prefetch of one CDN URL, read back block by block.

    Workers claim blocks in order and fetch them from the resolved edge URL,
    each over its own persistent connection; readers (the console's range
    requests) consume them in order and wait on whatever isn't cached yet.
    Blocks below every active reader are evicted, and workers pause once
    MAX_CACHED_BYTES are cached unread.

    Workers park (rather than exit) when there is nothing to fetch, and stay
    until the entry is aborted by the stale sweep: a reader that attaches at
    an already-evicted offset -- a console retry of an old range -- requeues
    that block and needs a live worker to pick it up.
    """

    def __init__(self, origin: str, url: str, total: int) -> None:
        self.origin = origin  # the redirector URL, for re-resolving `url`
        self.url = url        # the resolved edge URL workers fetch from
        self._resolved = (url, _target_of(url))  # swapped atomically
        self.total = total
        self.nblocks = -(-total // BLOCK_SIZE)
        self._pending = list(range(self.nblocks))  # sorted block indices
        self._cursor = 0  # where prefetch continues while no reader is attached
        self._started = False
        self._blocks: Dict[int, bytes] = {}
        self._inflight = set()
        self._attempts: Dict[int, int] = {}
        self._cached = 0
        self._cond = threading.Condition()
        self._readers: Dict[int, int] = {}  # reader id -> next block it needs
        self._next_reader = 0
        self.error: Optional[BaseException] = None
        self.aborted = False
        self._stat_blocks: list = []  # (seconds, bytes, connection reused) per block
        self._stat_t = time.monotonic()
        self._stat_wait = 0.0  # seconds readers spent blocked since the last line
        self.last_active = time.monotonic()

    def _start_workers(self) -> None:
        for i in range(PREFETCH_WORKERS):
            threading.Thread(
                target=self._work, daemon=True, name=f"prefetch-{i}"
            ).start()

    def _is_pending(self, idx: int) -> bool:
        i = bisect.bisect_left(self._pending, idx)
        return i < len(self._pending) and self._pending[i] == idx

    def _next_pending_locked(self) -> Optional[int]:
        """Position in ``_pending`` of the block closest ahead of a reader.

        Workers fetch ahead of whoever is reading, so a request that lands deep
        in the file is served from there instead of after the whole prefix.
        """
        starts = set(self._readers.values()) or {self._cursor}
        best = None
        for pos in starts:
            i = bisect.bisect_left(self._pending, pos)
            if i < len(self._pending):
                gap = self._pending[i] - pos
                if best is None or gap < best[0]:
                    best = (gap, i)
        return None if best is None else best[1]

    def stale(self) -> bool:
        with self._cond:
            return not self._readers and time.monotonic() - self.last_active > STALE_AFTER

    def abort(self) -> None:
        with self._cond:
            self.aborted = True
            self._cond.notify_all()

    def _idle_locked(self) -> bool:
        """Whether a worker should park: nothing pending, or the unread cache
        is full and no reader is starved on a block still to be fetched (a
        re-fetched evicted block -- pausing then would deadlock it)."""
        if self._next_pending_locked() is None:
            return True
        if self._cached < MAX_CACHED_BYTES:
            return False
        return not any(
            pos not in self._blocks and self._is_pending(pos)
            for pos in self._readers.values()
        )

    def _reresolve(self) -> None:
        """Re-follow the origin's redirect chain: the edge URL may have expired."""
        try:
            _, url = _probe(self.origin)
        except Exception:  # noqa: BLE001 - keep the old URL if the probe fails
            return
        if url != self.url:
            self.url = url
            self._resolved = (url, _target_of(url))
            logger.info("proxy: re-resolved %s -> %s", self.origin, url)

    def _fetch_block(self, idx: int, conn):
        """Fetch block ``idx`` over the worker's persistent connection.

        A connection that errors, closes early, or goes silent for
        BLOCK_FETCH_TIMEOUT is dropped and the block's remaining bytes resume
        on a fresh one; only repeated back-to-back failures give up. A non-206
        status re-resolves the origin's redirect first -- the edge URL may have
        expired.
        """
        start = idx * BLOCK_SIZE
        want = min(start + BLOCK_SIZE, self.total) - start
        buf = bytearray()
        consecutive_errors = 0
        t_block = time.monotonic()
        while len(buf) < want:
            url, target = self._resolved
            if conn is None:
                conn = _connect(url, BLOCK_FETCH_TIMEOUT)
            lo, hi = start + len(buf), start + want - 1
            t_conn = time.monotonic()
            try:
                conn.request("GET", target, headers={"Range": f"bytes={lo}-{hi}"})
                resp = conn.getresponse()
                if resp.status != 206:
                    resp.read()  # drain so the connection could be reused
                    raise _BadStatus(resp.status)
                while len(buf) < want:
                    chunk = resp.read(want - len(buf))
                    if not chunk:
                        raise IOError(f"early EOF: got {len(buf)}, wanted {want}")
                    buf += chunk
            except Exception as e:  # noqa: BLE001 - dropped/stalled connection
                # Bytes already received are kept; the remainder resumes on a
                # fresh connection. Only repeated back-to-back failures give up.
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
                consecutive_errors += 1
                if consecutive_errors >= MAX_BLOCK_ATTEMPTS:
                    raise
                if isinstance(e, _BadStatus):
                    self._reresolve()
                logger.info(
                    "proxy: block %d of %s reconnecting (%.1fs, %d/%d bytes): %s",
                    idx, self.url, time.monotonic() - t_conn, len(buf), want, e,
                )
                continue
            consecutive_errors = 0
        elapsed = time.monotonic() - t_block
        if elapsed > 2:
            logger.info(
                "proxy: slow block %d of %s: %.1fs (%.1f MB/s)",
                idx, self.url, elapsed, len(buf) / elapsed / (1024 * 1024),
            )
        return bytes(buf), conn

    def _work(self) -> None:
        conn = None
        while True:
            with self._cond:
                while not self.aborted and self.error is None and self._idle_locked():
                    self._cond.wait()
                if self.aborted or self.error is not None:
                    if conn is not None:
                        conn.close()
                    return
                idx = self._pending.pop(self._next_pending_locked())
                self._inflight.add(idx)
            t_fetch = time.monotonic()
            reused = conn is not None
            try:
                data, conn = self._fetch_block(idx, conn)
            except Exception as e:  # noqa: BLE001 - any fetch failure is retried
                if conn is not None:
                    conn.close()
                    conn = None
                with self._cond:
                    self._inflight.discard(idx)
                    self._attempts[idx] = self._attempts.get(idx, 0) + 1
                    if self._attempts[idx] >= MAX_BLOCK_ATTEMPTS:
                        self.error = e
                    else:
                        bisect.insort(self._pending, idx)
                    self._cond.notify_all()
                logger.warning(
                    "proxy: block %d of %s failed (attempt %d, %.1fs): %s",
                    idx, self.url, self._attempts[idx], time.monotonic() - t_fetch, e,
                )
                continue
            with self._cond:
                self._inflight.discard(idx)
                self._blocks[idx] = data
                self._cached += len(data)
                self._note_block_locked(time.monotonic() - t_fetch, len(data), reused)
                self._cond.notify_all()

    def _note_block_locked(self, seconds: float, size: int, reused: bool) -> None:
        """Record a fetched block and, once per STATS_INTERVAL, log a summary.

        Upstream rate, block times, cache fill and reader wait together show
        whether a slow transfer is waiting on the CDN or on the console. Readers
        flush too, so a line still appears while the workers are parked.
        """
        self._stat_blocks.append((seconds, size, reused))
        self._flush_stats_locked()

    def _flush_stats_locked(self) -> None:
        now = time.monotonic()
        window = now - self._stat_t
        if window < STATS_INTERVAL:
            return

        def pct(values, q):
            values = sorted(values)
            return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0

        times = [b[0] for b in self._stat_blocks]
        reused_t = [b[0] for b in self._stat_blocks if b[2]]
        fresh_t = [b[0] for b in self._stat_blocks if not b[2]]
        logger.info(
            "proxy: stats %s %.0fs: upstream %.1f MB/s, blocks p50 %.2fs p90 %.2fs "
            "(reused conn p50 %.2fs n=%d, fresh p50 %.2fs n=%d), readers waited "
            "%.1fs, cache %d MB, %d/%d workers fetching",
            self.origin.rsplit("/", 1)[-1].split("?", 1)[0], window,
            sum(b[1] for b in self._stat_blocks) / window / (1024 * 1024),
            pct(times, 0.5), pct(times, 0.9),
            pct(reused_t, 0.5), len(reused_t), pct(fresh_t, 0.5), len(fresh_t),
            self._stat_wait, self._cached // (1024 * 1024),
            len(self._inflight), PREFETCH_WORKERS,
        )
        self._stat_blocks = []
        self._stat_wait = 0.0
        self._stat_t = now

    def read(self, start: int, end: int):
        """Yield the bytes of [start, end] in order, waiting on the prefetch."""
        with self._cond:
            self.last_active = time.monotonic()
            rid = self._next_reader
            self._next_reader += 1
            self._readers[rid] = start // BLOCK_SIZE
            if not self._started:
                self._started = True
                self._start_workers()
            self._cond.notify_all()
        try:
            idx = start // BLOCK_SIZE
            pos = start
            while pos <= end:
                with self._cond:
                    waited = time.monotonic()
                    while idx not in self._blocks:
                        if self.error is not None:
                            raise self.error
                        if idx not in self._inflight and not self._is_pending(idx):
                            # Evicted before this reader attached (e.g. a console
                            # retry of an old range): queue it again.
                            bisect.insort(self._pending, idx)
                            self._cond.notify_all()
                        self._cond.wait()
                    self._stat_wait += time.monotonic() - waited
                    self._flush_stats_locked()
                    if time.monotonic() - waited > 3:
                        logger.info(
                            "proxy: reader waited %.1fs for block %d of %s",
                            time.monotonic() - waited, idx, self.url,
                        )
                    data = self._blocks[idx]
                    self.last_active = time.monotonic()
                offset = pos - idx * BLOCK_SIZE
                take = min(len(data) - offset, end - pos + 1)
                yield data[offset:offset + take]
                pos += take
                if pos >= idx * BLOCK_SIZE + len(data):
                    idx += 1
                    with self._cond:
                        self._readers[rid] = idx
                        self._evict_passed_locked()
        finally:
            with self._cond:
                self._cursor = self._readers.pop(rid)
                self.last_active = time.monotonic()
                self._evict_passed_locked()

    def _evict_passed_locked(self) -> None:
        """Drop blocks every active reader has moved past, waking paused workers."""
        if not self._readers:
            return  # no readers: keep everything for a returning console
        watermark = min(self._readers.values())
        for idx in [i for i in self._blocks if i < watermark]:
            self._cached -= len(self._blocks.pop(idx))
        self._cond.notify_all()


_downloads: Dict[str, Download] = {}
_downloads_lock = threading.Lock()


def _probe(url: str) -> Tuple[int, str]:
    """Learn a URL's total size and follow its redirect chain once.

    Sony's CDN sometimes 302s requests to an edge-cache host; resolving once here
    keeps per-block fetches from paying a redirect round-trip each.
    """
    with open_upstream(url, "bytes=0-0") as resp:
        content_range = resp.headers.get("content-range") or ""
        return int(content_range.rsplit("/", 1)[1]), resp.geturl()


def get_download(url: str) -> Download:
    """Return the (possibly new) prefetch entry for a URL.

    Stale entries are swept, and an entry whose prefetch failed is replaced so
    a console retry starts fresh.
    """
    with _downloads_lock:
        for key in [k for k, d in _downloads.items() if d.stale()]:
            _downloads.pop(key).abort()
        dl = _downloads.get(url)
        if dl is not None and dl.error is not None:
            dl.abort()
            dl = None
        if dl is None:
            total, resolved = _probe(url)
            dl = Download(url, resolved, total)
            _downloads[url] = dl
            logger.info(
                "proxy: prefetching %s (%d bytes, %d workers)%s",
                url, dl.total, PREFETCH_WORKERS,
                f", edge {resolved}" if resolved != url else "",
            )
        return dl


def head_total(url: str) -> int:
    """Total size for a HEAD request, without starting a prefetch."""
    with _downloads_lock:
        dl = _downloads.get(url)
    return dl.total if dl is not None else _probe(url)[0]
