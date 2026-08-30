"""Stratus terminal interface.

Every screen here follows the same three rules:

* Nothing that touches the network runs on the interface thread. Requests go
  through `@work(thread=True)` workers so typing, scrolling and clicking stay
  responsive while messages send, files encrypt and applications deploy.
* Screens stay open. Sending a message, uploading a file or starting an app
  updates the screen in place; failures arrive as notifications instead of
  destroying what the user was looking at.
* Escape always goes back exactly one level, and the visible footer always
  lists the keys that work on the current screen.
"""
from __future__ import annotations

import base64
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rich.markup import escape

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, OptionList,
    RichLog, Select, SelectionList, Static, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from core.security.e2ee import decrypt, encrypt
from core.security.identity import key_fingerprint, load_or_create_identity, pin_peer_keys
from core.system.presets import PRESETS
from core.client import bundle_project

MAX_TRANSFER_BYTES = 10 * 1024 * 1024

LOGO = """[bold #8b7cff]  ███████╗████████╗██████╗  █████╗ ████████╗██╗   ██╗███████╗
  ██╔════╝╚══██╔══╝██╔══██╗██╔══██╗╚══██╔══╝██║   ██║██╔════╝
  ███████╗   ██║   ██████╔╝███████║   ██║   ██║   ██║███████╗
  ╚════██║   ██║   ██╔══██╗██╔══██║   ██║   ██║   ██║╚════██║
  ███████║   ██║   ██║  ██║██║  ██║   ██║   ╚██████╔╝███████║
  ╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚══════╝[/]"""

THEME = """
Screen { background: #080b14; color: #e9ecf5; }
Header, Footer { background: #11162a; }
#logo, #client-logo { height: 8; padding: 1 2; background: #0d1222; text-align: center; }
.panel { border: round #394267; background: #101526; padding: 1 2; }
.panel-title { text-style: bold; color: #9b8cff; margin-bottom: 1; }
.help { color: #7d86a8; }
.dim { color: #7d86a8; }
.good { color: #7ee2a8; }
.warn { color: #ffcf7a; }
.bad { color: #ff8a9b; }
StatusBar { dock: bottom; height: 1; background: #0d1222; color: #7d86a8; padding: 0 2; }
Button { width: 100%; margin: 1 0 0 0; }
DataTable { background: #0d1222; }
DataTable > .datatable--header { background: #1a2039; color: #a99dff; text-style: bold; }
DataTable > .datatable--cursor { background: #2a3157; }
ModalScreen { align: center middle; background: #000a; }
#notice, #custom-policy, #group-creator, #deploy-dialog, #picker, #prompt, #confirm, #logview {
  width: 78; max-height: 90%; padding: 2 3; background: #131a2d; border: thick #7667ef;
}
#logview { width: 100; height: 80%; }
.modal-title { text-style: bold; color: #a99dff; margin-bottom: 1; }
.modal-buttons { height: auto; margin-top: 1; }
.modal-buttons Button { width: 1fr; margin: 0 1 0 0; }
"""


# --------------------------------------------------------------------- helpers


def human_size(value: int | float) -> str:
    number = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if number < 1024 or unit == "TB": return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return "0 B"


def relative_time(stamp: str) -> str:
    """SQLite timestamps are UTC; show them as an age the reader can scan."""
    if not stamp: return ""
    try: moment = datetime.fromisoformat(stamp.replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError: return stamp
    seconds = (datetime.now(timezone.utc) - moment).total_seconds()
    if seconds < 60: return "just now"
    if seconds < 3600: return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def clock(stamp: str) -> str:
    try: return datetime.fromisoformat(stamp.replace("Z", "")).replace(tzinfo=timezone.utc).astimezone().strftime("%H:%M")
    except (ValueError, AttributeError): return ""


def day_label(stamp: str) -> str:
    """"Today" / "Yesterday" / a written date, for separators between messages."""
    try: moment = datetime.fromisoformat(stamp.replace("Z", "")).replace(tzinfo=timezone.utc).astimezone()
    except (ValueError, AttributeError): return ""
    today = datetime.now().astimezone().date()
    days = (today - moment.date()).days
    if days == 0: return "Today"
    if days == 1: return "Yesterday"
    return moment.strftime("%A %d %B %Y")


# Attachments travel as an ordinary encrypted message naming a Drive object,
# so both front-ends agree on this one line and neither needs a new message type.
ATTACHMENT = re.compile(r"^\[Encrypted attachment: (.+) · Drive ID ([0-9a-f]{32})\]$")


def local_path(raw: str) -> Path:
    """Accept anything a Linux desktop drops into a terminal field.

    Dragging from Files pastes a quoted path or a percent-encoded file:// URI
    depending on the desktop, so both are normalised here.
    """
    text = (raw or "").strip().strip("'\"")
    if text.startswith("file://"): text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    return Path(text).expanduser()


def cached_call(client, action: str, **extra):
    """Use the session's short-lived cache when the client offers one."""
    getter = getattr(client, "cached", None)
    return getter(action, **extra) if callable(getter) else client.call(action, **extra)


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


class StatusBar(Static):
    """Connection line. Reads the session's own numbers; makes no requests."""

    def __init__(self, client, suffix: str = ""):
        super().__init__("", markup=True); self.client, self.suffix = client, suffix

    def on_mount(self) -> None:
        self.tick(); self.set_interval(2.0, self.tick)

    def tick(self) -> None:
        identity = getattr(self.client, "identity", {}) or {}
        latency = getattr(self.client, "latency_ms", None)
        server = getattr(self.client, "server_name", "local host")
        speed = f"{latency:.0f} ms" if isinstance(latency, (int, float)) and latency else "—"
        name = identity.get("display_name", "this device")
        self.update(f"[#7ee2a8]●[/] {server}   [dim]·[/]   {name}   [dim]·[/]   round trip {speed}   {self.suffix}")


# ---------------------------------------------------------------------- modals


class Notice(ModalScreen[None]):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def __init__(self, title: str, message: str):
        super().__init__(); self.notice_title, self.message = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="notice"):
            yield Label(self.notice_title, classes="modal-title")
            with VerticalScroll():
                yield Static(self.message)
            yield Button("Close", variant="primary", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None: self.dismiss()


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, message: str, confirm_label: str = "Confirm"):
        super().__init__(); self.confirm_title, self.message, self.confirm_label = title, message, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.confirm_title, classes="modal-title")
            yield Static(self.message)
            with Horizontal(classes="modal-buttons"):
                yield Button(self.confirm_label, id="yes", variant="error")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None: self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None: self.dismiss(False)


class Prompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, hint: str, value: str = ""):
        super().__init__(); self.prompt_title, self.hint, self.value = title, hint, value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label(self.prompt_title, classes="modal-title")
            yield Static(self.hint, classes="dim")
            yield Input(self.value, id="prompt-value")
            with Horizontal(classes="modal-buttons"):
                yield Button("OK", id="ok", variant="success"); yield Button("Cancel", id="cancel")

    def on_mount(self) -> None: self.query_one("#prompt-value", Input).focus()

    @on(Input.Submitted, "#prompt-value")
    def submitted(self) -> None: self.dismiss(self.query_one("#prompt-value", Input).value.strip() or None)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        self.dismiss(self.query_one("#prompt-value", Input).value.strip() or None)

    def action_cancel(self) -> None: self.dismiss(None)


class Picker(ModalScreen[object | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, items: list[tuple[str, object]]):
        super().__init__(); self.picker_title, self.items = title, items

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label(self.picker_title, classes="modal-title")
            yield Static("Arrow keys move · Enter selects · Esc cancels", classes="dim")
            yield OptionList(*[Option(label, id=str(index)) for index, (label, _) in enumerate(self.items)], id="pick-list")
            yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if not self.items: return
        listing = self.query_one("#pick-list", OptionList)
        listing.highlighted = 0
        listing.focus()

    @on(OptionList.OptionSelected, "#pick-list")
    def selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.items[int(event.option.id)][1])

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None: self.dismiss(None)

    def action_cancel(self) -> None: self.dismiss(None)


class GroupCreator(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, people: list[dict]):
        super().__init__(); self.people = people

    def compose(self) -> ComposeResult:
        with Vertical(id="group-creator"):
            yield Label("CREATE ENCRYPTED GROUP", classes="modal-title")
            yield Input(placeholder="Group name", id="group-name")
            yield Static("Space toggles a member · mouse clicks work", classes="dim")
            yield SelectionList(*[(person["display_name"], person["id"], False) for person in self.people], id="group-members")
            with Horizontal(classes="modal-buttons"):
                yield Button("Create", id="create", variant="success"); yield Button("Cancel", id="cancel")

    def on_mount(self) -> None: self.query_one("#group-name", Input).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        name = self.query_one("#group-name", Input).value.strip()
        if not name: self.app.notify("Enter a group name", severity="error"); return
        self.dismiss({"name": name, "members": list(self.query_one("#group-members", SelectionList).selected)})

    def action_cancel(self) -> None: self.dismiss(None)


class CustomPolicy(ModalScreen[dict | None]):
    """Custom hosting limits, shown next to what the host machine actually has."""

    FIELDS = (
        ("max_apps_per_device", "Apps per joined device"),
        ("max_upload_mb", "Maximum upload size (MB)"),
        ("max_data_mb", "Persistent data per application (MB)"),
        ("max_memory_mb", "RAM per application (MB)"),
        ("max_cpu_percent", "CPU per application (%)"),
        ("inactivity_minutes", "Inactivity timeout (minutes, 0 = never)"),
    )
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, policy: dict, capabilities: dict):
        super().__init__(); self.policy, self.capabilities = policy, capabilities

    def compose(self) -> ComposeResult:
        with Vertical(id="custom-policy"):
            yield Label("CUSTOM HOSTING POLICY", classes="modal-title")
            yield Static(
                f"This host: {self.capabilities['cpu']} CPU threads · {human_size(self.capabilities['ram_mb'] * 1024 * 1024)} RAM\n"
                f"Recommended baseline: {self.capabilities['recommended'].title()}. "
                "CPU and RAM limits come from the host computer; data limits come from the selected drive.",
                classes="dim",
            )
            with VerticalScroll():
                for key, label in self.FIELDS:
                    yield Label(label)
                    yield Input(str(self.policy.get(key, 0)), id=key, type="integer")
            yield Static("", id="policy-warning", classes="warn")
            with Horizontal(classes="modal-buttons"):
                yield Button("Apply", variant="success", id="apply"); yield Button("Cancel", id="cancel")

    def values(self) -> dict:
        result = {}
        for key, _ in self.FIELDS:
            raw = self.query_one(f"#{key}", Input).value.strip()
            result[key] = int(raw) if raw.isdigit() else 0
        return result

    @on(Input.Changed)
    def review(self) -> None:
        values, warnings = self.values(), []
        total_ram = self.capabilities["ram_mb"]
        apps, memory, cpu = values["max_apps_per_device"], values["max_memory_mb"], values["max_cpu_percent"]
        if total_ram and apps * memory > total_ram // 2:
            warnings.append(f"{apps} apps × {memory} MB can reach {apps * memory} MB of this host's {total_ram} MB.")
        if apps * cpu > 100 * self.capabilities["cpu"]:
            warnings.append("Combined CPU allowance exceeds the host's total CPU capacity.")
        self.query_one("#policy-warning", Static).update("\n".join(warnings))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        self.dismiss(self.values())

    def action_cancel(self) -> None: self.dismiss(None)


