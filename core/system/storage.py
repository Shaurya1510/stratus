from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# lsblk gained the plural MOUNTPOINTS column in util-linux 2.37 (2021). Ubuntu
# 20.04 and Debian 11 ship older versions, where asking for it makes lsblk fail
# outright with "unknown column" and no drive is found at all. The singular
# MOUNTPOINT is understood everywhere, so it is used as a fallback.
COLUMNS = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,MODEL,MOUNTPOINTS,RM,ROTA,UUID"
COLUMNS_OLD = "NAME,PATH,TYPE,SIZE,FSTYPE,LABEL,MODEL,MOUNTPOINT,RM,ROTA,UUID"

USABLE_TYPES = {"disk", "part", "lvm", "crypt", "raid0", "raid1", "raid5", "raid6", "raid10", "dm"}

# Mount points that exist for the system's own sake and must never be offered.
SKIP_MOUNTS = ("/boot", "/snap", "/proc", "/sys", "/dev", "/run")

# ...except this one. Recent udisks2 mounts removable media under /run/media
# rather than /media, so the whole of /run cannot simply be excluded.
KEEP_MOUNTS = ("/run/media",)


def _is_system_mount(mount: str) -> bool:
    return mount.startswith(SKIP_MOUNTS) and not mount.startswith(KEEP_MOUNTS)

# Filesystems that hold no real storage.
PSEUDO = {"proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2", "squashfs",
          "overlay", "fuse.gvfsd-fuse", "fusectl", "debugfs", "tracefs", "mqueue",
          "hugetlbfs", "pstore", "securityfs", "configfs", "binfmt_misc", "autofs",
          "efivarfs", "bpf", "ramfs", "nsfs", "iso9660"}


def _flatten(devices):
    for device in devices:
        yield device
        yield from _flatten(device.get("children") or [])


def _mounts_of(item: dict) -> list[str]:
    """The mount points of one lsblk row, whichever column name it used."""
    if "mountpoints" in item:
        return [value for value in (item.get("mountpoints") or []) if value]
    single = item.get("mountpoint")
    return [single] if single else []


def _run_lsblk() -> list[dict]:
    """Every block device lsblk reports, or an empty list if it cannot be used."""
    for columns in (COLUMNS, COLUMNS_OLD):
        try:
            result = subprocess.run(["lsblk", "--json", "--bytes", "--output", columns],
                                    capture_output=True, text=True)
        except OSError:
            return []          # no lsblk on this machine; findmnt is tried next
        if result.returncode != 0: continue
        try:
            return list(_flatten(json.loads(result.stdout)["blockdevices"]))
        except (ValueError, KeyError):
            continue
    return []


def _describe(mount: str, **fields) -> dict | None:
    """One offerable drive, or None if its free space cannot even be read.

    `mount` stays exactly as the kernel reported it. Passing it through pathlib
    would rewrite it under the running host's rules, and a mount point is not a
    path to be normalised, it is a name to be quoted back.
    """
    try: usage = shutil.disk_usage(mount)
    except OSError: return None
    entry = {"name": "", "path": "", "type": "part", "size": usage.total, "free": usage.free,
             "filesystem": "", "label": "", "model": "", "mount": mount,
             "removable": False, "rotational": False, "uuid": "", "mounted": True}
    entry.update({key: value for key, value in fields.items() if value is not None})
    return entry


