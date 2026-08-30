from __future__ import annotations

import getpass
import json
import platform
import secrets
import socket
import uuid
import hashlib
from pathlib import Path

PUBLIC_FIELDS = {"id", "display_name", "hostname", "platform", "secret", "encryption_public_key", "ecdh_public_key"}


def identity_path() -> Path:
    return Path.home() / ".config" / "stratus" / "device.json"


def _write(path: Path, identity: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(identity, indent=2), encoding="utf-8")
    try: path.chmod(0o600)
    except OSError: pass


def load_or_create_identity() -> dict:
    from core.security.e2ee import generate_ecdh_keypair, generate_keypair
    path = identity_path()
    if path.exists():
        identity = json.loads(path.read_text(encoding="utf-8"))
        changed = False
        if "encryption_private_key" not in identity:
            identity["encryption_private_key"], identity["encryption_public_key"] = generate_keypair(); changed = True
        # Devices created before browser support have no P-256 key yet.
        if "ecdh_private_key" not in identity:
            identity["ecdh_private_key"], identity["ecdh_public_key"] = generate_ecdh_keypair(); changed = True
        if changed: _write(path, identity)
        return identity
    hostname = socket.gethostname()
    private_key, public_key = generate_keypair()
    ecdh_private, ecdh_public = generate_ecdh_keypair()
    identity = {
        "id": f"device-{uuid.uuid4().hex[:12]}",
        "display_name": f"{getpass.getuser()}@{hostname}",
        "hostname": hostname,
        "platform": platform.system().lower(),
        "secret": secrets.token_hex(32),
        "encryption_private_key": private_key,
        "encryption_public_key": public_key,
        "ecdh_private_key": ecdh_private,
        "ecdh_public_key": ecdh_public,
    }
    _write(path, identity)
    return identity


def public_identity(identity: dict) -> dict:
    return {key: identity[key] for key in PUBLIC_FIELDS if key in identity}


def key_fingerprint(public_key: str) -> str:
    digest = hashlib.sha256(public_key.encode()).hexdigest().upper()
    return " ".join(digest[index:index+4] for index in range(0, 32, 4))


def pin_peer_keys(recipients: list[dict], confirm_new: bool = False) -> None:
    """Trust on first use: a device's key must never silently change."""
    path = identity_path().with_name("known-peers.json")
    known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    changed = False
    for peer in recipients:
        key = peer.get("ecdh_public_key") or peer.get("encryption_public_key")
        if not key: continue
        existing = known.get(peer["id"])
        if existing and existing["public_key"] != key:
            raise RuntimeError(f"SECURITY WARNING: encryption key changed for {peer['display_name']}; refusing to continue")
        if not existing:
            fingerprint = key_fingerprint(key)
            if confirm_new:
                print(f"New device: {peer['display_name']}\nEncryption fingerprint: {fingerprint}")
                if input("Trust this key? [y/N]: ").strip().lower() != "y": raise RuntimeError("device key was not trusted")
            known[peer["id"]] = {"display_name": peer["display_name"], "public_key": key, "fingerprint": fingerprint}
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(known, indent=2), encoding="utf-8")
        try: path.chmod(0o600)
        except OSError: pass
