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


if __name__ == "__main__":
    unittest.main()
