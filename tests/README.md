# `tests/` — the automated test suite

Owner: **Shaurya Jain**

```bash
./stratus.sh test
```

143 tests. They must all pass before anything is committed.

| File | Covers |
|---|---|
| `test_shell.py` | Every shell script parses, uses strict mode, has a shebang, has no Windows line endings, answers `--help`, and returns the right exit codes. Plus the library's own functions. |
| `test_e2ee.py` | Encryption round trips, only named recipients can decrypt, the wrong conversation cannot open a message, version 1 data still decrypts, keys are in the formats WebCrypto imports, private keys never enter a public identity. |
| `test_browser_interop.py` | Runs the **real browser JavaScript** under Node against Python, both directions, including passphrase unsealing. Skipped if Node is absent. |
| `test_control_integration.py` | A real server on a real socket: connection reuse, bad tokens, drive listings carrying no ciphertext, conversation privacy. |
| `test_web_apps.py` | The websites enforce exactly what the terminal enforces. Skipped if Flask is absent. |
| `test_tui.py` | Screens mount and work: chat encrypts before sending, drive decrypts filenames, the drive picker warns about exFAT. |
| `test_registry_and_range.py` | Several servers on one drive, only one running, drive rediscovery, and the join-range restriction including the rule that the host can never lock itself out. |
| `test_security_review.py` | Regressions for five issues found in a security review: reused file identifiers destroying data, decompression bombs, guest code running unconfined, sign-in tickets not bound to their application, and groups naming devices that do not exist. |
| `test_desktop.py` | Terminal-window launching: the argv built for each of fourteen emulators, quoting, and graceful failure when there is no display. |
| `test_builtin_updates.py` | The built-in websites follow the installed Stratus: source fingerprinting, and in-place replacement that keeps the port and stored data. |
| `test_manifest.py` | Manifest parsing and rejection of invalid ones. |

## What is worth noticing

**The interop test is the unusual one.** It writes a small Node bridge, loads
the actual `stratus-crypto.js` that browsers load, and checks that Python can
read what JavaScript wrote and the reverse. Two independent implementations of
one format will drift apart eventually; this catches it the moment they do.

**The shell tests treat Bash as a real deliverable.** They caught two genuine
bugs during development: `human_size` truncating fractions because bash
arithmetic is integer-only, and two scripts not answering `--help` like the
rest.

**Several tests assert an absence.** That plaintext is *not* in the database,
that a private key is *not* in an outbound identity, that a device outside a
conversation gets an *empty* list. Those are the tests that would catch a
security regression, and they are the reason the encryption claims in the
documentation can be made without hedging.

## Running a subset

```bash
python -m unittest tests.test_shell -v          # just the shell layer
python -m unittest tests.test_e2ee -v           # just the cryptography
python -m unittest discover -s tests            # everything
```

Two suites skip themselves cleanly when an optional dependency is missing, so a
minimal machine still gets a meaningful run rather than a wall of errors.
