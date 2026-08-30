"""Regressions for five issues found in a security review.

Each test names the thing that could go wrong and proves it no longer does.
"""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.platform.database import Database
from core.security.e2ee import encrypt, generate_ecdh_keypair
from core.system.deployment import MAX_ARCHIVE_ENTRIES, safe_extract


def make_device(database, device_id):
    _, public = generate_ecdh_keypair()
    database.register_device({"id": device_id, "display_name": device_id, "hostname": "h",
                              "platform": "linux", "secret": "s-" + device_id, "ecdh_public_key": public})
    return {"id": device_id, "display_name": device_id, "ecdh_public_key": public}


class FileIdentifierCollisionTests(unittest.TestCase):
    """One member must not be able to destroy another member's stored file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.database = Database(root / "platform" / "stratus.db")
        self.objects = root / "drive" / "objects"
        self.alice = make_device(self.database, "device-alice")
        self.mallory = make_device(self.database, "device-mallory")

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def store(self, owner, recipients, body, file_id):
        self.database.store_encrypted_file(
            file_id, owner, "public",
            encrypt(b"name", recipients, file_id.encode()),
            encrypt(body, recipients, file_id.encode()), 0)

    def test_a_reused_identifier_is_refused(self):
        file_id = "a" * 32
        self.store("device-alice", [self.alice], b"alice's important data", file_id)
        with self.assertRaises(ValueError):
            self.store("device-mallory", [self.mallory], b"overwritten", file_id)

    def test_the_original_ciphertext_survives_the_attempt(self):
        # The real damage would be silent: the write lands, the insert fails,
        # and the owner's file is gone while the row still points at it.
        file_id = "b" * 32
        self.store("device-alice", [self.alice], b"alice's important data", file_id)
        original = (self.objects / f"{file_id}.bin").read_bytes()
        try:
            self.store("device-mallory", [self.mallory], b"x" * 500, file_id)
        except ValueError:
            pass
        self.assertEqual((self.objects / f"{file_id}.bin").read_bytes(), original,
                         "the original ciphertext was overwritten")

    def test_ownership_is_unchanged_after_a_failed_attempt(self):
        file_id = "c" * 32
        self.store("device-alice", [self.alice], b"mine", file_id)
        try:
            self.store("device-mallory", [self.mallory], b"theirs", file_id)
        except ValueError:
            pass
        row = self.database.encrypted_file(file_id, "device-alice", with_content=False)
        self.assertEqual(row["owner_device_id"], "device-alice")

    def test_an_orphaned_object_file_does_not_get_overwritten(self):
        # A file left on disk with no database row (an interrupted upload)
        # must still not be silently replaced.
        file_id = "d" * 32
        self.objects.mkdir(parents=True, exist_ok=True)
        (self.objects / f"{file_id}.bin").write_bytes(b"left behind")
        with self.assertRaises(ValueError):
            self.store("device-alice", [self.alice], b"new", file_id)
        self.assertEqual((self.objects / f"{file_id}.bin").read_bytes(), b"left behind")


class ArchiveExpansionTests(unittest.TestCase):
    """The upload limit caps compressed bytes; extraction needs its own cap."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, name, files):
        archive = self.base / name
        source = self.base / f"{name}-src"
        source.mkdir(exist_ok=True)
        with tarfile.open(archive, "w:gz") as bundle:
            for filename, body in files:
                path = source / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                bundle.add(path, arcname=filename)
        return archive

    def test_a_normal_project_extracts(self):
        archive = self.build("fine.tar.gz", [("stratus.yaml", b"name: x\n"), ("app.py", b"print(1)\n")])
        destination = self.base / "out"; destination.mkdir()
        safe_extract(archive, destination)
        self.assertTrue((destination / "app.py").is_file())

    def test_an_archive_that_expands_too_far_is_refused(self):
        # Highly compressible: a few kilobytes on disk, four megabytes unpacked.
        archive = self.build("bomb.tar.gz", [("big.bin", b"\0" * (4 * 1024 * 1024))])
        destination = self.base / "bomb-out"; destination.mkdir()
        with self.assertRaises(ValueError) as caught:
            safe_extract(archive, destination, max_bytes=1024 * 1024)
        self.assertIn("expands to more than", str(caught.exception))

    def test_nothing_is_written_when_the_archive_is_refused(self):
        archive = self.build("bomb2.tar.gz", [("big.bin", b"\0" * (4 * 1024 * 1024))])
        destination = self.base / "bomb2-out"; destination.mkdir()
        with self.assertRaises(ValueError):
            safe_extract(archive, destination, max_bytes=1024 * 1024)
        self.assertEqual(list(destination.iterdir()), [], "files were written before the size check")

    def test_too_many_entries_is_refused(self):
        archive = self.build("many.tar.gz", [(f"file{index}.txt", b"x") for index in range(30)])
        destination = self.base / "many-out"; destination.mkdir()
        with self.assertRaises(ValueError) as caught:
            safe_extract(archive, destination, max_entries=10)
        self.assertIn("entries", str(caught.exception))

    def test_the_entry_limit_is_generous_for_real_projects(self):
        self.assertGreaterEqual(MAX_ARCHIVE_ENTRIES, 10_000)

    def test_path_traversal_is_still_refused(self):
        archive = self.base / "escape.tar.gz"
        payload = self.base / "payload"; payload.write_bytes(b"pwned")
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(payload, arcname="../../escaped.txt")
        destination = self.base / "escape-out"; destination.mkdir()
        with self.assertRaises(ValueError):
            safe_extract(archive, destination)


