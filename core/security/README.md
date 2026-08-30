# `core/security/` — encryption and identity

Owner: **Shaurya Jain**

| File | What it does |
|---|---|
| `e2ee.py` | Envelope encryption: one random content key per message, wrapped separately for each recipient. P-256 ECDH + HKDF-SHA256 + AES-256-GCM. |
| `identity.py` | Device identity, key generation, and trust-on-first-use key pinning. |
| `browser_access.py` | Seals the device key with a passphrase (PBKDF2, 600k rounds) so a browser can unseal it locally and the host cannot. |

## How envelope encryption works

For every message or file:

1. Generate a random 256-bit **content key**
2. Encrypt the content **once** with AES-256-GCM under that key
3. For each recipient, do an ephemeral P-256 ECDH agreement with their public
   key, derive a wrapping key with HKDF-SHA256, and encrypt the content key
   with it
4. Send the ciphertext plus one small envelope per recipient

The host stores all of it and can open none of it. Encrypting the content once
rather than once per recipient is what keeps a 10 MB file from becoming 10 MB
times the number of members.

The conversation id is passed as **additional authenticated data**, so a message
captured from one conversation cannot be replayed into another — the tag will
not verify.

## Why P-256 and AES-GCM

The obvious modern choice is X25519 and ChaCha20-Poly1305, and version 1 of the
format used exactly that. But browsers cannot do either natively — WebCrypto
only gained X25519 in Chrome 133 and Firefox 132, and has never had ChaCha20.

The alternatives were to ship hand-written cryptography in JavaScript, or to
choose primitives both sides implement natively. P-256, HKDF and AES-GCM are in
every browser's WebCrypto and in Python's `cryptography` library. Same security
level, and it is what allows the terminal and the website to read the same
conversation.

Version 1 payloads still decrypt, so no existing server loses its history.

## Key pinning

`pin_peer_keys()` implements trust-on-first-use. The first time you see a
device, its public key is recorded in `~/.config/stratus/known-peers.json`. If
that key ever changes, the function raises an error rather than silently
accepting what might be an impostor.

## What the host can and cannot see

**Can:** who is in a conversation, when messages were sent, how large the
ciphertext is, which devices exist.

**Cannot:** any message text, any filename, any file content.

`tests/test_e2ee.py` and `tests/test_browser_interop.py` prove it.
