"""Membership rules for encrypted conversations and files.

The terminal reaches the platform through the control server, and the built-in
websites reach it directly, but both must apply exactly the same rules about
who may read what. Those rules live here once so the two paths cannot drift
apart, which is the kind of gap that quietly turns into a data leak.

Nothing in this module can read message content: it only ever sees ciphertext
and the list of device ids an envelope was sealed for.
"""
from __future__ import annotations

import uuid

MAX_ENCODED_UPLOAD = 14 * 1024 * 1024   # ~10 MB before base64


def conversation_members(database, conversation_id: str, device_id: str) -> set[str]:
    """Exactly which devices a message in this conversation may be sealed for."""
    if conversation_id == "general":
        return {item["id"] for item in database.encryption_recipients()}
    if conversation_id.startswith("group-"):
        return set(database.group_member_ids(conversation_id, device_id))
    if conversation_id.startswith("direct:"):
        members = set(conversation_id.split(":")[1:])
        if device_id not in members or len(members) != 2: raise ValueError("invalid direct conversation")
        return members
    raise ValueError("invalid conversation")


def scope_members(database, scope: str, device_id: str, envelope_ids: set[str]) -> set[str]:
    if scope == "private": return {device_id}
    if scope == "public": return {item["id"] for item in database.encryption_recipients()}
    if scope == "direct":
        if len(envelope_ids) != 2: raise ValueError("direct share needs exactly two devices")
        return envelope_ids
    if scope.startswith("group:"): return set(database.group_member_ids(scope.split(":", 1)[1], device_id))
    raise ValueError("invalid file scope")


def send_message(database, conversation_id: str, device_id: str, payload: dict) -> int:
    envelope_ids = set((payload or {}).get("envelopes", {}))
    if device_id not in envelope_ids: raise ValueError("sender must be able to decrypt its message")
    if envelope_ids != conversation_members(database, conversation_id, device_id):
        raise ValueError("encryption recipients do not match conversation membership")
    return database.store_encrypted_message(conversation_id, device_id, payload)


def messages(database, conversation_id: str, device_id: str, after: int = 0) -> list[dict]:
    rows = database.encrypted_messages(conversation_id, after)
    return [row for row in rows if device_id in row["payload"].get("envelopes", {})]


def store_file(database, device_id: str, scope: str, encrypted_name: dict, payload: dict, file_id: str | None = None) -> str:
    envelope_ids = set((payload or {}).get("envelopes", {}))
    if device_id not in envelope_ids or device_id not in (encrypted_name or {}).get("envelopes", {}):
        raise ValueError("owner encryption envelope missing")
    if set(encrypted_name.get("envelopes", {})) != envelope_ids:
        raise ValueError("filename and file recipients differ")
    if envelope_ids != scope_members(database, scope, device_id, envelope_ids):
        raise ValueError("file recipients do not match scope")
    encoded_size = len(payload.get("ciphertext", ""))
    if encoded_size > MAX_ENCODED_UPLOAD: raise ValueError("encrypted uploads are limited to 10 MB in this MVP")
    file_id = file_id or uuid.uuid4().hex
    if not isinstance(file_id, str) or len(file_id) != 32 or any(character not in "0123456789abcdef" for character in file_id):
        raise ValueError("invalid file id")
    database.store_encrypted_file(file_id, device_id, scope, encrypted_name, payload, encoded_size)
    return file_id


def conversation_catalogue(database, device_id: str) -> list[dict]:
    """Every conversation this device can open, with its display name."""
    people = [item for item in database.encryption_recipients() if item["id"] != device_id]
    catalogue = [{"id": "general", "title": "General", "kind": "general"}]
    catalogue += [{"id": group["id"], "title": group["name"], "kind": "group"} for group in database.groups_for(device_id)]
    catalogue += [{"id": "direct:" + ":".join(sorted([device_id, person["id"]])), "title": person["display_name"],
                   "kind": "direct", "device_id": person["id"]} for person in people]
    return catalogue
