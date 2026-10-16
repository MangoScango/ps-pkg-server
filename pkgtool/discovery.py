"""Console discovery: SRCH probes, subnet sweeps, DPI port checks, saved consoles.

A console answers a UDP ``SRCH`` datagram on port 9302 (PS5) or 987 (PS4),
whether it is awake or in rest mode. Discovery broadcasts one probe first. A
container bridge never carries a broadcast and a default-deny host firewall
drops its replies, so when nothing answers the subnet is swept with a unicast
probe per address, which both of those allow.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger("uvicorn.error")

PROBES: Mapping[int, bytes] = {
    9302: b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n",
    987: b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00020020\n",
}

# (protocol, port) of each known DPI listener, most capable first.
DPI_SERVERS = (
    ("ezremote", 9040),
    ("etahen_v2", 12800),
    ("etahen_v1", 9090),
    ("remote_pkg", 12800),
)
DPI_PORTS = tuple(sorted({port for _, port in DPI_SERVERS}))

# etaHEN DPI v2 (PS5) and the PS4 remote package installer share this port, so
# only the console type says which one is listening.
SHARED_PORT = 12800
SHARED_PORT_OWNER = {"PS5": "etahen_v2", "PS4": "remote_pkg"}

MAX_SWEEP_HOSTS = 1024
LIMITED_BROADCAST = "255.255.255.255"
MAX_NAME_LENGTH = 64

# The only ranges consoles are probed in. ipaddress.is_private is wider than it
# sounds: it also covers 0.0.0.0/8, link-local (cloud metadata) and broadcast.
LAN_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)


@dataclass
class ConsoleInfo:
    id: str
    ip: str
    name: str
    type: str  # "PS5" or "PS4"
    firmware: str  # "13.60", or the raw value when it is not BCD
    status: str  # "awake" or "standby"
    last_seen: float = 0.0

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


def _format_firmware(raw: str) -> str:
    """'07610000' -> '7.61'. Anything but eight hex digits is returned as-is."""
    if not re.fullmatch(r"[0-9a-fA-F]{8}", raw):
        return raw
    value = int(raw, 16)
    return "%x.%02x" % (value >> 24, (value >> 16) & 0xFF)


def _clean_name(name: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:MAX_NAME_LENGTH]


def parse_reply(data: bytes, ip: str) -> Optional[ConsoleInfo]:
    """Parse a SRCH reply. Returns None for anything that is not a console."""
    lines = data.decode("utf-8", "replace").replace("\r", "").split("\n")
    if not lines[0].startswith("HTTP/"):
        return None
    values: Dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip().lower()] = value.strip()
    host_id = values.get("host-id")
    if not host_id:
        return None
    parts = lines[0].split()
    return ConsoleInfo(
        id=host_id,
        ip=ip,
        name=_clean_name(values.get("host-name", "")) or ip,
        type=values.get("host-type", "").upper(),
        firmware=_format_firmware(values.get("system-version", "")),
        status="standby" if parts[1:2] == ["620"] else "awake",
        last_seen=time.time(),
    )


def is_lan_address(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return any(addr in net for net in LAN_NETWORKS)


def local_subnet() -> Optional[str]:
    """The /24 around the address this host uses to reach the LAN, if it has one.

    Connecting a UDP socket only selects a route; nothing is sent. The prefix is
    assumed to be 24, since the real netmask is not available portably.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    if not is_lan_address(address):
        return None
    return str(ipaddress.ip_network(f"{address}/24", strict=False))


def _udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # A sweep queues a datagram per address, and the kernel holds those for hosts
    # that have not answered ARP. The default send buffer fills and blocks sendto
    # for seconds; a larger one does not (the kernel clamps it to its own limit).
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    sock.bind(("0.0.0.0", 0))
    return sock


def probe(
    ip: str, timeout: float = 1.0, probes: Mapping[int, bytes] = PROBES
) -> Optional[ConsoleInfo]:
    """Ask one address whether it is a console. None when nothing answers."""
    with _udp_socket() as sock:
        try:
            for port, message in probes.items():
                sock.sendto(message, (ip, port))
        except OSError:
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                data, (source, _) = sock.recvfrom(4096)
            except socket.timeout:
                return None
            if source == ip:
                info = parse_reply(data, ip)
                if info:
                    return info


def _network(subnet: str) -> ipaddress.IPv4Network:
    network = ipaddress.ip_network(subnet, strict=False)
    if network.version != 4:
        raise ValueError(f"{subnet} is not an IPv4 subnet")
    if network.num_addresses > MAX_SWEEP_HOSTS:
        raise ValueError(f"{subnet} has {network.num_addresses} addresses; the limit is {MAX_SWEEP_HOSTS}")
    return network


def _send_all(sock: socket.socket, targets: Iterable[str], probes: Mapping[int, bytes]) -> None:
    """Send every probe to every target. Raises only when nothing could be sent."""
    sent = 0
    first_error: Optional[OSError] = None
    for target in targets:
        for port, message in probes.items():
            try:
                sock.sendto(message, (target, port))
                sent += 1
            except OSError as e:
                first_error = first_error or e
    if not sent and first_error:
        raise first_error


