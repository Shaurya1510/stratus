"""The built-in websites must enforce exactly what the terminal enforces."""
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.security.browser_access import seal
from core.platform.database import Database
from core.security.e2ee import encrypt, generate_ecdh_keypair, generate_keypair

try:
    import flask
    HAS_FLASK = bool(flask)
except ImportError:
    HAS_FLASK = False


def register(database, device_id, name):
    legacy_private, legacy_public = generate_keypair()
    private, public = generate_ecdh_keypair()
    database.register_device({"id": device_id, "display_name": name, "hostname": name, "platform": "linux",
                              "secret": "secret-" + device_id, "encryption_public_key": legacy_public,
                              "ecdh_public_key": public})
    return ({"id": device_id, "display_name": name, "ecdh_public_key": public},
            {"id": device_id, "ecdh_private_key": private})


@unittest.skipUnless(HAS_FLASK, "Flask is not installed")
class ChatWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.database = Database(root / "platform" / "stratus.db")
        self.alice, self.alice_private = register(self.database, "device-alice", "alice@linux")
        self.bob, self.bob_private = register(self.database, "device-bob", "bob@linux")
        self.carol, self.carol_private = register(self.database, "device-carol", "carol@linux")
        os.environ["STRATUS_PLATFORM_DB"] = str(self.database.path)
        os.environ["STRATUS_DATA_DIR"] = str(root / "appdata")
        os.environ["STRATUS_CONTROL_PATH"] = str(ROOT / "core" / "platform")
        sys.path.insert(0, str(ROOT / "web" / "chat"))
        self.module = importlib.import_module("app")
        importlib.reload(self.module)
        self.module._database = None
        self.client = self.module.app.test_client()

    def tearDown(self):
        sys.path.remove(str(ROOT / "web" / "chat"))
        sys.modules.pop("app", None)
        if self.module._database is not None:
            self.module._database.close(); self.module._database = None
        self.database.close()
        self.temp.cleanup()

    def sign_in(self, device_id):
        ticket = self.database.issue_app_ticket(device_id)
        return self.client.get(f"/device-login?ticket={ticket}")

    def test_api_refuses_anyone_who_has_not_signed_in(self):
        self.assertEqual(self.client.get("/api/conversations").status_code, 401)
        self.assertEqual(self.client.get("/api/overview").status_code, 401)
        self.assertEqual(self.client.get("/api/keyblob").status_code, 401)

    def test_ticket_signs_in_once_and_then_expires(self):
        ticket = self.database.issue_app_ticket("device-alice")
        self.assertEqual(self.client.get(f"/device-login?ticket={ticket}").status_code, 302)
        self.assertEqual(self.client.get("/api/session").get_json()["id"], "device-alice")
        fresh = self.module.app.test_client()
        self.assertEqual(fresh.get(f"/device-login?ticket={ticket}").status_code, 403)

    def test_invented_ticket_is_refused(self):
        self.assertEqual(self.client.get("/device-login?ticket=not-a-real-ticket").status_code, 403)

    def test_website_reads_a_message_the_terminal_sent(self):
        recipients = self.database.encryption_recipients()
        payload = encrypt(b"written in the terminal", recipients, b"general")
        self.database.store_encrypted_message("general", "device-alice", payload)
        self.sign_in("device-bob")
        rows = self.client.get("/api/conversations/general/messages?after=0").get_json()
        self.assertEqual(len(rows), 1)
        self.assertIn("device-bob", rows[0]["payload"]["envelopes"])
        self.assertNotIn("written in the terminal", json.dumps(rows[0]))

    def test_website_rejects_a_message_sealed_for_the_wrong_people(self):
        self.sign_in("device-alice")
        partial = encrypt(b"only for two of us", [self.alice, self.bob], b"general")
        response = self.client.post("/api/conversations/general/messages", json={"payload": partial})
        self.assertEqual(response.status_code, 403)
        self.assertIn("membership", response.get_json()["error"])

    def test_a_direct_conversation_stays_between_two_devices(self):
        conversation = "direct:device-alice:device-bob"
        payload = encrypt(b"private", [self.alice, self.bob], conversation.encode())
        self.database.store_encrypted_message(conversation, "device-alice", payload)
        self.sign_in("device-carol")
        response = self.client.get(f"/api/conversations/{conversation}/messages?after=0")
        self.assertEqual(response.get_json(), [])
        members = self.client.get(f"/api/conversations/{conversation}/members")
        self.assertEqual(members.status_code, 400)

    def test_sealed_key_is_served_but_is_useless_to_the_host(self):
        blob = seal(self.alice_private["ecdh_private_key"], "a good passphrase", "device-alice")
        self.database.store_key_blob("device-alice", blob)
        self.sign_in("device-alice")
        served = self.client.get("/api/keyblob").get_json()
        self.assertEqual(served["kdf"], "PBKDF2-SHA256")
        self.assertNotIn(self.alice_private["ecdh_private_key"], json.dumps(served))

    def test_missing_browser_access_is_explained_not_crashed(self):
        self.sign_in("device-bob")
        response = self.client.get("/api/keyblob")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Enable Browser Access", response.get_json()["error"])

    def test_conversation_catalogue_lists_people_and_general(self):
        self.sign_in("device-alice")
        catalogue = self.client.get("/api/conversations").get_json()
        titles = {item["title"] for item in catalogue}
        self.assertIn("General", titles)
        self.assertIn("bob@linux", titles)
        self.assertNotIn("alice@linux", titles)


