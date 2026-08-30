# `core/system/` — Linux integration

Owner: **Vivaswaan Singh Shekhawat**

This is where Python talks to the operating system.

| File | What it does |
|---|---|
| `storage.py` | Find real mounted drives with `lsblk`; record and verify the filesystem UUID. Never formats anything. |
| `sandbox.py` | The confinement policy: the systemd properties that stop a hosted app reading your home directory. |
| `deployment.py` | Archive to running application: safe extraction, dependency install, build, confined start. |
| `discovery.py` | UDP broadcast so clients can find servers. |
| `network.py` | Which address other machines should use, and which addresses may join. |
| `registry.py` | The servers this machine knows about, and which one is running. |
| `desktop.py` | Opening a new terminal window, across fourteen different emulators. |
| `presets.py` | Low, Medium and High hosting policies. |

## The confinement problem

Without `sandbox.py`, a hosted application runs as you, with your whole home
directory in reach — including `~/.config/stratus/device.json`, the private key
protecting every encrypted message on the server. A fifteen-line Flask app could
copy it and the entire encryption design would be pointless.

systemd fixes this with no root required. Guest applications run as transient
**user services** with:

| Property | Effect |
|---|---|
| `ProtectHome=tmpfs` | Your home directory is replaced by an empty one |
| `ProtectSystem=strict` | The whole filesystem is read-only |
| `ReadWritePaths=` | Except the application's own data directory |
| `PrivateTmp=yes` | Its own `/tmp`, invisible to everything else |
| `NoNewPrivileges=yes` | It cannot gain privileges, even via setuid binaries |
| `SystemCallFilter=@system-service` | A seccomp allowlist of permitted kernel calls |
| `MemoryMax` / `CPUQuota` / `TasksMax` | cgroup limits from the hosting policy |

Sandboxing applies to systemd *services*, not to `--scope` units. That is the
reason applications are started as transient units rather than scopes, and why
the deployment layer tracks them with `systemctl` rather than a raw PID.

## Graceful degradation

Not every machine can do this. A bare SSH session without lingering has no user
manager at all. `deployment.py` tries three tiers in order:

1. Full sandbox with resource limits
2. Resource limits only, for older systemd or kernels
3. A plain child process

If it lands on tier 3 it **writes a warning into the application log** rather
than pretending the confinement is there. Claiming isolation you do not have is
worse than having none.

## Safe extraction

`safe_extract()` rejects any archive entry whose resolved path escapes the
destination directory, and any symlink or hardlink. Without that check, an
uploaded tar containing `../../../../home/user/.ssh/authorized_keys` would
overwrite exactly that file when unpacked.

## Several servers, one at a time

A person may keep more than one server: one for a class, one for a project
team, one on a drive they carry between machines. `registry.py` keeps two
separate records to make that work:

- `~/.config/stratus/servers.json` — every server this machine has created or
  opened, so the start screen can offer them again
- `~/.config/stratus/active-server.json` — the single server running right now,
  which the shell tooling reads to know what to monitor and back up

Only one runs at a time, because two would collide on the control port and
fight over discovery. "Running" is decided by checking the recorded process id
against `/proc`, never by trusting the file — a crash or a reboot leaves it
behind.

The registry lives in the home directory while the data lives on the drive, so
the two can fall out of step. `servers_on_drive()` reads each server's own
config file, which recovers servers on a reinstalled system or a different
computer. The join token is deliberately **not** written to the drive: anyone
holding the disk would otherwise be able to join.

## Restricting who may join

`allowed_networks` in the hosting policy accepts single addresses, CIDR blocks
and ranges (`192.168.1.50-192.168.1.80`). An empty list means anyone who can
reach the host and knows the token.

The check runs in `setup()`, before a single byte is read from the socket, so a
client outside the range never reaches authentication and cannot probe the
token. Two safeguards matter: **loopback is always allowed**, so a typo cannot
lock the owner out of their own dashboard, and a corrupt stored value **fails
open** rather than bricking the server, leaving the token as the barrier.

Note this restricts *network addresses*, not physical distance. Radio range is
a property of the access point, not something software can set.
