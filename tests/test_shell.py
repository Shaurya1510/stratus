"""Checks on the shell layer.

The Bash toolchain is as much a deliverable as the Python is, so it gets the
same treatment: every script is parsed, the shared library's pure functions are
exercised, and the conventions that keep the scripts safe (strict mode, a
shebang, being reachable from the entry point) are enforced here rather than
relied on by habit.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "shell"
ENTRY = ROOT / "stratus.sh"
BASH = shutil.which("bash")

SCRIPTS = sorted(SHELL.glob("*.sh")) + [ENTRY]
LIBRARY = SHELL / "lib" / "common.sh"


def run_bash(script: str) -> subprocess.CompletedProcess:
    """Run a snippet with the shared library already sourced."""
    return subprocess.run(
        [BASH, "-c", f'cd "{ROOT.as_posix()}" && source shell/lib/common.sh && {script}'],
        capture_output=True, text=True, timeout=60)


@unittest.skipUnless(BASH, "bash is not available")
class ShellSyntaxTests(unittest.TestCase):
    def test_every_script_parses(self):
        for script in SCRIPTS + [LIBRARY]:
            with self.subTest(script=script.name):
                result = subprocess.run([BASH, "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f"{script.name}: {result.stderr}")

    def test_every_script_declares_an_interpreter(self):
        for script in SCRIPTS + [LIBRARY]:
            with self.subTest(script=script.name):
                first = script.read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(first, "#!/usr/bin/env bash",
                                 f"{script.name} must start with a portable shebang")

    def test_runnable_scripts_use_strict_mode(self):
        # The library is sourced rather than executed, so it must not set the
        # caller's shell options; every other script must.
        for script in SCRIPTS:
            with self.subTest(script=script.name):
                self.assertIn("set -euo pipefail", script.read_text(encoding="utf-8"),
                              f"{script.name} must fail fast on errors and undefined variables")
        self.assertNotIn("set -euo pipefail", LIBRARY.read_text(encoding="utf-8"),
                         "common.sh is sourced and must not change the caller's shell options")

    def test_entry_point_reaches_every_script(self):
        entry = ENTRY.read_text(encoding="utf-8")
        for script in SHELL.glob("*.sh"):
            with self.subTest(script=script.name):
                self.assertIn(script.name, entry,
                              f"{script.name} is unreachable from stratus.sh")

    def test_no_windows_line_endings(self):
        # A carriage return in a shell script makes Linux report
        # "bad interpreter: /usr/bin/env bash^M", which is baffling if you have
        # not seen it before.
        for script in SCRIPTS + [LIBRARY]:
            with self.subTest(script=script.name):
                self.assertNotIn(b"\r\n", script.read_bytes(),
                                 f"{script.name} has Windows line endings and will not run on Linux")


@unittest.skipUnless(BASH, "bash is not available")
class CommonLibraryTests(unittest.TestCase):
    def test_human_size_keeps_the_fraction(self):
        cases = {
            "0": "0 B", "512": "512 B", "1023": "1023 B", "1024": "1.0 KB",
            "1048576": "1.0 MB", "1610612736": "1.5 GB", "1099511627776": "1.0 TB",
        }
        for value, expected in cases.items():
            with self.subTest(bytes=value):
                self.assertEqual(run_bash(f"human_size {value}").stdout.strip(), expected)

    def test_have_detects_commands(self):
        self.assertEqual(run_bash("have bash && echo yes").stdout.strip(), "yes")
        self.assertEqual(run_bash("have zzz_not_a_command || echo no").stdout.strip(), "no")

    def test_project_root_is_resolved(self):
        result = run_bash('[[ -f "$STRATUS_ROOT/stratus.sh" ]] && echo found')
        self.assertEqual(result.stdout.strip(), "found")

    def test_library_is_only_loaded_once(self):
        result = run_bash('source shell/lib/common.sh; echo "$STRATUS_COMMON_LOADED"')
        self.assertEqual(result.stdout.strip(), "1")

    def test_die_exits_non_zero(self):
        result = run_bash('die "something went wrong"')
        self.assertEqual(result.returncode, 1)
        self.assertIn("something went wrong", result.stderr)

    def test_diagnostics_go_to_stderr(self):
        # Warnings must not pollute stdout, or they would corrupt the output of
        # any script whose result is captured with $(...).
        result = run_bash('warn "careful"; fail "broken"; say "data"')
        self.assertEqual(result.stdout.strip(), "data")
        self.assertIn("careful", result.stderr)
        self.assertIn("broken", result.stderr)

    def test_colour_is_disabled_when_not_a_terminal(self):
        # Output is captured here, so stdout is a pipe and must contain no
        # escape sequences.
        result = run_bash('ok "plain text"')
        self.assertNotIn("\x1b[", result.stdout)


@unittest.skipUnless(BASH, "bash is not available")
class ArgumentHandlingTests(unittest.TestCase):
    def test_help_succeeds_for_every_script(self):
        for script in SHELL.glob("*.sh"):
            with self.subTest(script=script.name):
                result = subprocess.run([BASH, str(script), "--help"],
                                        capture_output=True, text=True, timeout=60, cwd=ROOT)
                self.assertEqual(result.returncode, 0, f"{script.name} --help failed")
                self.assertTrue(result.stdout.strip(), f"{script.name} --help printed nothing")

    def test_unknown_option_is_rejected(self):
        result = subprocess.run([BASH, str(SHELL / "logs.sh"), "--not-an-option"],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(result.returncode, 1)

    def test_entry_point_rejects_unknown_commands(self):
        result = subprocess.run([BASH, str(ENTRY), "not-a-command"],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(result.returncode, 2, "an unusable command line should exit 2")

    def test_entry_point_help_lists_the_main_commands(self):
        result = subprocess.run([BASH, str(ENTRY), "--help"],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        self.assertEqual(result.returncode, 0)
        for command in ("install", "doctor", "monitor", "logs", "backup", "netcheck", "service", "test"):
            self.assertIn(command, result.stdout, f"'{command}' is missing from the help text")


if __name__ == "__main__": unittest.main()