@unittest.skipUnless(HAS_FLASK, "Flask is not installed")
class DriveWebsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.temp.name)
        self.database = Database(root / "platform" / "stratus.db")
        self.alice, self.alice_private = register(self.database, "device-alice", "alice@linux")
        self.bob, self.bob_private = register(self.database, "device-bob", "bob@linux")
        os.environ["STRATUS_PLATFORM_DB"] = str(self.database.path)
        os.environ["STRATUS_DATA_DIR"] = str(root / "appdata")
        os.environ["STRATUS_CONTROL_PATH"] = str(ROOT / "core" / "platform")
        sys.path.insert(0, str(ROOT / "web" / "drive"))
        self.module = importlib.import_module("app")
        importlib.reload(self.module)
        self.module._database = None
        self.client = self.module.app.test_client()

    def tearDown(self):
        sys.path.remove(str(ROOT / "web" / "drive"))
        sys.modules.pop("app", None)
        if self.module._database is not None:
            self.module._database.close(); self.module._database = None
        self.database.close(); self.temp.cleanup()

    def sign_in(self, device_id):
        return self.client.get(f"/device-login?ticket={self.database.issue_app_ticket(device_id)}")

    def store(self, scope, recipients, owner="device-alice", body=b"secret contents", name=b"report.pdf"):
        file_id = "d" * 32
        self.database.store_encrypted_file(
            file_id, owner, scope, encrypt(name, recipients, file_id.encode()),
            encrypt(body, recipients, file_id.encode()), 0)
        return file_id

    def test_a_private_file_is_invisible_to_everyone_else(self):
        self.store("private", [self.alice])
        self.sign_in("device-bob")
        self.assertEqual(self.client.get("/api/files").get_json(), [])

    def test_listing_never_carries_file_contents(self):
        self.store("public", [self.alice, self.bob])
        self.sign_in("device-bob")
        listing = self.client.get("/api/files").get_json()
        self.assertEqual(len(listing), 1)
        self.assertNotIn("ciphertext", listing[0]["payload"])
        self.assertNotIn("secret contents", json.dumps(listing))
        self.assertNotIn("report.pdf", json.dumps(listing))

    def test_fetching_one_file_returns_ciphertext_only(self):
        file_id = self.store("public", [self.alice, self.bob])
        self.sign_in("device-bob")
        item = self.client.get(f"/api/files/{file_id}").get_json()
        self.assertIn("ciphertext", item["payload"])
        self.assertNotIn("secret contents", json.dumps(item))

    def test_only_the_owner_may_delete(self):
        file_id = self.store("public", [self.alice, self.bob])
        self.sign_in("device-bob")
        self.assertEqual(self.client.post(f"/api/files/{file_id}/delete").status_code, 403)
        self.sign_in("device-alice")
        self.assertEqual(self.client.post(f"/api/files/{file_id}/delete").status_code, 200)

    def test_upload_must_match_the_scope_it_claims(self):
        self.sign_in("device-alice")
        file_id = "e" * 32
        payload = encrypt(b"x", [self.alice], file_id.encode())
        name = encrypt(b"n", [self.alice], file_id.encode())
        # "public" means everyone, so a file sealed only for its owner is refused.
        response = self.client.post("/api/files", json={"id": file_id, "scope": "public",
                                                        "payload": payload, "encrypted_name": name})
        self.assertEqual(response.status_code, 403)
        ok = self.client.post("/api/files", json={"id": file_id, "scope": "private",
                                                  "payload": payload, "encrypted_name": name})
        self.assertEqual(ok.status_code, 201)


