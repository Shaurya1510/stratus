from __future__ import annotations

import os
import json
import re
import shutil
import socket
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

from core.system import sandbox
from core.platform.database import Database

ALLOWED_RUNTIMES = {"static", "python", "node"}
BUILD_TIMEOUT_SECONDS = 600

# The upload limit caps the compressed archive; these cap what it may become
# once unpacked, so a small file cannot fill the host's drive.
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20_000


def parse_manifest(project: Path) -> dict[str, str]:
    manifest = project / "stratus.yaml"
    if not manifest.is_file():
        raise ValueError("stratus.yaml is required")
    fields: dict[str, str] = {}
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            raise ValueError("manifest lines must be key: value")
        key, value = raw.split(":", 1)
        fields[key.strip()] = value.strip().strip('"\'')
    for required in ("name", "runtime", "start"):
        if not fields.get(required):
            raise ValueError(f"manifest missing {required}")
    if fields["runtime"] not in ALLOWED_RUNTIMES:
        raise ValueError("runtime must be static, python or node")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}", fields["name"]):
        raise ValueError("manifest name contains unsupported characters")
    return fields


def safe_extract(archive: Path, destination: Path,
                 max_bytes: int = MAX_EXTRACTED_BYTES, max_entries: int = MAX_ARCHIVE_ENTRIES) -> None:
    """Unpack an upload, refusing anything that could damage the host.

    Three separate dangers are checked before a single byte is written:

    * Path traversal — an entry named ``../../.ssh/authorized_keys`` would
      otherwise overwrite exactly that file.
    * Links — a symlink or hardlink pointing outside the destination turns a
      later write into a write anywhere on the filesystem.
    * Decompression bombs — the upload limit caps the *compressed* size, and a
      few megabytes of gzip can expand to many gigabytes. The declared sizes in
      the archive header are summed first, so an oversized archive is rejected
      rather than filling the drive during extraction.
    """
    total = 0
    with tarfile.open(archive, "r:*") as bundle:
        members = bundle.getmembers()
        if len(members) > max_entries:
            raise ValueError(f"archive contains {len(members)} entries; the limit is {max_entries}")
        for member in members:
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or member.issym() or member.islnk():
                raise ValueError("unsafe archive entry")
            if member.size < 0:
                raise ValueError("archive declares a negative file size")
            total += member.size
            if total > max_bytes:
                raise ValueError(
                    f"archive expands to more than {max_bytes // (1024 * 1024)} MB; "
                    "refusing to extract it")
        bundle.extractall(destination, filter="data")


def inspect_archive(archive: Path) -> dict:
    """Summarise an upload so a host can approve it knowing what is inside."""
    files, total, dependencies, flags = [], 0, [], []
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle.getmembers():
            if member.issym() or member.islnk(): flags.append(f"archive contains a link: {member.name}")
            if member.mode & 0o4000 or member.mode & 0o2000: flags.append(f"setuid/setgid file: {member.name}")
            if not member.isfile(): continue
            files.append({"name": member.name, "size": member.size})
            total += member.size
            if Path(member.name).name in {"requirements.txt", "package.json"}:
                try: dependencies.append({"file": member.name, "body": bundle.extractfile(member).read(65536).decode("utf-8", "replace")})
                except Exception: pass
    files.sort(key=lambda item: -item["size"])
    return {"file_count": len(files), "total_bytes": total, "files": files[:200], "dependencies": dependencies, "flags": flags}