def _from_findmnt() -> list[dict]:
    """Real filesystems according to findmnt.

    A last resort for machines where lsblk reports nothing useful: virtual disks
    in some hypervisors, and devices behind a mapper that lsblk does not label.
    findmnt asks the kernel what is actually mounted, which is the question that
    matters here.
    """
    try:
        result = subprocess.run(["findmnt", "--json", "--real", "--output", "TARGET,SOURCE,FSTYPE,UUID"],
                                capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode != 0: return []
    try: rows = json.loads(result.stdout).get("filesystems") or []
    except ValueError: return []

    found = []
    def walk(entries):
        for entry in entries:
            target, fstype = entry.get("target", ""), (entry.get("fstype") or "")
            if target and fstype not in PSEUDO and not _is_system_mount(target):
                item = _describe(target, filesystem=fstype, uuid=entry.get("uuid") or "",
                                 path=entry.get("source") or "")
                if item: found.append(item)
            walk(entry.get("children") or [])
    walk(rows)
    return found


def discover_storage() -> list[dict]:
    """Mounted filesystems this machine could keep a server on.

    Never fails: a machine with no recognisable drive still gets its home
    directory offered, because a server has to be able to start somewhere.
    """
    candidates = []
    seen = set()
    for item in _run_lsblk():
        if item.get("type") not in USABLE_TYPES or not item.get("fstype"): continue
        if (item.get("fstype") or "") in PSEUDO: continue

        shared = {"name": item.get("name"), "path": item.get("path"), "type": item.get("type"),
                  "filesystem": item.get("fstype"), "label": item.get("label") or "",
                  "model": (item.get("model") or "").strip(),
                  "removable": bool(item.get("rm")), "rotational": bool(item.get("rota")),
                  "uuid": item.get("uuid") or ""}
        size = int(item.get("size") or 0)

        mounts = _mounts_of(item)
        if mounts:
            mount = mounts[0]
            if _is_system_mount(mount) or mount in seen: continue
            entry = _describe(mount, size=size or None, **shared)
            if entry: candidates.append(entry); seen.add(mount)
            continue

        # Attached but not mounted. This is the ordinary state of an external
        # disk nobody has opened yet, and usually the very drive the user wants
        # to hand to a server, so it is offered and mounted when chosen.
        if not item.get("path"): continue
        candidates.append({"size": size, "free": 0, "mount": "", "mounted": False, **shared})

    if not any(item.get("mounted", True) for item in candidates):
        for entry in _from_findmnt():
            if entry["mount"] in seen: continue
            candidates.append(entry); seen.add(entry["mount"])

    # Whatever else happened, the user's own home must be offerable.
    home = str(Path.home())
    if not any(item["mount"] and home.startswith(item["mount"]) for item in candidates):
        entry = _describe(home, label="This computer", filesystem="", uuid="custom")
        if entry: candidates.append(entry)

    return sorted(candidates, key=lambda value: (not value.get("mounted", True),
                                                 not value["removable"], -value["size"]))


def mount_drive(device: str) -> str:
    """Mount a drive through udisks2 and return where it landed.

    `udisksctl` is used rather than `mount` because it works as the logged-in
    user through polkit: the desktop already trusts this person to mount their
    own removable media, so no password and no root is needed. It is also what
    the file manager itself calls, so the drive ends up in the same place the
    rest of the desktop expects.
    """
    try:
        result = subprocess.run(["udisksctl", "mount", "-b", device],
                                capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("udisksctl is not installed, so this drive cannot be mounted "
                           f"automatically. Mount {device} yourself, then rescan.") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Mounting {device} timed out") from None

    # "Mounted /dev/sdb1 at /run/media/you/External" — but the wording has
    # changed between versions, so the mount table is consulted afterwards
    # rather than trusting the sentence.
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and "already mounted" not in combined.lower():
        raise RuntimeError(combined.strip().splitlines()[-1] if combined.strip()
                           else f"Could not mount {device}")

    where = mount_point_of(device)
    if not where:
        raise RuntimeError(f"{device} reported as mounted, but no mount point was found")
    return where


def mount_point_of(device: str) -> str:
    """Where a device is mounted right now, or an empty string."""
    try:
        result = subprocess.run(["findmnt", "--noheadings", "--output", "TARGET", "--source", device],
                                capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def format_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB": return f"{number:.1f} {unit}"
        number /= 1024
    return "0 B"


def choose_storage() -> dict:
    candidates = discover_storage()
    if not candidates: raise RuntimeError("No mounted writable storage was found")
    print("\nSelect Stratus server storage\n")
    for index, item in enumerate(candidates, 1):
        kind = "HDD" if item["rotational"] else "SSD/flash"
        location = "removable" if item["removable"] else "internal"
        print(f"{index}. {item['label'] or item['model'] or item['path']} — {kind}, {location}")
        print(f"   {item['filesystem']} · {format_bytes(item['free'])} free of {format_bytes(item['size'])} · {item['mount']}")
    print(f"{len(candidates)+1}. Enter another mounted directory")
    choice = int(input("Choose storage: ").strip())
    if choice == len(candidates) + 1:
        path = Path(input("Mounted directory: ").strip()).expanduser().resolve()
        if not path.is_dir(): raise ValueError("directory does not exist")
        usage = shutil.disk_usage(path)
        return {"mount": str(path), "uuid": "custom", "filesystem": "unknown", "free": usage.free}
    if choice < 1 or choice > len(candidates): raise ValueError("invalid selection")
    return candidates[choice - 1]


def initialize_server_root(selection: dict, server_slug: str, name: str = "", port: int = 5050) -> Path:
    # "/" and the home directory both mean "keep it on this computer": a normal
    # user cannot write to the root of the filesystem, so the data goes to the
    # standard per-user location instead of a StratusData folder at /.
    mount = str(selection["mount"])
    if mount == "/" or mount == str(Path.home()):
        root = Path.home() / ".local" / "share" / "stratus" / "servers" / server_slug
    else:
        root = Path(mount).resolve() / "StratusData" / "servers" / server_slug
    for relative in ("config", "platform/device-identities", "platform/events", "users", "chat/attachments", "drive/private", "drive/public", "drive/direct-shares", "drive/group-spaces", "deployments", "app-data", "uploads", "logs", "backups"):
        (root / relative).mkdir(parents=True, exist_ok=True)
    # The server's own name and port are recorded on the drive so it can be
    # rediscovered on a machine whose registry has never seen it. The join token
    # is deliberately NOT stored here: anyone holding the drive would then be
    # able to join, and the token is the only thing gating membership.
    config = {"server_slug": server_slug, "server_name": name or server_slug, "port": port,
              "storage_root": str(root), "storage_uuid": selection.get("uuid"),
              "filesystem": selection.get("filesystem")}
    (root / "config" / "storage.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return root


def validate_server_storage(root: Path) -> None:
    config_path = root / "config" / "storage.json"
    if not config_path.is_file(): return
    config = json.loads(config_path.read_text(encoding="utf-8"))
    expected = config.get("storage_uuid")
    if expected in {None, "", "custom"}: return
    result = subprocess.run(["findmnt", "--json", "--target", str(root), "--output", "UUID,TARGET"], capture_output=True, text=True)
    if result.returncode != 0: raise RuntimeError("configured Stratus storage is not mounted")
    filesystems = json.loads(result.stdout).get("filesystems") or []
    actual = filesystems[0].get("uuid") if filesystems else None
    if actual != expected: raise RuntimeError(f"storage UUID mismatch: expected {expected}, found {actual or 'none'}")
