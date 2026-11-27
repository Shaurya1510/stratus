"""Choosing a network, public and private servers, join codes and join approval."""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.platform.database import Database
from core.platform.protocol import receive_message, send_message
from core.server import WAITING_FOR_HOST, ControlHandler, ThreadedServer
from core.system import discovery, network
from core.system.deployment import DeploymentManager
from tests.test_control_integration import identity


class JoinCodeTests(unittest.TestCase):
    def test_a_code_round_trips_to_the_same_address_and_port(self):
        for address, port in (("192.168.43.117", 5050), ("10.0.0.1", 1), ("172.20.10.2", 65535)):
            code = network.join_code(address, port)
            self.assertRegex(code, r"^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{2}$")
            self.assertEqual(network.parse_join_code(code), (address, port))

    def test_codes_forgive_case_spaces_and_lookalike_letters(self):
        code = network.join_code("192.168.1.20", 5050)
        self.assertEqual(network.parse_join_code(code.lower().replace("-", " ")), ("192.168.1.20", 5050))
        self.assertEqual(network.parse_join_code(code.replace("0", "O")), ("192.168.1.20", 5050))

    def test_anything_else_is_not_a_code(self):
        for text in ("", "hello", "192.168.1.1", "UUUU-UUUU-UU", "ZZZZ-ZZZZ-ZZZ"):
            self.assertIsNone(network.parse_join_code(text))

    def test_the_join_box_accepts_codes_addresses_and_ports(self):
        code = network.join_code("10.1.2.3", 6000)
        self.assertEqual(network.parse_join_target(code), ("10.1.2.3", 6000))
        self.assertEqual(network.parse_join_target("10.1.2.3"), ("10.1.2.3", 5050))
        self.assertEqual(network.parse_join_target("10.1.2.3:7000"), ("10.1.2.3", 7000))
        self.assertIsNone(network.parse_join_target("10.1.2.3:99999"))
        self.assertIsNone(network.parse_join_target(""))


class NetworkChoiceTests(unittest.TestCase):
    INTERFACES = [
        {"interface": "wlp2s0", "address": "192.168.1.20", "prefix": 24, "broadcast": "192.168.1.255", "network": "192.168.1.0/24"},
        {"interface": "enp3s0", "address": "10.0.0.5", "prefix": 24, "broadcast": "10.0.0.255", "network": "10.0.0.0/24"},
        {"interface": "docker0", "address": "172.17.0.1", "prefix": 16, "broadcast": "172.17.255.255", "network": "172.17.0.0/16"},
    ]

    def test_networks_are_named_by_what_they_are_and_docker_is_hidden(self):
        with patch("core.system.network.local_interfaces", return_value=self.INTERFACES), \
             patch("core.system.network._wifi_name", return_value="Bennett-Campus"):
            choices = network.network_choices()
        self.assertEqual([item["title"] for item in choices], ["Wi-Fi · Bennett-Campus", "LAN cable"])

    def test_visibility_is_always_complete_and_falls_back_safely(self):
        self.assertEqual(network.normalise_visibility(None)["mode"], "all")
        self.assertTrue(network.normalise_visibility({})["listed"])
        broken = network.normalise_visibility({"mode": "network", "network": "nonsense"})
        self.assertEqual(broken["mode"], "all")
        chosen = network.normalise_visibility({"mode": "network", "network": "192.168.1.0/24",
                                               "address": "192.168.1.20", "listed": False})
        self.assertEqual(chosen["network"], "192.168.1.0/24")
        self.assertFalse(chosen["listed"])
        self.assertIn("private", network.describe_visibility(chosen))

    def test_server_flags_follow_the_choice(self):
        from core.interface.setup import visibility_arguments
        with patch("core.system.network.local_interfaces", return_value=self.INTERFACES):
            flags = visibility_arguments({"mode": "network", "network": "192.168.1.0/24", "address": "192.168.1.20",
                                          "title": "Wi-Fi · Home", "listed": False, "approve_joins": True})
            self.assertEqual(flags[:2], ["--visibility", "network"])
            self.assertIn("--private", flags)
            self.assertIn("--approve-joins", flags)
            # A network that is not connected today widens instead of hiding the server.
            gone = visibility_arguments({"mode": "network", "network": "172.31.0.0/24", "address": "172.31.0.9"})
            self.assertEqual(gone, ["--visibility", "all"])


