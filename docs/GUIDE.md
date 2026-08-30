# Stratus — complete code guide

This document explains every folder and every file in the project: what it is
for, how it works, and why it was built that way. Read it top to bottom the
first time; after that use it as a reference.

---

## Contents

1. [What happens when you run it](#1-what-happens-when-you-run-it)
2. [Directory map](#2-directory-map)
3. [The shell layer — `shell/`](#3-the-shell-layer--shell)
4. [The Python core — `core/`](#4-the-python-core--core)
5. [The websites — `web/`](#5-the-websites--web)
6. [The tests — `tests/`](#6-the-tests--tests)
7. [How data flows through the system](#7-how-data-flows-through-the-system)
8. [Where everything is stored](#8-where-everything-is-stored)
9. [Linux concepts used, explained](#9-linux-concepts-used-explained)
10. [Who owns what](#10-who-owns-what)

---

## 1. What happens when you run it

Start here, because it makes the rest of the guide make sense.

You type `./stratus.sh`. That script works out where the project lives, sources
the shared shell library, sees no arguments, and launches the Python setup
interface. You pick a drive, name the server, choose a join token. Python writes
a folder structure onto that drive, starts the server as a background process,
and records where everything is in `~/.config/stratus/active-server.json`.

The server now listens on TCP port 5050 for clients, answers UDP broadcasts on
port 5051 so other machines can find it, and starts the two built-in websites as
child processes.

On a second Linux machine, someone runs `./stratus.sh terminal`. That broadcasts
on 5051, finds your server, asks for the token, and opens a full-screen
workspace. When they send a message, their machine encrypts it for every member
before it leaves, and your host stores ciphertext it cannot read.

Meanwhile the shell layer keeps the whole thing alive: `monitor.sh` checks the
applications every 15 minutes and restarts what has died, `logs.sh` stops the
logs eating the disk, `backup.sh` preserves the data, and `doctor.sh` tells you
what is wrong when something breaks.

---

## 2. Directory map

```
stratus.sh              The one command. Everything goes through here.
requirements.txt        The two Python libraries this project depends on.

shell/                  THE SHELL LAYER — installation and operations
  lib/common.sh         Shared functions every script sources
  install.sh            Set up a machine from scratch
  doctor.sh             Diagnose what is broken
  monitor.sh            Watch applications, restart the dead ones
  logs.sh               Rotate, prune and analyse logs
  backup.sh             Back up, verify and restore
  netcheck.sh           Check the network, open firewall ports
  service.sh            Run Stratus at boot via systemd
  servers.sh            List servers, scan a drive, remove or delete one

core/                   THE PLATFORM — Python
  server.py             The host: listens, authenticates, dispatches
  client.py             Command-line client
  security/             Encryption and identity
  platform/             Storage, protocol, membership rules
  system/               Linux integration: drives, sandboxing, deployment
  interface/            Everything the user sees

web/                    THE WEBSITES — browser clients
  chat/                 Encrypted chat in a browser
  drive/                Encrypted files in a browser

tests/                  143 automated tests
docs/                   This guide and the other documentation
examples/               Sample projects you can deploy
config/                 Example configuration
```

The separation is deliberate: **`shell/` operates the system, `core/` is the
system, `web/` is one view of it, `tests/` proves it works.**

---

## 3. The shell layer — `shell/`

2,347 lines of Bash. This is where the Linux and Shell Programming content of
the project lives. Every script is heavily commented in place — this section
explains the design; the files explain the lines.

### `lib/common.sh` — 278 lines

Sourced by every other script. Sourcing means "run this file inside my own
shell", so its functions become part of the calling script.

**What it provides**

| Group | Functions | Notes |
|---|---|---|
| Paths | `STRATUS_ROOT`, `STRATUS_SHELL_DIR`, `STRATUS_VENV` | Found from `BASH_SOURCE[0]`, so scripts work from any directory |
| Colour | `C_RED`, `C_GREEN`, … | Set via `tput`; empty when output is not a terminal |
| Logging | `say` `info` `ok` `warn` `fail` `note` `section` `die` | Warnings and errors go to stderr |
| Checks | `have` `need` `require_linux` `require_not_root` | |
| System | `distro_id` `distro_name` `uses_apt` | Reads `/etc/os-release` |
| Format | `human_size` `label` | |
| State | `server_configured` `server_field` `platform_db` `db_query` | Reads the active server's JSON and SQLite |
| Process | `pid_alive` `pid_rss_kb` `port_listening` | Reads `/proc` and `ss` |
| Network | `lan_address` | Via `ip route get` |

**Three design decisions worth understanding**

*Colour only when a human is watching.* `[[ -t 1 ]]` tests whether stdout is a
terminal. When you pipe output to a file, colour is disabled automatically, so
the file contains clean text instead of escape codes.

*Diagnostics go to stderr.* `warn` and `fail` write to file descriptor 2. This
matters because `value="$(some_function)"` captures stdout only — if warnings
went to stdout they would end up inside the captured value and corrupt it. There
is a test for this.

*The double-source guard.* The first two lines return early if the library is
already loaded, so sourcing it twice is harmless.

### `install.sh` — 249 lines

Turns a bare Linux machine into one that can run Stratus.

1. Checks the OS and refuses to run as root
2. Verifies ten required commands and eleven optional ones
3. Checks Python is 3.10 or newer
4. Installs missing packages with `apt-get`, then **verifies they actually
   arrived** rather than trusting the exit code
5. Builds the Python virtual environment and installs dependencies
6. Confirms the libraries import
7. Makes scripts executable and creates config directories with mode 700
8. Reports whether application confinement is available

**The technique to notice** is the `binary:package` table. The command you test
for and the package that provides it are often different — `ss` comes from
`iproute2`, `lsblk` from `util-linux`. The pairs are split with parameter
expansion, which runs entirely inside bash without spawning a process:

```bash
entry="ss:iproute2"
binary="${entry%%:*}"    # ss          — strip the longest match of :* from the end
package="${entry##*:}"   # iproute2    — strip everything up to the last colon
```

### `doctor.sh` — 298 lines

Twelve groups of checks, each printing pass, warning or failure, then a summary.
Exits 0 when healthy and 1 when anything failed, so it composes:
`./stratus.sh doctor && ./stratus.sh service start`.

It checks: the system, ten required commands, the Python environment, the device
identity file **and its permissions**, the configured server, whether the process
is actually alive, whether the ports are listening, storage space and filesystem
type, **the drive UUID against the recorded one**, the platform database, every
hosted application, networking, the firewall, and confinement availability.

The UUID check is the one people find surprising. Stratus records which drive it
was set up on. If you plug in a different USB stick that happens to mount at the
same path, the check catches it instead of writing your server data onto a
stranger's disk.

### `monitor.sh` — 239 lines

Reads resource usage directly from the kernel, with no external monitoring
tools.

`cpu_percent()` is the most interesting function in the shell layer. Linux
exposes `/proc/<pid>/stat`, a single line of numbers. Field 14 is `utime` and
field 15 is `stime` — CPU time in user and kernel mode, counted in clock ticks.
Field 22 is `starttime`. So:

```
cpu% = 100 × (utime + stime) / clock_ticks / (uptime − starttime/clock_ticks)
```

There is one trap: field 2 is the process name in parentheses and can itself
contain spaces, which shifts every field after it. The awk code finds the
closing bracket first and counts from there.

`--repair` restarts anything that has died, asking systemd first and falling
back to the Python client. `--watch` redraws every few seconds, and uses `trap`
to restore the cursor when you press Ctrl+C.

### `logs.sh` — 279 lines

Four modes. **Summary** lists every log with size and age. **Rotate** renames
logs past 10 MB, shifts the older generations down, compresses with `gzip`, and
deletes anything past the retention window. **Analyse** is the text-processing
showcase. **Tail** follows one application's log.

The rotation uses a detail worth knowing: after copying the log it truncates the
original with `: > file` rather than deleting it. Truncating keeps the same
inode, so a process still holding the file open keeps writing into it. Deleting
would leave that process writing to a file with no name, and the output would
vanish.

The analysis pipeline is the classic Unix chain:

```bash
grep -hiE 'error|exception|failed' $files       # select the interesting lines
  | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}...//g; s/\b[0-9]+\b/N/g'
                                                # normalise timestamps and numbers
  | sort | uniq -c | sort -rn                   # count duplicates, rank them
  | head -8
```

The `sed` step is what makes it work. Without it every line is unique because of
its timestamp, so nothing groups. Replacing timestamps and numbers with a
placeholder turns a thousand distinct lines into eight recurring messages.

### `backup.sh` — 262 lines

Create, list, verify and restore.

The parts that matter for real safety:

- **Checks free space first**, using the uncompressed size as a pessimistic
  estimate, rather than filling the disk and failing halfway
- **Verifies every archive immediately** by reading it back, and deletes it if
  it does not read — an unverified backup is only a hope
- **Writes a SHA-256 checksum** so a future restore can prove the file has not
  rotted
- **Restore moves the current data aside** instead of deleting it, and refuses
  to run while the server is up

Retention keeps the newest N archives. Backups that fill the drive they protect
are a real failure mode.

### `netcheck.sh` — 220 lines

Diagnoses the three things joining depends on: an address other machines can
reach, an open control port, and working UDP broadcast.

`--fix` opens the firewall — but only for private address ranges
(192.168/16, 10/8, 172.16/12). Opening a port to the whole internet when the
host happens to be on a public network would expose the server far beyond the
classroom.

It also checks the ARP table for other devices. If the only thing visible is the
gateway, the access point probably has **client isolation** enabled, which stops
devices on the same Wi-Fi talking to each other. Stratus cannot work through
that, and the symptom looks exactly like a bug in the software, so the script
names it.

### `service.sh` — 233 lines

Generates systemd **user** units — no root required — so the server starts at
boot and restarts if it crashes. Also installs a timer that runs the monitor and
log rotation every fifteen minutes.

Unit files are written with heredocs. The server unit contains the join token,
so it is written with mode 600.

---

## 4. The Python core — `core/`

3,968 lines. Four subpackages plus two entry points.

### `core/security/` — encryption and identity

**`e2ee.py` (106 lines)** — the envelope encryption every message and file uses.

The scheme: generate a random content key, encrypt the content once with it,
then wrap that content key separately for each recipient using a key agreement
with their public key. The host stores the ciphertext plus one small envelope
per recipient, and can open none of them.

Two versions exist. Version 1 used X25519 and ChaCha20-Poly1305. Version 2 —
what is written today — uses **P-256 ECDH, HKDF-SHA256 and AES-256-GCM**,
because browsers implement those natively in WebCrypto and do not implement
X25519 or ChaCha20. That choice is what allows the terminal and the website to
read the same conversation without anyone hand-writing cryptography in
JavaScript. Version 1 data still decrypts.

**`identity.py` (88 lines)** — each device generates a random ID, an
authentication secret and a P-256 key pair, stored in
`~/.config/stratus/device.json` with mode 600. `public_identity()` is a
whitelist, so the private key cannot leak by accident — there is a test that
proves it. `pin_peer_keys()` implements trust-on-first-use: a device's key is
remembered the first time it is seen, and a later change raises an error instead
of silently accepting a possible impostor.

**`browser_access.py` (63 lines)** — seals the device private key with a
passphrase using PBKDF2-SHA256 at 600,000 iterations plus AES-256-GCM. The
sealed blob is stored on the host, which cannot open it. The browser downloads
it and unseals it locally with the passphrase you type. This is what lets a
website read your encrypted messages without the host ever holding your key.

### `core/platform/` — storage, protocol and rules

**`database.py` (384 lines)** — SQLite with WAL mode and a lock around a shared
connection. Tables for deployments, events, devices, hosting policy, deployment
requests, encrypted messages, encrypted files, conversations and members, app
tickets and sealed key blobs. Encrypted file *contents* live on disk; only
metadata and envelopes are in the database, so listing a drive is instant no
matter how large the files are.

**`platform_api.py` (78 lines)** — the membership rules, in one place. Who may
read a conversation, who a file may be shared with, whether an envelope set
matches what it claims. Both the control server and the websites call these
functions, so the two paths cannot drift apart and quietly disagree about who
can see what.

**`protocol.py` (36 lines)** — length-prefixed JSON framing over TCP. Four bytes
of length, then that many bytes of JSON. Simple and unambiguous.

**`session.py` (114 lines)** — the client side. Keeps **one connection open** and
reuses it, reconnecting automatically when it drops. Measured at 13× faster per
request than opening a new connection each time. Also caches directory lookups
for a few seconds, since they are needed to encrypt every single message.

### `core/system/` — Linux integration

**`storage.py` (89 lines)** — finds real mounted drives with `lsblk --json`,
filtering out loop devices, snaps and boot partitions. Records the filesystem
UUID so a wrong drive is detected. Never formats anything.

**`sandbox.py` (104 lines)** — the confinement policy. Builds the systemd
properties that stop a hosted application reading your home directory:
`ProtectHome=tmpfs`, `ProtectSystem=strict`, `ReadWritePaths` limited to the
app's own data, `PrivateTmp`, `NoNewPrivileges`, a syscall filter, and cgroup
CPU/memory/task limits. Sandboxing applies to systemd *services*, not to
`--scope` units, which is why applications run as transient units.

**`deployment.py` (282 lines)** — takes an uploaded archive to a running
application. Safe extraction (path traversal, symlinks and hardlinks rejected),
manifest parsing, port allocation, dependency install for Python and Node, the
optional build step, then a confined start with a three-tier fallback: full
sandbox, then resource limits only, then a plain process with a warning written
to the log. It never pretends an app is confined when it is not.

**`network.py`** and **`discovery.py`** — finding the LAN address, the UDP
broadcast protocol that lets clients discover servers, and the address matching
behind the join restriction (single addresses, CIDR blocks and ranges, with
loopback always permitted so the owner cannot lock themselves out).

**`registry.py`** — the servers this machine knows about and which one is
running. Several servers may exist; only one runs at a time. Servers can also
be rediscovered by scanning a drive, because the registry lives in the home
directory while the data lives on the disk.

**`presets.py` (5 lines)** — the Low, Medium and High hosting policies.

### `core/interface/` — what the user sees

**`app.py` (1,759 lines)** — the entire terminal interface, built with Textual.
Every screen follows three rules: no network call on the interface thread,
screens stay open and update in place, and Escape always goes back exactly one
level. Contains the join screen, dashboard, chat, drive, applications, members,
host dashboard, first-run setup and every dialog.

**`terminal.py`**, **`admin.py`**, **`setup.py`** — the entry points for joining,
host administration and first-run, each falling back to plain menus if Textual
is missing. **`menu.py`** — the curses fallback menu.

### `core/server.py` (293 lines)

The host. One connection serves many requests; the token is checked on every
frame but the device is only re-registered every 30 seconds. Admin rights come
from connecting over loopback. Every action that touches encrypted data goes
through `platform_api`, so the rules are enforced in exactly one place.

Also contains `review_notes()`, which inspects an upload before the host
approves it and flags files that reach the network, start other programs, or
read outside the project.

---

## 5. The websites — `web/`

Two Flask applications, deployed as ordinary Stratus apps owned by `system`.

Each has `app.py` (the API), `templates/` (the pages) and
`static/stratus-crypto.js` — the browser's implementation of the same envelope
format the terminal uses, written entirely against WebCrypto.

**These servers never see plaintext.** The browser holds the device key, unsealed
locally with your passphrase, and does all encryption and decryption itself. The
Flask app stores and returns ciphertext, exactly like the control server.

The websites import `database.py` and `platform_api.py` — flat copies bundled
into the deployment at install time — so they enforce the same membership rules
as the terminal.

One inherent limit, stated plainly: the host serves the JavaScript that does the
browser cryptography. A malicious host could tamper with it. That is true of
every end-to-end encrypted web application, and it is why the terminal remains
the stronger path against a hostile host.

---

## 6. The tests — `tests/`

143 tests. Run them with `./stratus.sh test`.

| File | Covers |
|---|---|
| `test_shell.py` | Every script parses, uses strict mode, has a shebang, no CRLF, `--help` works, exit codes are right, library functions are correct |
| `test_e2ee.py` | Round trips, only named recipients can decrypt, wrong conversation fails, version 1 still readable, keys are in WebCrypto's formats, private keys never leave |
| `test_browser_interop.py` | Runs the **actual browser JavaScript** under Node against Python — both directions |
| `test_control_integration.py` | A real server: keep-alive, bad tokens, drive listing carries no ciphertext, conversation privacy |
| `test_web_apps.py` | The websites enforce the same rules as the terminal |
| `test_tui.py` | Screens mount, chat encrypts before sending, drive decrypts names |
| `test_registry_and_range.py` | Several servers, one running at a time, drive rediscovery, join-range enforcement |
| `test_manifest.py` | Manifest parsing and rejection |

---

## 7. How data flows through the system

**Sending a message**

```
You type in the terminal
  → client asks the host for the member list        (cached a few seconds)
  → client generates a random content key
  → encrypts the message once with AES-256-GCM
  → wraps that key separately for each member       (P-256 ECDH + HKDF)
  → sends ciphertext + envelopes over the open TCP connection
  → host checks the envelope set matches the conversation membership
  → host stores it, still unable to read a word of it
  → other clients poll, receive it, and decrypt with their own private key
```

**Deploying an application**

```
Member selects a folder containing stratus.yaml
  → client tars it and sends it
  → host checks policy: is guest hosting on, is the runtime allowed,
    is it within the size limit, is the device under its app limit
  → if approval is required, it waits in a queue and the host reviews
    the file tree, dependencies and flagged patterns
  → on approval: safe extraction, dependency install, build step
  → a free port is allocated
  → started as a confined systemd user service
  → the address appears in Hosted Applications
```

---

## 8. Where everything is stored

**On the drive you chose** — this is the data that matters:

```
StratusData/servers/<name>/
  config/          storage UUID, host policy
  platform/        stratus.db — devices, messages, files, deployments
  users/<id>/      per-device folders
  drive/objects/   encrypted file contents
  deployments/<id>/source/     uploaded application code
  app-data/<id>/   each application's own writable directory
  logs/            server and application logs
  backups/         archives from backup.sh
```

**In your home directory:**

```
~/.config/stratus/device.json           your identity and private key (mode 600)
~/.config/stratus/known-peers.json      pinned keys of devices you have met
~/.config/stratus/active-server.json    which server this machine runs
~/.local/share/stratus/runtimes/        virtual environments and node_modules
```

**In the project folder:** only code. Nothing here is precious — you can delete
the checkout and clone it again without losing a single message.

---

## 9. Linux concepts used, explained

| Concept | Where | What it is |
|---|---|---|
| `/proc` | `monitor.sh`, `deployment.py` | A pretend filesystem where the kernel exposes live process data as readable files |
| systemd user services | `sandbox.py`, `service.sh` | Services owned by you, needing no root, managed with `systemctl --user` |
| cgroups | `sandbox.py` | Kernel groups that cap the CPU, memory and process count a service may use |
| Namespaces | `sandbox.py` | What `ProtectHome` uses to give a process a different view of the filesystem |
| Syscall filtering | `sandbox.py` | A seccomp allowlist of kernel calls a process may make |
| `lsblk` / `findmnt` | `storage.py`, `doctor.sh` | Query block devices and mount points as structured data |
| `ss` | `common.sh`, `netcheck.sh` | Socket statistics; the modern replacement for `netstat` |
| UDP broadcast | `discovery.py` | One packet to every device on the network at once |
| File descriptors | throughout | 0 stdin, 1 stdout, 2 stderr; `>&2` writes to stderr |
| Exit codes | every script | 0 success, non-zero failure; what makes `&&` and `\|\|` work |
| Inodes | `logs.sh` | The real identity of a file; truncating keeps it, deleting does not |
| Process groups | `deployment.py` | Signal a whole process tree at once with `killpg` |

---

## 10. Who owns what

| Area | Owner | Files |
|---|---|---|
| **Security** | Shaurya Jain | `core/security/` — encryption, identity, key sealing; and `tests/` |
| **Interface** | Vansh Negi | `core/interface/`, `web/`, documentation |
| **Linux systems** | Vivaswaan Singh Shekhawat | `shell/`, `core/system/` — sandboxing, deployment, storage |

Shared: integration, multi-device testing, the report.

---

## Where to look when something breaks

| Symptom | Start here |
|---|---|
| Anything at all | `./stratus.sh doctor` |
| Clients cannot find the server | `./stratus.sh netcheck --scan` |
| An application keeps dying | `./stratus.sh logs --tail NAME` |
| Disk filling up | `./stratus.sh logs --rotate` |
| Something used to work | `./stratus.sh backup --list` |
