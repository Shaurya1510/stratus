# `core/` — the platform

3,968 lines of Python, split into four subpackages plus two entry points.
`docs/GUIDE.md` section 4 explains each file in detail.

| Path | What lives there |
|---|---|
| `server.py` | The host. Listens on TCP, authenticates, dispatches every action. |
| `client.py` | Command-line client, used by scripts and by the monitor. |
| `security/` | Encryption and identity — the part that makes messages private. |
| `platform/` | Storage, wire protocol, and the membership rules. |
| `system/` | Linux integration — drives, sandboxing, deployment, networking. |
| `interface/` | Everything the user sees in the terminal. |

## Import style

`core` is a normal Python package. Modules import each other by full path:

```python
from core.security.e2ee import encrypt
from core.platform.database import Database
```

Entry points are run as modules from the project root:

```bash
python -m core.server --help
python -m core.interface.terminal
```

`stratus.sh` does this for you.

## The one rule

Any code that decides **who may read what** belongs in
`platform/platform_api.py` and nowhere else. The control server and both
websites all call those functions. Duplicating a membership check is how two
code paths quietly come to disagree about who can see a private message.
