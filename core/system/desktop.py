"""Opening new terminal windows on a Linux desktop.

Stratus needs this in two places: the host dashboard opens alongside a starting
server, and the workspace opens from a button on that dashboard. Neither should
make the user retype a command.

There is no standard way to do it. Every desktop ships a different terminal with
different arguments — some take the command as separate words after ``-e``,
others want one quoted string, and GNOME switched from ``-e`` to ``--`` years
ago. So the emulators are listed in preference order with the argument shape
each one actually wants, and the first that exists on the machine wins.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

# (binary, builder). The builder returns the full argv for that emulator.
# Ordered by how likely a desktop is to ship it, so the user gets the terminal
# they already recognise.
TERMINALS = (
    ("gnome-terminal", lambda title, command: ["gnome-terminal", "--title", title, "--", "bash", "-lc", command]),
    ("ptyxis", lambda title, command: ["ptyxis", "--title", title, "--", "bash", "-lc", command]),
    ("mate-terminal", lambda title, command: ["mate-terminal", "--title", title, "--", "bash", "-lc", command]),
    ("konsole", lambda title, command: ["konsole", "-p", f"tabtitle={title}", "-e", "bash", "-lc", command]),
    ("xfce4-terminal", lambda title, command: ["xfce4-terminal", "--title", title, "--command", f"bash -lc {shlex.quote(command)}"]),
    ("qterminal", lambda title, command: ["qterminal", "-e", f"bash -lc {shlex.quote(command)}"]),
    ("tilix", lambda title, command: ["tilix", "-t", title, "-e", "bash", "-lc", command]),
    ("terminator", lambda title, command: ["terminator", "-T", title, "-e", f"bash -lc {shlex.quote(command)}"]),
    ("kitty", lambda title, command: ["kitty", "--title", title, "bash", "-lc", command]),
    ("alacritty", lambda title, command: ["alacritty", "-t", title, "-e", "bash", "-lc", command]),
    ("wezterm", lambda title, command: ["wezterm", "start", "--", "bash", "-lc", command]),
    ("lxterminal", lambda title, command: ["lxterminal", "-t", title, "-e", f"bash -lc {shlex.quote(command)}"]),
    ("x-terminal-emulator", lambda title, command: ["x-terminal-emulator", "-e", "bash", "-lc", command]),
    ("xterm", lambda title, command: ["xterm", "-T", title, "-e", "bash", "-lc", command]),
)


def has_display() -> bool:
    """Is there a graphical session to open a window in?

    Over plain SSH there is not, and trying would fail with a confusing error
    rather than an explanation.
    """
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def available_terminal() -> str | None:
    """The first terminal emulator installed on this machine."""
    for binary, _ in TERMINALS:
        if shutil.which(binary): return binary
    return None


def terminal_command(binary: str, title: str, command: str) -> list[str] | None:
    """The argv that makes one particular emulator run a command."""
    for name, build in TERMINALS:
        if name == binary: return build(title, command)
    return None


def open_terminal(title: str, command: str, working_directory: Path | str | None = None) -> bool:
    """Open a new terminal window running a command.

    Returns False rather than raising when there is no graphical session or no
    emulator installed, so the caller can fall back to telling the user what to
    type. `exec bash` keeps the window open after the command exits, which
    matters when the command fails: otherwise the window vanishes taking the
    error with it.
    """
    if not has_display(): return False
    binary = available_terminal()
    if not binary: return False

    if working_directory:
        command = f"cd {shlex.quote(str(working_directory))} && {command}"
    command = f"{command}; exec bash"

    argv = terminal_command(binary, title, command)
    if not argv: return False
    try:
        subprocess.Popen(argv, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def why_not() -> str:
    """A plain explanation of why a window could not be opened."""
    if not has_display():
        return "There is no graphical session here (this looks like a plain SSH login)."
    if not available_terminal():
        return "No terminal emulator was found on this machine."
    return "The terminal emulator could not be started."
