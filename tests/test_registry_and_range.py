"""Multiple servers, and restricting who may join.

Two features that are easy to get subtly wrong: a registry that loses a server
when a drive is unplugged, and an address restriction that either lets the
wrong people in or locks the owner out of their own host.
"""
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.platform.database import Database
from core.platform.protocol import receive_message, send_message
from core.security.e2ee import generate_ecdh_keypair, generate_keypair
from core.server import ControlHandler, ThreadedServer
from core.system import registry
from core.system.deployment import DeploymentManager
from core.system.network import address_allowed, describe_networks, parse_networks
from core.system.storage import initialize_server_root


class NetworkRangeTests(unittest.TestCase):
    def test_no_restriction_allows_anyone(self):
        self.assertTrue(address_allowed("8.8.8.8", parse_networks([])))
        self.assertTrue(address_allowed("192.168.1.5", []))

    def test_cidr_block(self):
        networks = parse_networks(["192.168.1.0/24"])
        self.assertTrue(address_allowed("192.168.1.1", networks))
        self.assertTrue(address_allowed("192.168.1.254", networks))
        self.assertFalse(address_allowed("192.168.2.1", networks))
        self.assertFalse(address_allowed("10.0.0.1", networks))

    def test_explicit_range(self):
        networks = parse_networks(["192.168.1.50-192.168.1.80"])
        self.assertTrue(address_allowed("192.168.1.50", networks))
        self.assertTrue(address_allowed("192.168.1.65", networks))
        self.assertTrue(address_allowed("192.168.1.80", networks))
        self.assertFalse(address_allowed("192.168.1.49", networks))
        self.assertFalse(address_allowed("192.168.1.81", networks))

    def test_single_address(self):
        networks = parse_networks(["192.168.1.7"])
        self.assertTrue(address_allowed("192.168.1.7", networks))
        self.assertFalse(address_allowed("192.168.1.8", networks))

    def test_several_entries_combine(self):
        networks = parse_networks(["192.168.1.0/24", "10.0.0.5"])
        self.assertTrue(address_allowed("192.168.1.99", networks))
        self.assertTrue(address_allowed("10.0.0.5", networks))
        self.assertFalse(address_allowed("10.0.0.6", networks))

    def test_the_host_can_never_lock_itself_out(self):
        # Administration connects over loopback. A restriction that excluded it
        # would leave the owner unable to reach their own dashboard.
        networks = parse_networks(["192.168.99.0/24"])
        self.assertTrue(address_allowed("127.0.0.1", networks))
        self.assertTrue(address_allowed("::1", networks))

    def test_malformed_entries_are_refused(self):
        for bad in ["not-an-ip", "192.168.1.0/99", "1.2.3.4-", "300.1.1.1"]:
            with self.subTest(entry=bad), self.assertRaises(ValueError):
                parse_networks([bad])

    def test_unparseable_client_address_is_refused(self):
        self.assertFalse(address_allowed("garbage", parse_networks(["192.168.1.0/24"])))

    def test_description_is_human_readable(self):
        self.assertEqual(describe_networks([]), "any device on this network")
        self.assertIn("192.168.1.0/24", describe_networks(parse_networks(["192.168.1.0/24"])))


class PolicyStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Database(Path(self.temp.name) / "platform.db")

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def test_default_is_unrestricted(self):
        self.assertEqual(self.database.get_policy()["allowed_networks"], [])

    def test_round_trip(self):
        policy = self.database.update_policy({"allowed_networks": ["192.168.1.0/24"]})
        self.assertEqual(policy["allowed_networks"], ["192.168.1.0/24"])
        self.assertEqual(self.database.get_policy()["allowed_networks"], ["192.168.1.0/24"])

    def test_invalid_range_is_rejected_when_it_is_set(self):
        # Refused at the moment of setting, not silently stored to lock everyone
        # out the next time a client connects.
        with self.assertRaises(ValueError):
            self.database.update_policy({"allowed_networks": ["192.168.1.0/24", "nonsense"]})
        self.assertEqual(self.database.get_policy()["allowed_networks"], [])

    def test_clearing_restores_open_access(self):
        self.database.update_policy({"allowed_networks": ["192.168.1.0/24"]})
        self.database.update_policy({"allowed_networks": []})
        self.assertEqual(self.database.get_policy()["allowed_networks"], [])


