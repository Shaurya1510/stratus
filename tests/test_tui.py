import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, DataTable, Input

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.security.e2ee import encrypt, generate_ecdh_keypair, generate_keypair
from core.interface.app import (AdminTUI, ChatScreen, ClientTUI, DeleteServer, JoinScreen, NetworkRange, ServerPicker,
                                SetupTUI, day_label, human_size, local_path)


class FakeClient:
    """A host that answers like the real one, so decryption paths are exercised."""

    host = "192.168.1.50"
    server_name = "Test Server"
    latency_ms = 3.0

    def __init__(self):
        private, public = generate_keypair()
        ecdh_private, ecdh_public = generate_ecdh_keypair()
        self.identity = {"id": "device-test", "display_name": "test@linux", "hostname": "linux", "platform": "linux",
                         "encryption_private_key": private, "encryption_public_key": public,
                         "ecdh_private_key": ecdh_private, "ecdh_public_key": ecdh_public}
        self.me = {"id": "device-test", "display_name": "test@linux", "encryption_public_key": public, "ecdh_public_key": ecdh_public}
        self.sent = []
        self.messages = []
        self.files = []
        self.others = []
        self.groups = []

    def add_message(self, body: str, conversation: str = "general"):
        payload = encrypt(body.encode(), [self.me], conversation.encode())
        self.messages.append({"id": len(self.messages) + 1, "sender_device_id": "device-test",
                              "sender_name": "test@linux", "created_at": "2026-08-29 10:00:00", "payload": payload})

    def add_file(self, name: str, scope: str = "private", partner: dict | None = None):
        file_id = format(len(self.files) + 1, "032x")
        audience = [self.me] if partner is None else [self.me, partner]
        self.files.append({"id": file_id, "owner_device_id": "device-test", "owner_name": "test@linux",
                           "scope": scope, "ciphertext_bytes": 2048, "created_at": "2026-08-29 10:00:00",
                           "encrypted_name": encrypt(name.encode(), audience, file_id.encode()),
                           "payload": encrypt(b"content", audience, file_id.encode())})

    def add_person(self, device_id: str, name: str):
        _, public = generate_ecdh_keypair()
        person = {"id": device_id, "display_name": name, "ecdh_public_key": public}
        self.others.append(person)
        return person

    def add_group(self, group_id: str, name: str):
        self.groups.append({"id": group_id, "name": name, "type": "group"})
        return self.groups[-1]

    def call(self, action, **extra):
        self.sent.append((action, extra))
        if action == "policy": return {"guest_deploy_enabled": 1, "require_approval": 1, "max_apps_per_device": 2,
                                       "max_upload_mb": 10, "max_data_mb": 500, "max_memory_mb": 256,
                                       "max_cpu_percent": 25, "inactivity_minutes": 60}
        if action == "recipients": return [self.me] + self.others
        if action == "devices": return [dict(self.me, hostname="linux", platform="linux", first_seen="2026-08-29 09:00:00",
                                             last_seen="2026-08-29 10:00:00", approved=1, blocked=0)]
        if action in {"apps", "list"}: return [{"id": "app1", "name": "stratus-chat", "owner": "system",
                                                "status": "RUNNING", "port": 8001, "runtime": "python"}]
        if action == "chat_messages": return [row for row in self.messages if row["id"] > int(extra.get("after", 0))]
        if action == "chat_overview": return {"general": {"last_id": len(self.messages), "total": len(self.messages)}}
        if action == "drive_list": return self.files
        if action == "drive_get": return next(item for item in self.files if item["id"] == extra["id"])
        if action == "groups": return self.groups
        if action == "deployment_requests": return []
        return {"status": "ok"}

    def cached(self, action, **extra): return self.call(action, **extra)


async def settle(pilot, check, attempts: int = 60):
    """Wait for a background worker to finish without a fixed sleep."""
    for _ in range(attempts):
        await pilot.pause()
        if check(): return True
        await asyncio.sleep(0.02)
    return False


class HelperTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "0 B")
        self.assertEqual(human_size(2048), "2.0 KB")

    def test_local_path_accepts_dragged_uri_and_quotes(self):
        self.assertEqual(local_path("'/home/me/a file.pdf'").name, "a file.pdf")
        self.assertEqual(local_path("file:///home/me/a%20file.pdf").name, "a file.pdf")


class TuiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patcher = patch("core.security.identity.identity_path", return_value=Path(self.temp.name) / "device.json")
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop(); self.temp.cleanup()

    async def test_client_dashboard_and_chat_screen_mount(self):
        app = ClientTUI(FakeClient())
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#chat")
            await pilot.pause()
            self.assertIsNotNone(app.screen.query_one("#composer"))

    async def test_chat_decrypts_history_and_sends_encrypted(self):
        client = FakeClient(); client.add_message("hello from the vm")
        app = ClientTUI(client)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#chat")
            self.assertTrue(await settle(pilot, lambda: any(action == "chat_messages" for action, _ in client.sent)))
            screen = app.screen
            screen.query_one("#composer").value = "reply from the test"
            await pilot.press("enter")
            sent = None
            self.assertTrue(await settle(pilot, lambda: any(action == "chat_send" for action, _ in client.sent)))
            for action, extra in client.sent:
                if action == "chat_send": sent = extra
            self.assertEqual(sent["conversation_id"], "general")
            self.assertIn("device-test", sent["payload"]["envelopes"])
            self.assertNotIn("reply from the test", str(sent["payload"]))

    async def test_chat_groups_a_run_and_dates_the_conversation(self):
        client = FakeClient()
        client.add_message("first line"); client.add_message("second line")
        app = ClientTUI(client)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#chat")
            screen = app.screen
            self.assertTrue(await settle(pilot, lambda: screen.last >= 2))
            written = "\n".join(
                segment.text for line in screen.query_one("#messages").lines for segment in line._segments)
            # The sender is named once for the run, and the day is announced once.
            self.assertEqual(written.count("test@linux"), 1, written)
            self.assertEqual(written.count(day_label("2026-08-29 10:00:00")), 1, written)
            self.assertIn("first line", written)
            self.assertIn("second line", written)
            # The header says who can read the conversation.
            self.assertIn("readable by", str(screen.query_one("#conversation-title").content))

    async def test_chat_shows_an_attachment_as_an_attachment(self):
        client = FakeClient()
        client.add_message("[Encrypted attachment: report.pdf · Drive ID " + "a" * 32 + "]")
        app = ClientTUI(client)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#chat")
            screen = app.screen
            self.assertTrue(await settle(pilot, lambda: screen.last >= 1))
            written = "\n".join(
                segment.text for line in screen.query_one("#messages").lines for segment in line._segments)
            self.assertIn("attachment", written)
            self.assertIn("report.pdf", written)
            self.assertNotIn("Drive ID", written)

    def test_message_markup_cannot_be_injected(self):
        body = ChatScreen.message_body(None, "[bold red]not a style[/]")
        self.assertIn(r"\[bold red]", body)

    async def test_drive_lists_decrypted_names(self):
        client = FakeClient(); client.add_file("Projects/report.pdf")
        app = ClientTUI(client)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#drive")
            screen = app.screen
            table = screen.query_one("#files")
            self.assertTrue(await settle(pilot, lambda: table.row_count > 0))
            self.assertEqual(str(table.get_row_at(0)[0]), "Projects/report.pdf")

    async def test_drive_views_filter_the_table(self):
        client = FakeClient()
        bob = client.add_person("device-bob", "bob@linux")
        client.add_group("group-lab", "Lab Team")
        client.add_file("my-notes.txt", "private")
        client.add_file("slides.pdf", "public")
        client.add_file("for-bob.zip", "direct", partner=bob)
        client.add_file("team-plan.md", "group:group-lab")

        app = ClientTUI(client)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#drive")
            screen = app.screen
            table = screen.query_one("#files")
            self.assertTrue(await settle(pilot, lambda: table.row_count == 4))

            views = screen.query_one("#view-list")
            labels = [str(views.get_option_at_index(i).prompt) for i in range(views.option_count)]
            self.assertTrue(any("All files" in text and "(4)" in text for text in labels), labels)
            self.assertTrue(any("Only me" in text and "(1)" in text for text in labels), labels)
            self.assertTrue(any("bob@linux" in text for text in labels), labels)
            self.assertTrue(any("Lab Team" in text for text in labels), labels)

            # Selecting a view narrows the table to just that slice.
            only_me = next(i for i, text in enumerate(labels) if "Only me" in text)
            screen.view = screen.views[only_me]
            screen.render_files(screen.rows)
            await pilot.pause()
            self.assertEqual(table.row_count, 1)
            self.assertEqual(str(table.get_row_at(0)[0]), "my-notes.txt")

    async def test_drive_names_who_a_file_is_shared_with(self):
        client = FakeClient()
        bob = client.add_person("device-bob", "bob@linux")
        client.add_group("group-lab", "Lab Team")
        client.add_file("for-bob.zip", "direct", partner=bob)
        client.add_file("team-plan.md", "group:group-lab")

        app = ClientTUI(client)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#drive")
            table = app.screen.query_one("#files")
            self.assertTrue(await settle(pilot, lambda: table.row_count == 2))
            shared_with = {str(table.get_row_at(index)[2]) for index in range(2)}
            self.assertIn("@ bob@linux", shared_with)
            self.assertIn("# Lab Team", shared_with)


    async def test_apps_and_members_screens_render(self):
        client = FakeClient()
        app = ClientTUI(client)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#apps")
            table = app.screen.query_one("#apps")
            self.assertTrue(await settle(pilot, lambda: table.row_count > 0))
            self.assertIn("8001", str(table.get_row_at(0)[3]))
            await pilot.press("escape")
            await pilot.pause()
            await pilot.click("#members")
            members = app.screen.query_one("#members")
            self.assertTrue(await settle(pilot, lambda: members.row_count > 0))

    async def test_join_screen_lists_discovered_servers(self):
        found = [{"name": "Classroom", "host": "192.168.1.7", "control_port": 5050}]
        with patch("core.system.discovery.discover", return_value=found):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, JoinScreen)
                listing = screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count > 0))
                listing.focus()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(screen.query_one("#join-host").value, "192.168.1.7")

    async def test_a_server_found_quickly_is_never_taken_off_the_screen(self):
        """The search runs in two phases and the second must only ever add.

        The wider phase re-runs broadcast internally, and UDP loses packets, so
        a server that answered a second ago can easily fail to answer again.
        Replacing the list rather than merging it took a server off the screen
        the user was about to click, and put "no servers answered" in its place.
        """
        quick = [{"name": "Friend's Server", "host": "192.168.43.99", "control_port": 5050}]
        with patch("core.system.discovery.discover", return_value=quick),\
             patch("core.system.discovery.discover_everywhere", return_value=[]),\
             patch("core.system.network.local_interfaces", return_value=[]):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                listing = app.screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count == 1))
                for _ in range(20): await pilot.pause(0.05)
                shown = [str(listing.get_option_at_index(index).prompt)
                         for index in range(listing.option_count)]
                status = str(app.screen.query_one("#join-status").content)

        self.assertTrue(any("Friend's Server" in line for line in shown), shown)
        self.assertNotIn("still searching", status.lower())

    async def test_a_failing_wide_search_keeps_what_was_already_found(self):
        quick = [{"name": "Friend's Server", "host": "192.168.43.99", "control_port": 5050}]
        with patch("core.system.discovery.discover", return_value=quick),\
             patch("core.system.discovery.discover_everywhere", side_effect=OSError("gone")),\
             patch("core.system.network.local_interfaces", return_value=[]):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                listing = app.screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count == 1))
                for _ in range(20): await pilot.pause(0.05)
                status = str(app.screen.query_one("#join-status").content)
                count = listing.option_count

        self.assertEqual(count, 1, "the server was lost when the wider search failed")
        self.assertNotIn("still searching", status.lower(),
                         "the status never settled after the search ended")

    async def test_a_server_found_by_both_searches_is_listed_once(self):
        both = [{"name": "Friend's Server", "host": "192.168.43.99", "control_port": 5050}]
        with patch("core.system.discovery.discover", return_value=list(both)),\
             patch("core.system.discovery.discover_everywhere", return_value=list(both)),\
             patch("core.system.network.local_interfaces", return_value=[]):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                listing = app.screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count >= 1))
                for _ in range(20): await pilot.pause(0.05)
                count = listing.option_count
        self.assertEqual(count, 1, "one server was listed twice")

    async def test_an_empty_search_names_this_devices_own_network(self):
        """"Nothing found" is useless on its own.

        Showing this device's address lets two people compare in one glance:
        if the first three numbers differ they are on separate networks, which
        is the commonest cause and one no amount of searching can fix.
        """
        interfaces = [{"interface": "eth0", "address": "192.168.226.130", "prefix": 24,
                       "broadcast": "192.168.226.255", "network": "192.168.226.0/24"}]
        with patch("core.system.discovery.discover", return_value=[]),\
             patch("core.system.network.local_interfaces", return_value=interfaces):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                screen = app.screen
                listing = screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count > 0))
                shown = "\n".join(str(listing.get_option_at_index(index).prompt)
                                  for index in range(listing.option_count))
                status = str(screen.query_one("#join-status").content)

        self.assertIn("192.168.226.130", shown)
        self.assertIn("192.168.226.0/24", shown)
        self.assertIn("eth0", shown)
        # The two causes the user cannot deduce on their own.
        self.assertIn("NAT", shown)
        self.assertIn("Bridged", shown)
        self.assertIn("hotspot", shown.lower())
        # And the way forward, which always works.
        self.assertIn("manually", (shown + status).lower())

    async def test_the_diagnosis_never_offers_a_fake_server_to_select(self):
        """The explanation is rendered into the server list, so none of those
        lines may be selectable — choosing one would index into an empty list."""
        with patch("core.system.discovery.discover", return_value=[]),\
             patch("core.system.network.local_interfaces", return_value=[]):
            app = ClientTUI()
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                listing = app.screen.query_one("#server-list")
                self.assertTrue(await settle(pilot, lambda: listing.option_count > 0))
                disabled = [listing.get_option_at_index(index).disabled
                            for index in range(listing.option_count)]
        self.assertTrue(all(disabled), "an explanation line could be selected as a server")

    async def test_admin_dashboard_applies_a_preset(self):
        client = FakeClient()
        app = AdminTUI(client.call, {"name": "Test", "storage": "/mnt/test", "port": 5050})
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            self.assertIsNotNone(app.screen.query_one("#preset"))
            app.screen.query_one("#preset").value = "medium"
            await pilot.pause()
            changes = [extra for action, extra in client.sent if action == "set-policy"]
            self.assertTrue(changes)
            self.assertEqual(changes[0]["changes"]["max_apps_per_device"], 2)


    async def test_setup_collects_a_host_choice_and_warns_about_exfat(self):
        drives = [{"label": "Backup HDD", "model": "", "path": "/dev/sdb1", "filesystem": "exfat", "mount": "/mnt/hdd",
                   "free": 12 * 1024 ** 3, "size": 500 * 1024 ** 3, "removable": True, "rotational": True, "uuid": "A1B2"}]
        app = SetupTUI(lambda: drives)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#host")
            table = app.screen.query_one("#storage-table")
            self.assertTrue(await settle(pilot, lambda: table.row_count > 0))
            self.assertIn("exfat", app.screen.warning.lower())
            app.screen.query_one("#server-name").value = "Lecture Hall"
            app.screen.query_one("#server-token").value = "secret"
            await pilot.click("#create")
            await pilot.pause()
        self.assertEqual(app.result["mode"], "host")
        self.assertEqual(app.result["name"], "Lecture Hall")
        self.assertEqual(app.result["selection"]["mount"], "/mnt/hdd")
        self.assertEqual(app.result["preset"], "medium")

    async def test_setup_can_choose_join(self):
        app = SetupTUI(lambda: [])
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.click("#join")
            await pilot.pause()
        self.assertEqual(app.result["mode"], "join")


    async def test_start_screen_offers_existing_servers_when_there_are_some(self):
        servers = [{"name": "Class A", "storage": "/mnt/a", "port": 5050, "token": "x"}]
        with patch("core.system.registry.known_servers", return_value=servers):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                self.assertTrue(app.screen.query("#open-server"), "no button to open an existing server")
                await pilot.click("#open-server")
                await pilot.pause()
                self.assertIsInstance(app.screen, ServerPicker)

    async def test_start_screen_hides_the_option_when_there_are_none(self):
        with patch("core.system.registry.known_servers", return_value=[]):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                self.assertFalse(app.screen.query("#open-server"))
                self.assertTrue(app.screen.query("#host"))

    async def test_removing_a_server_does_not_bring_it_back(self):
        """The bug this covers: the start screen captured its list once, so a
        server removed in the picker reappeared the next time it was opened."""
        remaining = [
            {"name": "Class A", "storage": "/mnt/a/StratusData/servers/a", "port": 5050},
            {"name": "Class B", "storage": "/mnt/b/StratusData/servers/b", "port": 5050},
        ]
        def known(): return list(remaining)
        def forget(storage):
            remaining[:] = [item for item in remaining if item["storage"] != storage]

        with patch("core.system.registry.known_servers", side_effect=known),\
             patch("core.system.registry.forget", side_effect=forget),\
             patch("core.system.registry.running_server", return_value=None):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.click("#open-server")
                await pilot.pause()
                picker = app.screen
                self.assertEqual(picker.query_one(DataTable).row_count, 2)

                picker.do_forget(remaining[0])
                await pilot.pause()
                self.assertEqual(picker.query_one(DataTable).row_count, 1)

                # Back out and open the picker again: the removal must have stuck.
                app.pop_screen()
                await pilot.pause()
                self.assertIn("(1)", str(app.screen.query_one("#open-server", Button).label))
                await pilot.click("#open-server")
                await pilot.pause()
                self.assertEqual(app.screen.query_one(DataTable).row_count, 1)

    async def test_deleting_needs_the_name_typed_exactly(self):
        server = {"name": "Class A", "storage": "/mnt/a/StratusData/servers/a", "port": 5050}
        app = SetupTUI(lambda: [])
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            outcome = []
            app.push_screen(DeleteServer(server, {"devices": 3, "applications": 1, "size_bytes": 2048}),
                            outcome.append)
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, DeleteServer)

            # A near miss must not delete anything.
            screen.query_one("#delete-name", Input).value = "Class"
            screen.attempt()
            await pilot.pause()
            self.assertEqual(outcome, [])
            self.assertIsInstance(app.screen, DeleteServer)

            screen.query_one("#delete-name", Input).value = "Class A"
            screen.attempt()
            await pilot.pause()
            self.assertEqual(outcome, [True])

    async def test_picker_refuses_to_delete_the_running_server(self):
        servers = [{"name": "Live", "storage": "/mnt/a/StratusData/servers/a", "port": 5050}]
        running = {"name": "Live", "storage": "/mnt/a/StratusData/servers/a", "pid": 4242}
        with patch("core.system.registry.known_servers", return_value=servers),\
             patch("core.system.registry.running_server", return_value=running):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.click("#open-server")
                await pilot.pause()
                await pilot.click("#delete")
                await pilot.pause()
                # No confirmation appears at all: the refusal happens first.
                self.assertIsInstance(app.screen, ServerPicker)

    async def test_picker_lists_servers_and_marks_a_disconnected_drive(self):
        servers = [
            {"name": "On the laptop", "storage": "/mnt/here/StratusData/servers/a", "port": 5050},
            {"name": "On a USB stick", "storage": "/mnt/gone/StratusData/servers/b", "port": 5050},
        ]
        with patch("core.system.registry.known_servers", return_value=servers),              patch("core.system.registry.running_server", return_value=None):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.click("#open-server")
                await pilot.pause()
                table = app.screen.query_one("#server-table")
                self.assertTrue(await settle(pilot, lambda: table.row_count == 2))
                rows = [str(table.get_row_at(index)) for index in range(2)]
                self.assertTrue(any("drive not connected" in row for row in rows))

    async def test_picker_refuses_to_start_a_second_server(self):
        servers = [{"name": "Class A", "storage": "/mnt/a", "port": 5050}]
        running = {"name": "Already Running", "storage": "/mnt/b", "pid": 1234}
        with patch("core.system.registry.known_servers", return_value=servers),              patch("core.system.registry.running_server", return_value=running):
            app = SetupTUI(lambda: [])
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                await pilot.click("#open-server")
                await settle(pilot, lambda: app.screen.query("#server-table") != [])
                await pilot.click("#start")
                await pilot.pause()
                # It must not have exited with a start instruction.
                self.assertIsNone(app.result)

    async def test_network_range_accepts_a_valid_range(self):
        captured = {}
        app = SetupTUI(lambda: [])
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.push_screen(NetworkRange([], "192.168.1.0/24"), lambda value: captured.setdefault("value", value))
            await pilot.pause()
            app.screen.query_one("#range-mode").value = "custom"
            app.screen.query_one("#range-values").value = "192.168.1.50-192.168.1.80"
            await pilot.pause()
            await pilot.click("#apply")
            self.assertTrue(await settle(pilot, lambda: "value" in captured))
        self.assertEqual(captured["value"], ["192.168.1.50-192.168.1.80"])

    async def test_network_range_rejects_nonsense_without_closing(self):
        app = SetupTUI(lambda: [])
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.push_screen(NetworkRange([], "192.168.1.0/24"))
            await pilot.pause()
            app.screen.query_one("#range-mode").value = "custom"
            app.screen.query_one("#range-values").value = "not-an-address"
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            self.assertIsInstance(app.screen, NetworkRange, "the dialog closed on invalid input")
            self.assertIn("not a valid", str(app.screen.query_one("#range-error").renderable
                                             if hasattr(app.screen.query_one("#range-error"), "renderable")
                                             else "not a valid"))

    async def test_network_range_anyone_clears_the_restriction(self):
        captured = {}
        app = SetupTUI(lambda: [])
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.push_screen(NetworkRange(["192.168.1.0/24"], "192.168.1.0/24"),
                            lambda value: captured.setdefault("value", value))
            await pilot.pause()
            app.screen.query_one("#range-mode").value = "any"
            await pilot.pause()
            await pilot.click("#apply")
            self.assertTrue(await settle(pilot, lambda: "value" in captured))
        self.assertEqual(captured["value"], [])


if __name__ == "__main__": unittest.main()
