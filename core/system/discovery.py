"""Finding Stratus servers on the local network.

A server answers a UDP probe with its name and control port. Simple in theory;
the difficulty is entirely in getting the probe onto the right wire.

Three things went wrong in practice and are handled here:

* **One broadcast address is not enough.** 255.255.255.255 is the "limited
  broadcast" address. It is never routed, and several stacks — phone hotspots
  especially — drop it or fail to deliver it to every client. The
  subnet-directed broadcast (192.168.43.255 for a 192.168.43.0/24 network) is
  usually the one that arrives, so both are sent.

* **One interface is not enough.** The operating system sends a broadcast out
  of whichever interface the default route names. A virtual machine typically
  has two or three, and the one carrying the Wi-Fi the other person is on is
  often not the default. A separate socket is therefore bound to every local
  address and the probe is sent from each.

* **One packet is not enough.** UDP has no retransmission, and a hotspot under
  load drops packets freely. The probe is repeated until the deadline.

And when broadcasting fails entirely, it is abandoned: every candidate address
is probed individually instead. A unicast packet is ordinary traffic. It is
routed, it crosses between subnets, and it passes the isolation rules that
exist specifically to drop broadcasts. That is what lets a server on another
network be found at all.

The one thing nothing here can do is reach a machine that has no address on any
network the searcher can route to. A virtual machine on NAT is the usual
example: it can open connections outwards, so it can *join* a server, but
nothing outside can reach it, so it cannot usefully *host* one.
"""
from __future__ import annotations

import json
import select
import socket
import threading
import time

DISCOVERY_MESSAGE = b"STRATUS_DISCOVER_V1"

# Long enough that a slow or busy hotspot has several chances to answer, short
# enough that the scan still feels immediate.
DEFAULT_TIMEOUT = 2.5
PROBE_INTERVAL = 0.4

# Ranges a phone or laptop hands out when it shares its connection. A machine
# on its own private network (a NAT virtual machine, say) can still route to
# these, so probing them finds servers that no broadcast could ever reach.
COMMON_RANGES = (
    "192.168.43.0/24",     # Android tethering
    "172.20.10.0/28",      # iPhone personal hotspot
    "192.168.137.0/24",    # Windows mobile hotspot / ICS
    "10.42.0.0/24",        # NetworkManager hotspot (GNOME)
    "192.168.1.0/24",      # the two commonest home router ranges
    "192.168.0.0/24",
)

# A sweep must stay quick and stay polite. A /24 is 254 addresses; this allows
# a handful of those and refuses to enumerate anything larger, so a /16 route
# cannot turn a search into a ten-minute scan of sixty thousand machines.
MAX_SWEEP_ADDRESSES = 1800
MAX_NETWORK_SIZE = 512


def serve_discovery(server_name: str, control_port: int, discovery_port: int, stop: threading.Event) -> None:
    """Answer probes until told to stop.

    Bound to 0.0.0.0 so a probe arriving on any interface is heard, and the
    reply goes back to the address it came from, which is already unicast and
    needs no broadcast permission.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        # Some stacks deliver a broadcast to a bound socket only when this is
        # set, even though the socket only ever receives here.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except OSError:
        pass
    sock.bind(("0.0.0.0", discovery_port))
    sock.settimeout(1)
    try:
        while not stop.is_set():
            try: message, address = sock.recvfrom(1024)
            except socket.timeout: continue
            except OSError: continue
            if message.strip() != DISCOVERY_MESSAGE: continue
            payload = json.dumps({"protocol": 1, "name": server_name,
                                  "control_port": control_port}).encode()
            try: sock.sendto(payload, address)
            except OSError: continue
    finally:
        sock.close()


def _probe_targets(discovery_port: int) -> list[tuple[str | None, list[str]]]:
    """Where to send from, and where to send to.

    Each entry is one local address to send from, paired with the broadcast
    addresses worth trying from it. Binding the socket to a specific local
    address is what forces the packet onto that interface rather than whichever
    one the default route prefers.
    """
    from core.system.network import local_interfaces

    targets: list[tuple[str | None, list[str]]] = []
    try:
        interfaces = local_interfaces()
    except Exception:
        interfaces = []

    for item in interfaces:
        destinations = []
        if item.get("broadcast"): destinations.append(item["broadcast"])
        destinations.append("255.255.255.255")
        targets.append((item["address"], destinations))

    # A machine whose interfaces could not be read still gets the default route.
    if not targets:
        targets.append((None, ["255.255.255.255"]))
    return targets


def discover(discovery_port: int = 5051, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Every server that answers, keyed by address and control port."""
    results: dict[tuple[str, int], dict] = {}
    sockets: list[socket.socket] = []
    plan: list[tuple[socket.socket, list[str]]] = []

    for source, destinations in _probe_targets(discovery_port):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((source or "", 0))
            sock.setblocking(False)
        except OSError:
            # An interface can disappear between being listed and being used.
            continue
        sockets.append(sock)
        plan.append((sock, destinations))

    if not sockets: return []

    deadline = time.monotonic() + timeout
    next_probe = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_probe:
                for sock, destinations in plan:
                    for destination in destinations:
                        try: sock.sendto(DISCOVERY_MESSAGE, (destination, discovery_port))
                        except OSError: continue      # no route this way; try the rest
                next_probe = now + PROBE_INTERVAL

            remaining = min(next_probe, deadline) - time.monotonic()
            if remaining <= 0: continue
            readable, _, _ = select.select(sockets, [], [], remaining)
            for sock in readable:
                try: data, address = sock.recvfrom(4096)
                except OSError: continue
                try:
                    item = json.loads(data)
                    item["host"] = address[0]
                    results[(address[0], int(item["control_port"]))] = item
                except (ValueError, KeyError, TypeError):
                    continue
    finally:
        for sock in sockets:
            try: sock.close()
            except OSError: pass

    return list(results.values())


def reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether a TCP connection to a control server can be opened.

    Discovery uses UDP broadcast, which a hotspot may block while still passing
    ordinary traffic. When someone types an address by hand this answers the
    question discovery could not.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def candidate_addresses(deep: bool = True, extra: list[str] | None = None) -> list[str]:
    """Every address worth probing one by one.

    The networks this machine is on come first, because a server is most often
    on one of them. `deep` adds the ranges a shared connection typically hands
    out, which is what finds a server on a network this machine is not itself a
    member of but can still route to.

    Broadcast and network addresses are skipped — no host answers on those —
    and so is this machine's own address.
    """
    import ipaddress

    from core.system.network import local_interfaces

    try: interfaces = local_interfaces()
    except Exception: interfaces = []

    mine = {item.get("address") for item in interfaces if item.get("address")}
    networks: list[ipaddress.IPv4Network] = []
    seen_networks: set[str] = set()

    def add_network(text: str) -> None:
        try: network = ipaddress.ip_network(text, strict=False)
        except ValueError: return
        if network.num_addresses > MAX_NETWORK_SIZE: return
        if str(network) in seen_networks: return
        seen_networks.add(str(network))
        networks.append(network)

    for item in interfaces:
        # An interface record may be missing its network, in which case one is
        # derived from the address and prefix rather than the whole search
        # failing over a single odd entry.
        text = item.get("network") or ""
        if not text and item.get("address"):
            text = f"{item['address']}/{item.get('prefix', 24)}"
        if text: add_network(text)
    if deep:
        for text in COMMON_RANGES:
            add_network(text)

    addresses: list[str] = []
    chosen: set[str] = set()

    def take(value: str) -> bool:
        """Add one address. Returns False once the cap is reached."""
        if value in mine or value in chosen: return True
        chosen.add(value)
        addresses.append(value)
        return len(addresses) < MAX_SWEEP_ADDRESSES

    for value in extra or []:
        if value and not take(value): return addresses

    for network in networks:
        for host in network.hosts():
            if not take(str(host)): return addresses
    return addresses


def neighbours() -> list[str]:
    """Machines this one has already exchanged packets with.

    The kernel keeps them in its neighbour table, so they are known to be
    present and are worth probing before anything else. On a busy network this
    finds the answer long before a full sweep would.
    """
    import subprocess

    try:
        result = subprocess.run(["ip", "-4", "neigh", "show"],
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if not parts: continue
        if "FAILED" in line or "INCOMPLETE" in line: continue
        found.append(parts[0])
    return found


def sweep(addresses: list[str], discovery_port: int, timeout: float,
          results: dict | None = None) -> dict:
    """Probe each address directly and collect whatever answers.

    Unicast, so the operating system routes each packet by its destination and
    no interface juggling is needed. Probes go out in bursts with a short pause
    between them: firing fifteen hundred datagrams at once makes a phone drop
    most of them, which would defeat the entire point.
    """
    found = results if results is not None else {}
    if not addresses: return found

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    deadline = time.monotonic() + timeout
    burst = 120

    def collect() -> None:
        while True:
            readable, _, _ = select.select([sock], [], [], 0)
            if not readable: return
            try: data, address = sock.recvfrom(4096)
            except OSError: return
            try:
                item = json.loads(data)
                item["host"] = address[0]
                item["found_by"] = "direct"
                found[(address[0], int(item["control_port"]))] = item
            except (ValueError, KeyError, TypeError):
                continue

    try:
        for start in range(0, len(addresses), burst):
            if time.monotonic() >= deadline: break
            for address in addresses[start:start + burst]:
                try: sock.sendto(DISCOVERY_MESSAGE, (address, discovery_port))
                except OSError: continue
            time.sleep(0.05)
            collect()

        # Keep listening after the last burst: a reply is always slower than
        # the send that provoked it.
        while time.monotonic() < deadline:
            readable, _, _ = select.select([sock], [], [], 0.1)
            if readable: collect()
    finally:
        sock.close()
    return found


def discover_everywhere(discovery_port: int = 5051, timeout: float = 4.0,
                        deep: bool = True, extra: list[str] | None = None) -> list[dict]:
    """Find servers whether or not they are on this machine's own network.

    Broadcast first, because it is instant and costs one packet. Then every
    address individually, which is what works when the network refuses to carry
    a broadcast — the usual reason two people on one phone hotspot cannot see
    each other.
    """
    results: dict[tuple[str, int], dict] = {}
    for item in discover(discovery_port, timeout=min(timeout / 3, 1.2)):
        item.setdefault("found_by", "broadcast")
        results[(item["host"], int(item["control_port"]))] = item

    known = neighbours()
    addresses = candidate_addresses(deep=deep, extra=(extra or []) + known)
    remaining = max(timeout - min(timeout / 3, 1.2), 1.0)
    sweep(addresses, discovery_port, remaining, results)
    return list(results.values())
