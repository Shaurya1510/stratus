from __future__ import annotations

import getpass
import base64
import json
import socket
import uuid
from pathlib import Path

from core.system.discovery import discover
from core.security.identity import load_or_create_identity, pin_peer_keys, public_identity
from core.platform.protocol import receive_message, send_message
from core.security.e2ee import decrypt, encrypt
from core.interface.menu import choose_menu
from core.client import bundle_project


def call(host: str, port: int, token: str, action: str, **extra):
    request = {"action": action, "token": token, "device": public_identity(load_or_create_identity())}
    request.update(extra)
    with socket.create_connection((host, port), timeout=15) as connection:
        send_message(connection, request); reply = receive_message(connection)
    if not reply.get("ok"): raise RuntimeError(reply.get("error", "request failed"))
    return reply["data"]


def choose_server():
    print("\nSearching for Stratus servers on this LAN…")
    servers = discover()
    options = [f"{server['name']} — {server['host']}:{server['control_port']}" for server in servers] + ["Enter an IP manually"]
    choice = choose_menu("Nearby Stratus Servers", options)
    if choice is None: raise SystemExit(0)
    if choice < len(servers):
        item = servers[choice]; return item["host"], int(item["control_port"])
    return input("Host IP: ").strip(), int(input("Control port [5050]: ").strip() or "5050")


def conversation_id(first: str, second: str) -> str:
    return "direct:" + ":".join(sorted([first, second]))


def chat_screen(host: str, port: int, token: str, identity: dict) -> None:
    while True:
        print("\nEncrypted Chat\n1. General\n2. Direct message\n3. Group chat\n4. Create group\n5. Search my decryptable messages\n6. Back")
        choice = input("Choose: ").strip()
        if choice == "6": return
        recipients = call(host, port, token, "recipients")
        if choice == "1": conversation, selected = "general", recipients
        elif choice == "2":
            people = [item for item in recipients if item["id"] != identity["id"]]
            for index, item in enumerate(people, 1): print(f"{index}. {item['display_name']}")
            if not people: print("No other devices are registered."); continue
            person = people[int(input("Choose person: ")) - 1]
            conversation = conversation_id(identity["id"], person["id"])
            selected = [item for item in recipients if item["id"] in {identity["id"], person["id"]}]
            pin_peer_keys([person], confirm_new=True)
        elif choice == "3":
            groups = call(host, port, token, "groups")
            for index, group in enumerate(groups, 1): print(f"{index}. {group['name']}")
            if not groups: print("No groups yet."); continue
            group = groups[int(input("Choose group: ")) - 1]
            conversation, selected = group["id"], call(host, port, token, "group_recipients", group_id=group["id"])
        elif choice == "4":
            people = [item for item in recipients if item["id"] != identity["id"]]
            for index, person in enumerate(people, 1): print(f"{index}. {person['display_name']}")
            picks = input("Member numbers separated by commas: ").strip()
            members = [people[int(value.strip())-1]["id"] for value in picks.split(",") if value.strip()]
            print(call(host, port, token, "group_create", name=input("Group name: ").strip(), members=members)); continue
        elif choice == "5":
            needle = input("Search text: ").lower(); conversations = [("general", "General")]
            conversations += [(group["id"], group["name"]) for group in call(host, port, token, "groups")]
            for cid, label in conversations:
                for row in call(host, port, token, "chat_messages", conversation_id=cid, after=0):
                    try: body = decrypt(row["payload"], identity, cid.encode()).decode()
                    except Exception: continue
                    if needle in body.lower(): print(f"[{label}] {row['sender_name']}: {body}")
            continue
        else: continue
        print("\nMessages (server stores ciphertext only)")
        pin_peer_keys(selected)
        rows = call(host, port, token, "chat_messages", conversation_id=conversation, after=0)
        for row in rows:
            try: body = decrypt(row["payload"], identity, conversation.encode()).decode()
            except Exception: body = "[unable to decrypt]"
            print(f"{row['sender_name']}: {body}")
        body = input("Message (blank to go back, /file PATH to attach): ")
        if body:
            if body.startswith("/file "):
                source = Path(body[6:].strip()).expanduser()
                if not source.is_file() or source.stat().st_size > 10 * 1024 * 1024:
                    print("Attachment missing or larger than 10 MB."); continue
                file_id = uuid.uuid4().hex
                scope = "public" if conversation == "general" else ("group:" + conversation if conversation.startswith("group-") else "direct")
                call(host, port, token, "drive_upload", id=file_id, scope=scope,
                     encrypted_name=encrypt(source.name.encode(), selected, file_id.encode()),
                     payload=encrypt(source.read_bytes(), selected, file_id.encode()))
                body = f"[Encrypted attachment: {source.name} · Drive ID {file_id}]"
            payload = encrypt(body.encode(), selected, conversation.encode())
            call(host, port, token, "chat_send", conversation_id=conversation, payload=payload)


