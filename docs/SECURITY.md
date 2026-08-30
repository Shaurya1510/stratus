# Security

Stratus stores private messages and files for a group of people, and runs code
that other people uploaded, on someone's own computer. This document records
what it defends against, what it deliberately does not, and the findings of the
review carried out before the project was published.

---

## 1. The threat model

**What Stratus protects against**

| Threat | Defence |
|---|---|
| The host reading members' messages and files | End-to-end encryption; the host stores only ciphertext and the list of device ids each envelope was sealed for |
| Someone copying the drive | Same — the drive holds no plaintext and no private keys |
| An uninvited device on the network joining | A join token, plus an optional restriction on which addresses may connect at all |
| An uploaded application reading the host's files | systemd confinement: the host's home directory, including the device key, is replaced by an empty tmpfs |
| An uploaded application exhausting the machine | Memory, CPU, task and disk-quota limits, enforced by the kernel through cgroups |
| A malicious archive overwriting host files | Extraction refuses traversing paths, symlinks, hardlinks, and archives that expand beyond a size limit |
| A member impersonating another member | A device's registration is bound to a secret only that device holds |
| A device's key being silently swapped | Trust on first use: a changed key stops the client rather than being accepted |

**What Stratus does not protect against, by design**

- **A hostile host with physical access.** The host controls the machine the
  code runs on. It cannot read stored messages, but it could modify the
  software it serves. This is a local-network project for people who already
  trust each other with a shared computer.
- **Traffic analysis.** The host necessarily knows who is in a conversation,
  when messages were sent, and how large they were. Only content is hidden.
  This is the property the design deliberately keeps: the host "can know who
  the members are" but not what they said.
- **Message authorship against a malicious host.** Envelopes are encrypted but
  not signed. The sender's name comes from the authenticated session, so a
  host that modified its own database could re-attribute a message. Fixing this
  needs signatures and is recorded below as future work.
- **Denial of service.** A member holding the join token can flood the host.
  Members are known people on a local network; the host can block a device.
- **Malware detection.** The review screen reports what an upload *does*, it
  does not decide whether it is safe. See §4.

---

## 2. How the encryption works

Each message and file is encrypted once with a fresh random content key. That
content key is then wrapped separately for every authorised recipient.

```
content key  ──AES-256-GCM──►  ciphertext        (stored by the host)
             ├─wrapped for Alice─►  envelope[alice]
             ├─wrapped for Bob───►  envelope[bob]
             └─wrapped for Cara──►  envelope[cara]
```

Wrapping uses ephemeral P-256 ECDH to the recipient's published public key,
HKDF-SHA256 to derive the wrapping key, and AES-256-GCM to wrap. The recipient's
device id is authenticated data on the wrap, so an envelope cannot be moved to
another recipient. The conversation id (or file id) is authenticated data on the
content, so a message cannot be replayed into a different conversation.

P-256 and AES-GCM were chosen because WebCrypto implements both natively. The
browser therefore uses the same envelope format as the terminal with no
hand-written cryptography, which is what lets one conversation be read from
both.

**Browser access.** The website needs the device's private key. Sending it
through the host would defeat the whole design, so it is sealed with a
passphrase (PBKDF2-SHA256, 600,000 iterations, AES-256-GCM) that the host never
sees. The host stores the sealed blob and hands it back on request; it is
meaningless without the passphrase. The passphrase is never transmitted.

Because WebCrypto is only available in a secure context, the websites work over
HTTPS or on loopback. Members open them on the host itself; reaching them from
another machine over plain HTTP is a known limitation, recorded in §5.

---

## 3. Review findings

A review was carried out against the whole codebase before publication. Five
issues were found, all confirmed by reproducing them rather than by inspection
alone, and all fixed. Each has a regression test in
`tests/test_security_hardening.py`, and each of those tests was checked to fail
against the original code.

### 3.1 Path traversal through a client-chosen device id — high

A device sends its own identifier when it registers. That value was used
directly as a directory name:

```python
user_root = server_root / "users" / device["id"]      # before
```

An identifier of `../../../../tmp/x` created directories and wrote a file
outside the server's storage. Reproduced: the write landed outside the server
root. Any device holding the join token could do it.

The same value is rendered into HTML attributes by both websites, so it was
also a script-injection vector (see 3.4).

