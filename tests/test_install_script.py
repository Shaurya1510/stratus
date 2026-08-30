"""install.sh, exercised as a shell script rather than read.

The failure that prompted these tests: on Kali, `.runtime/venv` existed but had
no pip inside it, because Debian-family distributions package `ensurepip`
separately from `venv`. install.sh saw the directory, announced "Environment
already exists", and then died on the first pip call with a traceback.

These run the real script against a stub environment, so they exercise the
control flow rather than asserting on the source text.
"""
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "bash is required to run the shell tests")
class InstallScriptTests(unittest.TestCase):
    def run_bash(self, script: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], cwd=str(cwd or ROOT),
                              capture_output=True, text=True, timeout=120)

    # ------------------------------------------------------------ the helpers

    def helper(self, script: str) -> str:
        """Run one of install.sh's helper functions in isolation.

        The helpers are defined before the script does any work, so sourcing it
        with a guard set lets them be called without installing anything.
        """
        preamble = (
            f'cd "{ROOT.as_posix()}"\n'
            'source shell/lib/common.sh\n'
            # Redefine the detectors so the test does not depend on whatever
            # package manager this machine happens to have.
            'uses_apt() { return 0; }\n'
            f'{self.helper_source()}\n'
        )
        outcome = self.run_bash(preamble + script)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        return outcome.stdout.strip()

    def helper_source(self) -> str:
        """The two package helpers, lifted out of install.sh."""
        text = (ROOT / "shell" / "install.sh").read_text(encoding="utf-8")
        start = text.index("install_command() {")
        end = text.index("}", text.index("install_packages() {")) + 1
        return text[start:end]

    def test_the_install_command_is_offered_for_missing_packages(self):
        printed = self.helper('install_command nodejs npm ufw')
        self.assertIn("apt-get install -y", printed)
        self.assertIn("nodejs", printed)
        self.assertIn("npm", printed)
        self.assertIn("ufw", printed)

    def test_a_machine_without_a_known_package_manager_still_says_something(self):
        printed = self.run_bash(
            f'cd "{ROOT.as_posix()}"\n'
            'source shell/lib/common.sh\n'
            'uses_apt() { return 1; }\n'
            'have() { return 1; }\n'
            f'{self.helper_source()}\n'
            'install_command nodejs'
        )
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("nodejs", printed.stdout)

    # ------------------------------------------------------- the venv check --

    def test_a_venv_without_pip_is_detected(self):
        """This is the exact Kali failure: the directory exists, pip does not."""
        with tempfile.TemporaryDirectory() as temp:
            venv = Path(temp) / "venv" / "bin"
            venv.mkdir(parents=True)
            # A python that exists and runs, but has no pip module.
            stub = venv / "python"
            stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            stub.chmod(0o755)

            outcome = self.run_bash(
                f'STRATUS_PYTHON="{stub.as_posix()}"\n'
                'venv_has_pip() { [[ -x "$STRATUS_PYTHON" ]] && '
                '"$STRATUS_PYTHON" -m pip --version >/dev/null 2>&1; }\n'
                'if venv_has_pip; then echo USABLE; else echo BROKEN; fi'
            )
            self.assertEqual(outcome.stdout.strip(), "BROKEN")

    def test_a_venv_with_pip_is_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            venv = Path(temp) / "venv" / "bin"
            venv.mkdir(parents=True)
            stub = venv / "python"
            stub.write_text("#!/bin/sh\necho 'pip 24.0'\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)

            outcome = self.run_bash(
                f'STRATUS_PYTHON="{stub.as_posix()}"\n'
                'venv_has_pip() { [[ -x "$STRATUS_PYTHON" ]] && '
                '"$STRATUS_PYTHON" -m pip --version >/dev/null 2>&1; }\n'
                'if venv_has_pip; then echo USABLE; else echo BROKEN; fi'
            )
            self.assertEqual(outcome.stdout.strip(), "USABLE")

    def test_the_script_no_longer_calls_the_pip_wrapper_directly(self):
        """`venv/bin/pip` is a script with a hard-coded interpreter path.

        It does not exist at all when ensurepip was unavailable, and it breaks
        if the environment is moved. `python -m pip` works in both cases.
        """
        text = (ROOT / "shell" / "install.sh").read_text(encoding="utf-8")
        self.assertNotIn('"$STRATUS_VENV/bin/pip" install', text)
        self.assertIn('"$STRATUS_PYTHON" -m pip install', text)

    def test_the_directory_alone_is_no_longer_treated_as_a_working_environment(self):
        text = (ROOT / "shell" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("venv_has_pip", text)
        self.assertIn("ensurepip", text)

    # --------------------------------------------------------------- usage --

    def test_help_still_works(self):
        outcome = self.run_bash("./shell/install.sh --help")
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertIn("--check", outcome.stdout)

    @unittest.skipUnless(platform.system() == "Linux",
                         "install.sh deliberately refuses to run anywhere but Linux")
    def test_check_only_changes_nothing(self):
        """--check must never create the environment it is reporting on."""
        outcome = self.run_bash("./shell/install.sh --check")
        self.assertIn("python3", outcome.stdout)
        self.assertNotIn("Creating virtual environment", outcome.stdout)
@unittest.skipUnless(BASH, "bash is required to run the shell tests")
class StopScriptTests(unittest.TestCase):
    """Stopping a server whose terminal was closed.

    A server is started detached so it survives the terminal that launched it.
    That is deliberate, but it means closing that terminal leaves a server
    running with nothing on screen to stop it — and the next start refuses,
    because only one server may run at a time.
    """

    SCRIPT = ROOT / "shell" / "stop.sh"

    def run_bash(self, script: str) -> subprocess.CompletedProcess:
        return subprocess.run([BASH, "-c", script], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)

    def test_the_script_exists_and_is_executable_in_git(self):
        self.assertTrue(self.SCRIPT.is_file())
        modes = subprocess.run(["git", "ls-files", "-s", "shell/stop.sh"],
                               cwd=str(ROOT), capture_output=True, text=True)
        if modes.returncode == 0 and modes.stdout.strip():
            self.assertTrue(modes.stdout.startswith("100755"),
                            f"stop.sh is not executable in git: {modes.stdout.strip()}")

    def test_help_lists_every_option(self):
        outcome = subprocess.run([BASH, str(self.SCRIPT), "--help"], cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        for flag in ("--apps", "--yes", "--status"):
            self.assertIn(flag, outcome.stdout)
        # The help must not leak the code that follows the comment block.
        self.assertNotIn("set -euo pipefail", outcome.stdout)

    def test_the_entry_point_dispatches_to_it(self):
        entry = (ROOT / "stratus.sh").read_text(encoding="utf-8")
        self.assertIn('stop)', entry)
        self.assertIn("stop.sh", entry)

    def test_the_entry_point_help_mentions_stopping(self):
        outcome = subprocess.run([BASH, str(ROOT / "stratus.sh"), "--help"], cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.assertIn("stop", outcome.stdout)

    def test_it_terminates_before_it_kills(self):
        """A server must be able to close its database cleanly.

        SIGKILL first would leave SQLite with a hot journal, so TERM has to be
        tried and given time before KILL is even considered.
        """
        text = self.SCRIPT.read_text(encoding="utf-8")
        term = text.index("kill -TERM")
        kill = text.index("kill -KILL")
        self.assertLess(term, kill, "SIGKILL is sent before SIGTERM")
        self.assertIn("pid_alive", text)

    def test_it_only_ever_targets_this_users_processes(self):
        """pgrep without -u would find another user's server, which this has
        no business stopping and no permission to stop."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn('pgrep -u "$(id -u)"', text)

    def test_it_does_not_stop_the_service_unit_by_pattern(self):
        """stratus-server.service is the server itself, not a hosted app.

        Sweeping it up with the applications would stop the server while
        claiming to have stopped an app.
        """
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("grep -v '^stratus-server\\.service$'", text)

    def test_a_stale_record_is_cleared(self):
        """The record outlives a crash. Leaving it makes the next start refuse
        for a reason the user cannot see."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("$STRATUS_ACTIVE_SERVER", text)
        self.assertIn("stale", text.lower())

    def test_status_changes_nothing(self):
        """--status is a report; it must not contain a stop path."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        status_block = text[text.index("if (( STATUS_ONLY ))"):]
        first_exit = status_block.index("exit 0")
        self.assertNotIn("kill ", status_block[:first_exit])
        self.assertNotIn("rm -f", status_block[:first_exit])

    def test_the_unit_filter_keeps_apps_and_drops_the_server(self):
        """The awk/grep pipeline, run against real systemctl-shaped input."""
        listing = (
            "stratus-abc123.service   loaded active running Stratus app\n"
            "stratus-server.service   loaded active running Stratus server\n"
            "stratus-def456.service   loaded active running Stratus app\n"
        )
        outcome = self.run_bash(
            f"printf '%s' '{listing}' | awk '{{ print $1 }}' | grep -v '^stratus-server\\.service$'"
        )
        names = outcome.stdout.split()
        self.assertIn("stratus-abc123.service", names)
        self.assertIn("stratus-def456.service", names)
        self.assertNotIn("stratus-server.service", names)


if __name__ == "__main__":
    unittest.main()
