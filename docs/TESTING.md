# Linux validation checklist

Run `./stratus.sh test` first; it must report OK before manual testing.

## Interface

1. `./stratus.sh` opens the full-screen first-run screen, not a text prompt.
2. The drive table lists real mounted drives with free space, and warns when the
   selected filesystem is exFAT/NTFS.
3. The host dashboard opens in a second terminal window on your desktop
   (GNOME, KDE, XFCE, MATE, Cinnamon or LXQt).
4. On a joining device, the join screen lists the host by name; a wrong token
   shows one inline error and keeps the screen open.
5. In Chat, sending a message keeps the conversation open, clears the composer
   and keeps focus there; a second device's message appears within a second.
6. Unread counts appear next to other conversations in the sidebar.
7. `/file /path/to/something.pdf` attaches, and the object appears in Drive.
8. In Drive, upload/download/rename/delete each leave the table on screen and
   refresh it; errors appear as notifications.
9. Escape goes back exactly one level from every screen.
10. Resize the terminal window: no screen breaks or overlaps.

## Websites

1. In the terminal choose **Browser Access** and set a passphrase.
2. Open **Hosted Applications**, select `stratus-chat`, press **Open in Browser**.
   A browser opens and asks for that passphrase.
3. Messages sent in the terminal appear on the website, and messages sent on the
   website appear in the terminal.
4. Inspect the database and confirm no plaintext:
   `sqlite3 <storage>/platform/stratus.db 'select payload from encrypted_messages limit 1;'`
5. Reuse the same sign-in link a second time: it must be refused.
6. Enter a wrong passphrase in the browser: it must refuse and not load messages.
7. Do the same for `stratus-drive`: upload in the browser, download in the
   terminal, and the reverse.

## Confinement

1. Deploy a guest application that tries to read `~/.config/stratus/device.json`
   and confirm it cannot, while its own data directory still works.
2. Check the unit exists while running: `systemctl --user list-units 'stratus-*'`.
3. Stop it from the terminal and confirm the unit is gone.
4. If the host cannot run user services, confirm `runtime.log` says so rather
   than silently running unconfined.

## Platform

1. Clone into a fresh Ubuntu VM and run `./stratus.sh`.
2. Verify loop/CD/boot devices are absent from storage choices.
3. Select an external disk and verify the expected UUID in `config/storage.json`.
4. Confirm built-in apps install automatically and receive ports.
5. Join from a second Linux client through discovery.
6. Confirm two devices get distinct stable IDs and per-device folders.
7. Send General/direct/group encrypted messages and inspect SQLite to confirm plaintext is absent.
8. Upload private/public/direct/group files and confirm ciphertext is stored.
9. Download on an authorized device; verify unauthorized devices cannot list/decrypt the object.
10. Enable Medium hosting, request a deployment remotely, approve it locally, and verify CPU/RAM/data limits.
11. Restart the host and confirm auto-start apps return without trusting stale PIDs.
12. Unmount the configured external disk and confirm startup refuses the UUID mismatch/missing mount.