class LogViewer(ModalScreen[None]):
    """Live application log. Refreshes itself without closing."""

    BINDINGS = [Binding("escape", "dismiss", "Close"), Binding("r", "reload", "Refresh")]

    def __init__(self, client, deployment: dict):
        super().__init__(); self.client, self.deployment = client, deployment

    def compose(self) -> ComposeResult:
        with Vertical(id="logview"):
            yield Label(f"LOGS — {self.deployment['name']}", classes="modal-title")
            yield RichLog(id="log-body", wrap=False, markup=False, highlight=True)
            yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.action_reload(); self.set_interval(3.0, self.action_reload)

    def action_reload(self) -> None: self.fetch()

    @work(thread=True, exclusive=True, group="logs")
    def fetch(self) -> None:
        try: data = self.client.call("logs", id=self.deployment["id"], lines=300)
        except Exception as exc:
            self.safe(self.app.notify, f"Could not read logs: {exc}", severity="error"); return
        text = as_dict(data).get("logs", "") or "(no output yet)"
        self.safe(self.render_log, text)

    def render_log(self, text: str) -> None:
        body = self.query_one("#log-body", RichLog); body.clear(); body.write(text)

    def safe(self, fn, *args, **kwargs) -> None:
        try: self.app.call_from_thread(fn, *args, **kwargs)
        except Exception: pass

    @on(Button.Pressed, "#close")
    def close(self) -> None: self.dismiss()


