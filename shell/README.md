# `shell/` — the operations layer

2,347 lines of Bash. This is how Stratus is installed, diagnosed, monitored,
maintained and kept running. The Python platform is the application; these
scripts are how a person actually operates it.

Every script is heavily commented in place. `docs/GUIDE.md` section 3 explains
the design decisions behind them.

| File | Lines | What it does |
|---|---|---|
| `lib/common.sh` | 278 | Shared functions. Sourced by every other script, never run directly. |
| `install.sh` | 249 | Check the OS, verify and install dependencies, build the Python environment, set permissions. |
| `doctor.sh` | 298 | Twelve groups of health checks. Run this first when something is wrong. |
| `monitor.sh` | 239 | Read CPU and memory from `/proc`, check ports, restart dead applications. |
| `logs.sh` | 279 | Rotate, compress, prune and analyse logs. |
| `backup.sh` | 262 | Create, list, verify and restore backups with retention. |
| `netcheck.sh` | 220 | Diagnose the network, open firewall ports, scan for servers. |
| `service.sh` | 233 | Generate systemd units so Stratus starts at boot. |
| `servers.sh` | 241 | List the servers on this machine, scan a drive for others, remove one from the list, or delete one and all of its data. |

## Conventions

Every script:

- starts with `#!/usr/bin/env bash` — finds bash through `PATH` rather than
  assuming `/bin/bash`
- sets `set -euo pipefail` — stop on the first error, treat undefined variables
  as errors, and let a failure anywhere in a pipeline fail the pipeline
- sources `lib/common.sh` for logging, colour and helpers
- accepts `--help`
- exits 0 on success and non-zero on failure, so scripts can be chained

`lib/common.sh` deliberately does **not** set `set -euo pipefail`, because it is
sourced into other shells and must not change their behaviour.

`tests/test_shell.py` enforces all of this.

## Running them

Always through the entry point, which sets up paths first:

```bash
./stratus.sh doctor
./stratus.sh monitor --watch
./stratus.sh logs --analyse
./stratus.sh backup --to /mnt/usb
```
