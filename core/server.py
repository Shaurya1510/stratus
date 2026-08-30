from __future__ import annotations

import argparse
import base64
import secrets
import hashlib
import socket
import socketserver
import tempfile
import threading
import time
import tarfile
import uuid
from pathlib import Path

from core.platform.database import Database
from core.system.deployment import DeploymentManager
from core.platform.protocol import receive_message, send_message
from core.system.discovery import serve_discovery
from core.system.network import lan_address
from core.platform import platform_api
from core.system.storage import validate_server_storage


class ControlHandler(socketserver.BaseRequestHandler):
    """One connection, many requests.

    Terminal clients poll Chat and Drive constantly. Opening a fresh TCP
    connection, re-authenticating and re-registering the device for every
    single poll dominated the response time, so a connection now stays open
    and serves framed requests until the client closes it or goes idle.
    """

    IDLE_TIMEOUT = 300.0
    DEVICE_TOUCH_SECONDS = 30.0

    def setup(self) -> None:
        self.request.settimeout(self.IDLE_TIMEOUT)
        # Checked before anything is read from the socket, so a client outside
        # the permitted range never reaches authentication and cannot probe the
        # join token at all.
        self.permitted = self.address_permitted()
        try: self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError: pass
        self.device: dict | None = None
        self.device_touched = 0.0

    def address_permitted(self) -> bool:
        from core.system.network import address_allowed, parse_networks
        try:
            configured = self.server.manager.database.get_policy().get("allowed_networks") or []
            if not configured: return True
            return address_allowed(self.client_address[0], parse_networks(configured))
        except Exception:
            # A broken restriction must not brick the server; fail open and let
            # the token remain the barrier.
            return True

    def handle(self) -> None:
        if not self.permitted:
            # The client's opening frame is read and discarded before replying.
            # Closing a socket that still holds unread data makes the kernel
            # send RST rather than FIN, which throws away the queued reply and
            # leaves the user staring at a connection error instead of being
            # told why they were refused. Nothing in the frame is examined, so
            # the join token is still never checked.
            try: receive_message(self.request)
            except (ConnectionError, OSError, ValueError): pass
            self.reply({"ok": False, "error": "this device is not permitted to join from its network address"})
            try: self.request.shutdown(socket.SHUT_WR)
            except OSError: pass
            return
        while True:
            try: request = receive_message(self.request)
            except (ConnectionError, OSError, ValueError): return
            try: reply = {"ok": True, "data": self.dispatch(request)}
            except PermissionError as exc:
                self.reply({"ok": False, "error": str(exc)}); return
            except Exception as exc:
                reply = {"ok": False, "error": str(exc)}
            if not self.reply(reply): return

    def reply(self, payload: dict) -> bool:
        try: send_message(self.request, payload); return True
        except (OSError, ValueError): return False

    def authenticate(self, request: dict) -> dict:
        # Compared in constant time so the reply latency cannot be used to
        # recover the token one character at a time.
        if not secrets.compare_digest(str(request.get("token") or ""), self.server.token):
            raise PermissionError("authentication failed")
        supplied = request.get("device") or {}
        now = time.monotonic()
        cached = self.device is not None and self.device["id"] == supplied.get("id")
        if not cached or now - self.device_touched > self.DEVICE_TOUCH_SECONDS:
            self.device = self.server.manager.database.register_device(supplied)
            self.device_touched = now
        if self.device["blocked"]: raise PermissionError("this device has been blocked by the host")
        return self.device

    def dispatch(self, request: dict):
        database = self.server.manager.database
        device = self.authenticate(request)
        action, owner = request.get("action"), device["id"]
        is_admin = self.is_host(device)
        if action == "ping": return {"pong": True, "server": self.server.server_name}
        if action == "server_info":
            return {"name": self.server.server_name, "address": self.server.lan_address,
                    "port": self.server.server_address[1],
                    "allowed_networks": database.get_policy().get("allowed_networks") or []}
        if action == "app_ticket":
            # A single-use ticket signs this device into a built-in web app
            # without ever putting its long-lived secret in a URL.
            item = database.get(request["id"])
            if not item: raise ValueError("application not found")
            return {"ticket": database.issue_app_ticket(owner, item["id"]),
                    "address": self.server.lan_address,
                    "port": item["port"],
                    "url": f"http://{self.server.lan_address}:{item['port']}"}
        if action == "store_key_blob":
            # Sealed with a passphrase the host never sees; stored so a browser
            # on this network can unseal it locally and join the same
            # conversations. A device may only store its own.
            from core.security.browser_access import blob_is_wellformed
            blob = request.get("blob")
            if not blob_is_wellformed(blob): raise ValueError("sealed key is not in the expected format")
            database.store_key_blob(owner, blob); return {"status": "STORED"}
        if action == "key_blob":
            blob = database.key_blob(owner)
            if not blob: raise ValueError("browser access has not been set up for this device")
            return blob
        if action == "forget_key_blob":
            database.forget_key_blob(owner); return {"status": "FORGOTTEN"}
        if action == "browser_access_enabled": return {"enabled": database.key_blob(owner) is not None}
        if action == "list": return database.list_all() if is_admin else database.list(owner)
        if action == "deploy":
            raw = base64.b64decode(request["archive"], validate=True)
            if len(raw) > self.server.max_upload: raise ValueError("archive exceeds upload limit")
            database.validate_deployment_policy(owner, request.get("runtime", ""), len(raw), is_admin=is_admin)
            with tempfile.NamedTemporaryFile(dir=self.server.upload_dir, suffix=".tar.gz", delete=False) as archive:
                archive.write(raw); archive_path = Path(archive.name)
            policy = database.get_policy()
            if not is_admin and policy["require_approval"]:
                request_id = database.create_deployment_request(owner, request.get("app_name", "application"), request.get("runtime", ""), len(raw), str(archive_path))
                return {"status": "PENDING_APPROVAL", "request_id": request_id}
            try: return self.server.manager.deploy(owner, archive_path)
            finally: archive_path.unlink(missing_ok=True)
        if action in {"start", "stop", "status"}:
            self.owned(request["id"], owner, is_admin)
            return getattr(self.server.manager, action)(request["id"])
        if action == "restart":
            self.owned(request["id"], owner, is_admin)
            self.server.manager.stop(request["id"]); return self.server.manager.start(request["id"])
        if action == "logs":
            item = self.owned(request["id"], owner, is_admin)
            log = Path(item["source_path"]).parent / "runtime.log"
            lines = int(request.get("lines", 200))
            return {"logs": "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) if log.exists() else ""}
        if action == "devices": return database.list_devices()
        if action == "apps": return database.list_all()
        if action == "recipients": return database.encryption_recipients(request.get("device_ids"))
        if action == "chat_send":
            return {"id": platform_api.send_message(database, request["conversation_id"], owner, request.get("payload") or {})}
        if action == "chat_messages":
            return platform_api.messages(database, request["conversation_id"], owner, int(request.get("after", 0)))
        if action == "conversations": return platform_api.conversation_catalogue(database, owner)
        if action == "chat_overview": return database.conversation_overview(owner)
        if action == "drive_upload":
            file_id = platform_api.store_file(database, owner, request.get("scope", "private"),
                                              request.get("encrypted_name") or {}, request.get("payload") or {}, request.get("id"))
            return {"id": file_id}
        if action == "drive_list": return database.encrypted_files(owner)
        if action == "drive_get":
            item = database.encrypted_file(request["id"], owner)
            if not item: raise ValueError("file not found")
            return item
        if action == "drive_rename":
            current = database.encrypted_file(request["id"], owner, with_content=False)
            if not current or current["owner_device_id"] != owner: raise ValueError("file not owned by this device")
            encrypted_name = request.get("encrypted_name") or {}
            if set(encrypted_name.get("envelopes", {})) != set(current["payload"].get("envelopes", {})): raise ValueError("rename recipients differ")
            database.rename_encrypted_file(request["id"], owner, encrypted_name); return {"status": "RENAMED"}
        if action == "drive_delete":
            database.delete_encrypted_file(request["id"], owner); return {"status": "DELETED"}
        if action == "group_create":
            group_id = "group-" + uuid.uuid4().hex[:16]
            return database.create_group(group_id, request.get("name", "Group"), owner, request.get("members") or [])
        if action == "groups": return database.groups_for(owner)
        if action == "group_recipients":
            return database.encryption_recipients(database.group_member_ids(request["group_id"], owner))
        if action == "policy": return database.get_policy()
        if action == "set-policy":
            if not is_admin: raise ValueError("host policy can only be changed on the host computer")
            return database.update_policy(request.get("changes") or {})
        if action == "deployment_requests":
            if not is_admin: raise ValueError("host only")
            return database.pending_requests()
        if action == "review_deployment":
            if not is_admin: raise ValueError("host only")
            pending = database.deployment_request(int(request["request_id"]))
            if not pending: raise ValueError("pending request not found")
            from core.system.deployment import inspect_archive
            review = inspect_archive(Path(pending["archive_path"]))
            review["request"] = pending
            review["notes"] = review_notes(Path(pending["archive_path"]))
            return review
        if action in {"approve_deployment", "deny_deployment"}:
            if not is_admin: raise ValueError("host only")
            pending = database.deployment_request(int(request["request_id"]))
            if not pending or pending["status"] != "PENDING": raise ValueError("pending request not found")
            archive_path = Path(pending["archive_path"])
            try:
                if action == "approve_deployment":
                    result = self.server.manager.deploy(pending["device_id"], archive_path)
                    database.decide_request(pending["id"], "APPROVED")
                    return result
                database.decide_request(pending["id"], "DENIED"); return {"status": "DENIED"}
            finally: archive_path.unlink(missing_ok=True)
        raise ValueError("unsupported action")

    def is_host(self, device: dict) -> bool:
        """Whether this connection is the person running the server.

        Two things must hold: the connection comes from this machine, and the
        device is the one the server was started by. The address alone is not
        enough, because every hosted application also runs on this machine and
        would otherwise be an administrator the moment it learned the token.
        """
        if self.client_address[0] not in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}: return False
        expected = getattr(self.server, "admin_device", "") or ""
        # An older server started without an admin device keeps the previous
        # behaviour rather than locking its owner out of their own host.
        return not expected or device["id"] == expected

    def owned(self, deployment_id: str, owner: str, is_admin: bool) -> dict:
        item = self.server.manager.database.get(deployment_id)
        if not item: raise ValueError("deployment not found")
        if not is_admin and item["owner"] != owner:
            raise ValueError("only the device that deployed an application can control it")
        return item


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    server_name = "Stratus Server"
    lan_address = "127.0.0.1"
    admin_device = ""


