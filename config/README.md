# config

One example file. Stratus does not read anything from this folder at runtime.

## stratus.conf.example

Defaults a host can copy and edit when running the control server by hand
instead of through the interface:

| Setting | Meaning |
|---|---|
| `CONTROL_BIND` | Which address the control server listens on. `0.0.0.0` accepts the local network; `127.0.0.1` would accept only this machine |
| `CONTROL_PORT` | The control port other devices connect to |
| `STORAGE_ROOT` | Where server data lives. The interface picks this for you from the drives it finds |
| `APP_PORT_MIN` / `APP_PORT_MAX` | The range hosted applications are given ports from |

## Where the real settings live

Nothing secret belongs here, and nothing here is loaded automatically.

- **Per-machine state** — `~/.config/stratus/`: this device's identity and
  private key (`device.json`, mode 600), the list of known servers
  (`servers.json`), which server is running (`active-server.json`), pinned peer
  keys (`known-peers.json`), and join tokens (`tokens/`, mode 600).
- **Per-server state** — on the drive you chose, under
  `StratusData/servers/<name>/`: the database, encrypted objects, deployments
  and logs. Its own `config/storage.json` records the server's name and port so
  it can be found again on a machine that has never seen it. **The join token is
  deliberately not stored there** — anyone holding the drive would otherwise be
  able to join.
- **Hosting policy** — in the server's database, edited from the Admin screen:
  whether guests may deploy, whether approval is required, resource limits, and
  which network ranges may join.
