"""Length-prefixed JSON protocol for Stratus control traffic."""
from __future__ import annotations

import json
import socket
import struct
from typing import Any, Callable

MAX_FRAME_BYTES = 16 * 1024 * 1024


# Large frames are moved in pieces of this size so a progress callback can
# report how far a transfer has got. Small frames still go in one call.
CHUNK_BYTES = 64 * 1024

# progress(done_bytes, total_bytes) — called as a frame is sent or received.
Progress = Callable[[int, int], None] | None


def _read_exact(sock: socket.socket, length: int, progress: Progress = None) -> bytes:
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(min(length - len(data), CHUNK_BYTES * 4))
        if not chunk:
            raise ConnectionError("connection closed while reading frame")
        data.extend(chunk)
        if progress: progress(len(data), length)
    return bytes(data)


def send_message(sock: socket.socket, payload: dict[str, Any], progress: Progress = None) -> None:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ValueError("message exceeds maximum frame size")
    frame = struct.pack("!I", len(encoded)) + encoded
    if not progress or len(frame) <= CHUNK_BYTES:
        sock.sendall(frame)
        if progress: progress(len(frame), len(frame))
        return
    view = memoryview(frame)
    for start in range(0, len(frame), CHUNK_BYTES):
        sock.sendall(view[start:start + CHUNK_BYTES])
        progress(min(start + CHUNK_BYTES, len(frame)), len(frame))


def receive_message(sock: socket.socket, progress: Progress = None) -> dict[str, Any]:
    size = struct.unpack("!I", _read_exact(sock, 4))[0]
    if size == 0 or size > MAX_FRAME_BYTES:
        raise ValueError("invalid frame size")
    message = json.loads(_read_exact(sock, size, progress).decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("message must be a JSON object")
    return message
