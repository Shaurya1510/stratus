# `core/interface/` — the terminal interface

Owner: **Vansh Negi**

| File | What it does |
|---|---|
| `app.py` | Every screen, built with Textual. 1,759 lines. |
| `terminal.py` | Entry point for joining a server. |
| `admin.py` | Entry point for the host dashboard. |
| `setup.py` | First-run: host or join, drive picker, server creation. |
| `menu.py` | Curses fallback used when Textual is unavailable. |

## Three rules every screen follows

**Nothing touching the network runs on the interface thread.** All requests go
through `@work(thread=True)` workers and reach widgets only via
`call_from_thread`. Before this, uploads and polls ran inline and the whole
interface froze while a file encrypted.

One consequence worth knowing: a worker must never start another worker
directly, because Textual's context does not reliably propagate into a plain
thread. Background code hops back to the interface thread with `call_ui()`
first. Getting this wrong produces bugs that only appear under load.

**Screens stay open.** Sending a message, uploading a file or deleting one
updates the screen in place. Failures arrive as notifications instead of
destroying what you were looking at. The composer clears optimistically and
refills if the send fails, like a real chat client.

**Escape goes back exactly one level**, from everywhere, and the footer always
lists the keys that work on the current screen.

## Screens

Join → Dashboard → Chat, Drive, Applications, Members, Deploy, Browser Access.
Plus the host dashboard with live tables and the approval review, and the
first-run drive picker.

## A Textual detail that cost real debugging time

`render()` is a Widget method that Textual calls to draw. Defining a method with
that name on a Screen subclass overrides the drawing hook and the screen breaks
in a way the traceback does not explain. Any method that populates rows is
called `render_rows()` here for that reason.

Similarly, `[link=http://...]` markup crashes Textual 7 — the URL must be quoted
as `[link='http://...']`. Both were caught by the tests rather than by a user.
