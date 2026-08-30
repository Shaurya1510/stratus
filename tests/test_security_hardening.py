"""Regression tests for the security review.

Each test here corresponds to a specific weakness that was found by reading the
code and confirmed by reproducing it. They exist so the same mistake cannot
quietly return: every one of them fails against the code as it was before.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.platform.database import Database, valid_device_id
from core.security.e2ee import generate_ecdh_keypair
from core.system import sandbox


class DeviceIdentifierTests(unittest.TestCase):
    """A device id is chosen by the client and used as a directory name."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.storage = root / "StratusData" / "servers" / "victim"
        (self.storage / "platform").mkdir(parents=True)
        self.database = Database(self.storage / "platform" / "stratus.db")
        _, self.public = generate_ecdh_keypair()

    def tearDown(self):
        self.database.close()
        self.temp.cleanup()

    def register(self, device_id: str):
        return self.database.register_device({
            "id": device_id, "display_name": "somebody", "hostname": "vm",
            "platform": "linux", "secret": "s", "ecdh_public_key": self.public,
        })

    def test_a_traversing_identifier_is_refused(self):
        """It used to create <storage>/users/../../../.. and write into it."""
        with self.assertRaises(ValueError):
            self.register("../../../../escaped")
        outside = Path(self.temp.name) / "escaped"
        self.assertFalse(outside.exists(), "the id escaped the server directory")

    def test_an_absolute_identifier_is_refused(self):
        with self.assertRaises(ValueError):
            self.register("/tmp/escaped")

    def test_an_identifier_with_a_separator_is_refused(self):
        for hostile in ("a/b", "a\\b", "..", ".", ""):
            with self.subTest(hostile=hostile), self.assertRaises(ValueError):
                self.register(hostile)

    def test_an_identifier_that_could_break_out_of_an_html_attribute_is_refused(self):
        """These values are rendered into data- attributes by both websites."""
        with self.assertRaises(ValueError):
            self.register('x" onmouseover="steal()')

    def test_an_ordinary_identifier_is_accepted(self):
        registered = self.register("device-1a2b3c4d5e6f")
        self.assertEqual(registered["id"], "device-1a2b3c4d5e6f")
        self.assertTrue((self.storage / "users" / "device-1a2b3c4d5e6f").is_dir())

    def test_the_validator_agrees_with_the_ids_the_project_generates(self):
        from core.security.identity import load_or_create_identity
        with tempfile.TemporaryDirectory() as home:
            with patch.object(Path, "home", return_value=Path(home)):
                identity = load_or_create_identity()
        self.assertTrue(valid_device_id(identity["id"]), identity["id"])


class AdministratorTests(unittest.TestCase):
    """Being on loopback is not the same as being the host.

    Every hosted application runs on the host, so an address test alone made
    any application an administrator the moment it learned the join token.
    """

    def handler(self, address: str, admin_device: str):
        from core.server import ControlHandler

        class Fake(ControlHandler):
            def __init__(self, client_address, admin):        # no socket needed
                self.client_address = (client_address, 40000)
                self.server = type("S", (), {"admin_device": admin})()

        return Fake(address, admin_device)

    def test_the_host_device_is_an_administrator(self):
        handler = self.handler("127.0.0.1", "device-host")
        self.assertTrue(handler.is_host({"id": "device-host"}))

    def test_another_device_on_loopback_is_not(self):
        """A hosted application connecting to 127.0.0.1 with a stolen token."""
        handler = self.handler("127.0.0.1", "device-host")
        self.assertFalse(handler.is_host({"id": "device-guest-app"}))

    def test_the_host_device_from_the_network_is_not(self):
        handler = self.handler("192.168.1.44", "device-host")
        self.assertFalse(handler.is_host({"id": "device-host"}))

    def test_an_ipv4_mapped_loopback_address_still_counts_as_local(self):
        handler = self.handler("::ffff:127.0.0.1", "device-host")
        self.assertTrue(handler.is_host({"id": "device-host"}))


