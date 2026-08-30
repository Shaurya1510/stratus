from __future__ import annotations

import json
import socket
import threading
import time

DISCOVERY_MESSAGE = b"STRATUS_DISCOVER_V1"


def serve_discovery(server_name: str, control_port: int, discovery_port: int, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", discovery_port))
    sock.settimeout(1)
    try:
        while not stop.is_set():
            try: message, address = sock.recvfrom(1024)
            except socket.timeout: continue
            if message == DISCOVERY_MESSAGE:
                payload = json.dumps({"protocol": 1, "name": server_name, "control_port": control_port}).encode()
                sock.sendto(payload, address)
    finally: sock.close()


def discover(discovery_port: int = 5051, timeout: float = 1.5) -> list[dict]:
    results: dict[tuple[str, int], dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0)); sock.settimeout(0.2)
    sock.sendto(DISCOVERY_MESSAGE, ("255.255.255.255", discovery_port))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try: data, address = sock.recvfrom(4096)
        except socket.timeout: continue
        try:
            item = json.loads(data); item["host"] = address[0]
            results[(address[0], int(item["control_port"]))] = item
        except (ValueError, KeyError, TypeError): continue
    sock.close()
    return list(results.values())
