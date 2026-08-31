"""Finding a server on the local network.

Written after two people on the same phone hotspot could not see each other's
servers. The probe was sent once, to 255.255.255.255 only, from whichever
interface the default route happened to name — three separate ways to miss a
server that was running and reachable.
"""
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.system import discovery
from core.system.network import same_network


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DiscoveryRoundTripTests(unittest.TestCase):
    """A real server answering a real probe over the loopback interface."""

    def setUp(self):
        self.port = free_port()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=discovery.serve_discovery,
            args=("Test Server", 5050, self.port, self.stop), daemon=True)
        self.thread.start()
        time.sleep(0.2)          # let the socket bind before probing

    def tearDown(self):
        self.stop.set()
        self.thread.join(timeout=3)

    def probe_directly(self) -> list[dict]:
        """Send the probe straight to loopback, bypassing broadcast.

        Whether a broadcast reaches a loopback-bound listener differs between
        operating systems, so the round trip itself is tested this way and the
        broadcast planning is tested separately below.
        """
        with patch.object(discovery, "_probe_targets",
                          return_value=[("127.0.0.1", ["127.0.0.1"])]):
            return discovery.discover(self.port, timeout=1.5)

    def test_a_running_server_is_found(self):
        found = self.probe_directly()
        self.assertTrue(found, "the server did not answer its own probe")
        self.assertEqual(found[0]["name"], "Test Server")
        self.assertEqual(found[0]["control_port"], 5050)
        self.assertEqual(found[0]["host"], "127.0.0.1")

    def test_a_server_is_reported_once_however_many_probes_are_sent(self):
        """The probe repeats until the deadline, so replies arrive repeatedly."""
        found = self.probe_directly()
        self.assertEqual(len(found), 1, f"the same server was listed twice: {found}")

    def test_nothing_is_found_when_no_server_is_listening(self):
        self.stop.set()
        self.thread.join(timeout=3)
        with patch.object(discovery, "_probe_targets",
                          return_value=[("127.0.0.1", ["127.0.0.1"])]):
            self.assertEqual(discovery.discover(self.port, timeout=0.6), [])

    def test_a_stray_packet_does_not_become_a_server(self):
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"not a stratus probe", ("127.0.0.1", self.port))
            sender.settimeout(0.5)
            with self.assertRaises(socket.timeout):
                sender.recvfrom(1024)
        finally:
            sender.close()


class ProbePlanningTests(unittest.TestCase):
    """Where the probe is sent — the part that was actually broken."""

    HOTSPOT = [
        {"interface": "wlan0", "address": "192.168.43.17", "prefix": 24,
         "broadcast": "192.168.43.255", "network": "192.168.43.0/24"},
        {"interface": "eth0", "address": "192.168.226.130", "prefix": 24,
         "broadcast": "192.168.226.255", "network": "192.168.226.0/24"},
    ]

    def plan(self, interfaces):
        with patch("core.system.network.local_interfaces", return_value=interfaces):
            return discovery._probe_targets(5051)

    def test_every_interface_is_probed_not_just_the_default_route(self):
        """A virtual machine has several. The default one is often the wrong one."""
        sources = [source for source, _ in self.plan(self.HOTSPOT)]
        self.assertIn("192.168.43.17", sources)
        self.assertIn("192.168.226.130", sources)

    def test_the_subnet_broadcast_is_tried_as_well_as_the_limited_one(self):
        """255.255.255.255 is dropped by many hotspots; 192.168.43.255 is not."""
        plan = dict(self.plan(self.HOTSPOT))
        self.assertIn("192.168.43.255", plan["192.168.43.17"])
        self.assertIn("255.255.255.255", plan["192.168.43.17"])

    def test_a_machine_with_no_readable_interfaces_still_probes(self):
        plan = self.plan([])
        self.assertEqual(len(plan), 1)
        self.assertIsNone(plan[0][0])
        self.assertEqual(plan[0][1], ["255.255.255.255"])

    def test_an_interface_without_a_broadcast_address_is_still_usable(self):
        plan = dict(self.plan([
            {"interface": "tun0", "address": "10.8.0.2", "prefix": 24,
             "broadcast": "", "network": "10.8.0.0/24"},
        ]))
        self.assertEqual(plan["10.8.0.2"], ["255.255.255.255"])


class SameNetworkTests(unittest.TestCase):
    """The commonest real cause: the two machines are not on one network.

    A virtual machine on NAT sits on its own private subnet. No amount of
    broadcasting reaches the Wi-Fi the other person is using, and saying so is
    far more useful than reporting that nothing was found.
    """

    def test_two_addresses_on_one_subnet(self):
        self.assertTrue(same_network("192.168.43.17", "192.168.43.99"))

    def test_a_nat_virtual_machine_is_not_on_the_hotspot(self):
        self.assertFalse(same_network("192.168.226.130", "192.168.43.99"))

    def test_nonsense_is_not_a_match(self):
        self.assertFalse(same_network("", "192.168.1.1"))
        self.assertFalse(same_network("192.168.1.1", "not-an-address"))


class InterfaceReadingTests(unittest.TestCase):
    def test_interfaces_are_read_from_ip_json(self):
        import subprocess
        from core.system import network

        listing = json.dumps([
            {"ifname": "lo", "operstate": "UNKNOWN", "addr_info": [
                {"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
            {"ifname": "wlan0", "operstate": "UP", "addr_info": [
                {"family": "inet", "local": "192.168.43.17", "prefixlen": 24,
                 "broadcast": "192.168.43.255"}]},
            {"ifname": "eth1", "operstate": "DOWN", "addr_info": [
                {"family": "inet", "local": "10.0.0.5", "prefixlen": 24}]},
        ])
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=listing, stderr="")
        with patch.object(subprocess, "run", return_value=completed):
            found = network.local_interfaces()

        addresses = [item["address"] for item in found]
        self.assertIn("192.168.43.17", addresses)
        self.assertNotIn("127.0.0.1", addresses, "loopback is not a network to search")
        self.assertNotIn("10.0.0.5", addresses, "an interface that is DOWN cannot answer")
        self.assertEqual(found[0]["broadcast"], "192.168.43.255")

    def test_a_missing_broadcast_is_calculated(self):
        import subprocess
        from core.system import network

        listing = json.dumps([{"ifname": "wlan0", "operstate": "UP", "addr_info": [
            {"family": "inet", "local": "10.42.0.9", "prefixlen": 24}]}])
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=listing, stderr="")
        with patch.object(subprocess, "run", return_value=completed):
            found = network.local_interfaces()
        self.assertEqual(found[0]["broadcast"], "10.42.0.255")


if __name__ == "__main__":
    unittest.main()
