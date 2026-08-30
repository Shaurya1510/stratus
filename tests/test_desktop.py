"""Opening terminal windows across desktops.

Every Linux terminal emulator takes its command differently, and a wrong
argument shape produces a window that flashes and disappears rather than an
error. These check the argv built for each one, without launching anything.
"""
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.system import desktop


class TerminalCommandTests(unittest.TestCase):
    def test_every_listed_emulator_builds_a_command(self):
        for binary, _ in desktop.TERMINALS:
            with self.subTest(terminal=binary):
                argv = desktop.terminal_command(binary, "Title", "echo hello")
                self.assertIsInstance(argv, list)
                self.assertEqual(argv[0], binary)
                self.assertGreater(len(argv), 1)

    def test_the_command_survives_into_the_arguments(self):
        # Either as its own word, or inside a quoted string for the emulators
        # that want one. Both must still contain the command text.
        for binary, _ in desktop.TERMINALS:
            with self.subTest(terminal=binary):
                argv = desktop.terminal_command(binary, "Title", "./stratus.sh terminal")
                self.assertTrue(any("./stratus.sh terminal" in part for part in argv),
                                f"{binary} lost the command: {argv}")

    def test_gnome_style_uses_the_double_dash_separator(self):
        # gnome-terminal dropped -e years ago; using it prints a deprecation
        # warning and, on newer versions, refuses outright.
        argv = desktop.terminal_command("gnome-terminal", "T", "echo hi")
        self.assertIn("--", argv)
        self.assertNotIn("-e", argv)

    def test_emulators_taking_one_string_get_it_quoted(self):
        # xfce4-terminal takes the whole command as a single argument, so a
        # command containing spaces must be quoted or only the first word runs.
        argv = desktop.terminal_command("xfce4-terminal", "T", "./stratus.sh terminal")
        joined = [part for part in argv if "stratus" in part][0]
        self.assertIn(shlex.quote("./stratus.sh terminal"), joined)

    def test_an_unknown_emulator_returns_nothing(self):
        self.assertIsNone(desktop.terminal_command("not-a-terminal", "T", "echo"))


class AvailabilityTests(unittest.TestCase):
    def test_no_display_means_no_window(self):
        with patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": ""}, clear=False):
            self.assertFalse(desktop.has_display())
            self.assertFalse(desktop.open_terminal("T", "echo hi"))

    def test_wayland_alone_counts_as_a_display(self):
        with patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}, clear=False):
            self.assertTrue(desktop.has_display())

    def test_the_first_installed_emulator_is_chosen(self):
        with patch.object(desktop.shutil, "which", side_effect=lambda name: name == "konsole"):
            self.assertEqual(desktop.available_terminal(), "konsole")

    def test_none_installed_is_reported_not_raised(self):
        with patch.object(desktop.shutil, "which", return_value=None):
            self.assertIsNone(desktop.available_terminal())
            with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False):
                self.assertFalse(desktop.open_terminal("T", "echo hi"))
                self.assertIn("No terminal emulator", desktop.why_not())


class LaunchTests(unittest.TestCase):
    def launch(self, **kwargs):
        """Capture the argv that would be launched, without launching it."""
        captured = {}

        def fake_popen(argv, **_):
            captured["argv"] = argv
            return object()

        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False), \
             patch.object(desktop.shutil, "which", side_effect=lambda name: name == "gnome-terminal"), \
             patch.object(desktop.subprocess, "Popen", fake_popen):
            result = desktop.open_terminal(**kwargs)
        return result, captured.get("argv", [])

    def test_the_working_directory_is_entered_first(self):
        ok, argv = self.launch(title="T", command="./stratus.sh terminal", working_directory="/home/me/Stratus")
        self.assertTrue(ok)
        script = argv[-1]
        self.assertIn("cd /home/me/Stratus", script)
        self.assertLess(script.index("cd "), script.index("./stratus.sh"), "must cd before running")

    def test_a_path_with_spaces_is_quoted(self):
        _, argv = self.launch(title="T", command="./stratus.sh terminal",
                              working_directory="/home/me/My Projects/Stratus")
        self.assertIn(shlex.quote("/home/me/My Projects/Stratus"), argv[-1])

    def test_the_window_stays_open_after_the_command_exits(self):
        # Otherwise a failing command closes the window and takes the error with
        # it, which is the single most confusing way for this to break.
        _, argv = self.launch(title="T", command="./stratus.sh terminal")
        self.assertTrue(argv[-1].rstrip().endswith("exec bash"))

    def test_the_title_is_passed_through(self):
        _, argv = self.launch(title="Stratus — Class A", command="echo hi")
        self.assertIn("Stratus — Class A", argv)

    def test_a_failed_launch_is_reported_not_raised(self):
        with patch.dict("os.environ", {"DISPLAY": ":0"}, clear=False), \
             patch.object(desktop.shutil, "which", side_effect=lambda name: name == "gnome-terminal"), \
             patch.object(desktop.subprocess, "Popen", side_effect=OSError("boom")):
            self.assertFalse(desktop.open_terminal("T", "echo hi"))


if __name__ == "__main__": unittest.main()
