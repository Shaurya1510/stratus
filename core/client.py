from __future__ import annotations

import argparse
import base64
import json
import socket
import tarfile
import tempfile
from pathlib import Path

from core.platform.protocol import receive_message, send_message
from core.security.identity import load_or_create_identity, public_identity


def bundle_project(project: Path) -> bytes:
    if not project.is_dir(): raise ValueError("project directory does not exist")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as temp:
        archive = Path(temp.name)
    try:
        with tarfile.open(archive, "w:gz") as output:
            for path in project.rglob("*"):
                if ".git" not in path.parts and path.is_file(): output.add(path, arcname=path.relative_to(project))
        return archive.read_bytes()
    finally: archive.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True); parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--token", required=True); parser.add_argument("--owner", default="demo")
    parser.add_argument("action", choices=["list", "deploy", "start", "stop", "restart", "status", "logs", "devices", "policy", "set-policy"])
    parser.add_argument("argument", nargs="?")
    args = parser.parse_args()
    identity = load_or_create_identity()
    request = {"action": args.action, "token": args.token, "owner": identity["id"], "device": public_identity(identity)}
    if args.action == "deploy":
        if not args.argument: parser.error("deploy needs a project directory")
        project = Path(args.argument)
        request["archive"] = base64.b64encode(bundle_project(project)).decode("ascii")
        manifest = project / "stratus.yaml"
        values = dict(line.split(":", 1) for line in manifest.read_text(encoding="utf-8").splitlines() if ":" in line and not line.lstrip().startswith("#"))
        request["runtime"] = values.get("runtime", "").strip()
        request["app_name"] = values.get("name", project.name).strip()
    elif args.action == "set-policy":
        if not args.argument: parser.error("set-policy needs a JSON object")
        request["changes"] = json.loads(args.argument)
    elif args.action not in {"list", "devices", "policy"}:
        if not args.argument: parser.error(f"{args.action} needs a deployment id")
        request["id"] = args.argument
    with socket.create_connection((args.host, args.port), timeout=15) as sock:
        send_message(sock, request); reply = receive_message(sock)
    if not reply.get("ok"):
        raise SystemExit(f"Stratus error: {reply.get('error', 'unknown error')}")
    print(json.dumps(reply["data"], indent=2))


if __name__ == "__main__": main()
