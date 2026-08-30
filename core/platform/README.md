# `core/platform/` — storage, protocol and rules

| File | What it does |
|---|---|
| `database.py` | SQLite schema and every query. WAL mode, one locked connection. |
| `platform_api.py` | **The membership rules.** Who may read what. |
| `protocol.py` | Length-prefixed JSON framing over TCP. |
| `session.py` | Client-side connection: kept open, reused, auto-reconnecting. |

## `platform_api.py` is the important one

It is small, and it is the only place that decides access. `core/server.py` and
both websites in `web/` call these same functions, which is what stops the
terminal and the browser drifting into different ideas of who is allowed to read
a message.

The core check is simple and strict: the set of device ids an envelope was
sealed for must **exactly equal** the membership of the conversation or share
scope it claims. Not a subset, not a superset. A message sealed for fewer people
than the conversation is rejected, and so is one sealed for more.

## The wire protocol

Four bytes of big-endian length, then that many bytes of UTF-8 JSON. That is the
whole thing. TCP is a stream with no message boundaries, so something has to say
where one message ends — a length prefix is the simplest answer that cannot be
confused by content.

## Two performance decisions

**Connection reuse.** `session.py` keeps one TCP connection open instead of
opening a new one per request. Chat polls roughly once a second, so the
handshake, token check and device re-registration dominated everything else.
Measured at 13x faster per request on loopback, and the gap is wider over Wi-Fi
where every handshake costs a real round trip.

**Metadata-only listing.** `encrypted_files()` never reads file contents from
disk. Ciphertext lives in `drive/objects/` and is only loaded when a specific
file is fetched. Without this, listing a drive holding fifty 10 MB files moved
500 MB across the network just to draw a table.
