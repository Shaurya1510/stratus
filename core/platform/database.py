from __future__ import annotations

import re
import sqlite3
import threading
import json
import hashlib
import base64
import secrets
import time
from pathlib import Path


# A device id is supplied by the client and then used as a directory name and
# rendered into HTML, so it is restricted to characters that are safe in both.
# Real ids look like "device-1a2b3c4d5e6f"; the pattern is a little wider so
# older servers keep working.
DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def valid_device_id(value: object) -> bool:
    return isinstance(value, str) and bool(DEVICE_ID.match(value)) and value not in {".", ".."}


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        # The TCP host handles each client in its own thread. Serializing database
        # operations preserves SQLite's single-writer semantics while allowing all
        # handler threads to use this one platform metadata connection.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS deployments (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
          runtime TEXT NOT NULL, source_path TEXT NOT NULL, data_path TEXT NOT NULL,
          port INTEGER NOT NULL UNIQUE, start_command TEXT NOT NULL,
          pid INTEGER, desired_state TEXT NOT NULL DEFAULT 'STOPPED',
          status TEXT NOT NULL DEFAULT 'STOPPED', auto_start INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deployment_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id TEXT NOT NULL,
          event TEXT NOT NULL, details TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, display_name TEXT NOT NULL, hostname TEXT NOT NULL,
          platform TEXT NOT NULL, secret_hash TEXT NOT NULL,
          first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          approved INTEGER NOT NULL DEFAULT 1,
          blocked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS hosting_policy (
          id INTEGER PRIMARY KEY CHECK(id=1), guest_deploy_enabled INTEGER NOT NULL DEFAULT 0,
          require_approval INTEGER NOT NULL DEFAULT 1, max_apps_per_device INTEGER NOT NULL DEFAULT 2,
          max_upload_mb INTEGER NOT NULL DEFAULT 10, max_data_mb INTEGER NOT NULL DEFAULT 500,
          max_total_guest_gb INTEGER NOT NULL DEFAULT 20, allowed_runtimes TEXT NOT NULL DEFAULT '["static","python","node"]',
          max_memory_mb INTEGER NOT NULL DEFAULT 256, max_cpu_percent INTEGER NOT NULL DEFAULT 25,
          inactivity_minutes INTEGER NOT NULL DEFAULT 60
        );
        INSERT OR IGNORE INTO hosting_policy(id) VALUES(1);
        CREATE TABLE IF NOT EXISTS deployment_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL, app_name TEXT NOT NULL,
          runtime TEXT NOT NULL, upload_bytes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          decided_at TEXT, FOREIGN KEY(device_id) REFERENCES devices(id)
        );
        CREATE TABLE IF NOT EXISTS shared_spaces (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_device_id TEXT NOT NULL,
          visibility TEXT NOT NULL DEFAULT 'private', quota_bytes INTEGER NOT NULL DEFAULT 1073741824,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS shared_space_members (
          space_id TEXT NOT NULL, device_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
          PRIMARY KEY(space_id,device_id), FOREIGN KEY(space_id) REFERENCES shared_spaces(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS encrypted_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
          sender_device_id TEXT NOT NULL, payload TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS encrypted_files (
          id TEXT PRIMARY KEY, owner_device_id TEXT NOT NULL, scope TEXT NOT NULL,
          encrypted_name TEXT NOT NULL, payload TEXT NOT NULL, ciphertext_bytes INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversations (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
          owner_device_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversation_members (
          conversation_id TEXT NOT NULL, device_id TEXT NOT NULL,
          PRIMARY KEY(conversation_id,device_id), FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
        );
        """)
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS app_tickets (
          token TEXT PRIMARY KEY, device_id TEXT NOT NULL, deployment_id TEXT,
          created_at REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS device_key_blobs (
          device_id TEXT PRIMARY KEY, blob TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        try: self.conn.execute("ALTER TABLE devices ADD COLUMN encryption_public_key TEXT")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE devices ADD COLUMN ecdh_public_key TEXT")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE hosting_policy ADD COLUMN allowed_networks TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE hosting_policy ADD COLUMN require_confinement INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE deployment_requests ADD COLUMN archive_path TEXT")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE encrypted_files ADD COLUMN ciphertext_path TEXT")
        except sqlite3.OperationalError: pass
        self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def port_is_reserved(self, port: int) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM deployments WHERE port=?", (port,)).fetchone() is not None

    def create(self, values: dict) -> None:
        with self.lock:
            self.conn.execute("""INSERT INTO deployments
            (id, owner, name, runtime, source_path, data_path, port, start_command, auto_start)
            VALUES (:id,:owner,:name,:runtime,:source_path,:data_path,:port,:start_command,:auto_start)""", values)
            self.event(values["id"], "CREATED", f"runtime={values['runtime']}")
            self.conn.commit()

    def event(self, deployment_id: str, event: str, details: str = "") -> None:
        self.conn.execute("INSERT INTO deployment_events(deployment_id,event,details) VALUES(?,?,?)", (deployment_id, event, details))

    def get(self, deployment_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM deployments WHERE id=?", (deployment_id,)).fetchone()
            return dict(row) if row else None

    def list(self, owner: str) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute("SELECT * FROM deployments WHERE owner=? ORDER BY created_at DESC", (owner,))]

    def list_all(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute("SELECT * FROM deployments ORDER BY created_at DESC")]

    def update_state(self, deployment_id: str, status: str, pid: int | None, desired: str | None = None) -> None:
        with self.lock:
            self.conn.execute("""UPDATE deployments SET status=?,pid=?,desired_state=COALESCE(?,desired_state),updated_at=CURRENT_TIMESTAMP WHERE id=?""", (status, pid, desired, deployment_id))
            self.event(deployment_id, status, f"pid={pid or ''}")
            self.conn.commit()

    def register_device(self, device: dict) -> dict:
        required = ("id", "display_name", "hostname", "platform", "secret", "ecdh_public_key")
        if any(not device.get(key) for key in required):
            raise ValueError("incomplete device identity")
        if not valid_device_id(device["id"]):
            raise ValueError("device identifier contains unsupported characters")
        secret_hash = hashlib.sha256(device["secret"].encode("utf-8")).hexdigest()
        with self.lock:
            existing = self.conn.execute("SELECT * FROM devices WHERE id=?", (device["id"],)).fetchone()
            if existing and existing["secret_hash"] != secret_hash:
                raise ValueError("device authentication failed")
            self.conn.execute("""INSERT INTO devices(id,display_name,hostname,platform,secret_hash,encryption_public_key,ecdh_public_key)
              VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
              hostname=excluded.hostname,platform=excluded.platform,encryption_public_key=excluded.encryption_public_key,
              ecdh_public_key=excluded.ecdh_public_key,last_seen=CURRENT_TIMESTAMP""",
              (device["id"], device["display_name"][:80], device["hostname"][:80], device["platform"][:40], secret_hash,
               device.get("encryption_public_key"), device["ecdh_public_key"]))
            self.conn.commit()
            registered = dict(self.conn.execute("SELECT id,display_name,hostname,platform,first_seen,last_seen,approved,blocked FROM devices WHERE id=?", (device["id"],)).fetchone())
        server_root = self.path.parent.parent
        user_root = server_root / "users" / device["id"]
        for name in ("private-drive", "received-files", "chat-exports", "deployments"):
            (user_root / name).mkdir(parents=True, exist_ok=True)
        (user_root / "profile.json").write_text(json.dumps(registered, indent=2), encoding="utf-8")
        return registered

    def list_devices(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute("SELECT id,display_name,hostname,platform,first_seen,last_seen,approved,blocked,encryption_public_key FROM devices ORDER BY display_name")]

    def encryption_recipients(self, device_ids: list[str] | None = None) -> list[dict]:
        columns = "id,display_name,encryption_public_key,ecdh_public_key"
        with self.lock:
            if device_ids:
                marks = ",".join("?" for _ in device_ids)
                rows = self.conn.execute(f"SELECT {columns} FROM devices WHERE id IN ({marks}) AND approved=1 AND blocked=0", tuple(device_ids))
            else: rows = self.conn.execute(f"SELECT {columns} FROM devices WHERE approved=1 AND blocked=0")
            return [dict(row) for row in rows if row["ecdh_public_key"]]

    def store_encrypted_message(self, conversation_id: str, sender: str, payload: dict) -> int:
        with self.lock:
            cursor = self.conn.execute("INSERT INTO encrypted_messages(conversation_id,sender_device_id,payload) VALUES(?,?,?)", (conversation_id, sender, json.dumps(payload)))
            self.conn.commit(); return cursor.lastrowid

    def encrypted_messages(self, conversation_id: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("""SELECT encrypted_messages.*,devices.display_name AS sender_name FROM encrypted_messages
              JOIN devices ON devices.id=sender_device_id WHERE conversation_id=? AND encrypted_messages.id>? ORDER BY encrypted_messages.id LIMIT 500""", (conversation_id, after))
            result=[]
            for row in rows:
                item=dict(row); item["payload"]=json.loads(item["payload"]); result.append(item)
            return result

    def store_key_blob(self, device_id: str, blob: dict) -> None:
        """Keep a device's passphrase-sealed key. The host cannot open it."""
        with self.lock:
            self.conn.execute("""INSERT INTO device_key_blobs(device_id,blob) VALUES(?,?)
              ON CONFLICT(device_id) DO UPDATE SET blob=excluded.blob,updated_at=CURRENT_TIMESTAMP""",
              (device_id, json.dumps(blob)))
            self.conn.commit()

    def key_blob(self, device_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT blob FROM device_key_blobs WHERE device_id=?", (device_id,)).fetchone()
        return json.loads(row["blob"]) if row else None

    def forget_key_blob(self, device_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM device_key_blobs WHERE device_id=?", (device_id,)); self.conn.commit()

    def issue_app_ticket(self, device_id: str, deployment_id: str | None = None, lifetime: float = 60.0) -> str:
        """A short-lived, single-use ticket that logs a device into a built-in app.

        The device secret must never travel in a URL: web servers log request
        lines, and the log is stored on the host and copied into backups. A
        ticket is useless a minute later and useless twice.
        """
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.conn.execute("DELETE FROM app_tickets WHERE created_at < ?", (time.time() - max(lifetime, 60.0),))
            self.conn.execute("INSERT INTO app_tickets(token,device_id,deployment_id,created_at) VALUES(?,?,?,?)",
                              (token, device_id, deployment_id, time.time()))
            self.conn.commit()
        return token

    def redeem_app_ticket(self, token: str, deployment_id: str | None = None, lifetime: float = 60.0) -> str | None:
        """Spend a sign-in ticket, once.

        The ticket names the application it was issued for. An application that
        is handed a ticket meant for a different one must refuse it, otherwise a
        link for Chat would also open Drive, and a ticket would grant more than
        the person clicking it expected.
        """
        with self.lock:
            row = self.conn.execute("SELECT * FROM app_tickets WHERE token=? AND used=0", (token,)).fetchone()
            if not row: return None
            if time.time() - row["created_at"] > lifetime:
                self.conn.execute("DELETE FROM app_tickets WHERE token=?", (token,)); self.conn.commit(); return None
            if deployment_id and row["deployment_id"] and row["deployment_id"] != deployment_id:
                # Spend it anyway: a ticket presented to the wrong application
                # has been mishandled, and leaving it valid invites a retry.
                self.conn.execute("UPDATE app_tickets SET used=1 WHERE token=?", (token,))
                self.conn.commit()
                return None
            self.conn.execute("UPDATE app_tickets SET used=1 WHERE token=?", (token,))
            self.conn.commit()
        return row["device_id"]

    def conversation_overview(self, device_id: str) -> dict:
        """Latest message id per conversation this device belongs to.

        One grouped query replaces one poll per conversation, so the sidebar
        can show unread badges without the client asking about every chat it
        can see. Conversations belonging to other devices are filtered out
        here rather than on the client.
        """
        with self.lock:
            rows = self.conn.execute("SELECT conversation_id, MAX(id) AS last_id, COUNT(*) AS total FROM encrypted_messages GROUP BY conversation_id").fetchall()
            groups = {row[0] for row in self.conn.execute("SELECT conversation_id FROM conversation_members WHERE device_id=?", (device_id,))}
        overview = {}
        for row in rows:
            conversation = row["conversation_id"]
            mine = conversation == "general" or conversation in groups or (conversation.startswith("direct:") and device_id in conversation.split(":")[1:])
            if mine: overview[conversation] = {"last_id": row["last_id"], "total": row["total"]}
        return overview

    def store_encrypted_file(self, file_id: str, owner: str, scope: str, encrypted_name: dict, payload: dict, size: int) -> None:
        """Store one encrypted object.

        The file id is chosen by the client, so it must be claimed in the
        database *before* anything is written to disk. Writing first would let
        one member destroy another member's stored file simply by uploading
        with their id: the write would land, the insert would then fail on the
        primary key, and the original ciphertext would be gone. Opening with
        "xb" is a second guard — it refuses to open a path that already exists.
        """
        object_root = self.path.parent.parent / "drive" / "objects"
        object_root.mkdir(parents=True, exist_ok=True)
        ciphertext = base64.b64decode(payload["ciphertext"], validate=True)
        object_path = object_root / f"{file_id}.bin"
        stored_payload = dict(payload); stored_payload.pop("ciphertext", None)
        with self.lock:
            try:
                self.conn.execute("INSERT INTO encrypted_files(id,owner_device_id,scope,encrypted_name,payload,ciphertext_bytes,ciphertext_path) VALUES(?,?,?,?,?,?,?)", (file_id, owner, scope, json.dumps(encrypted_name), json.dumps(stored_payload), len(ciphertext), str(object_path)))
            except sqlite3.IntegrityError:
                raise ValueError("a file with that identifier already exists") from None
            try:
                with open(object_path, "xb") as handle:
                    handle.write(ciphertext)
            except FileExistsError:
                self.conn.rollback()
                raise ValueError("a stored object with that identifier already exists") from None
            except OSError:
                self.conn.rollback()
                raise
            self.conn.commit()

    def _decode_file_row(self, row) -> dict:
        item = dict(row)
        item["encrypted_name"] = json.loads(item["encrypted_name"])
        item["payload"] = json.loads(item["payload"])
        return item

    def encrypted_files(self, device_id: str) -> list[dict]:
        """List file metadata only.

        Ciphertext lives on disk and is never read here: listing a Drive must
        stay instant no matter how large the stored objects are. Envelopes are
        kept so membership can be checked, and the small encrypted filename is
        kept so authorized clients can render the real name.
        """
        with self.lock:
            rows = self.conn.execute("SELECT encrypted_files.*,devices.display_name AS owner_name FROM encrypted_files JOIN devices ON devices.id=owner_device_id ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = self._decode_file_row(row)
            item.pop("ciphertext_path", None)
            if device_id in item["payload"].get("envelopes", {}): result.append(item)
        return result

    def encrypted_file(self, file_id: str, device_id: str, with_content: bool = True) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT encrypted_files.*,devices.display_name AS owner_name FROM encrypted_files JOIN devices ON devices.id=owner_device_id WHERE encrypted_files.id=?", (file_id,)).fetchone()
        if not row: return None
        item = self._decode_file_row(row)
        if device_id not in item["payload"].get("envelopes", {}): return None
        path = item.pop("ciphertext_path", None)
        if with_content and path and Path(path).is_file():
            item["payload"]["ciphertext"] = base64.b64encode(Path(path).read_bytes()).decode()
        return item

    def rename_encrypted_file(self, file_id: str, owner: str, encrypted_name: dict) -> None:
        with self.lock:
            row = self.conn.execute("SELECT 1 FROM encrypted_files WHERE id=? AND owner_device_id=?", (file_id, owner)).fetchone()
            if not row: raise ValueError("file not found or not owned by this device")
            self.conn.execute("UPDATE encrypted_files SET encrypted_name=? WHERE id=?", (json.dumps(encrypted_name), file_id)); self.conn.commit()

    def delete_encrypted_file(self, file_id: str, owner: str) -> None:
        with self.lock:
            row = self.conn.execute("SELECT ciphertext_path FROM encrypted_files WHERE id=? AND owner_device_id=?", (file_id, owner)).fetchone()
            if not row: raise ValueError("file not found or not owned by this device")
            self.conn.execute("DELETE FROM encrypted_files WHERE id=?", (file_id,)); self.conn.commit()
        if row["ciphertext_path"]: Path(row["ciphertext_path"]).unlink(missing_ok=True)

    def create_group(self, group_id: str, name: str, owner: str, members: list[str]) -> dict:
        """Create a group conversation.

        Membership is checked against the device table first. A group naming a
        device that does not exist, or one the host has blocked, could never be
        encrypted for successfully: every send would fail the membership check
        and the group would simply appear broken.
        """
        wanted = {str(item) for item in members if str(item).strip()} | {owner}
        with self.lock:
            marks = ",".join("?" for _ in wanted)
            usable = {row[0] for row in self.conn.execute(
                f"SELECT id FROM devices WHERE id IN ({marks}) AND approved=1 AND blocked=0", tuple(wanted))}
            missing = wanted - usable
            if missing:
                raise ValueError(f"unknown or blocked device(s): {', '.join(sorted(missing))}")
            self.conn.execute("INSERT INTO conversations(id,name,type,owner_device_id) VALUES(?,?,'group',?)", (group_id, name[:80], owner))
            for device_id in usable:
                self.conn.execute("INSERT INTO conversation_members(conversation_id,device_id) VALUES(?,?)", (group_id, device_id))
            self.conn.commit()
        return {"id": group_id, "name": name, "type": "group"}

    def groups_for(self, device_id: str) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute("""SELECT conversations.* FROM conversations JOIN conversation_members
              ON conversation_members.conversation_id=conversations.id WHERE conversation_members.device_id=? ORDER BY name""", (device_id,))]

    def group_member_ids(self, group_id: str, device_id: str) -> list[str]:
        with self.lock:
            allowed = self.conn.execute("SELECT 1 FROM conversation_members WHERE conversation_id=? AND device_id=?", (group_id, device_id)).fetchone()
            if not allowed: raise ValueError("not a member of this group")
            return [row[0] for row in self.conn.execute("SELECT device_id FROM conversation_members WHERE conversation_id=?", (group_id,))]

    def get_policy(self) -> dict:
        with self.lock:
            row = dict(self.conn.execute("SELECT * FROM hosting_policy WHERE id=1").fetchone())
            row["allowed_runtimes"] = json.loads(row["allowed_runtimes"])
            row["allowed_networks"] = json.loads(row.get("allowed_networks") or "[]")
            return row

    def update_policy(self, changes: dict) -> dict:
        allowed = {"guest_deploy_enabled", "require_approval", "max_apps_per_device", "max_upload_mb", "max_data_mb", "max_total_guest_gb", "allowed_runtimes", "max_memory_mb", "max_cpu_percent", "inactivity_minutes", "allowed_networks", "require_confinement"}
        clean = {key: value for key, value in changes.items() if key in allowed}
        if "allowed_runtimes" in clean:
            runtimes = clean["allowed_runtimes"]
            if not isinstance(runtimes, list) or not set(runtimes) <= {"static", "python", "node"}: raise ValueError("invalid runtimes")
            clean["allowed_runtimes"] = json.dumps(runtimes)
        if "allowed_networks" in clean:
            # Parsed here so a malformed range is refused at the moment the host
            # sets it, rather than silently locking every client out later.
            from core.system.network import parse_networks
            entries = clean["allowed_networks"]
            if isinstance(entries, str): entries = [entries]
            if not isinstance(entries, list): raise ValueError("allowed networks must be a list")
            entries = [str(item).strip() for item in entries if str(item).strip()]
            parse_networks(entries)
            clean["allowed_networks"] = json.dumps(entries)
        if not clean: return self.get_policy()
        with self.lock:
            assignments = ",".join(f"{key}=?" for key in clean)
            self.conn.execute(f"UPDATE hosting_policy SET {assignments} WHERE id=1", tuple(clean.values()))
            self.conn.commit()
        return self.get_policy()

    def validate_deployment_policy(self, device_id: str, runtime: str, upload_bytes: int, is_admin: bool = False) -> None:
        if is_admin: return
        policy = self.get_policy()
        if not policy["guest_deploy_enabled"]: raise ValueError("guest application hosting is disabled by the host")
        if runtime not in policy["allowed_runtimes"]: raise ValueError("runtime is not allowed by host policy")
        if upload_bytes > policy["max_upload_mb"] * 1024 * 1024: raise ValueError("upload exceeds host policy")
        with self.lock:
            count = self.conn.execute("SELECT COUNT(*) FROM deployments WHERE owner=?", (device_id,)).fetchone()[0]
        if count >= policy["max_apps_per_device"]: raise ValueError("device application limit reached")

    def create_deployment_request(self, device_id: str, app_name: str, runtime: str, size: int, archive_path: str) -> int:
        with self.lock:
            cursor = self.conn.execute("INSERT INTO deployment_requests(device_id,app_name,runtime,upload_bytes,archive_path) VALUES(?,?,?,?,?)", (device_id, app_name[:80], runtime, size, archive_path))
            self.conn.commit(); return cursor.lastrowid

    def pending_requests(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute("""SELECT deployment_requests.*,devices.display_name FROM deployment_requests
              JOIN devices ON devices.id=device_id WHERE status='PENDING' ORDER BY created_at""")]

    def deployment_request(self, request_id: int) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM deployment_requests WHERE id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def decide_request(self, request_id: int, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE deployment_requests SET status=?,decided_at=CURRENT_TIMESTAMP WHERE id=?", (status, request_id)); self.conn.commit()