class DeployDialog(ModalScreen[Path | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="deploy-dialog"):
            yield Label("DEPLOY AN APPLICATION", classes="modal-title")
            yield Static(
                "Paste a project folder that contains stratus.yaml, or drag the folder "
                "from the Linux file manager into this field.", classes="dim")
            yield Input(placeholder="/home/you/my-application", id="project-path")
            yield Static("", id="manifest-preview")
            with Horizontal(classes="modal-buttons"):
                yield Button("Upload & Deploy", id="deploy-now", variant="success"); yield Button("Cancel", id="cancel")

    def on_mount(self) -> None: self.query_one("#project-path", Input).focus()

    @on(Input.Changed, "#project-path")
    def preview(self) -> None:
        project = local_path(self.query_one("#project-path", Input).value)
        manifest = project / "stratus.yaml"
        widget = self.query_one("#manifest-preview", Static)
        if not manifest.is_file():
            widget.update("[#7d86a8]Waiting for a folder containing stratus.yaml…[/]"); return
        try: fields = read_manifest(project)
        except Exception as exc: widget.update(f"[#ff8a9b]{exc}[/]"); return
        size = sum(path.stat().st_size for path in project.rglob("*") if path.is_file() and ".git" not in path.parts)
        widget.update(
            f"[#7ee2a8]{fields.get('name', '?')}[/] · runtime {fields.get('runtime', '?')} · "
            f"start `{fields.get('start', '?')}` · about {human_size(size)} before compression")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        project = local_path(self.query_one("#project-path", Input).value)
        if not project.is_dir() or not (project / "stratus.yaml").is_file():
            self.app.notify("Choose a folder containing stratus.yaml", severity="error"); return
        self.dismiss(project)

    def action_cancel(self) -> None: self.dismiss(None)


def read_manifest(project: Path) -> dict:
    fields = {}
    for line in (project / "stratus.yaml").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"): continue
        if ":" not in line: raise ValueError("manifest lines must be key: value")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip("\"'")
    for required in ("name", "runtime", "start"):
        if not fields.get(required): raise ValueError(f"manifest is missing {required}")
    return fields


# ----------------------------------------------------------------- chat screen


class ChatScreen(Screen):
    """Persistent conversation. Opens once and keeps updating in place."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("ctrl+n", "new_direct", "New direct"),
        Binding("ctrl+g", "create_group", "New group"),
        Binding("f5", "reload", "Reload"),
    ]
    CSS = """
    ChatScreen { layout: horizontal; background: #080b14; }
    #chat-sidebar { width: 34; border-right: solid #30395c; background: #101526; padding: 1; }
    #chat-list { height: 1fr; background: #101526; }
    #chat-main { width: 1fr; padding: 1 2; }
    #conversation-title { height: 2; text-style: bold; color: #a99dff; }
    #messages { height: 1fr; border: round #30395c; padding: 1; background: #0d1222; }
    #composer-row { height: 3; margin-top: 1; }
    #composer { width: 1fr; }
    #send { width: 12; margin: 0 0 0 1; }
    """

    POLL_SECONDS = 0.8
    OVERVIEW_SECONDS = 4.0

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.conversation = "general"
        self.title_text = "# General"
        self.recipients: list[dict] = []
        self.last = 0
        self.conversations: list[dict] = []
        self.seen: dict[str, int] = {}
        self.overview: dict[str, dict] = {}
        self.sending = False
        self.last_sender: str | None = None   # to group a run of messages
        self.last_day: str | None = None      # to place date separators

    def compose(self) -> ComposeResult:
        with Vertical(id="chat-sidebar"):
            yield Label("ENCRYPTED CHAT", classes="panel-title")
            yield OptionList(id="chat-list")
            yield Static("Ctrl+N direct · Ctrl+G group\n/file PATH attaches a file\nEsc goes back", classes="dim")
        with Vertical(id="chat-main"):
            yield Static("# General", id="conversation-title")
            yield RichLog(id="messages", wrap=True, markup=True)
            with Horizontal(id="composer-row"):
                yield Input(placeholder="Write a message, or /file /path/to/document.pdf", id="composer")
                yield Button("Send", id="send", variant="primary")
            yield StatusBar(self.client, "[dim]end-to-end encrypted[/]")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#composer", Input).focus()
        self.load_sidebar()
        self.open_conversation("general", "# General")
        self.set_interval(self.POLL_SECONDS, self.poll)
        self.set_interval(self.OVERVIEW_SECONDS, self.load_sidebar)

    # ------------------------------------------------------------- background

    @work(thread=True, exclusive=True, group="chat-sidebar")
    def load_sidebar(self) -> None:
        try:
            people = as_list(cached_call(self.client, "recipients"))
            groups = as_list(cached_call(self.client, "groups"))
            overview = as_dict(self.safe_overview())
        except Exception as exc:
            self.notify_thread(str(exc)); return
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        items = [{"id": "general", "title": "# General", "kind": "general"}]
        items += [{"id": group["id"], "title": f"# {group['name']}", "kind": "group"} for group in groups]
        items += [
            {"id": "direct:" + ":".join(sorted([me, person["id"]])), "title": f"@ {person['display_name']}", "kind": "direct", "person": person}
            for person in people if person.get("id") != me
        ]
        self.call_ui(self.render_sidebar, items, overview)

    def safe_overview(self) -> dict:
        try: return self.client.call("chat_overview")
        except Exception: return {}

    def render_sidebar(self, items: list[dict], overview: dict) -> None:
        self.conversations, self.overview = items, overview
        listing = self.query_one("#chat-list", OptionList)
        listing.clear_options()
        options = []
        for index, item in enumerate(items):
            last_id = as_dict(overview.get(item["id"])).get("last_id", 0)
            unread = max(0, last_id - self.seen.get(item["id"], 0)) if item["id"] != self.conversation else 0
            label = item["title"] if not unread else f"{item['title']}  ({unread})"
            options.append(Option(label, id=str(index)))
        listing.add_options(options)
        if options: listing.highlighted = next((index for index, item in enumerate(items) if item["id"] == self.conversation), 0)

    @work(thread=True, exclusive=True, group="chat-poll")
    def poll(self) -> None:
        conversation = self.conversation
        try: rows = as_list(self.client.call("chat_messages", conversation_id=conversation, after=self.last))
        except Exception: return
        if rows and conversation == self.conversation: self.call_ui(self.render_messages, conversation, rows)

    def render_messages(self, conversation: str, rows: list[dict]) -> None:
        if conversation != self.conversation: return
        log = self.query_one("#messages", RichLog)
        identity = getattr(self.client, "identity", {}) or {}
        for row in rows:
            self.last = max(self.last, row["id"])
            self.seen[conversation] = self.last
            try: body = decrypt(row["payload"], identity, conversation.encode()).decode()
            except Exception: body = None

            stamp = row.get("created_at", "")
            day = day_label(stamp)
            if day and day != self.last_day:
                log.write(f"\n[dim]──────── {day} ────────[/]\n")
                self.last_day, self.last_sender = day, None

            sender = row.get("sender_device_id")
            mine = sender == identity.get("id")
            if sender != self.last_sender:
                colour = "#7ee2a8" if mine else "#8b7cff"
                log.write(f"[bold {colour}]{row['sender_name']}[/] [dim]{clock(stamp)}[/]")
            self.last_sender = sender
            log.write(self.message_body(body) + "\n")

    def message_body(self, body: str | None) -> str:
        """Markup for one message, with attachments shown as attachments."""
        if body is None:
            return "[#ff8a9b][unable to decrypt this message on this device][/]"
        found = ATTACHMENT.match(body)
        if not found:
            return escape(body)   # a message must never be able to inject markup
        name, file_id = found.groups()
        return (f"[#a99dff]■ attachment[/] [bold]{escape(name)}[/]\n"
                f"  [dim]open the Drive screen and search for {escape(name)} to save it "
                f"· id {file_id[:8]}[/]")

    # ------------------------------------------------------------ conversation

    def open_conversation(self, conversation: str, title: str) -> None:
        self.conversation, self.title_text, self.last = conversation, title, 0
        self.last_sender = self.last_day = None
        self.query_one("#conversation-title", Static).update(f"{title} · end-to-end encrypted")
        self.query_one("#messages", RichLog).clear()
        self.query_one("#messages", RichLog).write("[dim]Loading conversation…[/]")
        self.load_history(conversation)

    @work(thread=True, exclusive=True, group="chat-history")
    def load_history(self, conversation: str) -> None:
        try:
            recipients = self.members_for(conversation)
            rows = as_list(self.client.call("chat_messages", conversation_id=conversation, after=0))
        except Exception as exc:
            self.notify_thread(str(exc)); return
        if conversation != self.conversation: return
        self.call_ui(self.finish_history, conversation, recipients, rows)

    def finish_history(self, conversation: str, recipients: list[dict], rows: list[dict]) -> None:
        if conversation != self.conversation: return
        self.recipients = recipients
        # Naming the readers is the clearest statement of what the encryption buys.
        names = sorted(person.get("display_name", "?") for person in recipients)
        readers = f"{len(names)} devices" if len(names) > 4 else ", ".join(names)
        self.query_one("#conversation-title", Static).update(
            f"{self.title_text} · [dim]readable by {escape(readers)}[/]")
        self.query_one("#messages", RichLog).clear()
        if not rows: self.query_one("#messages", RichLog).write("[dim]No messages yet. Say something.[/]")
        self.render_messages(conversation, rows)
        self.load_sidebar()

    def members_for(self, conversation: str) -> list[dict]:
        """Everyone whose key a message in this conversation must be wrapped for."""
        identity = getattr(self.client, "identity", {}) or {}
        if conversation.startswith("group-"):
            recipients = as_list(self.client.call("group_recipients", group_id=conversation))
        elif conversation.startswith("direct:"):
            members = set(conversation.split(":")[1:])
            recipients = [item for item in as_list(cached_call(self.client, "recipients")) if item["id"] in members]
        else:
            recipients = as_list(cached_call(self.client, "recipients"))
        pin_peer_keys(recipients)
        if identity.get("id") and identity["id"] not in {item["id"] for item in recipients}:
            raise RuntimeError("this device is not a member of that conversation yet")
        return recipients

    @on(OptionList.OptionSelected, "#chat-list")
    def choose(self, event: OptionList.OptionSelected) -> None:
        item = self.conversations[int(event.option.id)]
        self.open_conversation(item["id"], item["title"])
        self.query_one("#composer", Input).focus()

    # -------------------------------------------------------------- composing

    @on(Input.Submitted, "#composer")
    def submitted(self) -> None: self.send()

    @on(Button.Pressed, "#send")
    def send_pressed(self) -> None: self.send()

    def send(self) -> None:
        composer = self.query_one("#composer", Input)
        body = composer.value.strip()
        if not body or self.sending: return
        composer.value = ""
        self.sending = True
        self.deliver(self.conversation, body)

    @work(thread=True, group="chat-send")
    def deliver(self, conversation: str, body: str) -> None:
        try:
            recipients = self.recipients or self.members_for(conversation)
            if body.startswith("/file "):
                body = self.attach(conversation, recipients, local_path(body[6:]))
            payload = encrypt(body.encode(), recipients, conversation.encode())
            self.client.call("chat_send", conversation_id=conversation, payload=payload)
        except Exception as exc:
            self.call_ui(self.restore_draft, body, str(exc))
        finally:
            self.sending = False
        self.call_ui(self.poll)

    def attach(self, conversation: str, recipients: list[dict], source: Path) -> str:
        if not source.is_file(): raise ValueError(f"attachment not found: {source}")
        if source.stat().st_size > MAX_TRANSFER_BYTES: raise ValueError("attachments are limited to 10 MB in this MVP")
        self.notify_thread(f"Encrypting {source.name}…", "information")
        file_id = uuid.uuid4().hex
        scope = "public" if conversation == "general" else ("group:" + conversation if conversation.startswith("group-") else "direct")
        self.client.call(
            "drive_upload", id=file_id, scope=scope,
            encrypted_name=encrypt(source.name.encode(), recipients, file_id.encode()),
            payload=encrypt(source.read_bytes(), recipients, file_id.encode()),
        )
        return f"[Encrypted attachment: {source.name} · Drive ID {file_id}]"

    def restore_draft(self, body: str, error: str) -> None:
        composer = self.query_one("#composer", Input)
        if not composer.value: composer.value = body
        self.app.notify(error, severity="error")

    # ----------------------------------------------------------------- actions

    def action_reload(self) -> None:
        self.open_conversation(self.conversation, self.title_text); self.load_sidebar()

    def action_new_direct(self) -> None:
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        people = [item for item in as_list(cached_call(self.client, "recipients")) if item.get("id") != me]
        if not people: self.app.notify("No other devices have joined yet", severity="warning"); return
        self.app.push_screen(Picker("DIRECT MESSAGE", [(person["display_name"], person) for person in people]), self.start_direct)

    def start_direct(self, person) -> None:
        if not person: return
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        try: pin_peer_keys([person], confirm_new=False)
        except Exception as exc: self.app.notify(str(exc), severity="error"); return
        self.open_conversation("direct:" + ":".join(sorted([me, person["id"]])), f"@ {person['display_name']}")

    def action_create_group(self) -> None:
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        people = [item for item in as_list(cached_call(self.client, "recipients")) if item.get("id") != me]
        self.app.push_screen(GroupCreator(people), self.finish_group)

    def finish_group(self, result) -> None:
        if not result: return
        try:
            group = self.client.call("group_create", name=result["name"], members=result["members"])
            if hasattr(self.client, "invalidate"): self.client.invalidate("groups")
        except Exception as exc: self.app.notify(str(exc), severity="error"); return
        self.app.notify(f"Created {group['name']}")
        self.open_conversation(group["id"], f"# {group['name']}")
        self.load_sidebar()

    # ------------------------------------------------------------------ thread

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass

    def notify_thread(self, message: str, severity: str = "error") -> None:
        try: self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception: pass


# ---------------------------------------------------------------- drive screen


class DriveScreen(Screen):
    """Encrypted Drive. Stays open through every upload, download and delete."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "refresh_files", "Refresh"),
        Binding("u", "focus_path", "Upload path"),
        Binding("delete", "delete", "Delete"),
    ]
    CSS = """
    DriveScreen { layout: horizontal; background: #080b14; }
    #drive-sidebar { width: 32; border-right: solid #30395c; background: #101526; padding: 1; }
    #view-list { height: 1fr; background: #101526; }
    #drive-main { width: 1fr; }
    #drive-head { height: 4; padding: 1 2; background: #101526; }
    #drive-actions { height: 3; padding: 0 2; }
    #drive-actions2 { height: 3; padding: 0 2 1 2; }
    #file-path { width: 1fr; }
    #search { width: 1fr; }
    #drive-scope { width: 26; margin: 0 0 0 1; }
    #drive-actions Button, #drive-actions2 Button { width: 14; margin: 0 0 0 1; }
    #files { height: 1fr; margin: 0 2 1 2; }
    """

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.rows: list[dict] = []
        self.names: dict[str, str] = {}
        self.filter = ""
        self.view: dict = {"kind": "all"}   # which slice of the drive is shown
        self.views: list[dict] = []
        self.groups: list[dict] = []
        self.people: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="drive-sidebar"):
            yield Label("ENCRYPTED DRIVE", classes="panel-title")
            yield OptionList(id="view-list")
            yield Static("R refresh · U upload path\nDelete removes a file\nEsc goes back", classes="dim")
        with Vertical(id="drive-main"):
            yield Static("", id="drive-head")
            with Horizontal(id="drive-actions"):
                yield Input(placeholder="/home/you/document.pdf", id="file-path")
                yield Select(
                    (("Private — only me", "private"), ("Public — server members", "public"),
                     ("Direct — one person", "direct"), ("Group space", "group")),
                    value="private", allow_blank=False, id="drive-scope")
                yield Button("Upload", id="upload", variant="success")
            with Horizontal(id="drive-actions2"):
                yield Input(placeholder="Search decrypted filenames…", id="search")
                yield Button("Download", id="download", variant="primary")
                yield Button("Rename", id="rename")
                yield Button("Delete", id="delete", variant="error")
            yield DataTable(id="files", cursor_type="row", zebra_stripes=True)
            yield StatusBar(self.client, "[dim]encrypted before upload[/]")
        yield Footer()

    # --------------------------------------------------------------- views

    def group_id_of(self, item: dict) -> str | None:
        scope = str(item.get("scope", ""))
        return scope[6:] if scope.startswith("group:") else None

    def partner_of(self, item: dict) -> str | None:
        """Who a direct share is with: the envelope names two devices, one is us."""
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        ids = list((item.get("payload") or {}).get("envelopes", {}))
        return next((value for value in ids if value != me), None)

    def in_view(self, item: dict, view: dict) -> bool:
        kind = view["kind"]
        if kind == "all": return True
        if kind == "private": return item.get("scope") == "private"
        if kind == "public": return item.get("scope") == "public"
        if kind == "direct":
            return item.get("scope") == "direct" and (not view.get("id") or self.partner_of(item) == view.get("id"))
        if kind == "group": return self.group_id_of(item) == view.get("id")
        return True

    def person_name(self, device_id: str) -> str:
        found = next((item for item in self.people if item["id"] == device_id), None)
        return found["display_name"] if found else "unknown device"

    def build_views(self) -> list[dict]:
        views = [
            {"kind": "all", "mark": "▤", "label": "All files"},
            {"kind": "private", "mark": "◆", "label": "Only me"},
            {"kind": "public", "mark": "◎", "label": "Everyone"},
            {"kind": "direct", "mark": "@", "label": "Shared with a person"},
        ]
        # Only people who actually have a direct share, so the list stays short.
        partners: list[str] = []
        for item in self.rows:
            if item.get("scope") != "direct": continue
            partner = self.partner_of(item)
            if partner and partner not in partners: partners.append(partner)
        views += [{"kind": "direct", "id": partner, "mark": " ·", "label": self.person_name(partner)}
                  for partner in partners]
        views += [{"kind": "group", "id": group["id"], "mark": "#", "label": group["name"]}
                  for group in self.groups]
        return views

    def render_views(self) -> None:
        self.views = self.build_views()
        listing = self.query_one("#view-list", OptionList)
        listing.clear_options()
        options = []
        for index, view in enumerate(self.views):
            count = sum(1 for item in self.rows if self.in_view(item, view))
            options.append(Option(f"{view['mark']} {view['label']}  ({count})", id=str(index)))
        listing.add_options(options)
        if options:
            listing.highlighted = next(
                (index for index, view in enumerate(self.views)
                 if view["kind"] == self.view["kind"] and view.get("id") == self.view.get("id")), 0)

    def view_heading(self) -> tuple[str, str]:
        kind, identifier = self.view["kind"], self.view.get("id")
        if kind == "private": return "Only me", "Encrypted for this device alone."
        if kind == "public": return "Everyone on this server", "Shared with every member."
        if kind == "direct" and identifier:
            return self.person_name(identifier), "Shared privately between the two of you."
        if kind == "direct": return "Shared with a person", "Files exchanged privately with one member."
        if kind == "group":
            group = next((item for item in self.groups if item["id"] == identifier), None)
            return (group["name"] if group else "Group space"), "Encrypted for the members of this group."
        return "All files", "Everything on this server you can open."

    @on(OptionList.OptionSelected, "#view-list")
    def choose_view(self, event: OptionList.OptionSelected) -> None:
        self.view = self.views[int(event.option.id)]
        self.render_files(self.rows)

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Name", "Owner", "Shared with", "Size", "Added", "Object ID")
        self.action_refresh_files()

    # -------------------------------------------------------------- listing

    def action_refresh_files(self) -> None: self.fetch_files()

    @work(thread=True, exclusive=True, group="drive-list")
    def fetch_files(self) -> None:
        try:
            rows = as_list(self.client.call("drive_list"))
            # Needed to name the people and groups in the sidebar.
            people = as_list(cached_call(self.client, "recipients"))
            groups = as_list(cached_call(self.client, "groups"))
        except Exception as exc:
            self.notify_thread(str(exc)); return
        identity = getattr(self.client, "identity", {}) or {}
        for item in rows:
            if item["id"] in self.names: continue
            try: self.names[item["id"]] = decrypt(item["encrypted_name"], identity, item["id"].encode()).decode()
            except Exception: self.names[item["id"]] = None
        self.people, self.groups = people, groups
        self.call_ui(self.render_files, rows)

    def render_files(self, rows: list[dict]) -> None:
        self.rows = rows
        self.render_views()

        title, explanation = self.view_heading()
        needle = self.filter.lower()
        shown = []
        for item in rows:
            if not self.in_view(item, self.view): continue
            name = self.names.get(item["id"])
            if needle and (name is None or needle not in name.lower()): continue
            shown.append((item, name))

        self.query_one("#drive-head", Static).update(
            f"[bold #a99dff]{title}[/]  [dim]{len(shown)} file(s)[/]\n[dim]{explanation}[/]\n"
            "[dim]Drag a file into the terminal or paste its path, choose who it is for, then Upload.[/]")

        table = self.query_one(DataTable); table.clear()
        for item, name in shown:
            label = name if name is not None else "[dim]encrypted for other devices[/]"
            table.add_row(label, item.get("owner_name", ""), self.scope_label(item),
                          human_size(item.get("ciphertext_bytes", 0)), relative_time(item.get("created_at", "")),
                          item["id"], key=item["id"])

    def scope_label(self, item: dict) -> str:
        """Name the people and groups a file is shared with, not just the kind."""
        group_id = self.group_id_of(item)
        if group_id:
            group = next((entry for entry in self.groups if entry["id"] == group_id), None)
            return f"# {group['name']}" if group else "a group"
        scope = item.get("scope", "")
        if scope == "direct":
            partner = self.partner_of(item)
            return f"@ {self.person_name(partner)}" if partner else "one person"
        return {"private": "only me", "public": "server members"}.get(scope, scope)

    @on(Input.Changed, "#search")
    def search(self, event: Input.Changed) -> None:
        self.filter = event.value.strip(); self.render_files(self.rows)

    def selected_id(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count: return None
        try: return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception: return None

    # -------------------------------------------------------------- commands

    def action_focus_path(self) -> None: self.query_one("#file-path", Input).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        {"upload": self.upload, "download": self.download, "rename": self.rename, "delete": self.action_delete}[event.button.id]()

    def upload(self) -> None:
        source = local_path(self.query_one("#file-path", Input).value)
        if not source.is_file(): self.app.notify("Drop or paste a valid file path", severity="error"); return
        if source.stat().st_size > MAX_TRANSFER_BYTES: self.app.notify("Encrypted uploads are limited to 10 MB in this MVP", severity="error"); return
        scope = str(self.query_one("#drive-scope", Select).value)
        if scope == "direct": self.pick_person(source)
        elif scope == "group": self.pick_group(source)
        else: self.send_file(source, scope, None)

    def pick_person(self, source: Path) -> None:
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        people = [item for item in as_list(cached_call(self.client, "recipients")) if item.get("id") != me]
        if not people: self.app.notify("No other devices have joined yet", severity="warning"); return
        self.app.push_screen(Picker("SEND TO", [(person["display_name"], person) for person in people]),
                             lambda person: self.send_file(source, "direct", person) if person else None)

    def pick_group(self, source: Path) -> None:
        groups = as_list(cached_call(self.client, "groups"))
        if not groups: self.app.notify("Create a group in Chat first", severity="warning"); return
        self.app.push_screen(Picker("GROUP SPACE", [(group["name"], group) for group in groups]),
                             lambda group: self.send_file(source, "group:" + group["id"], group) if group else None)

    @work(thread=True, group="drive-upload")
    def send_file(self, source: Path, scope: str, target) -> None:
        try:
            identity = getattr(self.client, "identity", {}) or {}
            everyone = as_list(cached_call(self.client, "recipients"))
            if scope == "private": recipients = [item for item in everyone if item["id"] == identity.get("id")]
            elif scope == "public": recipients = everyone
            elif scope == "direct": recipients = [item for item in everyone if item["id"] in {identity.get("id"), target["id"]}]
            else: recipients = as_list(self.client.call("group_recipients", group_id=scope.split(":", 1)[1]))
            pin_peer_keys(recipients)
            self.notify_thread(f"Encrypting {source.name} for {len(recipients)} device(s)…", "information")
            file_id = uuid.uuid4().hex
            self.client.call(
                "drive_upload", id=file_id, scope=scope,
                encrypted_name=encrypt(source.name.encode(), recipients, file_id.encode()),
                payload=encrypt(source.read_bytes(), recipients, file_id.encode()))
            self.notify_thread(f"{source.name} uploaded and encrypted", "information")
            self.call_ui(self.clear_path)
            self.call_ui(self.fetch_files)
        except Exception as exc: self.notify_thread(str(exc))

    def clear_path(self) -> None: self.query_one("#file-path", Input).value = ""

    def download(self) -> None:
        file_id = self.selected_id()
        if not file_id: self.app.notify("Select a file first", severity="warning"); return
        default = Path.home() / "Downloads" if (Path.home() / "Downloads").is_dir() else Path.cwd()
        name = self.names.get(file_id, "download.bin")
        self.app.push_screen(
            Prompt("SAVE DECRYPTED FILE", "The file is decrypted on this device before it is written.", str(default / Path(name).name)),
            lambda destination: self.fetch_file(file_id, Path(destination).expanduser()) if destination else None)

    @work(thread=True, group="drive-download")
    def fetch_file(self, file_id: str, destination: Path) -> None:
        try:
            identity = getattr(self.client, "identity", {}) or {}
            item = self.client.call("drive_get", id=file_id)
            content = decrypt(item["payload"], identity, file_id.encode())
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            self.notify_thread(f"Saved {destination}", "information")
        except Exception as exc: self.notify_thread(f"Could not download: {exc}")

    def rename(self) -> None:
        file_id = self.selected_id()
        if not file_id: self.app.notify("Select a file first", severity="warning"); return
        self.app.push_screen(Prompt("RENAME", "The new name is encrypted for the same devices.", self.names.get(file_id, "")),
                             lambda value: self.send_rename(file_id, value) if value else None)

    @work(thread=True, group="drive-rename")
    def send_rename(self, file_id: str, new_name: str) -> None:
        try:
            item = self.client.call("drive_get", id=file_id)
            recipients = as_list(self.client.call("recipients", device_ids=list(item["payload"]["envelopes"])))
            self.client.call("drive_rename", id=file_id, encrypted_name=encrypt(new_name.encode(), recipients, file_id.encode()))
            self.names[file_id] = new_name
            self.notify_thread("Renamed", "information")
            self.call_ui(self.fetch_files)
        except Exception as exc: self.notify_thread(str(exc))

    def action_delete(self) -> None:
        file_id = self.selected_id()
        if not file_id: self.app.notify("Select a file first", severity="warning"); return
        name = self.names.get(file_id, file_id)
        self.app.push_screen(Confirm("DELETE OBJECT", f"Permanently delete “{name}”? This cannot be undone.", "Delete"),
                             lambda yes: self.send_delete(file_id) if yes else None)

    @work(thread=True, group="drive-delete")
    def send_delete(self, file_id: str) -> None:
        try:
            self.client.call("drive_delete", id=file_id)
            self.names.pop(file_id, None)
            self.notify_thread("Deleted", "information")
            self.call_ui(self.fetch_files)
        except Exception as exc: self.notify_thread(str(exc))

    # ------------------------------------------------------------------ thread

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass

    def notify_thread(self, message: str, severity: str = "error") -> None:
        try: self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception: pass


# ----------------------------------------------------------------- app screens


class AppsScreen(Screen):
    """Hosted applications, with controls for the ones this device owns."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("r", "reload", "Refresh"),
        Binding("l", "logs", "Logs"),
        Binding("o", "open_app", "Open in browser"),
    ]
    CSS = """
    AppsScreen { background: #080b14; }
    #apps-head { height: 3; padding: 1 2; background: #101526; }
    #apps-actions { height: 3; padding: 0 2 1 2; }
    #apps-actions Button { width: 16; margin: 0 1 0 0; }
    #apps { height: 1fr; margin: 0 2 1 2; }
    """

    def __init__(self, client, mine_only: bool = False):
        super().__init__(); self.client, self.mine_only = client, mine_only; self.rows: list[dict] = []

    def compose(self) -> ComposeResult:
        title = "MY HOSTED APPS" if self.mine_only else "HOSTED APPLICATIONS"
        yield Static(f"[bold #a99dff]{title}[/]\nOpen a listed address in any browser on this network.", id="apps-head")
        with Horizontal(id="apps-actions"):
            yield Button("Start", id="start", variant="success"); yield Button("Stop", id="stop", variant="error")
            yield Button("Restart", id="restart"); yield Button("Logs", id="logs", variant="primary")
            yield Button("Open in Browser", id="open"); yield Button("Refresh", id="refresh")
        yield DataTable(id="apps", cursor_type="row", zebra_stripes=True)
        yield StatusBar(self.client)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Application", "Owner", "Status", "Address", "Runtime", "ID")
        self.action_reload(); self.set_interval(5.0, self.action_reload)

    def action_reload(self) -> None: self.fetch()

    @work(thread=True, exclusive=True, group="apps")
    def fetch(self) -> None:
        try: rows = as_list(self.client.call("list" if self.mine_only else "apps"))
        except Exception as exc: self.notify_thread(str(exc)); return
        self.call_ui(self.render_rows, rows)

    def render_rows(self, rows: list[dict]) -> None:
        self.rows = rows
        table = self.query_one(DataTable)
        selected = self.selected_id()
        table.clear()
        host = getattr(self.client, "host", "127.0.0.1")
        for item in rows:
            status = item.get("status", "")
            mark = {"RUNNING": "[#7ee2a8]● RUNNING[/]", "STOPPED": "[#7d86a8]○ stopped[/]"}.get(status, f"[#ffcf7a]▲ {status}[/]")
            owner = "built-in" if item.get("owner") == "system" else item.get("owner", "")
            table.add_row(item.get("name", ""), owner, mark, f"http://{host}:{item.get('port', '')}",
                          item.get("runtime", ""), item.get("id", ""), key=item.get("id"))
        if selected:
            for index, item in enumerate(rows):
                if item.get("id") == selected: table.move_cursor(row=index); break

    def selected_id(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count: return None
        try: return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        except Exception: return None

    def selected_row(self) -> dict | None:
        deployment_id = self.selected_id()
        return next((item for item in self.rows if item.get("id") == deployment_id), None)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh": self.action_reload(); return
        if event.button.id == "logs": self.action_logs(); return
        if event.button.id == "open": self.action_open_app(); return
        item = self.selected_row()
        if not item: self.app.notify("Select an application first", severity="warning"); return
        self.control_app(event.button.id, item["id"])

    @work(thread=True, group="apps-control")
    def control_app(self, action: str, deployment_id: str) -> None:
        try:
            self.client.call(action, id=deployment_id)
            self.notify_thread(f"{action.title()} requested", "information")
        except Exception as exc: self.notify_thread(str(exc))
        self.call_ui(self.fetch)

    def action_logs(self) -> None:
        item = self.selected_row()
        if not item: self.app.notify("Select an application first", severity="warning"); return
        self.app.push_screen(LogViewer(self.client, item))

    def action_open_app(self) -> None:
        item = self.selected_row()
        if not item: self.app.notify("Select an application first", severity="warning"); return
        if item.get("status") != "RUNNING":
            self.app.notify("Start the application first", severity="warning"); return
        self.request_link(item)

    @work(thread=True, group="apps-open")
    def request_link(self, item: dict) -> None:
        """Built-in apps get a single-use sign-in ticket; others just get the address."""
        from core.system.network import browser_host, is_local
        # Browsers only expose WebCrypto in a secure context, so a local service
        # must be reached through loopback rather than its LAN address or the
        # page loads with no cryptography available at all.
        host = browser_host(getattr(self.client, "host", "127.0.0.1"))
        url = f"http://{host}:{item.get('port', '')}"
        if item.get("owner") == "system":
            try:
                ticket = as_dict(self.client.call("app_ticket", id=item["id"]))
                address = browser_host(as_dict(ticket).get("address") or getattr(self.client, "host", "127.0.0.1"))
                url = f"http://{address}:{item.get('port', '')}/device-login?ticket={ticket['ticket']}"
            except Exception as exc:
                self.notify_thread(f"Could not create a sign-in link: {exc}"); return
        opened = open_in_browser(url)
        display = url.split("/device-login")[0]
        message = (f"[link='{url}']{display}[/link]\n\n"
                   + ("Opened in your browser. " if opened else "No desktop browser was found on this machine. ")
                   + "Ctrl+click the address above, or type it into any browser on this network.")
        if item.get("owner") == "system":
            message += ("\n\nThis sign-in link works once and expires in a minute. "
                        "The website will ask for your browser passphrase.")
            if not is_local(getattr(self.client, "host", "")):
                # Browsers withhold the WebCrypto API from pages served over
                # plain http on anything but loopback, so a remote member simply
                # cannot decrypt in a browser. Say so here rather than letting
                # them meet an unexplained failure on the page itself.
                message += ("\n\n[#ffcf7a]This server is on another machine. Browsers only allow "
                            "encryption on secure pages, so this address will open but will not be "
                            "able to decrypt anything. Use the terminal client instead, or open the "
                            "site from a browser running on the host itself.[/]")
        self.call_ui(self.app.push_screen, Notice(item.get("name", "APPLICATION").upper(), message))

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass

    def notify_thread(self, message: str, severity: str = "error") -> None:
        try: self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception: pass


class MembersScreen(Screen):
    """Server members by Linux device name, with the key fingerprint to verify."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reload", "Refresh")]
    CSS = """
    MembersScreen { background: #080b14; }
    #members-head { height: 3; padding: 1 2; background: #101526; }
    #members { height: 1fr; margin: 1 2; }
    """

    def __init__(self, client): super().__init__(); self.client = client

    def compose(self) -> ComposeResult:
        yield Static("[bold #a99dff]SERVER MEMBERS[/]\nCompare a fingerprint out loud to verify a device before sharing secrets.", id="members-head")
        yield DataTable(id="members", cursor_type="row", zebra_stripes=True)
        yield StatusBar(self.client)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Device", "Platform", "First seen", "Last seen", "Encryption fingerprint")
        self.action_reload()

    def action_reload(self) -> None: self.fetch()

    @work(thread=True, exclusive=True, group="members")
    def fetch(self) -> None:
        try: rows = as_list(self.client.call("devices"))
        except Exception as exc: self.notify_thread(str(exc)); return
        self.call_ui(self.render_rows, rows)

    def render_rows(self, rows: list[dict]) -> None:
        table = self.query_one(DataTable); table.clear()
        me = (getattr(self.client, "identity", {}) or {}).get("id")
        for item in rows:
            key = item.get("encryption_public_key")
            label = item.get("display_name", "")
            if item.get("id") == me: label += "  (this device)"
            if item.get("blocked"): label += "  [blocked]"
            table.add_row(label, item.get("platform", ""), relative_time(item.get("first_seen", "")),
                          relative_time(item.get("last_seen", "")), key_fingerprint(key)[:24] if key else "—")

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass

    def notify_thread(self, message: str, severity: str = "error") -> None:
        try: self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception: pass