class RangeEnforcementTests(unittest.TestCase):
    """The restriction must actually stop a connection at the socket."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.database = Database(root / "platform" / "stratus.db")
        self.server = ThreadedServer(("127.0.0.1", 0), ControlHandler)
        self.server.token = "test"
        self.server.upload_dir = root / "uploads"; self.server.upload_dir.mkdir(parents=True)
        self.server.max_upload = 1 << 20
        self.server.manager = DeploymentManager(root, self.database, 8700, 8799)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        private, public = generate_keypair()
        _, ecdh_public = generate_ecdh_keypair()
        self.device = {"id": "device-a", "display_name": "a@linux", "hostname": "a", "platform": "linux",
                       "secret": "s", "encryption_public_key": public, "ecdh_public_key": ecdh_public}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.database.close(); self.temp.cleanup()

    def ping(self):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            send_message(connection, {"action": "ping", "token": "test", "device": self.device})
            return receive_message(connection)

    def test_loopback_still_works_under_a_restriction_that_excludes_it(self):
        # The tests connect from 127.0.0.1. Even with a restriction naming only
        # a foreign subnet, the host must still be able to reach itself.
        self.database.update_policy({"allowed_networks": ["10.99.0.0/24"]})
        self.assertTrue(self.ping()["ok"])

    def test_a_disallowed_address_is_refused_before_authentication(self):
        # The address check runs before any frame is read, so patch it to say
        # this connection is not permitted and confirm the refusal arrives
        # without the token ever being examined.
        self.database.update_policy({"allowed_networks": ["10.99.0.0/24"]})
        with patch.object(ControlHandler, "address_permitted", return_value=False):
            reply = self.ping()
        self.assertFalse(reply["ok"])
        self.assertIn("not permitted", reply["error"])

    def test_server_info_reports_the_restriction(self):
        self.database.update_policy({"allowed_networks": ["192.168.4.0/24"]})
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            send_message(connection, {"action": "server_info", "token": "test", "device": self.device})
            reply = receive_message(connection)
        self.assertEqual(reply["data"]["allowed_networks"], ["192.168.4.0/24"])

    def test_a_broken_restriction_fails_open_rather_than_bricking_the_server(self):
        # Corrupt the stored value behind the validation, as a hand-edited
        # database or a future migration could. The server must keep working.
        with self.database.lock:
            self.database.conn.execute("UPDATE hosting_policy SET allowed_networks='[\"###\"]' WHERE id=1")
            self.database.conn.commit()
        self.assertTrue(self.ping()["ok"])


class ServerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name) / "config"
        self.patches = [
            patch.object(registry, "CONFIG_DIR", self.home),
            patch.object(registry, "REGISTRY_PATH", self.home / "servers.json"),
            patch.object(registry, "ACTIVE_PATH", self.home / "active-server.json"),
        ]
        for item in self.patches: item.start()

    def tearDown(self):
        for item in self.patches: item.stop()
        self.temp.cleanup()

    def make_server(self, name: str) -> str:
        """A server directory shaped exactly as Stratus creates them."""
        slug = name.lower().replace(" ", "-")
        storage = Path(self.temp.name) / "drive" / registry.DATA_ROOT / registry.SERVERS_DIR / slug
        (storage / "config").mkdir(parents=True)
        (storage / "config" / "storage.json").write_text(
            '{"server_name": "' + name + '", "server_slug": "' + slug + '"}', encoding="utf-8")
        registry.remember({"name": name, "storage": str(storage), "port": 5050, "token": "t"})
        return str(storage)

    def test_empty_to_begin_with(self):
        self.assertEqual(registry.known_servers(), [])
        self.assertIsNone(registry.active_server())
        self.assertIsNone(registry.running_server())

    def test_servers_are_remembered_and_listed_newest_first(self):
        registry.remember({"name": "Class A", "storage": "/mnt/a", "port": 5050, "token": "x"})
        registry.remember({"name": "Class B", "storage": "/mnt/b", "port": 5050, "token": "y"})
        names = [item["name"] for item in registry.known_servers()]
        self.assertEqual(names, ["Class B", "Class A"])

    def test_remembering_the_same_storage_updates_rather_than_duplicates(self):
        registry.remember({"name": "Old name", "storage": "/mnt/a", "port": 5050})
        registry.remember({"name": "New name", "storage": "/mnt/a", "port": 5050})
        servers = registry.known_servers()
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0]["name"], "New name")

    def test_deleting_erases_the_data_and_the_entry(self):
        storage = self.make_server("Class Server")
        (Path(storage) / "platform").mkdir(parents=True, exist_ok=True)
        (Path(storage) / "platform" / "stratus.db").write_text("data", encoding="utf-8")

        outcome = registry.delete_server(storage)

        self.assertTrue(outcome["erased"])
        self.assertFalse(Path(storage).exists())
        self.assertEqual(registry.known_servers(), [])

    def test_deleting_refuses_a_directory_that_is_not_a_server(self):
        stray = Path(self.home) / "holiday-photos"
        (stray / "config").mkdir(parents=True)
        (stray / "config" / "storage.json").write_text("{}", encoding="utf-8")
        (stray / "keep-me.jpg").write_text("photo", encoding="utf-8")
        registry.remember({"name": "Bad Entry", "storage": str(stray)})

        # It has the config file, but it is not where Stratus puts servers.
        with self.assertRaises(ValueError):
            registry.delete_server(str(stray))
        self.assertTrue((stray / "keep-me.jpg").exists())

    def test_deleting_refuses_while_the_server_is_running(self):
        storage = self.make_server("Busy Server")
        registry.set_active({"name": "Busy Server", "storage": storage, "pid": 1})
        with patch.object(registry, "running_server",
                          return_value={"name": "Busy Server", "storage": storage, "pid": 1}):
            with self.assertRaises(RuntimeError):
                registry.delete_server(storage)
        self.assertTrue(Path(storage).is_dir())

    def test_deleting_an_unplugged_drive_still_clears_the_entry(self):
        registry.remember({"name": "Gone", "storage": str(Path(self.home) / "not-mounted" / "StratusData" / "servers" / "gone")})
        outcome = registry.delete_server(
            str(Path(self.home) / "not-mounted" / "StratusData" / "servers" / "gone"))
        self.assertFalse(outcome["erased"])
        self.assertTrue(outcome["forgotten"])
        self.assertEqual(registry.known_servers(), [])

    def test_deleting_clears_the_active_record_pointing_at_it(self):
        storage = self.make_server("Retired")
        registry.set_active({"name": "Retired", "storage": storage, "pid": 999999})
        with patch.object(registry, "running_server", return_value=None):
            registry.delete_server(storage)
        self.assertIsNone(registry.active_server())

    def test_forgetting_removes_only_that_entry(self):
        registry.remember({"name": "A", "storage": "/mnt/a"})
        registry.remember({"name": "B", "storage": "/mnt/b"})
        registry.forget("/mnt/a")
        self.assertEqual([item["name"] for item in registry.known_servers()], ["B"])

    def test_starting_a_server_records_it_in_both_places(self):
        registry.set_active({"name": "Live", "storage": "/mnt/live", "port": 5050, "token": "t", "pid": 1})
        self.assertEqual(registry.active_server()["name"], "Live")
        self.assertIn("/mnt/live", [item["storage"] for item in registry.known_servers()])

    def test_a_stale_pid_does_not_count_as_running(self):
        # A crash or reboot leaves active-server.json behind. The registry must
        # check the process rather than trust the file.
        registry.set_active({"name": "Ghost", "storage": "/mnt/g", "port": 5050, "pid": 999999})
        self.assertIsNone(registry.running_server())

    def test_clearing_active_keeps_the_server_in_the_list(self):
        registry.set_active({"name": "Live", "storage": "/mnt/live", "port": 5050, "pid": 1})
        registry.clear_active()
        self.assertIsNone(registry.active_server())
        self.assertEqual(len(registry.known_servers()), 1)

    def test_a_corrupt_registry_does_not_crash_the_start_screen(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "servers.json").write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(registry.known_servers(), [])

    def test_a_disconnected_drive_is_reported_not_hidden(self):
        registry.remember({"name": "On USB", "storage": "/mnt/definitely-not-here"})
        detail = registry.describe(registry.known_servers()[0])
        self.assertFalse(detail["available"])
        self.assertEqual(detail["size_bytes"], 0)

    def test_describe_reads_real_counts_from_a_real_server(self):
        drive = Path(self.temp.name) / "drive"
        drive.mkdir()
        root = initialize_server_root({"mount": str(drive), "uuid": "custom", "filesystem": "ext4"},
                                      "test-server", name="Test Server", port=5050)
        database = Database(root / "platform" / "stratus.db")
        _, public = generate_ecdh_keypair()
        database.register_device({"id": "d1", "display_name": "d1", "hostname": "h", "platform": "linux",
                                  "secret": "s", "ecdh_public_key": public})
        database.close()
        detail = registry.describe({"name": "Test Server", "storage": str(root)})
        self.assertTrue(detail["available"])
        self.assertEqual(detail["devices"], 1)
        self.assertGreater(detail["size_bytes"], 0)


class DriveDiscoveryTests(unittest.TestCase):
    """Servers must be findable on a drive even with no registry entry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.drive = Path(self.temp.name) / "usb"
        self.drive.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_nothing_on_a_blank_drive(self):
        self.assertEqual(registry.servers_on_drive(self.drive), [])

    def test_servers_are_recovered_from_their_own_config(self):
        selection = {"mount": str(self.drive), "uuid": "custom", "filesystem": "ext4"}
        initialize_server_root(selection, "class-one", name="Class One", port=5050)
        initialize_server_root(selection, "class-two", name="Class Two", port=5051)
        found = registry.servers_on_drive(self.drive)
        self.assertEqual({item["name"] for item in found}, {"Class One", "Class Two"})

    def test_the_join_token_is_never_written_to_the_drive(self):
        # Anyone holding the drive would otherwise be able to join the server.
        initialize_server_root({"mount": str(self.drive), "uuid": "custom", "filesystem": "ext4"},
                              "secure", name="Secure", port=5050)
        config = self.drive / "StratusData" / "servers" / "secure" / "config" / "storage.json"
        stored = json.loads(config.read_text(encoding="utf-8"))
        self.assertNotIn("token", stored)
        self.assertEqual(registry.servers_on_drive(self.drive)[0]["token"], "")


if __name__ == "__main__": unittest.main()
