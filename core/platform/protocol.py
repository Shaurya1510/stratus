"""Length-prefixed JSON protocol for Stratus control traffic."""
from __future__ import annotations

import json
import socket
import struct
from typing import Any

MAX_FRAME_BYTES = 16 * 1024 * 1024


def _read_exact(sock: socket.socket, length: int) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("connection closed while reading frame")
        data.extend(chunk)
    return bytes(data)


def send_message(sock: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("message exceeds maximum frame size")
    sock.sendall(struct.pack("!I", len(encoded)) + encoded)


def receive_message(sock: socket.socket) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if size == 0 or size > MAX_FRAME_BYTES:
        raise ValueError("invalid frame size")
    message = json.loads(_read_exact(sock, size).decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message
