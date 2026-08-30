# Stratus architecture

## Separation

The Git checkout is program code. Selected storage is server state. Updating or recloning code must not overwrite server state.

```text
StratusData/servers/<slug>/
  config/                 storage UUID and host policy
  platform/               SQLite metadata, events and device records
  users/<device-id>/      profile, private drive, received files, exports, deployment indexes
  chat/                   encrypted Chat data and attachments
  drive/                  private/public/direct/group object namespaces
  deployments/<id>/       uploaded source and logs
  app-data/<id>/          application-owned persistent data
  uploads/                pending deployment archives
  logs/
  backups/
```

Python virtual environments are regenerable and live under `~/.local/share/stratus/runtimes/` so external exFAT disks can be used safely for data.

## Identity

Each Linux client generates a random device ID, authentication secret, and P-256 encryption key pair under `~/.config/stratus/device.json`. Only public identity fields leave the device. Display names use `linux-user@hostname`; authorization uses the stable device ID and keys.

## Encrypted terminal data

For every message or file, the client creates a random content key and encrypts the content with AES-256-GCM. The content key is wrapped separately for each authorised device using an ephemeral P-256 ECDH agreement and HKDF-SHA256. Version 1 payloads (X25519 with ChaCha20-Poly1305) are still decrypted so existing servers keep working. Conversation/scope membership is validated by the host, while plaintext stays client-side.

This envelope MVP provides authenticated end-to-end content encryption but is not Signal Double Ratchet or MLS: it does not yet provide their forward-secrecy/post-compromise guarantees.

## Control connection

`core/platform/protocol.py` frames every request as length-prefixed JSON. A client
session (`core/platform/session.py`) opens one TCP connection and reuses it for every
request: the host authenticates the token on each frame but only re-registers
the device every 30 seconds, and the connection is dropped after 5 minutes of
silence. A failed write on a stale socket reconnects and retries once, so
sleeping laptops and roaming access points do not surface as errors.

Directory lookups that are needed to encrypt each message (`recipients`,
`groups`, `policy`) are cached client-side for a few seconds. `chat_overview`
returns the newest message id per conversation the device belongs to in one
grouped query, so unread badges cost one request rather than one per chat.
Listing the Drive returns metadata and envelopes only; object ciphertext is read
from disk exclusively by `drive_get`.

## Terminal interface

`core/interface/app.py` holds every screen. Network work runs in threaded workers
and touches widgets only through `call_from_thread`, so the interface stays
responsive during encryption, uploads and deployments. Screens are persistent:
sending, uploading or deleting updates the current screen and reports failures
as notifications instead of returning to a menu. When Textual is unavailable the
curses/plain menus in `core/interface/terminal.py` still provide every feature.

## Hosted apps

Static, Python and Node.js projects require `stratus.yaml`. Guest uploads pass policy, quota, runtime, archive-safety, port, and optional approval checks. Applications run as non-root processes; `systemd-run --user` applies CPU/RAM properties when available.