RISKY_PATTERNS = (
    ("reads files outside its own folder", (b"/etc/passwd", b"/etc/shadow", b".ssh", b"id_rsa", b"device.json", b"known-peers")),
    ("starts other programs", (b"subprocess", b"os.system", b"child_process", b"popen", b"execSync")),
    ("sends data somewhere else", (b"requests.post", b"urllib.request", b"http.client", b"fetch(", b"axios", b"curl ", b"wget ")),
    ("reaches Stratus's own storage", (b"StratusData", b"stratus.db", b"STRATUS_PLATFORM_DB")),
    ("runs code built at runtime", (b"eval(", b"exec(", b"base64.b64decode", b"Function(")),
)


def review_notes(archive: Path) -> list[str]:
    """Point out what a host should look at before approving an upload.

    This is not virus scanning and does not pretend to be: fresh malicious code
    matches no signature. It reports which files do things worth a second look,
    so approval is an informed decision instead of a guess at a filename.
    """
    notes: dict[str, set[str]] = {}
    try:
        with tarfile.open(archive, "r:*") as bundle:
            for member in bundle.getmembers():
                if not member.isfile() or member.size > 512 * 1024: continue
                if Path(member.name).suffix.lower() not in {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".html", ""}: continue
                try: body = bundle.extractfile(member).read(512 * 1024)
                except Exception: continue
                for label, needles in RISKY_PATTERNS:
                    if any(needle in body for needle in needles): notes.setdefault(label, set()).add(member.name)
    except (tarfile.TarError, OSError) as exc:
        return [f"The upload could not be inspected: {exc}"]
    return [f"{label}: {', '.join(sorted(files)[:6])}" for label, files in sorted(notes.items())]