# ------------------------------------------------------------- client shell


class ClientDashboard(Screen):
    BINDINGS = [
        Binding("c", "open('chat')", "Chat"),
        Binding("d", "open('drive')", "Drive"),
        Binding("a", "open('apps')", "Apps"),
        Binding("m", "open('members')", "Members"),
        Binding("b", "open('browser')", "Browser access"),
        Binding("escape", "app.quit", "Disconnect"),
    ]
    CSS = """
    #client-grid { grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 2; padding: 1 4; height: 1fr; }
    .menu-panel { border: round #394267; background: #101526; padding: 1 2; }
    .menu-panel Button { width: 100%; height: 3; margin: 1 0; }
    """

    def __init__(self, client): super().__init__(); self.client = client

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(LOGO, id="client-logo")
        with Grid(id="client-grid"):
            with Vertical(classes="menu-panel"):
                yield Label("COMMUNICATE & SHARE", classes="panel-title")
                yield Button("Encrypted Chat  (C)", id="chat", variant="primary")
                yield Button("Encrypted Drive  (D)", id="drive", variant="primary")
                yield Button("Server Members  (M)", id="members")
                yield Button("Browser Access  (B)", id="browser")
            with Vertical(classes="menu-panel"):
                yield Label("APPLICATION CLOUD", classes="panel-title")
                yield Button("Hosted Applications  (A)", id="apps")
                yield Button("Deploy an Application", id="deploy")
                yield Button("My Hosted Apps", id="mine")
                yield Button("Disconnect", id="disconnect", variant="error")
        yield StatusBar(self.client)
        yield Footer()

    def action_open(self, target: str) -> None: self.launch(target)

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None: self.launch(event.button.id)

    def launch(self, target: str) -> None:
        if target == "chat": self.app.push_screen(ChatScreen(self.client))
        elif target == "drive": self.app.push_screen(DriveScreen(self.client))
        elif target == "members": self.app.push_screen(MembersScreen(self.client))
        elif target == "apps": self.app.push_screen(AppsScreen(self.client))
        elif target == "mine": self.app.push_screen(AppsScreen(self.client, mine_only=True))
        elif target == "deploy": self.app.push_screen(DeployDialog(), self.deploy_project)
        elif target == "browser": self.browser_access()
        elif target == "disconnect": self.app.exit()

    def browser_access(self) -> None:
        try: enabled = as_dict(self.client.call("browser_access_enabled")).get("enabled", False)
        except Exception as exc: self.app.notify(str(exc), severity="error"); return
        self.app.push_screen(PassphraseDialog(enabled), self.seal_key)

    def seal_key(self, passphrase) -> None:
        if not passphrase: return
        self.store_sealed_key(passphrase)

    @work(thread=True, group="browser-access")
    def store_sealed_key(self, passphrase: str) -> None:
        from core.security.browser_access import seal
        identity = getattr(self.client, "identity", {}) or {}
        try:
            # Sealing is deliberately slow (600k PBKDF2 rounds), so it happens
            # off the interface thread.
            blob = seal(identity["ecdh_private_key"], passphrase, identity["id"])
            self.client.call("store_key_blob", blob=blob)
        except Exception as exc:
            self.notify_thread(f"Could not enable browser access: {exc}"); return
        self.call_ui(self.app.push_screen, Notice(
            "BROWSER ACCESS ENABLED",
            "Your device key is now stored sealed with that passphrase.\n\n"
            "Open Hosted Applications, select stratus-chat or stratus-drive and press "
            "Open in Browser. The website will ask for this passphrase once, unseal the key "
            "inside your browser, and show the same encrypted conversations as this terminal.\n\n"
            "The host cannot open the sealed key, and never receives the passphrase. If you forget it, "
            "enable browser access again to reseal with a new one."))

    def deploy_project(self, project) -> None:
        if not project: return
        self.app.notify(f"Bundling {project.name}…")
        self.upload_project(project)

    @work(thread=True, group="deploy")
    def upload_project(self, project: Path) -> None:
        try:
            fields = read_manifest(project)
            archive = base64.b64encode(bundle_project(project)).decode("ascii")
            result = self.client.call("deploy", archive=archive, runtime=fields["runtime"], app_name=fields["name"])
        except Exception as exc:
            self.notify_thread(str(exc)); return
        summary = as_dict(result)
        if summary.get("status") == "PENDING_APPROVAL":
            message = f"{fields['name']} was uploaded and is waiting for the host to approve it."
        else:
            host = getattr(self.client, "host", "the host")
            url = f"http://{host}:{summary.get('port', '?')}"
            message = (f"{summary.get('name', fields['name'])} is deployed.\n\n"
                       f"Address: [link='{url}']{url}[/link]\n"
                       f"Status: {summary.get('status', 'unknown')}\n\n"
                       "Anyone on this network can open that address in a browser.")
        self.call_ui(self.app.push_screen, Notice("DEPLOYMENT", message))

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass

    def notify_thread(self, message: str, severity: str = "error") -> None:
        try: self.app.call_from_thread(self.app.notify, message, severity=severity)
        except Exception: pass