class DiscoveryVisibilityTests(unittest.TestCase):
    def test_probes_from_outside_the_chosen_network_are_ignored(self):
        networks = discovery._parse_visible(["192.168.1.0/24"])
        self.assertTrue(discovery._visible_to("192.168.1.77", networks))
        self.assertFalse(discovery._visible_to("10.0.0.9", networks))
        self.assertTrue(discovery._visible_to("127.0.0.1", networks))
        self.assertTrue(discovery._visible_to("10.0.0.9", None))

    def test_announcements_are_heard_and_parsed(self):
        port = free_udp_port()
        stop = threading.Event()
        heard = []
        listener = threading.Thread(target=discovery.listen_for_announcements,
                                    args=(heard.append, stop), kwargs={"port": port}, daemon=True)
        listener.start()
        time.sleep(0.3)
        payload = discovery.announcement("Lecture Hall", 5050, {"network_title": "Wi-Fi · Campus", "locked": "yes"})
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for _ in range(5):
                sender.sendto(payload, ("127.0.0.1", port)); time.sleep(0.05)
        deadline = time.monotonic() + 3
        while not heard and time.monotonic() < deadline: time.sleep(0.05)
        stop.set(); listener.join(2)
        self.assertTrue(heard, "no announcement was heard")
        self.assertEqual(heard[0]["name"], "Lecture Hall")
        self.assertEqual(heard[0]["network_title"], "Wi-Fi · Campus")
        self.assertEqual(heard[0]["host"], "127.0.0.1")

    def test_junk_is_not_an_announcement(self):
        for junk in (b"", b"hello", json.dumps({"kind": "other"}).encode(),
                     json.dumps({"kind": discovery.ANNOUNCE_MESSAGE, "control_port": 0}).encode()):
            self.assertIsNone(discovery.parse_announcement(junk, "1.2.3.4"))


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0)); return probe.getsockname()[1]


class JoinApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        database = Database(root / "platform" / "stratus.db")
        self.server = ThreadedServer(("127.0.0.1", 0), ControlHandler)
        self.server.token = "test"; self.server.upload_dir = root / "uploads"; self.server.upload_dir.mkdir()
        self.server.max_upload = 1024 * 1024; self.server.manager = DeploymentManager(root, database, 8300, 8399)
        self.server.admin_device = "device-host"
        self.server.approve_joins = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.host, _ = identity("device-host", "host@linux")
        self.guest, _ = identity("device-guest", "guest@linux")

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.server.manager.database.close(); self.temp.cleanup()

    def request(self, device, action, **extra):
        payload = {"token": "test", "device": device, "action": action, **extra}
        with socket.create_connection(self.server.server_address) as connection:
            send_message(connection, payload); return receive_message(connection)

    def test_the_host_is_never_kept_waiting(self):
        self.assertTrue(self.request(self.host, "ping")["ok"])

    def test_a_new_device_waits_until_the_host_lets_it_in(self):
        reply = self.request(self.guest, "ping")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], WAITING_FOR_HOST)

        waiting = self.request(self.host, "join_requests")["data"]
        self.assertEqual([item["id"] for item in waiting], ["device-guest"])
        # A guest cannot let itself in.
        self.assertFalse(self.request(self.guest, "decide_join", device_id="device-guest", accept=True)["ok"])

        self.assertTrue(self.request(self.host, "decide_join", device_id="device-guest", accept=True)["ok"])
        self.assertTrue(self.request(self.guest, "ping")["ok"])
        self.assertEqual(self.request(self.host, "join_requests")["data"], [])

    def test_a_turned_away_device_stays_out(self):
        self.request(self.guest, "ping")
        self.request(self.host, "decide_join", device_id="device-guest", accept=False)
        reply = self.request(self.guest, "ping")
        self.assertFalse(reply["ok"])
        self.assertIn("blocked", reply["error"])

    def test_waiting_devices_receive_nothing_encrypted_for_them(self):
        self.request(self.guest, "ping")
        recipients = self.request(self.host, "recipients")["data"]
        self.assertNotIn("device-guest", [item["id"] for item in recipients])

    def test_server_info_hands_out_a_join_code(self):
        info = self.request(self.host, "server_info")["data"]
        self.assertEqual(network.parse_join_code(info["join_code"]), ("127.0.0.1", info["port"]))


if __name__ == "__main__":
    unittest.main()
