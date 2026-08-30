"""Passphrase-wrapped device keys, so a browser can decrypt without the host.

The website has to hold this device's private key to read the same encrypted
conversations the terminal reads. Sending that key through the host would make
the whole design pointless, and putting it in the link would leave it in browser
history and in the arguments of whatever command opened the browser.

Instead the key is sealed with a passphrase only the person knows. The host
stores the sealed blob and hands it to the browser on request; without the
passphrase it is meaningless to the host, to other members, and to anyone who
copies the database off the drive.

PBKDF2-SHA256 and AES-256-GCM are used because WebCrypto implements both
natively, so the browser unseals with no hand-written cryptography.
"""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000  # OWASP guidance for PBKDF2-SHA256
MINIMUM_PASSPHRASE = 8


def _derive(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations).derive(passphrase.encode("utf-8"))


def seal(private_key_b64: str, passphrase: str, device_id: str) -> dict:
    if len(passphrase) < MINIMUM_PASSPHRASE:
        raise ValueError(f"use at least {MINIMUM_PASSPHRASE} characters so the sealed key cannot be guessed")
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive(passphrase, salt, ITERATIONS)
    sealed = AESGCM(key).encrypt(nonce, private_key_b64.encode("utf-8"), device_id.encode())
    return {
        "version": 1, "kdf": "PBKDF2-SHA256", "iterations": ITERATIONS,
        "salt": base64.b64encode(salt).decode(), "nonce": base64.b64encode(nonce).decode(),
        "sealed": base64.b64encode(sealed).decode(),
    }


def unseal(blob: dict, passphrase: str, device_id: str) -> str:
    key = _derive(passphrase, base64.b64decode(blob["salt"]), int(blob.get("iterations", ITERATIONS)))
    opened = AESGCM(key).decrypt(base64.b64decode(blob["nonce"]), base64.b64decode(blob["sealed"]), device_id.encode())
    return opened.decode("utf-8")


def blob_is_wellformed(blob: object) -> bool:
    """The host stores this without being able to read it, so check the shape."""
    if not isinstance(blob, dict): return False
    if blob.get("kdf") != "PBKDF2-SHA256": return False
    try:
        iterations = int(blob.get("iterations", 0))
        if iterations < 100_000: return False
        for field in ("salt", "nonce", "sealed"):
            if len(base64.b64decode(str(blob[field]), validate=True)) < 12: return False
        return len(json.dumps(blob)) < 8192
    except (KeyError, ValueError, TypeError): return False
