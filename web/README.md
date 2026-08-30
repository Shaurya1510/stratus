# `web/` — the browser clients

Two Flask applications that open the **same encrypted conversations and files**
as the terminal. They are deployed as ordinary Stratus applications owned by
`system`, so they appear in Hosted Applications like any other app.

| Directory | What it is |
|---|---|
| `chat/` | Encrypted chat in a browser |
| `drive/` | Encrypted files in a browser |

Each contains `app.py` (the API), `templates/` (the pages), and
`static/stratus-crypto.js` (the browser's implementation of the envelope
format, written entirely against WebCrypto).

## These servers never see plaintext

The browser holds the device key — downloaded as a passphrase-sealed blob and
unsealed locally — and does all encryption and decryption itself. `app.py`
stores and returns ciphertext, exactly like the control server.

They import flat copies of `database.py` and `platform_api.py`, bundled into the
deployment at install time, so they enforce the same membership rules as the
terminal rather than reimplementing them.

## How you sign in

There is no username or password, on purpose. Your identity is your Linux
device. The flow:

1. In the terminal, choose **Browser Access** once and pick a passphrase. Your
   device key is sealed with it and stored on the host, which cannot open it.
2. Open **Hosted Applications**, select the app, press **Open in Browser**.
3. The terminal asks the host for a **single-use ticket valid for 60 seconds**
   and opens `http://host:port/device-login?ticket=…`
4. The website exchanges the ticket for a session, downloads your sealed key,
   and asks for the passphrase to unseal it in your browser.

The device secret never travels in a URL. Web servers log request lines, and
that log is stored on the host and copied into backups — an earlier version of
this project made exactly that mistake.

## The honest limitation

The host serves the JavaScript that does the browser cryptography. A host who
tampered with it could capture keys as they are typed. This is true of every
end-to-end encrypted web application, and it is why the terminal client remains
the stronger path against a hostile host. Against everyone else — other members,
someone who copies the drive, a host reading the database — the browser is
protected.

`tests/test_browser_interop.py` runs the real `stratus-crypto.js` under Node and
checks it against Python in both directions, so the two implementations cannot
silently drift apart.
