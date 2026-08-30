# Hosting an application on Stratus

## The contract

Stratus does not need to understand your project. It needs three things from
you, and gives you three things back.

You provide a `stratus.yaml` in the top folder of your project:

```yaml
name: my-application       # shown in the applications list
runtime: static | python | node
start: node server.js      # the one command that starts your app
build: npm run build       # optional, run once at deploy time
auto_start: true           # optional, start again when the server restarts
```

Stratus gives your application:

| Variable | Meaning |
|---|---|
| `$PORT` | The port you must listen on. Never hard-code a port. |
| `$STRATUS_DATA_DIR` | A writable folder that survives restarts and redeploys. |
| `$HOME` | Set to your data directory, so tools that write to `~` stay inside it. |

**Your application works if it is one process, listening on `$PORT`, speaking
HTTP.** Once it starts, it appears in Hosted Applications with an address like
`http://192.168.1.7:8002` that anyone on the network can open.

## What each runtime does

**static** — files served as they are. Build your React or Vue project first and
upload the output folder with a `start` line that serves it.

**python** — if `requirements.txt` exists, Stratus builds a virtual
environment and installs it. Then it runs your `start` command. Flask, FastAPI
and Django all work.

**node** — if `package.json` exists, Stratus runs `npm ci` (or `npm install`
when there is no lock file), then your optional `build` command, then `start`.
The host must have Node installed; if it does not, the deployment fails with a
message saying so.

## Full-stack projects

A "full-stack project" is usually two things: a frontend and an API. There are
two honest ways to host one here.

**One process.** Have your backend serve the built frontend. A Flask app that
serves `dist/` or a Next.js app in production mode is a single process on a
single port, and deploys as one application.

**Two deployments.** Upload the built frontend as a `static` app and the API as
a `python` or `node` app. They get separate addresses and separate ports. Your
frontend calls the API at its LAN address.

## What is not supported

- **Separate database services.** There is no way to declare Postgres, MySQL or
  Redis. Use SQLite inside `$STRATUS_DATA_DIR`, which is what the built-in
  Chat and Drive applications do.
- **More than one process per deployment.** No worker plus web split, no
  supervisor. Deploy them as two applications if you need both.
- **Anything that must bind a fixed port.** The port is assigned to you.
- **Anything needing root**, systemd units of its own, or Docker.

## What the host sees, and what confines you

Your source code is visible to the host. It must be: the host's computer runs
it. Terminal Chat and Drive encryption does not cover uploaded application
source.

Guest applications are started as confined systemd user services. Inside one,
your application can see its own source folder, its own data folder, and its
runtime — the rest of the filesystem is read-only and the host's home directory
is replaced with an empty one. It gets the CPU, memory and process limits from
the hosting policy, and cannot reach the platform database.

If the host machine cannot run confined services — a bare SSH session with no
user manager, for example — the deployment is **refused** rather than run with
your full file access. The error explains how to enable it:

```bash
sudo loginctl enable-linger $USER
```

A host who understands the risk can allow unconfined guest applications by
setting `require_confinement` to 0 in the hosting policy. It defaults to on,
because running someone else's code with access to your SSH keys and your
Stratus device key should be a decision, not an accident.

Uploads are also capped on the way in. The upload limit covers the compressed
archive; a separate limit covers what it becomes once unpacked, so a few
megabytes of gzip cannot expand into gigabytes and fill the drive. Guests get
the persistent-data allowance from the hosting policy.

## Approval

When the host requires approval, uploads wait in a queue. The host can open
**Review…** to see the file tree, the size, your `requirements.txt` or
`package.json`, and a list of files that touch the network, start other
programs, or reach outside the project. That is not virus scanning and does not
claim to be — fresh malicious code matches no signature. It exists so approval
is an informed decision rather than a guess.