class TokenHandlingTests(unittest.TestCase):
    def test_the_token_can_be_read_from_a_file_instead_of_argv(self):
        """Arguments are world-readable through /proc for the same user."""
        from core import server as server_module
        with tempfile.TemporaryDirectory() as temp:
            token_file = Path(temp) / "join.token"
            token_file.write_text("a-secret-token\n", encoding="utf-8")
            captured = {}

            def fake_serve(*args, **kwargs):
                raise SystemExit(0)

            argv = ["core.server", "--storage", temp, "--token-file", str(token_file),
                    "--admin-device", "device-host"]
            with patch.object(sys, "argv", argv), \
                 patch.object(server_module, "ThreadedServer", side_effect=fake_serve), \
                 patch.object(server_module, "Database", side_effect=lambda path: captured.setdefault("db", path)), \
                 patch.object(server_module, "validate_server_storage", lambda root: None):
                with self.assertRaises(SystemExit):
                    server_module.main()
        # Reaching ThreadedServer at all means the token file was read and
        # accepted in place of --token.
        self.assertIn("db", captured)

    def test_the_token_comparison_is_constant_time(self):
        source = (ROOT / "core" / "server.py").read_text(encoding="utf-8")
        self.assertIn("secrets.compare_digest", source)
        self.assertNotIn('request.get("token") != self.server.token', source)


class ConfinementTests(unittest.TestCase):
    def test_guest_applications_cannot_see_other_processes(self):
        """/proc is where the join token was readable from."""
        properties = sandbox.confinement_properties(Path("/srv/source"), Path("/srv/data"), None)
        self.assertIn("ProtectProc=invisible", properties)
        self.assertIn("ProcSubset=pid", properties)

    def test_guest_applications_cannot_see_the_hosts_home(self):
        properties = sandbox.confinement_properties(Path("/srv/source"), Path("/srv/data"), None)
        self.assertIn("ProtectHome=tmpfs", properties)

    def test_a_build_is_confined_and_may_still_write_where_it_must(self):
        source, runtime = Path("/srv/source"), Path("/srv/runtime")
        properties = sandbox.build_properties(source, runtime)
        self.assertIn("ProtectHome=tmpfs", properties)
        self.assertIn("ProtectProc=invisible", properties)
        # Written the way the code writes them, so the assertion is about the
        # property and not about this machine's path separator.
        self.assertIn(f"ReadWritePaths={source}", properties)
        self.assertIn(f"ReadWritePaths={runtime}", properties)

    def test_a_guest_build_step_goes_through_the_sandbox(self):
        """pip install, npm install and the manifest's build line all run
        code from the upload, and used to run unconfined."""
        from core.system.deployment import DeploymentManager

        with tempfile.TemporaryDirectory() as temp:
            storage = Path(temp) / "storage"
            (storage / "platform").mkdir(parents=True)
            database = Database(storage / "platform" / "stratus.db")
            try:
                with patch.object(sandbox, "available", return_value=True), \
                     patch.dict("os.environ", {"STRATUS_RUNTIME_ROOT": str(Path(temp) / "runtimes")}):
                    manager = DeploymentManager(storage, database, 8000, 8100)
                source = Path(temp) / "source"; source.mkdir()
                root = Path(temp) / "root"; root.mkdir()

                calls = []
                def fake_run_build(deployment_id, command, environment, properties, cwd, timeout):
                    calls.append((command, properties))
                    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

                with patch.object(sandbox, "run_build", side_effect=fake_run_build), \
                     patch.object(subprocess, "run") as direct:
                    manager.build("abc123", {"runtime": "static", "build": "make evil"},
                                  source, root, guest=True)

                self.assertEqual(len(calls), 1, "the build step did not go through the sandbox")
                self.assertEqual(calls[0][0], "make evil")
                self.assertIn("ProtectHome=tmpfs", calls[0][1])
                direct.assert_not_called()
            finally:
                database.close()


class WebEscapingTests(unittest.TestCase):
    """Values are put inside HTML attributes, so quotes must be escaped."""

    def test_both_pages_escape_quotes(self):
        for name in ("chat", "drive"):
            page = (ROOT / "web" / name / "templates" / f"{name}.html").read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn('replaceAll(\'"\', "&quot;")', page)
                self.assertNotIn("node.textContent = text; return node.innerHTML", page)


if __name__ == "__main__":
    unittest.main()
