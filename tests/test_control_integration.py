import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.platform.database import Database
from core.system.deployment import DeploymentManager
from core.security.e2ee import decrypt, encrypt, generate_ecdh_keypair, generate_keypair
from core.platform.protocol import receive_message, send_message
from core.server import ControlHandler, ThreadedServer


def identity(device_id, name):
    private, public = generate_keypair()
    ecdh_private, ecdh_public = generate_ecdh_keypair()
    public_identity = {"id": device_id, "display_name": name, "hostname": name, "platform": "linux",
                       "secret": "secret-" + device_id, "encryption_public_key": public, "ecdh_public_key": ecdh_public}
    private_identity = {"id": device_id, "encryption_private_key": private, "ecdh_private_key": ecdh_private}
    return public_identity, private_identity


class ControlIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        database = Database(root / "platform" / "stratus.db")
        self.server = ThreadedServer(("127.0.0.1", 0), ControlHandler)
        self.server.token = "test"; self.server.upload_dir = root / "uploads"; self.server.upload_dir.mkdir()
        self.server.max_upload = 1024 * 1024; self.server.manager = DeploymentManager(root, database, 8200, 8299)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.alice_public, self.alice_private = identity("device-alice", "alice@linux")
        self.bob_public, self.bob_private = identity("device-bob", "bob@linux")

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.server.manager.database.close(); self.temp.cleanup()

    def call(self, device, action, **extra):
        request = {"token": "test", "device": device, "action": action}; request.update(extra)
        with socket.create_connection(self.server.server_address) as connection:
            send_message(connection, request); reply = receive_message(connection)
        self.assertTrue(reply["ok"], reply.get("error")); return reply["data"]

    def test_encrypted_chat_and_drive_round_trip_through_host(self):
        self.call(self.alice_public, "devices"); self.call(self.bob_public, "devices")
        recipients = self.call(self.alice_public, "recipients")
        message = encrypt(b"server cannot read this", recipients, b"general")
        self.call(self.alice_public, "chat_send", conversation_id="general", payload=message)
        rows = self.call(self.bob_public, "chat_messages", conversation_id="general", after=0)
        self.assertEqual(decrypt(rows[0]["payload"], self.bob_private, b"general"), b"server cannot read this")

        file_id = "a" * 32
        name = encrypt(b"proof.txt", recipients, file_id.encode())
        content = encrypt(b"encrypted file", recipients, file_id.encode())
        self.call(self.alice_public, "drive_upload", id=file_id, scope="public", encrypted_name=name, payload=content)
        stored = self.call(self.bob_public, "drive_get", id=file_id)
        self.assertEqual(decrypt(stored["payload"], self.bob_private, file_id.encode()), b"encrypted file")


    def test_one_connection_serves_many_requests(self):
        request = {"token": "test", "device": self.alice_public, "action": "ping"}
        with socket.create_connection(self.server.server_address) as connection:
            for _ in range(5):
                send_message(connection, request)
                reply = receive_message(connection)
                self.assertTrue(reply["ok"], reply.get("error"))
                self.assertTrue(reply["data"]["pong"])

    def test_bad_token_is_refused_and_closes_the_connection(self):
        with socket.create_connection(self.server.server_address) as connection:
            send_message(connection, {"token": "wrong", "device": self.alice_public, "action": "ping"})
            self.assertFalse(receive_message(connection)["ok"])
            send_message(connection, {"token": "test", "device": self.alice_public, "action": "ping"})
            with self.assertRaises((ConnectionError, OSError, ValueError)):
                receive_message(connection)

    def test_listing_the_drive_never_ships_ciphertext(self):
        self.call(self.alice_public, "devices")
        recipients = self.call(self.alice_public, "recipients")
        file_id = "c" * 32
        self.call(self.alice_public, "drive_upload", id=file_id, scope="private",
                  encrypted_name=encrypt(b"big.bin", recipients, file_id.encode()),
                  payload=encrypt(b"x" * 200000, recipients, file_id.encode()))
        listing = self.call(self.alice_public, "drive_list")
        self.assertEqual(len(listing), 1)
        self.assertNotIn("ciphertext", listing[0]["payload"])
        # Recorded size is the stored ciphertext on disk (plaintext plus the AEAD tag).
        self.assertEqual(listing[0]["ciphertext_bytes"], 200016)
        fetched = self.call(self.alice_public, "drive_get", id=file_id)
        self.assertEqual(decrypt(fetched["payload"], self.alice_private, file_id.encode()), b"x" * 200000)

    def test_chat_overview_only_reports_conversations_this_device_is_in(self):
        self.call(self.alice_public, "devices"); self.call(self.bob_public, "devices")
        recipients = self.call(self.alice_public, "recipients")
        self.call(self.alice_public, "chat_send", conversation_id="general",
                  payload=encrypt(b"hello", recipients, b"general"))
        direct = "direct:device-alice:device-charlie"
        overview = self.call(self.bob_public, "chat_overview")
        self.assertIn("general", overview)
        self.assertEqual(overview["general"]["last_id"], 1)
        self.assertNotIn(direct, overview)


if __name__ == "__main__": unittest.main()
