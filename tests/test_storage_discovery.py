"""Finding the drives a server can live on.

A VM, a laptop and a machine with an external disk all report their storage
differently, and an older Linux reports it under a different column name. If
this goes wrong the setup screen simply shows nothing and the project cannot be
started at all, so every path through it is covered here with output recorded
from real machines.
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.system import storage


def result(stdout: str = "", code: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr="")


# A plain Ubuntu VM: one virtual disk, one root partition, plus the snap loops
# and the EFI partition that must never be offered.
MODERN_VM = json.dumps({"blockdevices": [
    {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": "74940416",
     "fstype": "squashfs", "mountpoints": ["/snap/core22/1122"]},
    {"name": "sda", "path": "/dev/sda", "type": "disk", "size": "68719476736", "fstype": None,
     "model": "VBOX HARDDISK", "mountpoints": [None], "rm": False, "rota": True, "children": [
        {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": "536870912", "fstype": "vfat",
         "mountpoints": ["/boot/efi"], "rm": False, "rota": True, "uuid": "AAAA-BBBB"},
        {"name": "sda2", "path": "/dev/sda2", "type": "part", "size": "68182605824", "fstype": "ext4",
         "label": "ubuntu", "mountpoints": ["/"], "rm": False, "rota": True, "uuid": "1111-2222"},
     ]},
]})

# The same machine on Ubuntu 20.04, whose lsblk only understands MOUNTPOINT.
OLD_VM = MODERN_VM.replace('"mountpoints": [None]', '"mountpoint": null') \
                  .replace('"mountpoints": ["/boot/efi"]', '"mountpoint": "/boot/efi"') \
                  .replace('"mountpoints": ["/"]', '"mountpoint": "/"') \
                  .replace('"mountpoints": ["/snap/core22/1122"]', '"mountpoint": "/snap/core22/1122"')

FINDMNT = json.dumps({"filesystems": [
    {"target": "/", "source": "/dev/sda2", "fstype": "ext4", "uuid": "1111-2222", "children": [
        {"target": "/boot/efi", "source": "/dev/sda1", "fstype": "vfat", "uuid": "AAAA-BBBB"},
        {"target": "/media/shaurya/USB", "source": "/dev/sdb1", "fstype": "exfat", "uuid": "3333-4444"},
    ]},
]})


# Recorded from the project's Ubuntu 26.04 test VM (util-linux 2.41.3): a
# virtual system disk, an external 2 TB WD disk that is attached but has never
# been mounted, two mounted ISO images, and eighteen snap loop devices.
REAL_VM = json.dumps({"blockdevices": [
    {"name": "fd0", "path": "/dev/fd0", "type": "disk", "size": 4096, "fstype": None,
     "mountpoints": [], "rm": True, "rota": True, "uuid": None},
    {"name": "loop0", "path": "/dev/loop0", "type": "loop", "size": 4096, "fstype": "squashfs",
     "mountpoints": ["/snap/bare/5"], "rm": False, "rota": True, "uuid": None},
    {"name": "loop5", "path": "/dev/loop5", "type": "loop", "size": 286994432, "fstype": "squashfs",
     "mountpoints": ["/snap/firefox/8107"], "rm": False, "rota": True, "uuid": None},
    {"name": "sda", "path": "/dev/sda", "type": "disk", "size": 161061273600, "fstype": None,
     "model": "VMware Virtual S", "mountpoints": [], "rm": False, "rota": True, "uuid": None,
     "children": [
        {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 1048576, "fstype": None,
         "mountpoints": [], "rm": False, "rota": True, "uuid": None},
        {"name": "sda2", "path": "/dev/sda2", "type": "part", "size": 161058127872, "fstype": "ext4",
         "mountpoints": ["/"], "rm": False, "rota": True,
         "uuid": "eaf3f05b-c46a-41a7-a903-9f36bb7f6075"},
     ]},
    {"name": "sdb", "path": "/dev/sdb", "type": "disk", "size": 2000365289472, "fstype": None,
     "model": "WDC WD20SMZW-59YFCS0", "mountpoints": [], "rm": False, "rota": True, "uuid": None,
     "children": [
        {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "size": 2000364224000,
         "fstype": "exfat", "label": "External", "mountpoints": [], "rm": False, "rota": True,
         "uuid": "1E84-5446"},
     ]},
    {"name": "sr0", "path": "/dev/sr0", "type": "rom", "size": 113184768, "fstype": "iso9660",
     "label": "CDROM", "model": "VMware Virtual SATA CDRW Drive",
     "mountpoints": ["/run/media/shaurya/CDROM"], "rm": True, "rota": True, "uuid": "2026-08-20"},
]})


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        # These mount points belong to the machine whose output was recorded,
        # not to the machine running the test, so free space is stubbed.
        usage = shutil._ntuple_diskusage(total=64 * 1024 ** 3, used=8 * 1024 ** 3, free=56 * 1024 ** 3)
        patcher = patch.object(shutil, "disk_usage", return_value=usage)
        patcher.start(); self.addCleanup(patcher.stop)

    def mounts(self, items): return sorted(item["mount"] for item in items)

    def test_a_virtual_machines_disk_is_found(self):
        with patch.object(subprocess, "run", return_value=result(MODERN_VM)):
            found = storage.discover_storage()
        self.assertIn("/", self.mounts(found))
        entry = next(item for item in found if item["mount"] == "/")
        self.assertEqual(entry["filesystem"], "ext4")
        self.assertEqual(entry["model"], "")          # the model sits on the parent disk
        self.assertGreater(entry["size"], 0)

    def test_boot_and_snap_are_never_offered(self):
        with patch.object(subprocess, "run", return_value=result(MODERN_VM)):
            mounts = self.mounts(storage.discover_storage())
        self.assertNotIn("/boot/efi", mounts)
        self.assertFalse([item for item in mounts if item.startswith("/snap")])

    def test_an_older_lsblk_still_finds_the_disk(self):
        """util-linux before 2.37 has no MOUNTPOINTS column.

        Asking for it makes lsblk exit non-zero, which used to leave the setup
        screen with no drives at all on Ubuntu 20.04 and Debian 11.
        """
        calls = []
        def fake(command, **kwargs):
            calls.append(command)
            if "MOUNTPOINTS" in command[-1]:
                return result("lsblk: unknown column: MOUNTPOINTS", code=1)
            return result(OLD_VM)

        with patch.object(subprocess, "run", side_effect=fake):
            found = storage.discover_storage()
        self.assertEqual(len(calls), 2, "the singular column was never tried")
        self.assertIn("/", self.mounts(found))

    def test_findmnt_is_used_when_lsblk_reports_nothing(self):
        def fake(command, **kwargs):
            if command[0] == "lsblk": return result(json.dumps({"blockdevices": []}))
            return result(FINDMNT)

        with patch.object(subprocess, "run", side_effect=fake):
            mounts = self.mounts(storage.discover_storage())
        self.assertIn("/", mounts)
        self.assertIn("/media/shaurya/USB", mounts)
        self.assertNotIn("/boot/efi", mounts)

    def test_home_is_offered_when_nothing_else_can_be_read(self):
        """A server must always be startable, even on an unrecognisable machine."""
        with patch.object(subprocess, "run", return_value=result("", code=1)):
            found = storage.discover_storage()
        self.assertTrue(found, "no storage was offered at all")
        self.assertEqual(found[0]["mount"], str(Path.home()))

    def test_a_missing_lsblk_does_not_raise(self):
        """A machine without util-linux must still be able to host a server."""
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("lsblk")):
            found = storage.discover_storage()
        self.assertEqual([item["mount"] for item in found], [str(Path.home())])


    def test_the_unmounted_external_disk_is_offered(self):
        """The bug this covers: an attached but unmounted drive was invisible.

        A 2 TB external disk that nobody has opened in the file manager is not
        mounted, which is its normal state, and is exactly the drive someone
        means to give a server.
        """
        with patch.object(subprocess, "run", return_value=result(REAL_VM)):
            found = storage.discover_storage()

        external = next((item for item in found if item.get("label") == "External"), None)
        self.assertIsNotNone(external, f"the external disk was not offered: {self.mounts(found)}")
        self.assertFalse(external["mounted"])
        self.assertEqual(external["path"], "/dev/sdb1")
        self.assertEqual(external["filesystem"], "exfat")
        self.assertEqual(external["size"], 2000364224000)

    def test_the_system_disk_is_still_offered_alongside_it(self):
        with patch.object(subprocess, "run", return_value=result(REAL_VM)):
            found = storage.discover_storage()
        root = next((item for item in found if item["mount"] == "/"), None)
        self.assertIsNotNone(root)
        # A drive that is ready to use is listed before one that needs mounting.
        self.assertTrue(found[0]["mounted"])

    def test_snaps_isos_and_empty_partitions_are_not_offered(self):
        with patch.object(subprocess, "run", return_value=result(REAL_VM)):
            found = storage.discover_storage()
        offered = {item.get("path") for item in found}
        self.assertNotIn("/dev/loop5", offered)      # a snap
        self.assertNotIn("/dev/sr0", offered)        # a mounted ISO
        self.assertNotIn("/dev/sda1", offered)       # no filesystem
        self.assertNotIn("/dev/fd0", offered)        # empty floppy device

    def test_removable_media_under_run_media_is_offered(self):
        """Recent udisks2 mounts removable media under /run/media, not /media."""
        listing = json.dumps({"blockdevices": [
            {"name": "sdb1", "path": "/dev/sdb1", "type": "part", "size": 2000364224000,
             "fstype": "exfat", "label": "External", "rm": True, "rota": True,
             "mountpoints": ["/run/media/shaurya/External"], "uuid": "1E84-5446"},
        ]})
        with patch.object(subprocess, "run", return_value=result(listing)):
            mounts = self.mounts(storage.discover_storage())
        self.assertIn("/run/media/shaurya/External", mounts)


class MountingTests(unittest.TestCase):
    def test_mounting_reports_where_the_drive_landed(self):
        def fake(command, **kwargs):
            if command[0] == "udisksctl":
                return result("Mounted /dev/sdb1 at /run/media/shaurya/External.\n")
            return result("/run/media/shaurya/External\n")

        with patch.object(subprocess, "run", side_effect=fake):
            self.assertEqual(storage.mount_drive("/dev/sdb1"), "/run/media/shaurya/External")

    def test_a_drive_already_mounted_is_not_an_error(self):
        def fake(command, **kwargs):
            if command[0] == "udisksctl":
                return result("Error mounting /dev/sdb1: already mounted", code=1)
            return result("/run/media/shaurya/External\n")

        with patch.object(subprocess, "run", side_effect=fake):
            self.assertEqual(storage.mount_drive("/dev/sdb1"), "/run/media/shaurya/External")

    def test_a_refused_mount_explains_itself(self):
        def fake(command, **kwargs):
            if command[0] == "udisksctl":
                return result("Error mounting /dev/sdb1: unknown filesystem exfat", code=1)
            return result("")

        with patch.object(subprocess, "run", side_effect=fake):
            with self.assertRaises(RuntimeError) as caught:
                storage.mount_drive("/dev/sdb1")
        self.assertIn("unknown filesystem", str(caught.exception))

    def test_a_missing_udisksctl_says_what_to_do(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("udisksctl")):
            with self.assertRaises(RuntimeError) as caught:
                storage.mount_drive("/dev/sdb1")
        self.assertIn("/dev/sdb1", str(caught.exception))


class ServerRootTests(unittest.TestCase):
    def test_a_server_on_the_system_disk_goes_under_home(self):
        """`/` is not writable by a normal user, so the root case redirects."""
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(Path, "home", return_value=Path(temp)):
                root = storage.initialize_server_root({"mount": "/", "uuid": "1111"}, "class-server", "Class")
        self.assertIn(".local/share/stratus/servers/class-server", root.as_posix())

    def test_a_server_on_a_drive_goes_in_stratusdata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            root = storage.initialize_server_root({"mount": temp, "uuid": "3333"}, "usb-server", "USB")
        self.assertTrue(root.as_posix().endswith("StratusData/servers/usb-server"))


if __name__ == "__main__":
    unittest.main()
