"""Envelope encryption shared by the terminal and the browser clients.

Every message and file is encrypted once with a random content key, and that
content key is wrapped separately for each authorised recipient. The host only
ever stores ciphertext plus the list of recipient device ids.

Two envelope versions exist:

* Version 1 — X25519 + HKDF-SHA256 + ChaCha20-Poly1305. Written by earlier
  builds; still decrypted so existing servers keep working.
* Version 2 — P-256 ECDH + HKDF-SHA256 + AES-256-GCM, and the format written
  today. Browsers cannot do X25519 or ChaCha20-Poly1305 in WebCrypto, and
  hand-written JavaScript crypto is not an acceptable substitute, so version 2
  uses primitives that Python and every browser implement natively. This is
  what lets the terminal and the website read the same conversation.
"""
from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM, ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CURVE = ec.SECP256R1()
ENVELOPE_VERSION = 2


def _b64(raw: bytes) -> str: return base64.b64encode(raw).decode()


def generate_keypair() -> tuple[str, str]:
    """Legacy X25519 pair, kept so version 1 data stays readable."""
    private = X25519PrivateKey.generate()
    private_raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(private_raw), _b64(public_raw)


def generate_ecdh_keypair() -> tuple[str, str]:
    """P-256 pair in the formats WebCrypto imports directly.

    The private key is PKCS#8 (importKey "pkcs8") and the public key is an
    uncompressed point (importKey "raw"), so the browser needs no conversion
    code of its own.
    """
    private = ec.generate_private_key(CURVE)
    private_der = private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public_point = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(private_der), _b64(public_point)


def _derive_v1(private: X25519PrivateKey, public: X25519PublicKey) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"stratus-envelope-v1").derive(private.exchange(public))


def _derive_v2(private, public) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"stratus-envelope-v2").derive(private.exchange(ec.ECDH(), public))


def recipient_key(recipient: dict) -> str | None:
    return recipient.get("ecdh_public_key") or None


def encrypt(plaintext: bytes, recipients: list[dict], aad: bytes = b"") -> dict:
    """Encrypt for every recipient that has published a P-256 key."""
    usable = [item for item in recipients if recipient_key(item)]
    missing = [item for item in recipients if not recipient_key(item)]
    if missing:
        names = ", ".join(item.get("display_name", item.get("id", "?")) for item in missing)
        raise ValueError(f"these devices have not published a current encryption key yet: {names}")
    if not usable: raise ValueError("no recipients to encrypt for")
    content_key, nonce = os.urandom(32), os.urandom(12)
    ciphertext = AESGCM(content_key).encrypt(nonce, plaintext, aad)
    envelopes = {}
    for recipient in usable:
        ephemeral = ec.generate_private_key(CURVE)
        public = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, base64.b64decode(recipient["ecdh_public_key"]))
        wrapping_key, wrapping_nonce = _derive_v2(ephemeral, public), os.urandom(12)
        wrapped = AESGCM(wrapping_key).encrypt(wrapping_nonce, content_key, recipient["id"].encode())
        envelopes[recipient["id"]] = {
            "ephemeral": _b64(ephemeral.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
            "nonce": _b64(wrapping_nonce), "key": _b64(wrapped),
        }
    return {"version": ENVELOPE_VERSION, "nonce": _b64(nonce), "ciphertext": _b64(ciphertext), "envelopes": envelopes}


def decrypt(payload: dict, identity: dict, aad: bytes = b"") -> bytes:
    envelope = payload["envelopes"][identity["id"]]
    if int(payload.get("version", 1)) == 1: return _decrypt_v1(payload, envelope, identity, aad)
    private = serialization.load_der_private_key(base64.b64decode(identity["ecdh_private_key"]), password=None)
    ephemeral = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, base64.b64decode(envelope["ephemeral"]))
    wrapping_key = _derive_v2(private, ephemeral)
    content_key = AESGCM(wrapping_key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["key"]), identity["id"].encode())
    return AESGCM(content_key).decrypt(base64.b64decode(payload["nonce"]), base64.b64decode(payload["ciphertext"]), aad)


def _decrypt_v1(payload: dict, envelope: dict, identity: dict, aad: bytes) -> bytes:
    private = X25519PrivateKey.from_private_bytes(base64.b64decode(identity["encryption_private_key"]))
    ephemeral = X25519PublicKey.from_public_bytes(base64.b64decode(envelope["ephemeral"]))
    wrapping_key = _derive_v1(private, ephemeral)
    content_key = ChaCha20Poly1305(wrapping_key).decrypt(base64.b64decode(envelope["nonce"]), base64.b64decode(envelope["key"]), identity["id"].encode())
    return ChaCha20Poly1305(content_key).decrypt(base64.b64decode(payload["nonce"]), base64.b64decode(payload["ciphertext"]), aad)