**Fixed** by validating the identifier at registration, the single point where
a device enters the system: it must match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` and
may not be `.` or `..`.

### 3.2 A hosted application could become the host — high

Administrator rights were granted by network address:

```python
is_admin = self.client_address[0] in {"127.0.0.1", "::1"}      # before
```

Every uploaded application runs *on the host*, so it connects from 127.0.0.1
too. It needed only the join token — and the token was passed on the server's
command line, where any process belonging to the same user can read it from
`/proc/<pid>/cmdline`. The chain was: read the token, connect to loopback, be
an administrator, turn `require_confinement` off, redeploy unconfined with full
access to the host.

**Fixed** in three independent places, so breaking one does not reopen it:

- Administrator means the host's *own device*, proven by its device secret, and
  not merely an address.
- The join token is passed in a file readable only by the host user, kept in
  `~/.config/stratus/`, which confined applications cannot see at all.
- Confined units get `ProtectProc=invisible` and `ProcSubset=pid`, so `/proc`
  shows an application nothing but itself.

The token comparison was also changed to `secrets.compare_digest`, so response
timing cannot be used to recover it.

### 3.3 The build step ran outside the sandbox — high

Confinement was applied when an application *started*. Getting there runs
`pip install`, `npm install` and the manifest's own `build:` line first, and all
three execute code from the upload: a `setup.py`, a package postinstall script,
or the build command itself. Those ran as the hosting user with the whole home
directory in reach — including `~/.config/stratus/device.json`, the private key
protecting every message on the server.

An attacker never needed the application to start.

**Fixed:** guest build steps now run inside the same confinement, with two
differences a build genuinely needs — it may write to its own source tree and
runtime folder, and it may reach the network to fetch dependencies. The system
call filter is wider than at run time because compilers and package managers
legitimately fork and exec.

### 3.4 Script injection through an HTML attribute — medium

The websites' escaping helper relied on the browser's own serialiser:

```javascript
const esc = (text) => { const n = document.createElement("div");
                        n.textContent = text; return n.innerHTML; };   // before
```

That escapes `<`, `>` and `&` — but **not quotes**. Several values are placed
inside HTML attributes, including attachment filenames and device ids, both of
which a member controls. A filename of `x" onmouseover="…` closed the attribute
and added an event handler. The chat page holds the unsealed device key, so this
was a path to reading a member's messages.

**Fixed:** the escaper now escapes quotes as well. Verified in a real browser:
the crafted filename produces one `data-name` attribute, no event handler, and
the value still reads back intact.

### 3.5 The join token was written into the systemd unit — low

`shell/service.sh` wrote the token into the unit file. The file was already
`chmod 600`, so this was not directly exploitable, but the token appeared in
`systemctl cat` output and could reach the journal.

**Fixed:** the token is written to its own file and referenced by path.

---

## 4. Uploaded applications

Hosting other people's code is the feature with the largest attack surface, so
it is layered:

1. **Off by default.** `guest_deploy_enabled` starts at 0.
2. **Approval required.** The host sees the upload before it runs.
3. **A review screen, not a virus scanner.** `review_notes()` reports which
   files read outside their own folder, start other programs, send data
   elsewhere, reach Stratus's own storage, or build code at runtime. This is
   deliberately not presented as a safety verdict: fresh malicious code matches
   no signature, and claiming otherwise would be worse than saying nothing. It
   makes approval an informed decision instead of a guess at a filename.
4. **Safe extraction.** Traversal, links and decompression bombs are refused
   before a byte is written.
5. **Confinement, and it fails closed.** If the host cannot run confined
   services, a guest application is refused rather than run unconfined —
   unless the host explicitly turns `require_confinement` off, which is
   recorded in the application's log in plain words.
6. **Resource limits** on memory, CPU, tasks and stored data.

---

## 5. Known limitations

These are understood and accepted for a local-network project of this size.

- **Messages are encrypted but not signed.** A malicious host could
  re-attribute a message. Signing envelopes would close this.
- **The websites need a secure context.** They work on the host over loopback;
  reaching them from another machine needs HTTPS, which needs a certificate.
- **The network restriction applies to the control server**, not to the ports
  hosted applications listen on. Reaching those still requires a valid session,
  which requires a control-server ticket.
- **A blocked device may finish requests for up to 30 seconds** before its
  cached record is refreshed.
- **Trust on first use.** The first time a device is seen its key is accepted
  without confirmation in the terminal client. A changed key always stops.
- **The identity file is written and then `chmod`ed**, leaving a brief window
  at default permissions on first creation.

---

## 6. Reporting

This is a university project. If you find something, open an issue describing
what you did and what happened. Please do not include anyone's real message
content in a report.
