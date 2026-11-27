# Examples

Five small projects that deploy to a Stratus server exactly as they are. Each
one appears on the **Send a Project** screen, so showing one off is a single
click. Copy any of them, change what it serves, and send your own.

| Folder | Runtime | What it shows |
|---|---|---|
| `class-quiz` | python | A five-question Linux quiz with a shared scoreboard kept in the app's data folder. Standard library only |
| `notes-board` | static | A sticky-notes page; notes stay in the visitor's browser |
| `live-counter` | node | A visitor counter that updates live in every open browser (server-sent events). No dependencies |
| `hello-static` | static | The smallest site that works |
| `hello-node` | node | A Node server with a real install step |

Every deployable project needs a `stratus.yaml` at its top level. That file is
the whole contract between a project and the host.

| Field | Required | What it means |
|---|---|---|
| `name` | yes | Shown in the Apps list. Letters, digits, spaces, `_ . -` |
| `runtime` | yes | `static`, `python`, or `node` |
| `start` | yes | The command that starts it. `$PORT` is replaced with the port the host assigned |
| `build` | no | A command run once before the first start |
| `auto_start` | no | `true` to start it again whenever the server restarts |

## hello-static

The smallest thing that works: one HTML page served by Python's own module. No
dependencies, no build step, nothing to install.

```yaml
name: Hello Static
runtime: static
start: python3 -m http.server $PORT
```

Good for a portfolio, a class handout, notes, or any set of files people in the
room need to open in a browser.

## hello-node

A Node.js server with a real dependency install. The host runs `npm ci` when a
`package-lock.json` is present and `npm install` otherwise, then starts it.

```yaml
name: Hello Node
runtime: node
start: node server.js
```

Note that `server.js` reads `process.env.PORT`. The host chooses the port, so a
hard-coded one will start an application nobody can reach.

## Things worth knowing before you upload

- **Bind to `0.0.0.0`, not `127.0.0.1`.** Binding to loopback means only the
  host machine can open your app, which is rarely what you want.
- **Always use `$PORT`.** The host assigns one from its own range and two apps
  cannot share it.
- **Your app runs confined.** It can read its own folder, write to its own data
  directory, and reach the network. It cannot see the host's home directory or
  any other application. Writing outside your own folder will fail.
- **Memory, CPU and disk are capped** by the host's policy. An app that exceeds
  its storage quota is stopped rather than allowed to fill the drive.
- **The host reviews uploads before they run** and can see what your code does.

See [../docs/HOSTING.md](../docs/HOSTING.md) for the full walkthrough.