def builtin_sources(root: Path) -> list[Path]:
    """The modules every built-in website carries a copy of."""
    return [root / "core" / "platform" / "database.py", root / "core" / "platform" / "platform_api.py"]


def source_digest(folder: Path, shared: list[Path]) -> str:
    """A fingerprint of exactly what would be deployed.

    Comparing this against the digest recorded at install time is how a newer
    Stratus notices that its built-in websites have changed and need replacing.
    Both the file names and their contents are hashed, so a renamed or deleted
    file counts as a change just as much as an edited one.
    """
    digest = hashlib.sha256()
    entries = sorted((path.relative_to(folder).as_posix(), path)
                     for path in folder.rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    entries += [(f"_stratus/{path.name}", path) for path in shared if path.is_file()]
    for name, path in entries:
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def bundle_builtin(folder: Path, shared: list[Path], archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as bundle:
        for path in folder.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                bundle.add(path, arcname=path.relative_to(folder))
        for path in shared:
            if path.is_file(): bundle.add(path, arcname=f"_stratus/{path.name}")


def install_builtins(manager: DeploymentManager) -> None:
    """Install the built-in websites, or update them when Stratus has changed.

    These ship with the project rather than being uploaded, so a server created
    with an older Stratus would otherwise keep running that older copy forever —
    no amount of updating the checkout would reach it.
    """
    existing = {item["name"]: item for item in manager.database.list_all()}
    root = Path(__file__).resolve().parent.parent
    web = root / "web"
    shared = builtin_sources(root)

    for folder, name in (("chat", "stratus-chat"), ("drive", "stratus-drive")):
        source = web / folder
        if not source.is_dir(): continue
        digest = source_digest(source, shared)
        current = existing.get(name)

        if current:
            marker = Path(current["source_path"]).parent / "source.digest"
            try: recorded = marker.read_text(encoding="utf-8").strip()
            except OSError: recorded = ""
            if recorded == digest: continue
            action = "Updating"
        else:
            action = "Installing"

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as temp:
            archive = Path(temp.name)
        try:
            bundle_builtin(source, shared, archive)
            print(f"{action} built-in application: {name}", flush=True)
            if current:
                deployed = manager.replace_source(current["id"], archive)
            else:
                deployed = manager.deploy("system", archive)
            marker = Path(deployed["source_path"]).parent / "source.digest"
            marker.write_text(digest, encoding="utf-8")
        except Exception as exc:
            print(f"Built-in {action.lower()} failed for {name}: {exc}", flush=True)
        finally:
            archive.unlink(missing_ok=True)


def monitor_deployments(manager: DeploymentManager, stop: threading.Event) -> None:
    while not stop.wait(30):
        try: manager.monitor_once()
        except Exception as exc: print(f"Monitor warning: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default="0.0.0.0"); parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--storage", required=True)
    parser.add_argument("--token", help="join token (visible in the process list; prefer --token-file)")
    parser.add_argument("--token-file", help="file holding the join token, readable only by the host user")
    parser.add_argument("--admin-device", default="",
                        help="device id allowed to administer this server from this machine")
    parser.add_argument("--port-min", type=int, default=8000); parser.add_argument("--port-max", type=int, default=8999)
    parser.add_argument("--max-upload-mb", type=int, default=10)
    parser.add_argument("--server-name", default="Stratus Server")
    parser.add_argument("--discovery-port", type=int, default=5051)
    args = parser.parse_args()
    # A token on the command line is readable by every process of this user
    # through /proc, so a file is preferred and the argument stays only for
    # compatibility with an already-installed service unit.
    if args.token_file:
        token = Path(args.token_file).expanduser().read_text(encoding="utf-8").strip()
    elif args.token is not None:
        token = args.token
    else:
        parser.error("either --token or --token-file is required")
    storage = Path(args.storage).expanduser().resolve()
    if storage == Path("/"): parser.error("--storage cannot be /")
    storage.mkdir(parents=True, exist_ok=True)
    validate_server_storage(storage)
    if not storage.is_dir() or not storage.stat(): parser.error("storage is unavailable")
    db = Database(storage / "platform" / "stratus.db")
    with ThreadedServer((args.bind, args.port), ControlHandler) as server:
        server.token, server.upload_dir, server.max_upload = token, storage / "uploads", args.max_upload_mb * 1024 * 1024
        server.admin_device = args.admin_device
        server.server_name = args.server_name
        server.lan_address = lan_address()
        server.upload_dir.mkdir(parents=True, exist_ok=True)
        server.manager = DeploymentManager(storage, db, args.port_min, args.port_max)
        install_builtins(server.manager)
        server.manager.restore_auto_start()
        discovery_stop = threading.Event()
        discovery_thread = threading.Thread(target=serve_discovery, args=(args.server_name, args.port, args.discovery_port, discovery_stop), daemon=True)
        discovery_thread.start()
        monitor_thread = threading.Thread(target=monitor_deployments, args=(server.manager, discovery_stop), daemon=True)
        monitor_thread.start()
        print(f"Stratus host listening on {args.bind}:{args.port}; storage={storage}", flush=True)
        print(f"Devices on this network reach it at {server.lan_address}:{args.port}", flush=True)
        try: server.serve_forever()
        finally: discovery_stop.set(); db.close()


if __name__ == "__main__": main()