class PageMarkupTests(unittest.TestCase):
    """HTML mistakes that browsers hide rather than report."""

    PAGES = sorted((ROOT / "web").rglob("*.html"))

    def test_pages_exist(self):
        self.assertTrue(self.PAGES)

    def test_no_nested_forms(self):
        # A browser silently deletes a form nested inside another, and its
        # buttons then belong to the outer form. That is how the lock screen's
        # "Sign out" button ended up submitting the unlock form instead.
        import re
        for page in self.PAGES:
            with self.subTest(page=page.name):
                depth = 0
                for token in re.findall(r"<form|</form>", page.read_text(encoding="utf-8")):
                    depth += -1 if token == "</form>" else 1
                    self.assertLessEqual(depth, 1, f"{page.name} nests a form inside another")
                self.assertEqual(depth, 0, f"{page.name} has unbalanced form tags")


    def test_stylesheets_let_the_hidden_attribute_win(self):
        """`hidden` must actually hide.

        The attribute is only `display: none` in the browser's default
        stylesheet, so any class rule setting `display` overrides it. Both apps
        set `display: grid` on the containers they toggle with `hidden`, which
        meant the unlock screen stayed on top of the chat panel it had just
        revealed. An explicit `[hidden]` rule puts the attribute back in charge.
        """
        for sheet in sorted((ROOT / "web").rglob("app.css")):
            with self.subTest(stylesheet=str(sheet.parent.parent.name)):
                css = sheet.read_text(encoding="utf-8")
                self.assertIn("[hidden]", css, f"{sheet} has no [hidden] rule")
                rule = css[css.index("[hidden]"):]
                rule = rule[:rule.index("}") + 1]
                self.assertIn("display", rule)
                self.assertIn("none", rule)
                self.assertIn("!important", rule)

    def test_static_assets_are_cache_busted(self):
        # Stylesheets and scripts keep their names across updates, so without a
        # changing query string a browser serves the copy it cached before the
        # update and the new code never runs. Every url_for that names one of
        # them must also pass a version.
        import re
        for page in self.PAGES:
            with self.subTest(page=page.name):
                for call in re.findall(r"url_for\([^)]*\)", page.read_text(encoding="utf-8")):
                    if "app.css" in call or "stratus-crypto.js" in call:
                        self.assertIn("v=", call, f"{page.name}: {call} has no version")

    def test_every_button_declares_its_type(self):
        # A button with no type defaults to submit, which silently submits the
        # form it happens to sit in.
        import re
        for page in self.PAGES:
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                for tag in re.findall(r"<button[^>]*>", html):
                    self.assertIn("type=", tag, f"{page.name}: {tag} has no type")


if __name__ == "__main__": unittest.main()