class ConfinementPolicyTests(unittest.TestCase):
    """Guest code must not quietly run unconfined."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Database(Path(self.temp.name) / "platform.db")

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def test_confinement_is_required_by_default(self):
        self.assertEqual(self.database.get_policy()["require_confinement"], 1)

    def test_the_host_can_accept_the_risk_deliberately(self):
        policy = self.database.update_policy({"require_confinement": 0})
        self.assertEqual(policy["require_confinement"], 0)


class TicketBindingTests(unittest.TestCase):
    """A ticket for one application must not open another."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Database(Path(self.temp.name) / "platform.db")

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def test_a_ticket_opens_the_application_it_was_issued_for(self):
        token = self.database.issue_app_ticket("device-a", "chat-app")
        self.assertEqual(self.database.redeem_app_ticket(token, "chat-app"), "device-a")

    def test_a_ticket_is_refused_by_a_different_application(self):
        token = self.database.issue_app_ticket("device-a", "chat-app")
        self.assertIsNone(self.database.redeem_app_ticket(token, "drive-app"))

    def test_a_misdirected_ticket_is_spent_not_left_valid(self):
        # Otherwise the same link could simply be retried against the right app.
        token = self.database.issue_app_ticket("device-a", "chat-app")
        self.database.redeem_app_ticket(token, "drive-app")
        self.assertIsNone(self.database.redeem_app_ticket(token, "chat-app"))

    def test_it_is_still_single_use_and_still_expires(self):
        token = self.database.issue_app_ticket("device-a", "chat-app")
        self.assertEqual(self.database.redeem_app_ticket(token, "chat-app"), "device-a")
        self.assertIsNone(self.database.redeem_app_ticket(token, "chat-app"))
        expiring = self.database.issue_app_ticket("device-a", "chat-app")
        self.assertIsNone(self.database.redeem_app_ticket(expiring, "chat-app", lifetime=-1))


class GroupMembershipTests(unittest.TestCase):
    """A group naming devices that do not exist could never encrypt."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = Database(Path(self.temp.name) / "platform.db")
        make_device(self.database, "device-alice")
        make_device(self.database, "device-bob")

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def test_a_group_of_real_devices_is_created(self):
        group = self.database.create_group("group-1", "Project", "device-alice", ["device-bob"])
        self.assertEqual(group["name"], "Project")
        self.assertEqual(set(self.database.group_member_ids("group-1", "device-alice")),
                         {"device-alice", "device-bob"})

    def test_an_unknown_device_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.database.create_group("group-2", "Ghosts", "device-alice", ["device-nobody"])
        self.assertIn("device-nobody", str(caught.exception))

    def test_nothing_is_created_when_a_member_is_unknown(self):
        try:
            self.database.create_group("group-3", "Ghosts", "device-alice", ["device-nobody"])
        except ValueError:
            pass
        self.assertEqual(self.database.groups_for("device-alice"), [])

    def test_a_blocked_device_cannot_be_added(self):
        with self.database.lock:
            self.database.conn.execute("UPDATE devices SET blocked=1 WHERE id='device-bob'")
            self.database.conn.commit()
        with self.assertRaises(ValueError):
            self.database.create_group("group-4", "Blocked", "device-alice", ["device-bob"])


if __name__ == "__main__": unittest.main()
