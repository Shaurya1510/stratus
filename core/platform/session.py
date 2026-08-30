"""Persistent client session for the Stratus terminal.

The terminal polls Chat and Drive continuously. A session keeps one TCP
connection open, reuses it for every request, transparently reconnects when
the host or the network drops it, and caches the small directory lookups
(recipients, groups) that would otherwise be re-fetched several times per
keystroke.
"""
from __future__ import annotations

import socket
import threading
import time

from core.security.identity import load_or_create_identity, public_identity
from core.platform.protocol import receive_message, send_message

CACHEABLE = {"recipients": 5.0, "groups": 10.0, "policy": 5.0}


class Session:
    def __init__(self, host: str, port: int, token: str, identity: dict | None = None, timeout: float = 15.0):
        self.host, self.port, self.token = host, port, token
        self.identity = identity or load_or_create_identity()
        self.public = public_identity(self.identity)
        self.timeout = timeout
        self.lock = threading.RLock()
        self.socket: socket.socket | None = None
        self.latency_ms = 0.0
        self.server_name = f"{host}:{port}"
        self._cache: dict[tuple, tuple[float, object]] = {}

    # ------------------------------------------------------------------ wire

    def connect(self) -> socket.socket:
        connection = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try: connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError: pass
        self.socket = connection
        return connection

    def close(self) -> None:
        with self.lock:
            if self.socket is not None:
                try: self.socket.close()
                except OSError: pass
                self.socket = None

    def _exchange(self, request: dict) -> dict:
        connection = self.socket or self.connect()
        send_message(connection, request)
        return receive_message(connection)

    def call(self, action: str, **extra):
        request = {"action": action, "token": self.token, "device": self.public}
        request.update(extra)
        with self.lock:
            started = time.perf_counter()
            try:
                reply = self._exchange(request)
            except (OSError, ConnectionError, ValueError):
                # A reused connection can be closed by the host, a sleeping
                # laptop, or a roaming access point. Reconnect once and retry
                # so the interface never shows an error for a stale socket.
                self.close()
                reply = self._exchange(request)
            self.latency_ms = (time.perf_counter() - started) * 1000
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "request failed"))
        return reply["data"]

    # ---------------------------------------------------------------- caches

    def cached(self, action: str, **extra):
        """Call, but reuse a very recent identical answer.

        Membership lists are needed to encrypt every message and every upload.
        Re-asking the host for them on each keystroke is pure latency, while a
        few seconds of staleness only means a device that joined a moment ago
        is included on the next send.
        """
        ttl = CACHEABLE.get(action)
        if ttl is None: return self.call(action, **extra)
        key = (action, tuple(sorted((k, str(v)) for k, v in extra.items())))
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl: return hit[1]
        value = self.call(action, **extra)
        self._cache[key] = (now, value)
        return value

    def invalidate(self, action: str | None = None) -> None:
        if action is None: self._cache.clear()
        else: self._cache = {key: value for key, value in self._cache.items() if key[0] != action}

    # ------------------------------------------------------------- lifecycle

    def ping(self) -> float:
        self.call("ping")
        return self.latency_ms

    def __enter__(self) -> "Session": return self
    def __exit__(self, *_) -> None: self.close()


def probe(host: str, port: int, token: str, timeout: float = 6.0) -> Session:
    """Open a session and verify the join token before any dashboard appears."""
    session = Session(host, port, token, timeout=timeout)
    try:
        session.cached("policy")
    except Exception:
        session.close()
        raise
    return session