class JoinScreen(Screen):
    """Discovery, token entry and validation, before any dashboard appears."""

    BINDINGS = [Binding("r", "rescan", "Rescan"), Binding("escape", "app.quit", "Quit")]
    CSS = """
    #join-grid { grid-size: 2; grid-columns: 1fr 1fr; grid-gutter: 2; padding: 1 4; height: 1fr; }
    #server-list { height: 1fr; background: #0d1222; }
    #join-status { margin-top: 1; }
    """

    def __init__(self, discovery_port: int = 5051):
        super().__init__(); self.discovery_port = discovery_port; self.servers: list[dict] = []
        self.identity = load_or_create_identity()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(LOGO, id="client-logo")
        with Grid(id="join-grid"):
            with Vertical(classes="panel"):
                yield Label("NEARBY SERVERS", classes="panel-title")
                yield OptionList(id="server-list")
                yield Static("Press R to scan this network again.", classes="dim")
            with Vertical(classes="panel"):
                yield Label("CONNECT", classes="panel-title")
                yield Label("Host address")
                yield Input(placeholder="192.168.1.10", id="join-host")
                yield Label("Control port")
                yield Input("5050", id="join-port", type="integer")
                yield Label("Join token (blank for an open server)")
                yield Input(password=True, id="join-token")
                yield Button("Join Server", id="join", variant="success")
                yield Static("", id="join-status")
        yield Static(f"[dim]This device joins as[/] {self.identity['display_name']}", classes="dim")
        yield Footer()

    def on_mount(self) -> None:
        self.action_rescan(); self.query_one("#join-host", Input).focus()

    def action_rescan(self) -> None:
        self.status("Searching this local network…")
        self.scan()

    @work(thread=True, exclusive=True, group="discover")
    def scan(self) -> None:
        from core.system.discovery import discover
        try: servers = discover(self.discovery_port)
        except Exception as exc:
            self.call_ui(self.status, f"Discovery unavailable ({exc}). Enter an address manually.", "warn"); return
        self.call_ui(self.render_servers, servers)

    def render_servers(self, servers: list[dict]) -> None:
        self.servers = servers
        listing = self.query_one("#server-list", OptionList); listing.clear_options()
        if not servers:
            self.status("No servers answered. A firewall may block discovery — enter the address manually.", "warn"); return
        listing.add_options([Option(f"{item['name']}  —  {item['host']}:{item['control_port']}", id=str(index)) for index, item in enumerate(servers)])
        listing.highlighted = 0
        self.status(f"Found {len(servers)} server(s). Choose one, then enter the token.", "good")

    @on(OptionList.OptionSelected, "#server-list")
    def choose(self, event: OptionList.OptionSelected) -> None:
        item = self.servers[int(event.option.id)]
        self.query_one("#join-host", Input).value = item["host"]
        self.query_one("#join-port", Input).value = str(item["control_port"])
        self.query_one("#join-token", Input).focus()

    @on(Input.Submitted, "#join-token")
    def submitted(self) -> None: self.join()

    @on(Button.Pressed, "#join")
    def join_pressed(self) -> None: self.join()

    def join(self) -> None:
        host = self.query_one("#join-host", Input).value.strip()
        port = self.query_one("#join-port", Input).value.strip() or "5050"
        if not host: self.status("Enter a host address or choose a discovered server.", "bad"); return
        self.status(f"Connecting to {host}:{port}…")
        self.connect(host, int(port), self.query_one("#join-token", Input).value)

    @work(thread=True, exclusive=True, group="join")
    def connect(self, host: str, port: int, token: str) -> None:
        from core.platform.session import probe
        try: session = probe(host, port, token)
        except Exception as exc:
            self.call_ui(self.status, f"Could not join: {exc}", "bad")
            self.call_ui(self.focus_token); return
        for name in self.servers:
            if name["host"] == host: session.server_name = f"{name['name']} ({host}:{port})"
        self.call_ui(self.enter, session)

    def focus_token(self) -> None: self.query_one("#join-token", Input).focus()

    def enter(self, session) -> None:
        self.app.client = session
        self.app.push_screen(ClientDashboard(session))

    def status(self, message: str, kind: str = "dim") -> None:
        widget = self.query_one("#join-status", Static)
        widget.set_classes(kind); widget.update(message)

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass


