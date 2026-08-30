"""Confinement for guest applications.

Without this, an uploaded project runs as the person hosting it, with their whole
home directory in reach: SSH keys, documents, browser data, and
~/.config/stratus/device.json, which holds the private key protecting every
encrypted message on the server. A fifteen-line Flask app could quietly copy it.

systemd's own sandboxing closes that, and needs no root: a transient user
service can be told it may see only its own directories. Sandboxing applies to
services, not to `--scope` units, which is why applications are started as
transient units here.

Not every machine can do this — a bare SSH login without lingering has no user
manager — so `start_command` reports which tier it achieved and the caller can
tell the host plainly rather than pretending an app is confined when it is not.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

UNIT_PREFIX = "stratus-"


def unit_name(deployment_id: str) -> str:
    return f"{UNIT_PREFIX}{deployment_id}.service"


def available() -> bool:
    """Can this machine run transient user services at all?"""
    if not shutil.which("systemd-run") or not shutil.which("systemctl"): return False
    if not os.environ.get("XDG_RUNTIME_DIR"): return False
    try:
        completed = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return False
    # "degraded" and "starting" are still usable; only a missing manager is not.
    return completed.returncode == 0 or (completed.stdout or "").strip() in {"degraded", "starting", "running", "maintenance"}


def confinement_properties(source: Path, data: Path, runtime: Path | None) -> list[str]:
    """Let the application see its own three directories and nothing else."""
    readable = [str(source)]
    if runtime and runtime.exists(): readable.append(str(runtime))
    properties = [
        "ProtectHome=tmpfs",            # the host's documents and keys disappear
        "ProtectSystem=strict",         # the rest of the filesystem is read-only
        "ProtectProc=invisible",        # other processes are not listed in /proc
        "ProcSubset=pid",               # ...and neither is the rest of /proc
        "PrivateIPC=yes",
        f"ReadWritePaths={data}",       # except this application's own data
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "RestrictNamespaces=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "LockPersonality=yes",
        "UMask=0077",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "SystemCallFilter=@system-service",
        "SystemCallErrorNumber=EPERM",
    ]
    properties += [f"BindReadOnlyPaths={path}" for path in readable]
    properties.append(f"BindPaths={data}")
    return properties


def resource_properties(memory_mb: int, cpu_percent: int, tasks: int = 96) -> list[str]:
    return [f"MemoryMax={memory_mb}M", f"CPUQuota={cpu_percent}%", f"TasksMax={tasks}", "MemorySwapMax=0"]


def build_command(deployment_id: str, command: str, environment: dict, properties: list[str], working_directory: Path) -> list[str]:
    launch = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={unit_name(deployment_id)}",
              f"--working-directory={working_directory}"]
    for key, value in environment.items():
        launch += ["--setenv", f"{key}={value}"]
    for item in properties:
        launch += ["--property", item]
    launch += ["/bin/bash", "-lc", command]
    return launch


def build_properties(source: Path, runtime: Path) -> list[str]:
    """Confinement for a build step.

    A build has to write where a running application does not: into its own
    source tree, to unpack node_modules, and into the runtime folder, to create
    a virtual environment. Everything protecting the host is kept, above all
    ProtectHome, which is what keeps ~/.config/stratus/device.json out of reach
    of a package's install script.

    The system call filter is wider than the one used at run time because
    compilers and package managers legitimately fork, exec and change file
    modes; @system-service alone makes many wheels fail to build.
    """
    return [
        "ProtectHome=tmpfs",
        "ProtectSystem=strict",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        f"ReadWritePaths={source}",
        f"ReadWritePaths={runtime}",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "NoNewPrivileges=yes",
        "RestrictSUIDSGID=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectControlGroups=yes",
        "LockPersonality=yes",
        "UMask=0077",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",
        "SystemCallFilter=@system-service @process @file-system",
        "SystemCallErrorNumber=EPERM",
    ]


def run_build(deployment_id: str, command: list[str] | str, environment: dict,
              properties: list[str], working_directory: Path, timeout: float) -> subprocess.CompletedProcess:
    """Run one build command as a confined, synchronous transient unit.

    `--wait` makes systemd-run block until the unit finishes and adopt its exit
    status, so this behaves like an ordinary subprocess call while still being
    sandboxed. `--pipe` keeps the output on our own stdout/stderr instead of
    sending it to the journal, so the build log still reaches the person who
    uploaded the project.
    """
    launch = ["systemd-run", "--user", "--quiet", "--wait", "--pipe", "--collect",
              f"--unit={UNIT_PREFIX}build-{deployment_id}.service",
              f"--working-directory={working_directory}"]
    for key, value in environment.items():
        launch += ["--setenv", f"{key}={value}"]
    for item in properties:
        launch += ["--property", item]
    if isinstance(command, str):
        launch += ["/bin/bash", "-lc", command]
    else:
        launch += list(command)
    return subprocess.run(launch, capture_output=True, text=True, timeout=timeout)


def main_pid(deployment_id: str) -> int | None:
    try:
        completed = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", unit_name(deployment_id)],
                                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return None
    value = (completed.stdout or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def is_active(deployment_id: str) -> bool:
    try:
        completed = subprocess.run(["systemctl", "--user", "is-active", unit_name(deployment_id)],
                                   capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return False
    return (completed.stdout or "").strip() == "active"


def stop_unit(deployment_id: str) -> None:
    for verb in ("stop", "reset-failed"):
        try: subprocess.run(["systemctl", "--user", verb, unit_name(deployment_id)], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError): pass
