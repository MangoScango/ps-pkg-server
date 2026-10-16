import json
import os
import socket
import tempfile
import threading

import pytest

from pkgtool import discovery
from pkgtool.discovery import ConsoleInfo, ConsoleStore

STANDBY = (
    b"HTTP/1.1 620 Server Standby\nhost-id:EC748C02C215\nhost-type:PS5\nhost-name:PS5-054\n"
    b"host-request-port:997\ndevice-discovery-protocol-version:00030010\nsystem-version:07610000\n"
)
AWAKE = (
    b"HTTP/1.1 200 Ok\nhost-id:78C881C0A2B4\nhost-type:PS5\nhost-name:Living Room\n"
    b"host-request-port:997\ndevice-discovery-protocol-version:00030010\nsystem-version:13600007\n"
)


def _info(host_id="A", ip="192.168.1.5", name="PS5", type="PS5", firmware="9.00", status="awake"):
    return ConsoleInfo(host_id, ip, name, type, firmware, status, 1.0)


def test_parse_reply_standby():
    info = discovery.parse_reply(STANDBY, "192.168.1.172")
    assert (info.id, info.ip, info.name, info.type) == ("EC748C02C215", "192.168.1.172", "PS5-054", "PS5")
    assert info.firmware == "7.61"
    assert info.status == "standby"


def test_parse_reply_awake():
    info = discovery.parse_reply(AWAKE, "192.168.1.18")
    assert info.status == "awake"
    assert info.firmware == "13.60"
    assert info.name == "Living Room"


def test_parse_reply_rejects_non_console_traffic():
    assert discovery.parse_reply(b"SRCH * HTTP/1.1\n", "10.0.0.1") is None
    assert discovery.parse_reply(b"HTTP/1.1 200 Ok\nhost-type:PS5\n", "10.0.0.1") is None
    assert discovery.parse_reply(b"\xff\xfe\x00garbage", "10.0.0.1") is None
    assert discovery.parse_reply(b"", "10.0.0.1") is None


def test_parse_reply_cleans_the_name():
    raw = b"HTTP/1.1 200 Ok\nhost-id:X\nhost-name:Bad\x07Name\x1b[0m" + b"z" * 200 + b"\n"
    info = discovery.parse_reply(raw, "10.0.0.1")
    assert "\x07" not in info.name and "\x1b" not in info.name
    assert len(info.name) == discovery.MAX_NAME_LENGTH


def test_firmware_formatting():
    assert discovery._format_firmware("07610000") == "7.61"
    assert discovery._format_firmware("13600007") == "13.60"
    assert discovery._format_firmware("11500000") == "11.50"
    assert discovery._format_firmware("") == ""
    assert discovery._format_firmware("not-hex!") == "not-hex!"


