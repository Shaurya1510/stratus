import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from core.platform.database import Database
from core.security.e2ee import decrypt, encrypt, generate_ecdh_keypair, generate_keypair
from core.security.identity import key_fingerprint, public_identity


def device(device_id: str = "device-a") -> tuple[dict, dict]:
    legacy_private, legacy_public = generate_keypair()
    private, public = generate_ecdh_keypair()
    return ({"id": device_id, "display_name": device_id, "encryption_public_key": legacy_public, "ecdh_public_key": public},
            {"id": device_id, "encryption_private_key": legacy_private, "ecdh_private_key": private})


class EncryptionTests(unittest.TestCase):
    def test_message_round_trip_and_no_plaintext(self):
        public, private = device()
        payload = encrypt(b"very private message", [public], b"general")
        self.assertEqual(payload["version"], 2)
        self.assertNotIn("very private message", json.dumps(payload))
        self.assertEqual(decrypt(payload, private, b"general"), b"very private message")

    def test_only_named_recipients_can_decrypt(self):
        alice_public, alice_private = device("device-alice")
        bob_public, bob_private = device("device-bob")
        carol_public, carol_private = device("device-carol")
        payload = encrypt(b"for alice and bob", [alice_public, bob_public], b"direct")
        self.assertEqual(decrypt(payload, bob_private, b"direct"), b"for alice and bob")
        self.assertNotIn("device-carol", payload["envelopes"])
        with self.assertRaises(KeyError):
            decrypt(payload, carol_private, b"direct")

    def test_wrong_conversation_cannot_open_the_message(self):
        public, private = device()
        payload = encrypt(b"scoped", [public], b"general")
        with self.assertRaises(Exception):
            decrypt(payload, private, b"group-other")

    def test_recipient_without_a_current_key_is_refused(self):
        public, _ = device()
        stale = {"id": "device-old", "display_name": "old@linux", "encryption_public_key": "x"}
        with self.assertRaises(ValueError):
            encrypt(b"hello", [public, stale], b"general")

    def test_version_one_payloads_are_still_readable(self):
        # Servers created before the browser clients hold X25519/ChaCha20 data.
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
        public, private = device()
        recipient_private = X25519PrivateKey.from_private_bytes(base64.b64decode(private["encryption_private_key"]))
        content_key, nonce = os.urandom(32), os.urandom(12)
        ciphertext = ChaCha20Poly1305(content_key).encrypt(nonce, b"legacy message", b"general")
        ephemeral = X25519PrivateKey.generate()
        shared = ephemeral.exchange(recipient_private.public_key())
        wrapping = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"stratus-envelope-v1").derive(shared)
        wrap_nonce = os.urandom(12)
        wrapped = ChaCha20Poly1305(wrapping).encrypt(wrap_nonce, content_key, public["id"].encode())
        payload = {"version": 1, "nonce": base64.b64encode(nonce).decode(), "ciphertext": base64.b64encode(ciphertext).decode(),
                   "envelopes": {public["id"]: {
                       "ephemeral": base64.b64encode(ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode(),
                       "nonce": base64.b64encode(wrap_nonce).decode(), "key": base64.b64encode(wrapped).decode()}}}
        self.assertEqual(decrypt(payload, private, b"general"), b"legacy message")

    def test_keys_are_in_the_formats_webcrypto_imports(self):
        private_b64, public_b64 = generate_ecdh_keypair()
        point = base64.b64decode(public_b64)
        self.assertEqual(len(point), 65)          # uncompressed P-256 point, importKey("raw")
        self.assertEqual(point[0], 0x04)
        loaded = serialization.load_der_private_key(base64.b64decode(private_b64), password=None)
        self.assertIsInstance(loaded.curve, ec.SECP256R1)   # PKCS#8 DER, importKey("pkcs8")

    def test_private_keys_never_enter_public_identity(self):
        identity = {"id": "d", "display_name": "d", "hostname": "h", "platform": "linux", "secret": "s",
                    "encryption_public_key": "pub", "encryption_private_key": "PRIVATE",
                    "ecdh_public_key": "pub2", "ecdh_private_key": "ALSOPRIVATE"}
        outbound = public_identity(identity)
        self.assertNotIn("encryption_private_key", outbound)
        self.assertNotIn("ecdh_private_key", outbound)
        self.assertNotIn("PRIVATE", json.dumps(outbound))
        self.assertNotIn("ALSOPRIVATE", json.dumps(outbound))

    def test_fingerprint_is_stable(self):
        self.assertEqual(key_fingerprint("public-key"), key_fingerprint("public-key"))
        self.assertNotEqual(key_fingerprint("public-key"), key_fingerprint("other-key"))


class PlatformTests(unittest.TestCase):
    def test_policy_defaults_to_guest_hosting_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "platform.db")
            self.assertEqual(database.get_policy()["guest_deploy_enabled"], 0)
            database.close()

    def test_app_ticket_is_single_use_and_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(Path(folder) / "platform.db")
            token = database.issue_app_ticket("device-a", "app1")
            self.assertEqual(database.redeem_app_ticket(token), "device-a")
            self.assertIsNone(database.redeem_app_ticket(token))
            expired = database.issue_app_ticket("device-a", "app1")
            self.assertIsNone(database.redeem_app_ticket(expired, lifetime=-1))
            database.close()


if __name__ == "__main__": unittest.main()