class ClientTUI(App):
    TITLE = "Stratus"
    SUB_TITLE = "encrypted local cloud"
    CSS = THEME + ClientDashboard.CSS + JoinScreen.CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self, client=None, discovery_port: int = 5051):
        super().__init__(); self.client = client; self.discovery_port = discovery_port

    def on_mount(self) -> None:
        if self.client is None: self.push_screen(JoinScreen(self.discovery_port))
        else: self.push_screen(ClientDashboard(self.client))

    def on_unmount(self) -> None:
        closer = getattr(self.client, "close", None)
        if callable(closer):
            try: closer()
            except Exception: pass


# ------------------------------------------------------------------- host side


class ApprovalsPanel(Vertical):
    """Pending guest deployments, with the facts the host needs to decide."""

    def __init__(self, call_fn): super().__init__(); self.call = call_fn; self.rows: list[dict] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="approvals", cursor_type="row", zebra_stripes=True)
        with Horizontal(classes="modal-buttons"):
            yield Button("Review…", id="review", variant="primary")
            yield Button("Approve", id="approve", variant="success"); yield Button("Deny", id="deny", variant="error")

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Request", "Application", "Submitted by", "Runtime", "Upload", "When")
        self.reload()

    def reload(self) -> None:
        try: rows = as_list(self.call("deployment_requests"))
        except Exception: return
        self.rows = rows
        table = self.query_one(DataTable); table.clear()
        for item in rows:
            table.add_row(str(item["id"]), item.get("app_name", ""), item.get("display_name", ""), item.get("runtime", ""),
                          human_size(item.get("upload_bytes", 0)), relative_time(item.get("created_at", "")), key=str(item["id"]))

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()  # the host dashboard has its own button handler
        table = self.query_one(DataTable)
        if not table.row_count: self.app.notify("Nothing is waiting for approval", severity="warning"); return
        request_id = int(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))
        if event.button.id == "review":
            try: review = self.call("review_deployment", request_id=request_id)
            except Exception as exc: self.app.notify(str(exc), severity="error"); return
            self.app.push_screen(DeploymentReview(review), lambda decision: self.decide(request_id, decision))
            return
        action = "approve_deployment" if event.button.id == "approve" else "deny_deployment"
        try:
            self.call(action, request_id=request_id)
            self.app.notify("Approved and deployed" if event.button.id == "approve" else "Denied")
        except Exception as exc: self.app.notify(str(exc), severity="error")
        self.reload()

    def decide(self, request_id: int, decision) -> None:
        if not decision: return
        action = "approve_deployment" if decision == "approve" else "deny_deployment"
        try:
            self.call(action, request_id=request_id)
            self.app.notify("Approved and deployed" if decision == "approve" else "Denied")
        except Exception as exc: self.app.notify(str(exc), severity="error")
        self.reload()


class AdminDashboard(Screen):
    BINDINGS = [Binding("r", "reload", "Refresh"), Binding("q", "app.quit", "Exit"), Binding("escape", "app.quit", "Exit")]
    CSS = """
    #admin-grid { grid-size: 2; grid-columns: 1fr 1fr; grid-rows: 1fr; height: 1fr; padding: 1 3 0 3; grid-gutter: 2; }
    #status { color: #7ee2a8; margin: 1 0; }
    Select { margin: 1 0; }
    #overview-tabs { height: 1fr; }
    DataTable { height: 1fr; }
    """

    def __init__(self, call_fn, config): super().__init__(); self.call, self.config = call_fn, config

    def compose(self) -> ComposeResult:
        hardware = self.capabilities()
        yield Header(show_clock=True)
        yield Static(LOGO, id="logo")
        with Grid(id="admin-grid"):
            with Vertical(classes="panel"):
                yield Label("HOSTING CONTROL", classes="panel-title")
                yield Static(
                    f"Detected [bold]{hardware['cpu']}[/] CPU threads · [bold]{human_size(hardware['ram_mb'] * 1024 * 1024)}[/] RAM\n"
                    f"Recommended preset: [#7ee2a8]{hardware['recommended'].title()}[/]")
                yield Label("Safety preset")
                yield Select(
                    (("Low — safest, weak or battery-powered hosts", "low"),
                     ("Medium — recommended classroom balance", "medium"),
                     ("High — powerful, trusted machines", "high"),
                     ("Custom…", "custom")),
                    prompt="Choose a hosting preset…", id="preset")
                yield Button("Enable / Disable Guest Hosting", id="toggle", variant="warning")
                yield Button("Who May Join (network range)", id="range")
                yield Button("Enter Server Workspace", id="workspace", variant="success")
                yield Static("", id="status")
                yield Static("", id="policy-summary", classes="dim")
            with Vertical(classes="panel"):
                yield Label("SERVER OVERVIEW", classes="panel-title")
                with TabbedContent(id="overview-tabs"):
                    with TabPane("Devices", id="tab-devices"):
                        yield DataTable(id="admin-devices", cursor_type="row", zebra_stripes=True)
                    with TabPane("Applications", id="tab-apps"):
                        yield DataTable(id="admin-apps", cursor_type="row", zebra_stripes=True)
                    with TabPane("Approvals", id="tab-approvals"):
                        yield ApprovalsPanel(self.call)
        yield Static("", id="admin-status-line", classes="dim")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#admin-devices", DataTable).add_columns("Device", "Platform", "First seen", "Last seen", "State")
        self.query_one("#admin-apps", DataTable).add_columns("Application", "Owner", "Status", "Port", "Runtime")
        self.action_reload(); self.set_interval(4.0, self.action_reload)

    def capabilities(self) -> dict:
        ram = 0
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"): ram = int(line.split()[1]) // 1024
        except OSError: pass
        cpu = os.cpu_count() or 1
        recommended = "low" if cpu < 4 or (ram and ram < 4096) else ("high" if cpu >= 8 and ram >= 16384 else "medium")
        return {"cpu": cpu, "ram_mb": ram, "recommended": recommended}

    def action_reload(self) -> None: self.refresh_tables()

    @work(thread=True, exclusive=True, group="admin")
    def refresh_tables(self) -> None:
        try:
            devices = as_list(self.call("devices"))
            apps = as_list(self.call("apps"))
            policy = as_dict(self.call("policy"))
        except Exception as exc:
            self.call_ui(self.app.notify, str(exc)); return
        self.call_ui(self.render_tables, devices, apps, policy)

    def render_tables(self, devices: list[dict], apps: list[dict], policy: dict) -> None:
        table = self.query_one("#admin-devices", DataTable); table.clear()
        for item in devices:
            state = "blocked" if item.get("blocked") else "allowed"
            table.add_row(item.get("display_name", ""), item.get("platform", ""), relative_time(item.get("first_seen", "")),
                          relative_time(item.get("last_seen", "")), state)
        table = self.query_one("#admin-apps", DataTable); table.clear()
        running = 0
        for item in apps:
            status = item.get("status", "")
            running += status == "RUNNING"
            owner = "built-in" if item.get("owner") == "system" else item.get("owner", "")
            table.add_row(item.get("name", ""), owner, status, str(item.get("port", "")), item.get("runtime", ""))
        self.query_one("#policy-summary", Static).update(
            f"Guest hosting {'enabled' if policy.get('guest_deploy_enabled') else 'disabled'} · "
            f"approval {'required' if policy.get('require_approval') else 'automatic'} · "
            f"{policy.get('max_apps_per_device', '?')} apps/device · {policy.get('max_memory_mb', '?')} MB RAM · "
            f"{policy.get('max_cpu_percent', '?')}% CPU · {policy.get('max_data_mb', '?')} MB data per app\n"
            f"Joining allowed from: {', '.join(policy.get('allowed_networks') or []) or 'any device on this network'}\n"
            f"Guest applications: {'must be confined' if policy.get('require_confinement', 1) else 'may run unconfined (risk accepted)'}")
        self.query_one("#admin-status-line", Static).update(
            f"  {len(devices)} device(s) registered · {running}/{len(apps)} application(s) running · "
            f"storage {self.config.get('storage', 'unknown')}")
        try: self.query_one(ApprovalsPanel).reload()
        except Exception: pass

    @on(Select.Changed, "#preset")
    def preset(self, event: Select.Changed) -> None:
        if event.value == Select.BLANK: return
        if event.value == "custom":
            self.app.push_screen(CustomPolicy(as_dict(self.call("policy")), self.capabilities()), self.apply_custom); return
        result = as_dict(self.call("set-policy", changes=PRESETS[str(event.value)]))
        self.query_one("#status", Static).update(
            f"{str(event.value).title()} preset applied · {result.get('max_apps_per_device')} app(s) per device")
        self.action_reload()

    def apply_range(self, entries) -> None:
        if entries is None: return
        try:
            self.call("set-policy", changes={"allowed_networks": entries})
        except Exception as exc:
            self.app.notify(str(exc), severity="error"); return
        from core.system.network import describe_networks, parse_networks
        self.query_one("#status", Static).update(f"Joining restricted to: {describe_networks(parse_networks(entries))}")
        self.action_reload()

    def apply_custom(self, changes) -> None:
        if not changes: return
        self.call("set-policy", changes=changes)
        self.query_one("#status", Static).update("Custom policy applied")
        self.action_reload()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle":
            policy = as_dict(self.call("policy"))
            result = as_dict(self.call("set-policy", changes={"guest_deploy_enabled": 0 if policy.get("guest_deploy_enabled") else 1}))
            self.query_one("#status", Static).update("Guest hosting enabled" if result.get("guest_deploy_enabled") else "Guest hosting disabled")
            self.action_reload()
        elif event.button.id == "range":
            from core.system.network import local_subnet
            policy = as_dict(self.call("policy"))
            self.app.push_screen(NetworkRange(policy.get("allowed_networks") or [], local_subnet()), self.apply_range)
        elif event.button.id == "workspace":
            self.open_workspace()

    def open_workspace(self) -> None:
        """Join this server as an ordinary member, in a new window.

        The host is a member of their own server like anyone else, so this opens
        the same client a joining device would run. A new window is used rather
        than replacing the dashboard, because the host usually wants both.
        """
        from core.system.desktop import open_terminal, why_not
        name = self.config.get("name", "Stratus")
        root = Path(__file__).resolve().parents[2]
        if open_terminal(f"Stratus — {name}", "./stratus.sh terminal", root):
            self.app.notify("Workspace opening in a new window")
            return
        # No graphical session, or no emulator: say why, and what to type.
        self.app.push_screen(Notice(
            "OPEN THE WORKSPACE",
            f"{why_not()}\n\n"
            "Open another terminal yourself and run:\n\n"
            f"    cd {root}\n"
            "    ./stratus.sh terminal\n\n"
            f"Server: {name}\n"
            f"Storage: {self.config.get('storage', 'unknown')}\n"
            f"Control port: {self.config.get('port', 5050)}"))

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass


