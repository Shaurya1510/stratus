"""Stratus Chat — the browser view of the same encrypted conversations.

This app never sees message text. The browser holds the device key (unsealed
locally with the member's passphrase), does all encryption and decryption with
WebCrypto, and this server only stores and returns ciphertext plus the list of
device ids each envelope was sealed for — exactly like the terminal client.

Membership rules are not reimplemented here; they come from the same
platform_api module the control server uses, so the website cannot accidentally
be more permissive than the terminal.
"""
from __future__ import annotations

import os
import secrets
import sys
from functools import wraps
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

CONTROL_PATH = os.environ.get("STRATUS_CONTROL_PATH") or str(Path(__file__).resolve().parent / "_stratus")
sys.path.insert(0, CONTROL_PATH)

from database import Database          # noqa: E402  (path must be set first)
import platform_api                    # noqa: E402

DATA_DIR = Path(os.environ.get("STRATUS_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PLATFORM_DB = Path(os.environ.get("STRATUS_PLATFORM_DB", ""))
# A sign-in ticket is issued for one application; this is how we know
# whether a presented ticket was meant for us.
DEPLOYMENT_ID = os.environ.get("STRATUS_DEPLOYMENT_ID") or None

app = Flask(__name__)


def session_secret() -> bytes:
    """A per-server signing key, kept out of the source tree."""
    path = DATA_DIR / "session.key"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        try: path.chmod(0o600)
        except OSError: pass
    return path.read_bytes()


app.secret_key = session_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=16 * 1024 * 1024)

def static_version() -> str:
    """A short fingerprint of the static files, used to bust browser caches.

    Stylesheets and scripts keep their names across updates, so a browser will
    happily keep serving the copy it cached before the update and the new code
    never runs. Appending this to the URL makes the address change whenever the
    files do.
    """
    import hashlib
    digest = hashlib.sha256()
    folder = Path(__file__).resolve().parent / "static"
    for path in sorted(folder.glob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(str(path.stat().st_mtime_ns).encode())
    return digest.hexdigest()[:12]


STATIC_VERSION = static_version()


@app.context_processor
def inject_static_version():
    return {"v": STATIC_VERSION}


_database: Database | None = None


def platform() -> Database:
    global _database
    if _database is None:
        if not PLATFORM_DB.is_file():
            raise RuntimeError("This app must be started by Stratus so it can reach the server database.")
        _database = Database(PLATFORM_DB)
    return _database


def device_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "device_id" not in session:
            if request.path.startswith("/api/"): return jsonify(error="not signed in"), 401
            return redirect(url_for("welcome"))
        return view(*args, **kwargs)
    return wrapped


def current_device() -> dict:
    for item in platform().list_devices():
        if item["id"] == session["device_id"]: return item
    session.clear()
    abort(403)


@app.get("/device-login")
def device_login():
    """Redeem the single-use ticket the terminal generated."""
    device_id = platform().redeem_app_ticket(request.args.get("ticket", ""), DEPLOYMENT_ID)
    if not device_id:
        return render_template("welcome.html", error="That sign-in link has expired. Open Chat from the Stratus terminal again."), 403
    session.clear()
    session["device_id"] = device_id
    return redirect(url_for("home"))


@app.get("/welcome")
def welcome():
    return render_template("welcome.html", error=None)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("welcome"))


@app.get("/")
@device_required
def home():
    device = current_device()
    return render_template("chat.html", device=device)


@app.get("/api/session")
@device_required
def api_session():
    device = current_device()
    return jsonify(id=device["id"], display_name=device["display_name"])


@app.get("/api/keyblob")
@device_required
def api_keyblob():
    """The passphrase-sealed device key. Useless without the passphrase."""
    blob = platform().key_blob(session["device_id"])
    if not blob:
        return jsonify(error="Browser access has not been enabled for this device. "
                             "Open Encrypted Chat in the Stratus terminal and choose Enable Browser Access."), 404
    return jsonify(blob)


@app.get("/api/conversations")
@device_required
def api_conversations():
    return jsonify(platform_api.conversation_catalogue(platform(), session["device_id"]))


@app.get("/api/recipients")
@device_required
def api_recipients():
    ids = request.args.getlist("device_id")
    return jsonify(platform().encryption_recipients(ids or None))


@app.get("/api/conversations/<conversation_id>/members")
@device_required
def api_members(conversation_id: str):
    try:
        member_ids = platform_api.conversation_members(platform(), conversation_id, session["device_id"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(platform().encryption_recipients(sorted(member_ids)))


@app.get("/api/conversations/<conversation_id>/messages")
@device_required
def api_messages(conversation_id: str):
    after = request.args.get("after", 0, type=int)
    try:
        rows = platform_api.messages(platform(), conversation_id, session["device_id"], after)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(rows)


@app.post("/api/conversations/<conversation_id>/messages")
@device_required
def api_send(conversation_id: str):
    payload = (request.get_json(silent=True) or {}).get("payload")
    if not isinstance(payload, dict): return jsonify(error="missing encrypted payload"), 400
    try:
        message_id = platform_api.send_message(platform(), conversation_id, session["device_id"], payload)
    except ValueError as exc:
        return jsonify(error=str(exc)), 403
    return jsonify(id=message_id), 201


@app.get("/api/overview")
@device_required
def api_overview():
    return jsonify(platform().conversation_overview(session["device_id"]))


@app.post("/api/groups")
@device_required
def api_create_group():
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()[:80]
    members = [str(item) for item in body.get("members", [])]
    if not name: return jsonify(error="Give the group a name"), 400
    import uuid
    group = platform().create_group("group-" + uuid.uuid4().hex[:16], name, session["device_id"], members)
    return jsonify(group), 201


@app.get("/api/files")
@device_required
def api_files():
    return jsonify(platform().encrypted_files(session["device_id"]))


@app.get("/api/files/<file_id>")
@device_required
def api_file(file_id: str):
    item = platform().encrypted_file(file_id, session["device_id"])
    if not item: return jsonify(error="file not found"), 404
    return jsonify(item)


@app.post("/api/files")
@device_required
def api_upload():
    body = request.get_json(silent=True) or {}
    try:
        file_id = platform_api.store_file(platform(), session["device_id"], body.get("scope", "private"),
                                          body.get("encrypted_name") or {}, body.get("payload") or {}, body.get("id"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 403
    return jsonify(id=file_id), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8001")), debug=False, threaded=True)