class DeploymentManager:
    def __init__(self, storage: Path, database: Database, port_min: int, port_max: int):
        self.storage = storage.resolve()
        if self.storage == Path("/"):
            raise ValueError("storage root cannot be /")
        self.database, self.port_min, self.port_max = database, port_min, port_max
        # Python virtual environments and node_modules need Unix symlinks and
        # executable bits. External demo disks may be exFAT, so keep regenerable
        # runtime files on the native filesystem while source and persistent app
        # data stay on the selected Stratus storage disk.
        self.runtime_root = Path(
            os.environ.get(
                "STRATUS_RUNTIME_ROOT",
                Path.home() / ".local" / "share" / "stratus" / "runtimes",
            )
        ).expanduser().resolve()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.sandboxed = sandbox.available()
        for name in ("deployments", "app-data", "uploads", "logs", "backups"):
            (self.storage / name).mkdir(parents=True, exist_ok=True)

    def _allocate_port(self) -> int:
        for port in range(self.port_min, self.port_max + 1):
            if self.database.port_is_reserved(port):
                continue
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return port
        raise RuntimeError("no free application port available")

    def deploy(self, owner: str, archive: Path) -> dict:
        deployment_id = uuid.uuid4().hex[:12]
        root = self.storage / "deployments" / deployment_id
        source = root / "source"
        source.mkdir(parents=True)
        # A guest may not unpack more than the persistent-data allowance the
        # host granted it; built-in applications use the standard ceiling.
        budget = MAX_EXTRACTED_BYTES
        if owner != "system":
            budget = min(budget, max(self.database.get_policy()["max_data_mb"], 1) * 1024 * 1024)
        safe_extract(archive, source, max_bytes=budget)
        children = list(source.iterdir())
        if len(children) == 1 and children[0].is_dir() and not (source / "stratus.yaml").exists():
            inner = children[0]
            for item in inner.iterdir():
                shutil.move(str(item), source)
            inner.rmdir()
        manifest = parse_manifest(source)
        port = self._allocate_port()
        data = self.storage / "app-data" / deployment_id
        data.mkdir(parents=True)
        values = {"id": deployment_id, "owner": owner, "name": manifest["name"], "runtime": manifest["runtime"], "source_path": str(source), "data_path": str(data), "port": port, "start_command": manifest["start"], "auto_start": int(manifest.get("auto_start", "false").lower() == "true")}
        self.database.create(values)
        owner_deployments = self.storage / "users" / owner / "deployments"
        owner_deployments.mkdir(parents=True, exist_ok=True)
        (owner_deployments / f"{deployment_id}.json").write_text(json.dumps(values, indent=2), encoding="utf-8")
        try:
            self.build(deployment_id, manifest, source, root, guest=owner != "system")
            self.start(deployment_id)
            return self.database.get(deployment_id) or values
        except Exception:
            self.database.update_state(deployment_id, "BUILD_FAILED", None, "STOPPED")
            raise

    def build(self, deployment_id: str, manifest: dict, source: Path, root: Path,
              guest: bool = True) -> None:
        """Install dependencies and run the project's declared build step.

        Everything here executes code that came out of the upload, so for a
        guest project it all runs confined. The built-in websites are part of
        Stratus itself and are built directly.
        """
        build_log = root / "build.log"
        runtime_home = self.runtime_root / deployment_id
        runtime_home.mkdir(parents=True, exist_ok=True)
        confined = guest and self.sandboxed

        def step(command, shell: bool = False) -> None:
            if confined:
                self.run_confined_build(deployment_id, command, source, runtime_home, build_log)
                return
            with build_log.open("a", encoding="utf-8") as log:
                subprocess.run(command, cwd=source, shell=shell, check=True, stdout=log,
                               stderr=subprocess.STDOUT, timeout=BUILD_TIMEOUT_SECONDS)

        if manifest["runtime"] == "python" and (source / "requirements.txt").exists():
            venv = runtime_home / "venv"
            # Creating the environment runs no project code, so it is done
            # directly; installing into it does, so that step is confined.
            subprocess.run(["python3", "-m", "venv", str(venv)], check=True, stdout=subprocess.DEVNULL, timeout=BUILD_TIMEOUT_SECONDS)
            step([str(venv / "bin" / "pip"), "install", "-r", "requirements.txt"])
        if manifest["runtime"] == "node":
            if not shutil.which("npm"):
                raise RuntimeError("this host has no Node.js installed; ask the host to install nodejs and npm")
            if (source / "package.json").exists():
                step(["npm", "ci"] if (source / "package-lock.json").exists() else ["npm", "install"])
        build_command = manifest.get("build", "").strip()
        if build_command:
            step(build_command, shell=True)

    def run_confined_build(self, deployment_id: str, command, source: Path,
                           runtime_home: Path, build_log: Path) -> None:
        """One build step inside the sandbox, with its output kept for the log."""
        environment = {"HOME": str(runtime_home), "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
        completed = sandbox.run_build(deployment_id, command, environment,
                                      sandbox.build_properties(source, runtime_home),
                                      source, BUILD_TIMEOUT_SECONDS)
        with build_log.open("a", encoding="utf-8") as log:
            log.write(completed.stdout or "")
            log.write(completed.stderr or "")
        if completed.returncode != 0:
            self.note(build_log, f"build step failed inside the sandbox (exit {completed.returncode})")
            raise subprocess.CalledProcessError(completed.returncode, command)

    def environment(self, item: dict) -> dict:
        env = {
            "PORT": str(item["port"]),
            "STRATUS_DATA_DIR": item["data_path"],
            "HOME": item["data_path"],
            "NODE_ENV": "production",
        }
        if item["owner"] == "system":
            # Only the built-in applications may reach the platform database. A
            # guest application with this path could read every device record and
            # rewrite the hosting policy, so it never receives it.
            env["STRATUS_PLATFORM_DB"] = str(self.database.path)
            env["STRATUS_CONTROL_PATH"] = str(Path(item["source_path"]) / "_stratus")
            env["STRATUS_DEPLOYMENT_ID"] = item["id"]
        venv_bin = self.runtime_root / item["id"] / "venv" / "bin"
        path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        if item["runtime"] == "python" and (venv_bin / "python").exists(): path = f"{venv_bin}:{path}"
        if item["runtime"] == "node": path = f"{Path(item['source_path']) / 'node_modules' / '.bin'}:{path}"
        env["PATH"] = path
        return env

    def replace_source(self, deployment_id: str, archive: Path) -> dict:
        """Update an application in place from a new archive.

        The built-in websites ship with Stratus, so a newer Stratus must be able
        to replace the copy that was unpacked when the server was created.
        Redeploying from scratch would hand out a new port and a new data
        directory; updating in place keeps the identifier, the port and
        everything the application has stored, and only swaps the code.
        """
        item = self.database.get(deployment_id)
        if not item: raise ValueError("deployment not found")
        source = Path(item["source_path"])
        root = source.parent

        try: self.stop(deployment_id)
        except Exception: pass

        staging = root / "source.incoming"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        safe_extract(archive, staging)
        children = list(staging.iterdir())
        if len(children) == 1 and children[0].is_dir() and not (staging / "stratus.yaml").exists():
            inner = children[0]
            for entry in inner.iterdir(): shutil.move(str(entry), staging)
            inner.rmdir()
        manifest = parse_manifest(staging)

        # Only swap once the new copy is known good, so a bad update cannot
        # leave the application with no source at all.
        retired = root / "source.previous"
        shutil.rmtree(retired, ignore_errors=True)
        if source.exists(): source.rename(retired)
        staging.rename(source)
        shutil.rmtree(retired, ignore_errors=True)

        with self.database.lock:
            self.database.conn.execute(
                "UPDATE deployments SET runtime=?, start_command=?, auto_start=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (manifest["runtime"], manifest["start"],
                 int(manifest.get("auto_start", "false").lower() == "true"), deployment_id))
            self.database.event(deployment_id, "UPDATED", "source replaced")
            self.database.conn.commit()

        self.build(deployment_id, manifest, source, root, guest=item["owner"] != "system")
        return self.start(deployment_id)

    def start(self, deployment_id: str) -> dict:
        item = self.database.get(deployment_id)
        if not item:
            raise ValueError("deployment not found")
        if self.status_is_live(item):
            return item
        root = Path(item["source_path"]).parent
        command = item["start_command"].replace("$PORT", str(item["port"]))
        runtime_log = root / "runtime.log"
        env = self.environment(item)
        guest = item["owner"] != "system"
        if guest and self.sandboxed:
            pid = self.start_confined(item, command, env, runtime_log)
        else:
            pid = self.start_plain(item, command, env, runtime_log, guest)
        if pid is None:
            self.database.update_state(deployment_id, "CRASHED", None, "STOPPED")
            raise RuntimeError("application exited during startup; read runtime.log")
        self.database.update_state(deployment_id, "RUNNING", pid, "RUNNING")
        return self.database.get(deployment_id) or item

    def start_confined(self, item: dict, command: str, env: dict, runtime_log: Path) -> int | None:
        """Run as a transient user service that can only see its own folders."""
        policy = self.database.get_policy()
        source, data = Path(item["source_path"]), Path(item["data_path"])
        runtime_home = self.runtime_root / item["id"]
        resources = sandbox.resource_properties(policy["max_memory_mb"], policy["max_cpu_percent"])
        # The second tier keeps CPU and memory limits but has no filesystem
        # isolation, so guest code could read the host's home directory and the
        # device key with it. That is only attempted when the host has
        # explicitly accepted the risk; otherwise the application is refused.
        attempts = [sandbox.confinement_properties(source, data, runtime_home) + resources]
        if not policy.get("require_confinement", 1):
            attempts.append(resources)
        sandbox.stop_unit(item["id"])
        for index, properties in enumerate(attempts):
            launch = sandbox.build_command(item["id"], command, env, properties, source)
            completed = subprocess.run(launch, capture_output=True, text=True)
            if completed.returncode == 0:
                if index == 1:
                    self.note(runtime_log, "WARNING: full confinement could not be applied on this host. "
                                           "Only CPU and memory limits are active, so this application can "
                                           "read the hosting user's files. The host allowed this by turning "
                                           "off require_confinement.")
                time.sleep(0.4)
                pid = sandbox.main_pid(item["id"])
                if pid and sandbox.is_active(item["id"]): return pid
                sandbox.stop_unit(item["id"])
                return None
            self.note(runtime_log, f"systemd refused these limits: {(completed.stderr or '').strip()}")
            sandbox.stop_unit(item["id"])
        return None

    def start_plain(self, item: dict, command: str, env: dict, runtime_log: Path, guest: bool) -> int | None:
        if guest:
            if self.database.get_policy().get("require_confinement", 1):
                self.note(runtime_log, "REFUSED: this host cannot run confined services, and the hosting "
                                       "policy requires confinement for guest applications.")
                raise RuntimeError(
                    "this host cannot confine guest applications (no systemd user session). "
                    "Enable it with 'sudo loginctl enable-linger', or turn off require_confinement "
                    "in the hosting policy to accept the risk.")
            self.note(runtime_log, "WARNING: this host cannot run confined services, so this application "
                                   "runs with the same access as the person hosting it.")
        with runtime_log.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=item["source_path"], env=os.environ | env, shell=True,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(0.25)
        return None if process.poll() is not None else process.pid

    def note(self, runtime_log: Path, message: str) -> None:
        try:
            with runtime_log.open("a", encoding="utf-8") as log: log.write(f"[stratus] {message}\n")
        except OSError: pass

    def status_is_live(self, item: dict) -> bool:
        if item["owner"] != "system" and self.sandboxed and sandbox.is_active(item["id"]): return True
        return bool(item["pid"]) and Path(f"/proc/{item['pid']}").exists()

    def stop(self, deployment_id: str) -> dict:
        item = self.database.get(deployment_id)
        if not item:
            raise ValueError("deployment not found")
        if self.sandboxed: sandbox.stop_unit(deployment_id)
        pid = item["pid"]
        if pid and Path(f"/proc/{pid}").exists():
            try:
                os.killpg(pid, 15)
                for _ in range(20):
                    if not Path(f"/proc/{pid}").exists(): break
                    time.sleep(0.1)
                if Path(f"/proc/{pid}").exists(): os.killpg(pid, 9)
            except (ProcessLookupError, PermissionError): pass
        self.database.update_state(deployment_id, "STOPPED", None, "STOPPED")
        return self.database.get(deployment_id) or item

    def status(self, deployment_id: str) -> dict:
        item = self.database.get(deployment_id)
        if not item: raise ValueError("deployment not found")
        if item["pid"] and not self.status_is_live(item):
            self.database.update_state(deployment_id, "STOPPED", None)
            item = self.database.get(deployment_id)
        return item or {}

    def restore_auto_start(self) -> None:
        for item in self.database.list_all():
            if item["auto_start"] and item["status"] != "BUILD_FAILED":
                try: self.start(item["id"])
                except Exception:
                    self.database.update_state(item["id"], "CRASHED", None, "RUNNING")

    def monitor_once(self) -> None:
        policy = self.database.get_policy()
        quota = policy["max_data_mb"] * 1024 * 1024
        for item in self.database.list_all():
            if item["owner"] == "system":
                self.status(item["id"]); continue
            data_root = Path(item["data_path"])
            used = sum(path.stat().st_size for path in data_root.rglob("*") if path.is_file()) if data_root.exists() else 0
            if used > quota and item["status"] == "RUNNING":
                self.stop(item["id"])
                self.database.update_state(item["id"], "QUOTA_EXCEEDED", None, "STOPPED")
            else: self.status(item["id"])