class AdminTUI(App):
    TITLE = "Stratus Host Control"
    SUB_TITLE = "hosting policy, devices and approvals"
    CSS = THEME + AdminDashboard.CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self, call_fn, config): super().__init__(); self.call_fn, self.config = call_fn, config

    def on_mount(self) -> None: self.push_screen(AdminDashboard(self.call_fn, self.config))


# ------------------------------------------------------------ first-run setup


class FirstRunScreen(Screen):
    """The screen shown by ./stratus.sh before a server exists."""

    BINDINGS = [Binding("escape", "app.quit", "Exit")]
    CSS = """
    #first-run { padding: 2 6; height: 1fr; }
    #first-run Button { width: 100%; height: 3; margin: 1 0; }
    """

    def on_screen_resume(self) -> None:
        """Re-read the registry whenever this screen comes back to the front.

        `compose` runs once, so without this the button below still counted
        servers that were removed or deleted in the picker, and handed that
        stale list straight back to it.
        """
        from core.system.registry import known_servers
        self.servers = known_servers()
        try: button = self.query_one("#open-server", Button)
        except Exception: return
        button.display = bool(self.servers)
        button.label = f"Open an Existing Server  ({len(self.servers)})"

    def compose(self) -> ComposeResult:
        from core.system.registry import known_servers
        # Servers this machine has created or opened before. When there are any,
        # reopening one is the likelier intent than creating another, so it is
        # offered first.
        self.servers = known_servers()
        yield Header(show_clock=True)
        yield Static(LOGO, id="logo")
        with Vertical(id="first-run"):
            yield Static(
                "A Stratus server turns this Linux computer and a drive you choose into a private "
                "local cloud. Everything it stores stays on that drive.\n", classes="dim")
            if self.servers:
                yield Button(f"Open an Existing Server  ({len(self.servers)})", id="open-server", variant="success")
                yield Button("Host a New Server", id="host", variant="primary")
            else:
                yield Button("Host a Server on this computer", id="host", variant="success")
            yield Button("Join a Server on this network", id="join", variant="primary")
            yield Button("Exit", id="exit", variant="error")
        yield Footer()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "exit": self.app.exit(); return
        if event.button.id == "join": self.app.result = {"mode": "join"}; self.app.exit(); return
        if event.button.id == "open-server":
            from core.system.registry import known_servers
            self.app.push_screen(ServerPicker(known_servers())); return
        self.app.push_screen(HostSetupScreen())


class HostSetupScreen(Screen):
    """Name the server, choose its drive, set the token and the starting policy."""

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reload", "Rescan drives")]
    CSS = """
    #setup-grid { grid-size: 2; grid-columns: 3fr 2fr; grid-gutter: 2; padding: 1 3; height: 1fr; }
    #storage-table { height: 1fr; }
    #setup-warning { margin-top: 1; }
    """

    def __init__(self): super().__init__(); self.options: list[dict] = []; self.warning = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(LOGO, id="logo")
        with Grid(id="setup-grid"):
            with Vertical(classes="panel"):
                yield Label("SERVER STORAGE", classes="panel-title")
                yield Static("Stratus never formats a drive. It creates one StratusData folder on the drive you pick.", classes="dim")
                yield DataTable(id="storage-table", cursor_type="row", zebra_stripes=True)
                yield Button("Use another mounted directory…", id="custom-storage")
                yield Static("", id="setup-warning", classes="warn")
            with Vertical(classes="panel"):
                yield Label("SERVER DETAILS", classes="panel-title")
                yield Label("Server name")
                yield Input("My Stratus Server", id="server-name")
                yield Label("Join token (blank makes an open server)")
                yield Input(password=True, id="server-token")
                yield Label("Starting hosting policy")
                yield Select((("Low — safest", "low"), ("Medium — recommended", "medium"), ("High — powerful hosts", "high")),
                             value="medium", allow_blank=False, id="server-preset")
                yield Button("Create & Start Server", id="create", variant="success")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#storage-table", DataTable).add_columns("Drive", "Kind", "Filesystem", "Free", "Total", "Mounted at")
        self.action_reload()

    def action_reload(self) -> None: self.scan()

    @work(thread=True, exclusive=True, group="storage")
    def scan(self) -> None:
        try: options = self.app.storage_source()
        except Exception as exc:
            self.call_ui(self.app.notify, f"Could not read drives: {exc}"); return
        self.call_ui(self.render_storage, options)

    def render_storage(self, options: list[dict]) -> None:
        self.options = options
        table = self.query_one("#storage-table", DataTable); table.clear()
        for index, item in enumerate(options):
            kind = "HDD" if item.get("rotational") else "SSD / flash"
            kind += " · removable" if item.get("removable") else " · internal"
            if item.get("mounted", True):
                where, free = item.get("mount", ""), human_size(item.get("free", 0))
            else:
                # Attached but not opened yet. Choosing it mounts it.
                where, free = "[#ffcf7a]not mounted — will be mounted[/]", "—"
            table.add_row(item.get("label") or item.get("model") or item.get("path", ""), kind,
                          item.get("filesystem", ""), free, human_size(item.get("size", 0)),
                          where, key=str(index))
        if options: table.move_cursor(row=0)
        self.review()

    @on(DataTable.RowHighlighted)
    def review(self) -> None:
        item = self.selection()
        warning = ""
        notes = []
        if item:
            if not item.get("mounted", True):
                notes.append(f"{item.get('path', 'This drive')} is attached but not mounted. "
                             "Creating the server here will mount it first.")
            filesystem = (item.get("filesystem") or "").lower()
            if filesystem in {"exfat", "vfat", "ntfs", "msdos"}:
                notes.append(f"{filesystem} cannot store Linux permissions or symlinks. Server data is safe there; "
                             "application virtual environments stay on this computer's own filesystem.")
            elif item.get("free", 0) and item["free"] < 2 * 1024 ** 3:
                notes.append("Less than 2 GB free on this drive.")
        warning = "\n".join(notes)
        self.warning = warning
        try: self.query_one("#setup-warning", Static).update(warning)
        except Exception: pass

    def selection(self) -> dict | None:
        table = self.query_one("#storage-table", DataTable)
        if not table.row_count or not self.options: return None
        try: return self.options[int(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))]
        except Exception: return None

    @on(Button.Pressed, "#custom-storage")
    def custom(self) -> None:
        self.app.push_screen(Prompt("MOUNTED DIRECTORY", "Any writable directory on a mounted filesystem.", str(Path.home())), self.use_custom)

    def use_custom(self, value) -> None:
        if not value: return
        path = Path(value).expanduser()
        if not path.is_dir(): self.app.notify("That directory does not exist", severity="error"); return
        import shutil
        usage = shutil.disk_usage(path)
        self.options.insert(0, {"mount": str(path), "uuid": "custom", "filesystem": "unknown", "label": path.name,
                                "free": usage.free, "size": usage.total, "removable": False, "rotational": False,
                                "path": str(path), "mounted": True})
        self.render_storage(self.options)

    @on(Button.Pressed, "#create")
    def create(self) -> None:
        name = self.query_one("#server-name", Input).value.strip()
        item = self.selection()
        if not name: self.app.notify("Give the server a name", severity="error"); return
        if not item: self.app.notify("Choose where the server data should live", severity="error"); return

        details = {"name": name, "token": self.query_one("#server-token", Input).value,
                   "preset": str(self.query_one("#server-preset", Select).value)}
        if item.get("mounted", True):
            self.finish(item, details); return

        # Mounting talks to udisks2 over the bus, which can take a moment on a
        # large disk, so it happens off the interface thread.
        self.query_one("#create", Button).disabled = True
        self.app.notify(f"Mounting {item.get('path', 'the drive')}\u2026")
        self.mount_then_create(item, details)

    @work(thread=True, exclusive=True, group="mount")
    def mount_then_create(self, item: dict, details: dict) -> None:
        from core.system.storage import mount_drive
        try:
            where = mount_drive(item["path"])
        except (RuntimeError, OSError) as exc:
            self.call_ui(self.mount_failed, str(exc)); return
        chosen = dict(item); chosen["mount"] = where; chosen["mounted"] = True
        try:
            import shutil
            chosen["free"] = shutil.disk_usage(where).free
        except OSError:
            pass
        self.call_ui(self.finish, chosen, details)

    def mount_failed(self, message: str) -> None:
        self.query_one("#create", Button).disabled = False
        self.app.notify(message, severity="error", timeout=12)

    def finish(self, item: dict, details: dict) -> None:
        self.app.result = {"mode": "host", "selection": item, **details}
        self.app.exit()

    def call_ui(self, fn, *args) -> None:
        try: self.app.call_from_thread(fn, *args)
        except Exception: pass


class SetupTUI(App):
    TITLE = "Stratus"
    SUB_TITLE = "create or join a local cloud"
    CSS = THEME + FirstRunScreen.CSS + HostSetupScreen.CSS
    BINDINGS = [Binding("ctrl+q", "quit", "Quit")]

    def __init__(self, storage_source):
        super().__init__(); self.storage_source = storage_source; self.result: dict | None = None

    def on_mount(self) -> None: self.push_screen(FirstRunScreen())


# --------------------------------------------------------------- browser access