def _gather(
    sock: socket.socket, timeout: float, accept: Callable[[str], bool]
) -> List[ConsoleInfo]:
    """Collect console replies until ``timeout``, from sources ``accept`` allows."""
    found: Dict[str, ConsoleInfo] = {}
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, (source, _) = sock.recvfrom(4096)
        except socket.timeout:
            break
        if accept(source):
            info = parse_reply(data, source)
            if info:
                found[info.id] = info
    return list(found.values())


def sweep(
    subnet: str, timeout: float = 3.0, probes: Mapping[int, bytes] = PROBES
) -> List[ConsoleInfo]:
    """Probe every host in ``subnet`` once and return the consoles that answer."""
    hosts = [str(h) for h in _network(subnet).hosts()]
    with _udp_socket() as sock:
        _send_all(sock, hosts, probes)
        return _gather(sock, timeout, set(hosts).__contains__)


def broadcast(
    addresses: Iterable[str],
    timeout: float = 1.5,
    probes: Mapping[int, bytes] = PROBES,
    accept: Callable[[str], bool] = is_lan_address,
) -> List[ConsoleInfo]:
    """Send each probe to broadcast ``addresses`` and collect the replies."""
    with _udp_socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        _send_all(sock, addresses, probes)
        return _gather(sock, timeout, accept)


def discover(
    subnet: str = "",
    broadcast_timeout: float = 1.5,
    sweep_timeout: float = 3.0,
    probes: Mapping[int, bytes] = PROBES,
) -> Tuple[List[ConsoleInfo], str]:
    """Find consoles with one broadcast, sweeping ``subnet`` only if it finds none.

    Returns the consoles and the step that produced them. A host firewall that
    drops the replies to a broadcast, or a container bridge that never carries
    it, leaves the sweep as the fallback.
    """
    addresses = [LIMITED_BROADCAST]
    if subnet:
        addresses.append(str(_network(subnet).broadcast_address))
    try:
        found = broadcast(addresses, broadcast_timeout, probes)
    except OSError as e:
        logger.info("broadcast discovery failed: %s", e)
        found = []
    if found or not subnet:
        return found, "broadcast"
    return sweep(subnet, sweep_timeout, probes), "sweep"


def check_ports(
    ip: str, ports: Iterable[int] = DPI_PORTS, timeout: float = 1.0
) -> Dict[int, bool]:
    """Which of ``ports`` accept a TCP connection."""
    wanted = sorted(set(ports))

    def is_open(port: int) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False

    with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        return dict(zip(wanted, pool.map(is_open, wanted)))


def dpi_servers(console_type: str) -> List[tuple]:
    """Known (protocol, port) pairs a console of this type may be running."""
    return [
        (protocol, port)
        for protocol, port in DPI_SERVERS
        if port != SHARED_PORT or SHARED_PORT_OWNER.get(console_type) == protocol
    ]


def suggest(
    open_ports: Mapping[int, bool],
    console_type: str,
    port: Optional[int],
) -> Optional[Dict[str, object]]:
    """The DPI to switch to, or None when the current port is already open or
    nothing known is listening."""
    if port and open_ports.get(port):
        return None
    for protocol, dpi_port in dpi_servers(console_type):
        if open_ports.get(dpi_port):
            return {"protocol": protocol, "port": dpi_port}
    return None


def inspect(ip: str, port: Optional[int] = None) -> Dict[str, object]:
    """Probe a console for status and check which DPI ports are open."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        info_job = pool.submit(probe, ip)
        ports_job = pool.submit(check_ports, ip, set(DPI_PORTS) | ({port} if port else set()))
        info, open_ports = info_job.result(), ports_job.result()
    console_type = info.type if info else ""
    return {
        "console": info.to_dict() if info else None,
        "ports": {str(p): ok for p, ok in open_ports.items()},
        "dpi": [
            {"protocol": protocol, "port": dpi_port}
            for protocol, dpi_port in dpi_servers(console_type)
            if open_ports.get(dpi_port)
        ],
        "suggested": suggest(open_ports, console_type, port),
    }


class ConsoleStore:
    """Consoles seen so far, persisted as a small JSON file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._consoles: Dict[str, ConsoleInfo] = self._load()

    def _load(self) -> Dict[str, ConsoleInfo]:
        try:
            with open(self.path, encoding="utf-8") as f:
                entries = json.load(f)["consoles"]
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        known = {f.name for f in fields(ConsoleInfo)}
        loaded: Dict[str, ConsoleInfo] = {}
        for entry in entries if isinstance(entries, list) else []:
            try:
                info = ConsoleInfo(**{k: v for k, v in entry.items() if k in known})
            except (TypeError, AttributeError):
                continue
            loaded[info.id] = info
        return loaded

    def all(self) -> List[ConsoleInfo]:
        with self._lock:
            return sorted(self._consoles.values(), key=lambda c: (c.name.lower(), c.ip))

    def upsert(self, infos: Iterable[ConsoleInfo]) -> None:
        with self._lock:
            for info in infos:
                # An address belongs to one console; a newer owner replaces the old.
                for other in [c for c in self._consoles.values() if c.ip == info.ip and c.id != info.id]:
                    del self._consoles[other.id]
                self._consoles[info.id] = info
            self._save()

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".part")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"consoles": [c.to_dict() for c in self._consoles.values()]}, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("could not save consoles to %s: %s", self.path, e)
