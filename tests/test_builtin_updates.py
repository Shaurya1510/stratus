"""The built-in websites must follow the version of Stratus that is installed.

They ship with the project rather than being uploaded, so a server created with
an older Stratus kept running that older copy forever: updating the checkout
changed nothing, because installation skipped any application whose name already
existed. These tests cover the digest comparison and the in-place replacement
that fixes it.
"""
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.platform.database import Database
from core.server import bundle_builtin, source_digest
from core.system.deployment import DeploymentManager


class SourceDigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.folder = Path(self.temp.name) / "app"
        self.folder.mkdir()
        (self.folder / "stratus.yaml").write_text("name: demo\nruntime: static\nstart: x\n", encoding="utf-8")
        (self.folder / "app.py").write_text("print(1)\n", encoding="utf-8")
        self.shared = []

    def tearDown(self):
        self.temp.cleanup()

    def digest(self):
        return source_digest(self.folder, self.shared)

    def test_the_same_tree_gives_the_same_digest(self):
        self.assertEqual(self.digest(), self.digest())

    def test_editing_a_file_changes_the_digest(self):
        before = self.digest()
        (self.folder / "app.py").write_text("print(2)\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_adding_a_file_changes_the_digest(self):
        before = self.digest()
        (self.folder / "extra.js").write_text("//\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_deleting_a_file_changes_the_digest(self):
        (self.folder / "extra.js").write_text("//\n", encoding="utf-8")
        before = self.digest()
        (self.folder / "extra.js").unlink()
        self.assertNotEqual(before, self.digest())

    def test_renaming_a_file_changes_the_digest(self):
        # Contents alone are not enough: a rename must count as a change, or a
        # script referenced by its new name would 404 while the digest matched.
        before = self.digest()
        (self.folder / "app.py").rename(self.folder / "main.py")
        self.assertNotEqual(before, self.digest())

    def test_shared_modules_are_included(self):
        shared_file = Path(self.temp.name) / "database.py"
        shared_file.write_text("# v1\n", encoding="utf-8")
        self.shared = [shared_file]
        before = self.digest()
        shared_file.write_text("# v2\n", encoding="utf-8")
        self.assertNotEqual(before, self.digest())

    def test_compiled_python_is_ignored(self):
        cache = self.folder / "__pycache__"
        cache.mkdir()
        before = self.digest()
        (cache / "app.cpython-312.pyc").write_bytes(b"\x00\x01")
        self.assertEqual(before, self.digest(), "a stray .pyc must not look like a change")


class ReplaceSourceTests(unittest.TestCase):
    """Updating in place must keep the identity, port and stored data."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp.name)
        self.database = Database(self.base / "platform" / "stratus.db")
        self.manager = DeploymentManager(self.base, self.database, 8900, 8999)
        # A deployment record standing in for an installed built-in.
        self.source = self.base / "deployments" / "app1" / "source"
        self.source.mkdir(parents=True)
        (self.source / "stratus.yaml").write_text(
            "name: demo\nruntime: static\nstart: old-command\n", encoding="utf-8")
        (self.source / "page.html").write_text("<p>version one</p>", encoding="utf-8")
        self.data = self.base / "app-data" / "app1"
        self.data.mkdir(parents=True)
        (self.data / "keep.txt").write_text("must survive", encoding="utf-8")
        self.database.create({"id": "app1", "owner": "system", "name": "demo", "runtime": "static",
                              "source_path": str(self.source), "data_path": str(self.data),
                              "port": 8901, "start_command": "old-command", "auto_start": 0})

    def tearDown(self):
        self.database.close(); self.temp.cleanup()

    def build_archive(self, page: str, start: str = "new-command") -> Path:
        folder = self.base / "incoming"
        folder.mkdir(exist_ok=True)
        (folder / "stratus.yaml").write_text(f"name: demo\nruntime: static\nstart: {start}\n", encoding="utf-8")
        (folder / "page.html").write_text(page, encoding="utf-8")
        archive = self.base / "incoming.tar.gz"
        bundle_builtin(folder, [], archive)
        return archive

    def replace(self, archive):
        # start() would try to run a real process; the swap itself is what matters.
        try:
            self.manager.replace_source("app1", archive)
        except Exception:
            pass

    def test_the_new_files_land(self):
        self.replace(self.build_archive("<p>version two</p>"))
        self.assertEqual((self.source / "page.html").read_text(encoding="utf-8"), "<p>version two</p>")

    def test_the_start_command_is_updated_from_the_new_manifest(self):
        self.replace(self.build_archive("<p>two</p>", start="new-command"))
        self.assertEqual(self.database.get("app1")["start_command"], "new-command")

    def test_the_identifier_and_port_are_kept(self):
        self.replace(self.build_archive("<p>two</p>"))
        row = self.database.get("app1")
        self.assertEqual(row["id"], "app1")
        self.assertEqual(row["port"], 8901)

    def test_application_data_survives(self):
        # Redeploying from scratch would hand out a new data directory and lose
        # whatever the application had stored.
        self.replace(self.build_archive("<p>two</p>"))
        self.assertEqual((self.data / "keep.txt").read_text(encoding="utf-8"), "must survive")

    def test_no_staging_directories_are_left_behind(self):
        self.replace(self.build_archive("<p>two</p>"))
        parent = self.source.parent
        self.assertFalse((parent / "source.incoming").exists())
        self.assertFalse((parent / "source.previous").exists())

    def test_a_broken_archive_leaves_the_old_version_running(self):
        broken = self.base / "broken.tar.gz"
        with tarfile.open(broken, "w:gz") as bundle:
            junk = self.base / "junk.txt"
            junk.write_text("no manifest here", encoding="utf-8")
            bundle.add(junk, arcname="junk.txt")
        with self.assertRaises(ValueError):
            self.manager.replace_source("app1", broken)
        self.assertEqual((self.source / "page.html").read_text(encoding="utf-8"), "<p>version one</p>")
        self.assertEqual(self.database.get("app1")["start_command"], "old-command")

    def test_an_unknown_deployment_is_refused(self):
        with self.assertRaises(ValueError):
            self.manager.replace_source("no-such-app", self.build_archive("<p>x</p>"))


if __name__ == "__main__": unittest.main()