class PassphraseDialog(ModalScreen[str | None]):
    """Choose the passphrase that unseals this device's key in a browser."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, existing: bool):
        super().__init__(); self.existing = existing

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt"):
            yield Label("BROWSER ACCESS", classes="modal-title")
            yield Static(
                "The website needs this device's private key to read your encrypted messages. "
                "The key is sealed with a passphrase before it is stored, so the host keeps a blob "
                "it cannot open. You type this passphrase in the browser, never in a form the host sees.\n",
                classes="dim")
            if self.existing:
                yield Static("Browser access is already enabled. Entering a new passphrase replaces it.", classes="warn")
            yield Label("Passphrase (at least 8 characters)")
            yield Input(password=True, id="pass1")
            yield Label("Repeat passphrase")
            yield Input(password=True, id="pass2")
            yield Static("", id="pass-error", classes="bad")
            with Horizontal(classes="modal-buttons"):
                yield Button("Enable", id="ok", variant="success")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None: self.query_one("#pass1", Input).focus()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        first = self.query_one("#pass1", Input).value
        second = self.query_one("#pass2", Input).value
        if len(first) < 8: self.query_one("#pass-error", Static).update("Use at least 8 characters."); return
        if first != second: self.query_one("#pass-error", Static).update("The two passphrases do not match."); return
        self.dismiss(first)

    def action_cancel(self) -> None: self.dismiss(None)


def open_in_browser(url: str) -> bool:
    """Hand a link to the desktop browser without blocking the interface."""
    import shutil as _shutil, subprocess as _subprocess
    for opener in ("xdg-open", "gio", "sensible-browser", "x-www-browser", "firefox"):
        if not _shutil.which(opener): continue
        command = [opener, "open", url] if opener == "gio" else [opener, url]
        try:
            _subprocess.Popen(command, stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError: continue
    return False


class DeploymentReview(ModalScreen[str | None]):
    """What is actually inside an upload, before the host approves it."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """#review { width: 92; height: 85%; padding: 2 3; background: #131a2d; border: thick #7667ef; }
    #review-body { height: 1fr; }"""

    def __init__(self, review: dict):
        super().__init__(); self.review = review

    def compose(self) -> ComposeResult:
        request = as_dict(self.review.get("request"))
        with Vertical(id="review"):
            yield Label(f"REVIEW — {request.get('app_name', 'application')}", classes="modal-title")
            yield Static(
                f"From [bold]{request.get('display_name', 'unknown device')}[/] · runtime {request.get('runtime', '?')} · "
                f"{self.review.get('file_count', 0)} files · {human_size(self.review.get('total_bytes', 0))} unpacked")
            with VerticalScroll(id="review-body"):
                notes = as_list(self.review.get("notes"))
                flags = as_list(self.review.get("flags"))
                if flags: yield Static("\n".join(f"[#ff8a9b]! {item}[/]" for item in flags))
                if notes:
                    yield Static("\n[bold #ffcf7a]Worth a look before approving[/]")
                    yield Static("\n".join(f"[#ffcf7a]•[/] {item}" for item in notes))
                    yield Static("[dim]These are not proof of anything bad. They show which files touch the "
                                 "network, other programs, or paths outside the project.[/]")
                else:
                    yield Static("\n[#7ee2a8]Nothing in this upload matched the patterns worth flagging.[/]")
                for dependency in as_list(self.review.get("dependencies")):
                    yield Static(f"\n[bold]{dependency.get('file')}[/]")
                    yield Static(f"[dim]{dependency.get('body', '')[:1500]}[/]")
                yield Static("\n[bold]Largest files[/]")
                yield Static("\n".join(f"  {item['name']}  [dim]{human_size(item['size'])}[/]"
                                       for item in as_list(self.review.get("files"))[:40]) or "  (none)")
            with Horizontal(classes="modal-buttons"):
                yield Button("Approve & Deploy", id="approve", variant="success")
                yield Button("Deny", id="deny", variant="error")
                yield Button("Decide later", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None: self.dismiss(None)


# ------------------------------------------------------- multiple servers


class DeleteServer(ModalScreen[bool]):
    """Confirm deleting a server by typing its name.

    Everything a server holds lives in one directory, so this cannot be undone
    and there is no copy anywhere else. A plain yes/no button is too easy to hit
    by accident for that, so the name has to be typed out.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, server: dict, detail: dict):
        super().__init__(); self.server, self.detail = server, detail

    def compose(self) -> ComposeResult:
        name = self.server.get("name", "server")
        with Vertical(id="confirm"):
            yield Label("DELETE SERVER FOREVER", classes="modal-title")
            yield Static(
                f"[bold #ff8a9b]“{escape(name)}” and everything in it will be erased.[/]\n\n"
                f"  {self.detail.get('devices', 0)} member device(s)\n"
                f"  {self.detail.get('applications', 0)} deployed application(s)\n"
                f"  {human_size(self.detail.get('size_bytes', 0))} of messages, files and keys\n\n"
                f"[dim]{escape(self.server.get('storage', ''))}[/]\n\n"
                "This cannot be undone. There is no copy anywhere else. Members will not be "
                "able to reach the server again, and their messages cannot be recovered.\n")
            yield Static(f"Type the server name to confirm: [bold]{escape(name)}[/]")
            yield Input(placeholder=name, id="delete-name")
            with Horizontal(classes="modal-buttons"):
                yield Button("Delete Forever", id="yes", variant="error")
                yield Button("Cancel", id="no")

    def on_mount(self) -> None:
        self.query_one("#delete-name", Input).focus()

    @on(Input.Submitted, "#delete-name")
    def submitted(self) -> None: self.attempt()

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "no": self.dismiss(False); return
        self.attempt()

    def attempt(self) -> None:
        typed = self.query_one("#delete-name", Input).value.strip()
        if typed != self.server.get("name", "").strip():
            self.app.notify("The name does not match, so nothing was deleted", severity="warning")
            return
        self.dismiss(True)

    def action_cancel(self) -> None: self.dismiss(False)


class ServerPicker(Screen):
    """Choose which of this machine's servers to start.

    A person can keep several servers — one per class, one per project, one on
    a drive they carry — but only one runs at a time, so this screen also has
    to say clearly when something is already running.
    """

    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("r", "reload", "Refresh")]
    CSS = """
    #picker-head { height: 4; padding: 1 2; background: #101526; }
    #server-table { height: 1fr; margin: 1 2; }
    #picker-actions { height: 3; padding: 0 2 1 2; }
    #picker-actions Button { width: 21; margin-right: 1; }
    """

    def __init__(self, servers: list[dict]):
        super().__init__(); self.servers = servers; self.details: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("[bold #a99dff]YOUR SERVERS[/]\nChoose one to start. Only one server runs at a time.", id="picker-head")
        yield DataTable(id="server-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="picker-actions"):
            yield Button("Start This Server", id="start", variant="success")
            yield Button("Create Another", id="create", variant="primary")
            yield Button("Remove From List", id="forget")
            yield Button("Delete Forever", id="delete", variant="error")
        yield Static("", id="picker-status", classes="dim")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns("Server", "Storage", "Size", "Members", "Apps", "Status")
        self.action_reload()

    def action_reload(self) -> None:
        # Read the registry rather than trusting the list handed in at
        # construction, so a server removed here really is gone from the screen.
        from core.system.registry import describe, known_servers, running_server
        self.servers = known_servers()
        self.details = {}
        table = self.query_one(DataTable); table.clear()
        running = running_server()
        self.query_one("#picker-status", Static).update("")
        for index, server in enumerate(self.servers):
            detail = describe(server)
            self.details[server.get("storage", "")] = detail
            if not detail["available"]:
                status = "[#ff8a9b]drive not connected[/]"
            elif running and running.get("storage") == server.get("storage"):
                status = "[#7ee2a8]running now[/]"
            else:
                status = "[#7d86a8]stopped[/]"
            table.add_row(detail.get("name", "unnamed"), str(Path(detail.get("storage", "")).parent.parent.parent),
                          human_size(detail["size_bytes"]), str(detail["devices"]), str(detail["applications"]),
                          status, key=str(index))
        if running:
            self.query_one("#picker-status", Static).update(
                f"[#ffcf7a]{running.get('name', 'A server')} is running as PID {running.get('pid')}. "
                "Stop it before starting another.[/]")

    def selected(self) -> dict | None:
        table = self.query_one(DataTable)
        if not table.row_count: return None
        try: return self.servers[int(str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value))]
        except Exception: return None

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        from core.system.registry import running_server
        if event.button.id == "create":
            self.app.push_screen(HostSetupScreen()); return

        server = self.selected()
        if not server: self.app.notify("Select a server first", severity="warning"); return
        running = running_server()

        if event.button.id == "forget":
            self.app.push_screen(
                Confirm("REMOVE FROM LIST",
                        f"Remove \u201c{server.get('name')}\u201d from this list?\n\n"
                        "The server's data on the drive is not deleted, and it can be found again "
                        "by choosing that drive when creating a server.\n\n"
                        "To erase it instead, use Delete Forever.", "Remove"),
                lambda yes: self.do_forget(server) if yes else None)
            return

        if event.button.id == "delete":
            if running and running.get("storage") == server.get("storage"):
                self.app.notify("Stop that server before deleting it", severity="error"); return
            detail = self.details.get(server.get("storage", ""), {})
            self.app.push_screen(DeleteServer(server, detail),
                                 lambda yes: self.do_delete(server) if yes else None)
            return

        if not Path(server.get("storage", "")).is_dir():
            self.app.notify("That drive is not connected", severity="error"); return
        if running:
            self.app.notify(f"{running.get('name', 'A server')} is already running. Stop it first.", severity="error")
            return
        self.app.result = {"mode": "open", "server": server}
        self.app.exit()

    def do_forget(self, server: dict) -> None:
        from core.system.registry import forget
        forget(server.get("storage", ""))
        self.action_reload()
        self.app.notify(f"Removed {server.get('name', 'the server')} from the list")
        self.leave_if_empty()

    def do_delete(self, server: dict) -> None:
        from core.system.registry import delete_server
        try:
            outcome = delete_server(server.get("storage", ""))
        except (OSError, RuntimeError, ValueError) as exc:
            self.app.notify(str(exc), severity="error"); return
        self.action_reload()
        if outcome["erased"]:
            self.app.notify(f"Deleted {outcome['name']} and all of its data", severity="warning")
        else:
            self.app.notify(
                f"{outcome['name']} was removed from the list, but its drive is not connected "
                "so the data could not be erased. Reconnect the drive and delete it again.",
                severity="warning", timeout=10)
        self.leave_if_empty()

    def leave_if_empty(self) -> None:
        """Nothing left to pick from, so go back rather than sit on an empty table."""
        if not self.servers: self.app.pop_screen()


class NetworkRange(ModalScreen[list | None]):
    """Restrict which addresses may join this server."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: list[str], suggestion: str):
        super().__init__(); self.current = current or []; self.suggestion = suggestion

    def compose(self) -> ComposeResult:
        with Vertical(id="custom-policy"):
            yield Label("WHO MAY JOIN", classes="modal-title")
            yield Static(
                "By default any device that can reach this host and knows the join token may join. "
                "You can narrow that to particular addresses \u2014 useful when the server is on a "
                "shared campus network and you only want your own lab to reach it.\n", classes="dim")
            yield Select(
                (("Anyone on this network", "any"),
                 (f"Only this subnet ({self.suggestion})", "subnet"),
                 ("Only the addresses I list below", "custom")),
                value="custom" if self.current else "any", allow_blank=False, id="range-mode")
            yield Label("Addresses, ranges or networks (one per line)")
            yield Input("\n".join(self.current) if self.current else "", id="range-values",
                        placeholder="192.168.1.0/24   or   192.168.1.50-192.168.1.80   or   192.168.1.7")
            yield Static(
                "Separate several entries with commas. Your own machine can always connect, "
                "so a mistake here cannot lock you out of the host dashboard.", classes="dim")
            yield Static("", id="range-error", classes="bad")
            with Horizontal(classes="modal-buttons"):
                yield Button("Apply", id="apply", variant="success"); yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel": self.dismiss(None); return
        mode = str(self.query_one("#range-mode", Select).value)
        if mode == "any": self.dismiss([]); return
        if mode == "subnet": self.dismiss([self.suggestion]); return
        raw = self.query_one("#range-values", Input).value
        entries = [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]
        if not entries:
            self.query_one("#range-error", Static).update("Enter at least one address, or choose 'Anyone'."); return
        try:
            from core.system.network import parse_networks
            parse_networks(entries)
        except ValueError as error:
            self.query_one("#range-error", Static).update(str(error)); return
        self.dismiss(entries)

    def action_cancel(self) -> None: self.dismiss(None)