def _responder(reply, host="127.0.0.1"):
    """A UDP socket that answers any datagram with ``reply``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, 0))
    sock.settimeout(5)

    def serve():
        try:
            while True:
                _, addr = sock.recvfrom(1024)
                sock.sendto(reply, addr)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return sock


def test_probe_reads_a_live_reply():
    sock = _responder(STANDBY)
    try:
        info = discovery.probe("127.0.0.1", timeout=2, probes={sock.getsockname()[1]: b"SRCH"})
    finally:
        sock.close()
    assert info.name == "PS5-054"


def test_probe_times_out_quietly():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    try:
        assert discovery.probe("127.0.0.1", timeout=0.2, probes={sock.getsockname()[1]: b"SRCH"}) is None
    finally:
        sock.close()


def test_probe_ignores_replies_from_other_hosts():
    sock = _responder(AWAKE)
    try:
        port = sock.getsockname()[1]
        assert discovery.probe("127.0.0.2", timeout=0.3, probes={port: b"SRCH"}) is None
    finally:
        sock.close()


def test_sweep_collects_responders_in_the_subnet():
    sock = _responder(AWAKE, host="127.0.0.2")
    try:
        found = discovery.sweep("127.0.0.2/32", timeout=1.5, probes={sock.getsockname()[1]: b"SRCH"})
    finally:
        sock.close()
    assert [c.name for c in found] == ["Living Room"]
    assert found[0].ip == "127.0.0.2"


def test_sweep_refuses_huge_and_invalid_subnets():
    for bad in ("10.0.0.0/8", "192.168.0.0/16", "not a subnet", "fd00::/64"):
        with pytest.raises(ValueError):
            discovery.sweep(bad)


def test_check_ports_reports_open_and_closed():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    open_port, closed_port = listener.getsockname()[1], closed.getsockname()[1]
    closed.close()
    try:
        result = discovery.check_ports("127.0.0.1", [open_port, closed_port], timeout=1)
    finally:
        listener.close()
    assert result == {open_port: True, closed_port: False}


def test_dpi_servers_depend_on_console_type():
    ps5 = [p for p, _ in discovery.dpi_servers("PS5")]
    ps4 = [p for p, _ in discovery.dpi_servers("PS4")]
    assert "etahen_v2" in ps5 and "remote_pkg" not in ps5
    assert "remote_pkg" in ps4 and "etahen_v2" not in ps4
    # An unidentified console could be running either installer on the shared port.
    assert "remote_pkg" not in [p for p, _ in discovery.dpi_servers("")]


@pytest.mark.parametrize(
    "open_ports, console_type, current, expected",
    [
        ({9040: True, 9090: False, 12800: False}, "PS5", 9090, ("ezremote", 9040)),
        ({9040: False, 9090: False, 12800: True}, "PS5", 9040, ("etahen_v2", 12800)),
        ({9040: False, 9090: False, 12800: True}, "PS4", 9040, ("remote_pkg", 12800)),
        ({9040: False, 9090: True, 12800: False}, "PS5", 9040, ("etahen_v1", 9090)),
        # The port already in use is open: leave the user's protocol alone.
        ({9040: True, 9090: True, 12800: False}, "PS5", 9090, None),
        # Nothing listening, nothing to switch to.
        ({9040: False, 9090: False, 12800: False}, "PS5", 9040, None),
        # Several open: the most capable one wins.
        ({9040: True, 9090: True, 12800: True}, "PS5", 12801, ("ezremote", 9040)),
    ],
)
def test_suggest(open_ports, console_type, current, expected):
    got = discovery.suggest(open_ports, console_type, current)
    assert (got and (got["protocol"], got["port"])) == (expected or None)


def test_inspect_combines_status_and_ports(monkeypatch):
    monkeypatch.setattr(discovery, "probe", lambda ip, **kw: discovery.parse_reply(STANDBY, ip))
    monkeypatch.setattr(discovery, "check_ports", lambda ip, ports=(), **kw: {9040: False, 9090: False, 12800: True})
    result = discovery.inspect("192.168.1.172", 9040)
    assert result["console"]["name"] == "PS5-054"
    assert result["dpi"] == [{"protocol": "etahen_v2", "port": 12800}]
    assert result["suggested"] == {"protocol": "etahen_v2", "port": 12800}


def test_inspect_unknown_address(monkeypatch):
    monkeypatch.setattr(discovery, "probe", lambda ip, **kw: None)
    monkeypatch.setattr(discovery, "check_ports", lambda ip, ports=(), **kw: {9040: False, 9090: False, 12800: False})
    result = discovery.inspect("192.168.1.99")
    assert result["console"] is None and result["dpi"] == [] and result["suggested"] is None


def test_store_round_trip_and_ordering():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "consoles.json")
        store = ConsoleStore(path)
        assert store.all() == []
        store.upsert([_info("B", "10.0.0.2", "Zed"), _info("A", "10.0.0.1", "alpha")])
        assert [c.name for c in ConsoleStore(path).all()] == ["alpha", "Zed"]
        assert not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".part")]


def test_store_new_owner_of_an_address_replaces_the_old():
    with tempfile.TemporaryDirectory() as d:
        store = ConsoleStore(os.path.join(d, "c.json"))
        store.upsert([_info("OLD", "10.0.0.7", "old")])
        store.upsert([_info("NEW", "10.0.0.7", "new")])
        assert [c.id for c in store.all()] == ["NEW"]
        store.upsert([_info("NEW", "10.0.0.8", "new")])
        assert [(c.id, c.ip) for c in store.all()] == [("NEW", "10.0.0.8")]


def test_store_survives_a_damaged_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "c.json")
        for content in ("", "{not json", '{"consoles": 5}', '{"consoles": [1, {"id": "x"}]}', "[]"):
            with open(path, "w") as f:
                f.write(content)
            assert ConsoleStore(path).all() == []


def test_store_ignores_unknown_fields_in_saved_entries():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "c.json")
        entry = dict(_info().to_dict(), future_field=1)
        with open(path, "w") as f:
            json.dump({"consoles": [entry]}, f)
        assert [c.id for c in ConsoleStore(path).all()] == ["A"]


def test_is_lan_address_is_limited_to_rfc1918():
    for good in ("10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.1.1", "192.168.255.254"):
        assert discovery.is_lan_address(good), good
    for bad in ("8.8.8.8", "127.0.0.1", "169.254.169.254", "0.0.0.0", "255.255.255.255",
                "100.64.0.1", "172.32.0.1", "224.0.0.1", "::1", "nope", ""):
        assert not discovery.is_lan_address(bad), bad


class _RouteSocket:
    """Stands in for the UDP socket local_subnet() uses to pick a route."""

    def __init__(self, address, fail=False):
        self.address, self.fail = address, fail

    def connect(self, _target):
        if self.fail:
            raise OSError("network is unreachable")

    def getsockname(self):
        return (self.address, 40000)

    def close(self):
        pass


@pytest.mark.parametrize(
    "address, expected",
    [
        ("192.168.1.117", "192.168.1.0/24"),
        ("10.4.5.6", "10.4.5.0/24"),
        ("172.20.9.9", "172.20.9.0/24"),
        ("203.0.113.5", None),  # a public address is not a LAN
        ("127.0.0.1", None),
        ("0.0.0.0", None),
    ],
)
def test_local_subnet(monkeypatch, address, expected):
    monkeypatch.setattr(discovery.socket, "socket", lambda *a, **k: _RouteSocket(address))
    assert discovery.local_subnet() == expected


def test_local_subnet_without_a_route(monkeypatch):
    monkeypatch.setattr(discovery.socket, "socket", lambda *a, **k: _RouteSocket("", fail=True))
    assert discovery.local_subnet() is None


def _wildcard_responder(reply):
    """A UDP responder that, bound to the wildcard address, also hears broadcasts."""
    return _responder(reply, host="0.0.0.0")


def test_broadcast_collects_replies_from_any_host():
    sock = _wildcard_responder(AWAKE)
    try:
        found = discovery.broadcast(
            ["127.255.255.255"], timeout=1.5, probes={sock.getsockname()[1]: b"SRCH"},
            accept=lambda ip: True,
        )
    finally:
        sock.close()
    assert [c.name for c in found] == ["Living Room"]


def test_broadcast_ignores_replies_from_outside_the_lan_by_default():
    sock = _wildcard_responder(AWAKE)
    try:
        # The reply comes from 127.0.0.1, which is not an RFC1918 address.
        found = discovery.broadcast(
            ["127.255.255.255"], timeout=0.5, probes={sock.getsockname()[1]: b"SRCH"},
        )
    finally:
        sock.close()
    assert found == []


def _record_steps(monkeypatch, broadcast_result=(), broadcast_error=None, sweep_result=()):
    steps = []

    def fake_broadcast(addresses, *args, **kwargs):
        steps.append(("broadcast", list(addresses)))
        if broadcast_error:
            raise broadcast_error
        return list(broadcast_result)

    def fake_sweep(subnet, *args, **kwargs):
        steps.append(("sweep", subnet))
        return list(sweep_result)

    monkeypatch.setattr(discovery, "broadcast", fake_broadcast)
    monkeypatch.setattr(discovery, "sweep", fake_sweep)
    return steps


def test_discover_stops_when_the_broadcast_finds_a_console(monkeypatch):
    steps = _record_steps(monkeypatch, broadcast_result=[_info("A")])
    found, method = discovery.discover("192.168.1.0/24")
    assert (len(found), method) == (1, "broadcast")
    assert steps == [("broadcast", ["255.255.255.255", "192.168.1.255"])]


def test_discover_sweeps_when_the_broadcast_finds_nothing(monkeypatch):
    steps = _record_steps(monkeypatch, sweep_result=[_info("A"), _info("B", "192.168.1.6")])
    found, method = discovery.discover("192.168.1.0/24")
    assert (len(found), method) == (2, "sweep")
    assert [s[0] for s in steps] == ["broadcast", "sweep"]
    assert steps[1] == ("sweep", "192.168.1.0/24")


def test_discover_sweeps_when_the_broadcast_cannot_be_sent(monkeypatch):
    steps = _record_steps(monkeypatch, broadcast_error=OSError("network is unreachable"), sweep_result=[_info()])
    found, method = discovery.discover("192.168.1.0/24")
    assert method == "sweep" and len(found) == 1
    assert [s[0] for s in steps] == ["broadcast", "sweep"]


def test_discover_without_a_subnet_only_broadcasts(monkeypatch):
    steps = _record_steps(monkeypatch)
    assert discovery.discover("") == ([], "broadcast")
    assert steps == [("broadcast", ["255.255.255.255"])]


def test_discover_rejects_a_bad_subnet_before_sending_anything(monkeypatch):
    steps = _record_steps(monkeypatch)
    for bad in ("10.0.0.0/8", "not a subnet", "fd00::/64"):
        with pytest.raises(ValueError):
            discovery.discover(bad)
    assert steps == []
