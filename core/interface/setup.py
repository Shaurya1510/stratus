from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from core.system.presets import PRESETS
from core.system import registry
from core.system.desktop import open_terminal
from core.system.storage import choose_storage, discover_storage, initialize_server_root

def slugify(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return value[:48] or "stratus-server"


def open_dashboard(root: Path, name: str) -> bool:
    return open_terminal(f"Stratus Host — {name}", "./stratus.sh admin", root)


def apply_starting_policy(port: int, token: str, preset: str) -> None:
    """The host chose a preset before the server existed; apply it now."""
    from core.platform.session import Session
    session = Session("127.0.0.1", port, token, timeout=5)
    try: session.call("set-policy", changes=PRESETS[preset])
    except Exception as exc: print(f"Could not apply the {preset} preset yet: {exc}")
    finally: session.close()


def free_port(preferred: int = 5050) -> int:
    """The control port, or the next free one if something else has it."""
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def read_log_tail(log_path: Path, lines: int = 20) -> str:
    try:
        return "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def echo_startup(log_path: Path, seconds: float = 12.0) -> None:
    """Show what the server says while it starts.

    Installing or updating the built-in websites happens here and can take a
    few seconds. Printing it means the person watching sees progress and, more
    importantly, sees anything that went wrong instead of a silent prompt.
    """
    interesting = ("Installing built-in", "Updating built-in", "failed", "Error", "error",
                   "listening on", "reach it at", "Traceback")
    shown: set[str] = set()
    deadline = time.monotonic() + seconds
    settled = 0
    while time.monotonic() < deadline:
        new = False
        for line in read_log_tail(log_path, 200).splitlines():
            if line in shown or not any(word in line for word in interesting): continue
            shown.add(line)
            print(f"  {line}")
            new = True
        # Stop early once the log has been quiet for a moment and the server has
        # announced itself, so starting stays quick when there is nothing to do.
        settled = 0 if new else settled + 1
        if settled >= 3 and any("listening on" in line for line in shown): return
        time.sleep(0.4)


def launch(root: Path, name: str, server_root: Path, token: str, port: int, preset: str | None) -> None:
    """Start the control server and stay attached until it stops."""
    running = registry.running_server()
    if running:
        raise SystemExit(
            f"'{running.get('name')}' is already running as PID {running.get('pid')}.\n"
            "Only one server may run at a time. Stop it first (Ctrl+C in its terminal, "
            "or ./stratus.sh service stop).")

    # The token goes in a file under the host's own config directory rather
    # than on the command line: every process of this user can read another's
    # arguments through /proc, and hosted applications run as this user.
    # Confined applications cannot see this directory at all.
    from core.security.identity import load_or_create_identity
    token_path = registry.CONFIG_DIR / "tokens" / f"{server_root.name}.token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token, encoding="utf-8")
    try: token_path.chmod(0o600)
    except OSError: pass

    command = [sys.executable, "-m", "core.server", "--bind", "0.0.0.0", "--port", str(port),
               "--storage", str(server_root), "--token-file", str(token_path),
               "--admin-device", load_or_create_identity()["id"], "--server-name", name]
    log_path = server_root / "logs" / "stratus.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    print(f"\nServer data: {server_root}\nStarting {name}…")
    process = subprocess.Popen(command, cwd=str(root), stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    entry = {"name": name, "storage": str(server_root), "token": token, "port": port,
             "pid": process.pid, "log": str(log_path)}
    registry.set_active(entry)

    time.sleep(1)
    if process.poll() is not None:
        registry.clear_active()
        print(read_log_tail(log_path, 20))
        raise SystemExit(f"Stratus failed to start. Full log: {log_path}")

    # The server writes to its log, not to this terminal, so anything it says
    # while starting up — installing the built-in websites, refusing a drive,
    # a port already taken — would otherwise be invisible to the person who
    # just pressed start.
    echo_startup(log_path)

    if preset: apply_starting_policy(port, token, preset)
    if open_dashboard(root, name): print("Host dashboard opened in a new terminal window.")
    else: print("Open another terminal and run: ./stratus.sh admin")
    print(f"\n{name} is running as PID {process.pid} on port {port}.")
    print("Members join with: ./stratus.sh terminal")
    print(f"Logs: {log_path}\nPress Ctrl+C here to stop the server.")
    try: process.wait()
    except KeyboardInterrupt:
        print("\nStopping Stratus…")
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired: process.kill()
    finally:
        registry.clear_active()


def create_server(root: Path, name: str, selection: dict, token: str, preset: str, port: int = 5050) -> None:
    server_root = initialize_server_root(selection, slugify(name), name=name, port=port)
    launch(root, name, server_root, token, free_port(port), preset)


def open_server(root: Path, server: dict) -> None:
    storage = Path(server["storage"])
    if not storage.is_dir():
        raise SystemExit(f"That server's drive is not connected: {storage}")
    launch(root, server.get("name", storage.name), storage, server.get("token", ""),
           free_port(int(server.get("port", 5050))), None)


def text_menu(root: Path) -> None:
    while True:
        servers = registry.known_servers()
        print("\nSTRATUS\n")
        if servers:
            print("Existing servers on this machine:")
            for index, item in enumerate(servers, 1):
                mark = "" if Path(item["storage"]).is_dir() else "   (drive not connected)"
                print(f"  {index}. {item.get('name', 'unnamed')}{mark}")
            print()
        print("N. Host a new server\nJ. Join a server\nX. Exit")
        choice = input("Choose: ").strip().lower()
        if choice == "x": return
        if choice == "j": join(root); return
        if choice == "n":
            name = input("Server name [My Stratus Server]: ").strip() or "My Stratus Server"
            selection = choose_storage()
            token = input("Join token (leave blank for an open server): ").strip()
            create_server(root, name, selection, token, "medium"); return
        if choice.isdigit() and 1 <= int(choice) <= len(servers):
            open_server(root, servers[int(choice) - 1]); return


def join(root: Path) -> None:
    os.execv(sys.executable, [sys.executable, "-m", "core.interface.terminal"])


def main() -> None:
    if not sys.platform.startswith("linux"): raise SystemExit("Stratus supports Linux only.")
    root = Path(__file__).resolve().parents[2]
    try:
        from core.interface.app import SetupTUI
    except ImportError:
        SetupTUI = None
    if SetupTUI is None:
        print("Textual is unavailable; using the basic compatibility menu.")
        text_menu(root); return
    app = SetupTUI(discover_storage)
    app.run()
    result = app.result
    if not result: return
    if result["mode"] == "join": join(root)
    elif result["mode"] == "open": open_server(root, result["server"])
    else: create_server(root, result["name"], result["selection"], result["token"], result["preset"])


if __name__ == "__main__": main()