def drive_screen(host: str, port: int, token: str, identity: dict) -> None:
    while True:
        print("\nEncrypted Drive\n1. List accessible files\n2. Upload private file\n3. Publish to current server members\n4. Send to one person\n5. Upload to a group space\n6. Download a file\n7. Search files\n8. Rename my file\n9. Delete my file\n0. Back")
        choice = input("Choose: ").strip()
        if choice == "0": return
        if choice == "1":
            rows = call(host, port, token, "drive_list")
            for item in rows:
                try: name = decrypt(item["encrypted_name"], identity, item["id"].encode()).decode()
                except Exception: name = "[encrypted name]"
                print(f"- {item['id']} · {name} · {item['owner_name']} · {item['scope']}")
        elif choice in {"2", "3", "4", "5"}:
            source = Path(input("Local file path: ").strip()).expanduser()
            if not source.is_file(): print("File not found."); continue
            if source.stat().st_size > 10 * 1024 * 1024: print("Terminal encrypted uploads are currently limited to 10 MB."); continue
            folder = input("Encrypted folder path (optional, e.g. Projects/Final): ").strip().strip("/")
            logical_name = f"{folder}/{source.name}" if folder else source.name
            recipients = call(host, port, token, "recipients")
            if choice == "2": selected, scope = [r for r in recipients if r["id"] == identity["id"]], "private"
            elif choice == "3": selected, scope = recipients, "public"; pin_peer_keys(selected)
            elif choice == "4":
                people = [r for r in recipients if r["id"] != identity["id"]]
                for index, person in enumerate(people, 1): print(f"{index}. {person['display_name']}")
                if not people: print("No recipients available."); continue
                other = people[int(input("Choose person: ")) - 1]
                selected = [r for r in recipients if r["id"] in {identity["id"], other["id"]}]; scope = "direct"
                pin_peer_keys([other], confirm_new=True)
            else:
                groups = call(host, port, token, "groups")
                for index, group in enumerate(groups, 1): print(f"{index}. {group['name']}")
                if not groups: print("Create a Chat group first."); continue
                group = groups[int(input("Choose group: ")) - 1]
                selected = call(host, port, token, "group_recipients", group_id=group["id"]); scope = "group:" + group["id"]
            pin_peer_keys(selected)
            file_id = uuid.uuid4().hex
            result = call(host, port, token, "drive_upload", id=file_id, scope=scope, encrypted_name=encrypt(logical_name.encode(), selected, file_id.encode()), payload=encrypt(source.read_bytes(), selected, file_id.encode()))
            print(f"Encrypted upload stored as {result['id']}")
        elif choice == "6":
            file_id = input("File ID: ").strip(); item = call(host, port, token, "drive_get", id=file_id)
            try:
                output_name = decrypt(item["encrypted_name"], identity, file_id.encode()).decode()
                content = decrypt(item["payload"], identity, file_id.encode())
            except Exception: print("Unable to decrypt this file."); continue
            safe_default = Path(output_name).name
            destination = Path(input(f"Save as [{safe_default}]: ").strip() or safe_default).expanduser()
            destination.write_bytes(content); print(f"Saved {destination}")
        elif choice == "7":
            needle = input("Search filename: ").lower()
            for item in call(host, port, token, "drive_list"):
                try: name = decrypt(item["encrypted_name"], identity, item["id"].encode()).decode()
                except Exception: continue
                if needle in name.lower(): print(f"- {item['id']} · {name} · {item['owner_name']} · {item['scope']}")
        elif choice == "8":
            file_id = input("Owned file ID: ").strip(); item = call(host, port, token, "drive_get", id=file_id)
            recipients = call(host, port, token, "recipients", device_ids=list(item["payload"]["envelopes"]))
            new_name = input("New filename: ").strip()
            call(host, port, token, "drive_rename", id=file_id, encrypted_name=encrypt(new_name.encode(), recipients, file_id.encode()))
        elif choice == "9":
            file_id = input("Owned file ID: ").strip()
            if input("Permanently delete encrypted object? [y/N]: ").lower() == "y": call(host, port, token, "drive_delete", id=file_id)


def main() -> None:
    """Full-screen terminal first; the plain menus are the compatibility path."""
    try:
        from core.interface.app import ClientTUI
    except ImportError:
        ClientTUI = None
    if ClientTUI is not None:
        ClientTUI().run()
        return
    print("Textual is unavailable; using the basic compatibility menu.")
    identity = load_or_create_identity()
    print(f"\nSTRATUS\nDevice: {identity['display_name']} ({identity['id']})")
    host, port = choose_server()
    token = getpass.getpass("Server join token: ")
    try:
        call(host, port, token, "policy")
    except Exception as exc:
        print(f"\nCould not join Stratus: {exc}\nCheck the token and try again.")
        return
    while True:
        selected = choose_menu("STRATUS", ["Encrypted Chat", "Encrypted Drive", "Hosted Applications", "Deploy an Application", "My Deployments", "Server Members", "Disconnect"], f"Connected as {identity['display_name']} · {host}:{port}")
        if selected is None: return
        choice = str(selected + 1)
        try:
            if choice in {"1", "2"}:
                if choice == "1": chat_screen(host, port, token, identity)
                else: drive_screen(host, port, token, identity)
                continue
            elif choice == "3":
                for item in call(host, port, token, "apps"): print(f"- {item['name']} [{item['status']}] http://{host}:{item['port']}")
            elif choice == "4":
                project = Path(input("Project directory: ").strip()).expanduser()
                manifest_path = project / "stratus.yaml"
                if not manifest_path.is_file(): print("stratus.yaml is required."); continue
                values = dict(line.split(":", 1) for line in manifest_path.read_text(encoding="utf-8").splitlines() if ":" in line and not line.lstrip().startswith("#"))
                archive = base64.b64encode(bundle_project(project)).decode("ascii")
                result = call(host, port, token, "deploy", archive=archive, runtime=values.get("runtime", "").strip(), app_name=values.get("name", project.name).strip())
                print(json.dumps(result, indent=2))
            elif choice == "5":
                for item in call(host, port, token, "list"): print(f"- {item['name']} ({item['id']}) {item['status']} :{item['port']}")
            elif choice == "6":
                for item in call(host, port, token, "devices"): print(f"- {item['display_name']} ({item['id']})")
            elif choice == "7": return
        except Exception as exc: print(f"Stratus error: {exc}")


if __name__ == "__main__": main()
