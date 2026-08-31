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
