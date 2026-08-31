"""The list of servers this machine knows about.

A person can create more than one server — one for a class, one for a project
team, one on an external drive they carry around — but only one may run at a
time, because they would otherwise fight over the same control port.

Two separate records make that work:

* ``servers.json`` remembers every server this machine has created or opened,
  so the first screen can offer them again instead of asking the user to
  remember which folder on which drive held their data.
* ``active-server.json`` records the single server running right now. The shell
  tooling reads it to know what to monitor, diagnose and back up.

The registry lives in the user's home directory while the server data lives on
the chosen drive, so the two can be out of step: a drive may be unplugged, or a
fresh checkout may have no registry at all. Every function here treats the drive
as the truth and the registry as a convenience, which is why servers can also be
rediscovered by scanning a drive.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "stratus"
REGISTRY_PATH = CONFIG_DIR / "servers.json"
JOINED_PATH = CONFIG_DIR / "joined-servers.json"
ACTIVE_PATH = CONFIG_DIR / "active-server.json"

# Server data always lives at <mount>/StratusData/servers/<slug>/
DATA_ROOT = "StratusData"
SERVERS_DIR = "servers"


def _read(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try: path.chmod(0o600)      # join tokens live in here
    except OSError: pass


# --------------------------------------------------------------------- registry

def known_servers() -> list[dict]:
    """Every server this machine has created or opened, newest use first."""
    entries = _read(REGISTRY_PATH)
    if not isinstance(entries, list): return []
    clean = [item for item in entries if isinstance(item, dict) and item.get("storage")]
    clean.sort(key=lambda item: item.get("last_used", 0), reverse=True)
    return clean


def remember(server: dict) -> None:
    """Add or update a server in the registry, keyed by its storage path."""
    entries = [item for item in known_servers() if item.get("storage") != server.get("storage")]
    entry = dict(server)
    entry["last_used"] = time.time()
    entry.setdefault("created_at", entry["last_used"])
    entries.insert(0, entry)
    _write(REGISTRY_PATH, entries)


def forget(storage: str) -> None:
    """Remove a server from the list. The data on the drive is not touched."""
    _write(REGISTRY_PATH, [item for item in known_servers() if item.get("storage") != storage])


def is_server_directory(storage: str | Path) -> bool:
    """Whether a path is really a Stratus server, and not some other folder.

    `delete_server` removes a directory tree outright, so it must never act on a
    path it has not positively identified. A registry file can be hand-edited or
    left over from an older layout, and a wrong entry would otherwise destroy
    whatever it happened to name. Three things must all hold: the server's own
    config file is present, and the directory sits exactly where Stratus puts
    its servers, at <mount>/StratusData/servers/<slug>.
    """
    path = Path(storage)
    if not path.is_dir(): return False
    if not (path / "config" / "storage.json").is_file(): return False
    return path.parent.name == SERVERS_DIR and path.parent.parent.name == DATA_ROOT


def delete_server(storage: str) -> dict:
    """Delete a server permanently: its data on the drive and its registry entry.

    This is the one irreversible action in the project. Every message, file,
    device key and deployed application lives under that one directory, so
    removing it removes the server completely; nothing is kept anywhere else.

    Returns a dict saying what actually happened, because the two halves can
    come apart: a server whose drive is unplugged can be dropped from the list
    but not erased, and the caller has to be able to tell the user which it was.
    """
    entry = next((item for item in known_servers() if item.get("storage") == storage), None)
    running = running_server()
    if running and running.get("storage") == storage:
        raise RuntimeError("that server is running; stop it before deleting it")

    path = Path(storage)
    erased = False
    if path.is_dir():
        if not is_server_directory(path):
            raise ValueError(f"{storage} does not look like a Stratus server, so nothing was deleted")
        shutil.rmtree(path)
        erased = True

    # The list entry goes either way, otherwise a server with an unplugged drive
    # could never be removed from the start screen at all.
    forget(storage)
    active = active_server()
    if active and active.get("storage") == storage:
        clear_active()
    return {"erased": erased, "forgotten": True,
            "name": (entry or {}).get("name", "server"), "storage": storage}


def describe(server: dict) -> dict:
    """Add the facts that can only be learned by looking at the drive."""
    storage = Path(server.get("storage", ""))
    detail = dict(server)
    detail["available"] = storage.is_dir()
    detail["size_bytes"] = 0
    detail["devices"] = 0
    detail["applications"] = 0
    if not detail["available"]:
        return detail
    try:
        detail["size_bytes"] = sum(item.stat().st_size for item in storage.rglob("*") if item.is_file())
    except OSError:
        pass
    database = storage / "platform" / "stratus.db"
    if database.is_file():
        import sqlite3
        connection = None
        try:
            # Opened read-only: this only ever reports, and must never disturb a
            # running server. `with sqlite3.connect(...)` commits but does NOT
            # close, so the handle is released explicitly instead.
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            detail["devices"] = connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            detail["applications"] = connection.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
        except Exception:
            pass
        finally:
            if connection is not None:
                try: connection.close()
                except Exception: pass
    return detail


# ------------------------------------------------------------------- discovery

def servers_on_drive(mount: str | Path) -> list[dict]:
    """Find Stratus servers already present on a drive.

    The registry lives in the home directory, so a reinstalled system or a
    different computer has no record of servers sitting on an external disk.
    Reading each server's own config file recovers them.
    """
    root = Path(mount) / DATA_ROOT / SERVERS_DIR
    if not root.is_dir(): return []
    found = []
    for directory in sorted(root.iterdir()):
        config = directory / "config" / "storage.json"
        if not config.is_file(): continue
        stored = _read(config)
        if not isinstance(stored, dict): continue
        found.append({
            "name": stored.get("server_name") or directory.name.replace("-", " ").title(),
            "slug": stored.get("server_slug", directory.name),
            "storage": str(directory),
            "port": stored.get("port", 5050),
            "token": stored.get("token", ""),
            "discovered": True,
        })
    return found


# ---------------------------------------------------------------------- active

def active_server() -> dict | None:
    entry = _read(ACTIVE_PATH)
    return entry if isinstance(entry, dict) else None


def set_active(server: dict) -> None:
    _write(ACTIVE_PATH, server)
    remember({key: server[key] for key in ("name", "storage", "token", "port") if key in server})


def clear_active() -> None:
    ACTIVE_PATH.unlink(missing_ok=True)


def running_server() -> dict | None:
    """The server running on this machine right now, if there is one.

    Only one may run at a time: a second would collide on the control port and
    the two would fight over discovery. The recorded process id is checked
    against /proc rather than trusted, because a crash or a reboot leaves the
    file behind.
    """
    entry = active_server()
    if not entry: return None
    pid = entry.get("pid")
    if not pid: return None
    return entry if Path(f"/proc/{pid}").exists() else None


# ------------------------------------------------------------ joined servers

def joined_servers() -> list[dict]:
    """Servers this device has connected to, most recently used first.

    Separate from `known_servers`, which is about servers hosted on this
    machine. These belong to other people and are remembered only so they can
    be found again: an address, a port and a name. No token is ever kept —
    that is the one thing gating membership, and it stays in the person's head.
    """
    entries = _read(JOINED_PATH)
    if not isinstance(entries, list): return []
    clean = [item for item in entries if isinstance(item, dict) and item.get("host")]
    clean.sort(key=lambda item: item.get("last_used", 0), reverse=True)
    return clean[:20]


def remember_joined(host: str, port: int, name: str = "") -> None:
    if not host: return
    entries = [item for item in joined_servers()
               if not (item.get("host") == host and int(item.get("port", 0)) == int(port))]
    entries.insert(0, {"host": host, "port": int(port), "name": name or host,
                       "last_used": time.time()})
    _write(JOINED_PATH, entries[:20])


def joined_hosts() -> list[str]:
    """Just the addresses, for a search to probe directly."""
    hosts: list[str] = []
    for item in joined_servers():
        host = str(item.get("host") or "").strip()
        if host and host not in hosts: hosts.append(host)
    return hosts
