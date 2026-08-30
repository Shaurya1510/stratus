#!/usr/bin/env bash
# =============================================================================
# install.sh — prepare this machine to run Stratus
# =============================================================================
# Checks the operating system, verifies every command Stratus depends on,
# installs the missing ones, builds the Python environment and sets file
# permissions. The goal is that a person who has never seen this project can
# clone it and run one command.
#
#   ./stratus.sh install              install everything, asking before apt
#   ./stratus.sh install --yes        never ask (for scripted or CI use)
#   ./stratus.sh install --check      report what is missing, change nothing
# =============================================================================

# set -e   : stop at the first command that fails, instead of ploughing on
# set -u   : treat an undefined variable as an error, which catches typos
# set -o pipefail : a pipeline fails if ANY stage fails, not just the last one
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# -----------------------------------------------------------------------------
# Required commands, written as "binary:package" pairs.
# The binary is what we test for; the package is what apt must install to get
# it. They differ often enough (ss lives in iproute2, lsblk in util-linux) that
# guessing would produce confusing failures.
# -----------------------------------------------------------------------------
REQUIRED=(
    "python3:python3"
    "tar:tar"
    "lsblk:util-linux"
    "findmnt:util-linux"
    "ip:iproute2"
    "ss:iproute2"
    "sqlite3:sqlite3"
    "awk:gawk"
    "sed:sed"
    "grep:grep"
)

# Not required, but each one enables a feature. Their absence is a warning.
OPTIONAL=(
    "systemd-run:systemd:resource limits and application confinement"
    "systemctl:systemd:running applications as supervised services"
    "node:nodejs:hosting Node.js projects"
    "npm:npm:installing Node.js project dependencies"
    "ufw:ufw:opening firewall ports automatically"
    "rsync:rsync:faster incremental backups"
    "xdg-open:xdg-utils:opening hosted applications in a browser"
)

# -----------------------------------------------------------------------------
# Package installation, written once so the required and optional paths cannot
# disagree about how this distribution installs software.
# -----------------------------------------------------------------------------
install_command() {
    if uses_apt;        then printf 'sudo apt-get install -y %s' "$*"
    elif have dnf;      then printf 'sudo dnf install -y %s' "$*"
    elif have pacman;   then printf 'sudo pacman -S --needed %s' "$*"
    elif have zypper;   then printf 'sudo zypper install -y %s' "$*"
    else                     printf 'install with your package manager: %s' "$*"
    fi
}

install_packages() {
    if uses_apt;      then sudo apt-get update && sudo apt-get install -y "$@"
    elif have dnf;    then sudo dnf install -y "$@"
    elif have pacman; then sudo pacman -S --needed --noconfirm "$@"
    elif have zypper; then sudo zypper install -y "$@"
    else warn "Unknown package manager."; return 1
    fi
}

ASSUME_YES=0
CHECK_ONLY=0

# -----------------------------------------------------------------------------
# Argument parsing. A while/case loop is used rather than getopts because
# getopts only understands single-letter flags, and long names read better in
# a command a student types by hand.
# `shift` discards the argument just handled and moves to the next.
# -----------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)   ASSUME_YES=1; shift ;;
        -c|--check) CHECK_ONLY=1; shift ;;
        -h|--help)
            sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

require_linux
require_not_root

section "Stratus installation"
info "Distribution : $(distro_name)"
info "Kernel       : $(uname -r)"
info "Architecture : $(uname -m)"
info "User         : $(id -un)"

# =============================================================================
# 1. Work out what is missing
# =============================================================================
section "Checking required commands"

missing_packages=()   # empty array; entries appended with +=
missing_count=0

for entry in "${REQUIRED[@]}"; do
    # "${entry%%:*}" strips the longest match of ":*" from the end, leaving the
    # binary. "${entry##*:}" strips everything up to the last colon, leaving the
    # package. This is parameter expansion — no external process is spawned.
    binary="${entry%%:*}"
    package="${entry##*:}"
    if have "$binary"; then
        ok "$(label "$binary") found"
    else
        fail "$(label "$binary") missing (package: $package)"
        missing_packages+=("$package")
        missing_count=$(( missing_count + 1 ))
    fi
done

# python3-venv provides no binary of its own, so it is tested by asking Python
# whether the module can be imported at all.
if python3 -c "import venv" >/dev/null 2>&1; then
    ok "$(label "python3-venv") found"
else
    fail "$(label "python3-venv") missing (package: python3-venv)"
    missing_packages+=("python3-venv")
    missing_count=$(( missing_count + 1 ))
fi

# Python must be at least 3.10 for the syntax the project uses (match
# statements and the `X | Y` type union form).
if have python3; then
    version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    major="${version%%.*}"
    minor="${version##*.}"
    if (( major > 3 || (major == 3 && minor >= 10) )); then
        ok "$(label "python version") $version"
    else
        fail "$(label "python version") $version — Stratus needs 3.10 or newer"
        missing_count=$(( missing_count + 1 ))
    fi
fi

section "Checking optional commands"
missing_optional=()
for entry in "${OPTIONAL[@]}"; do
    binary="${entry%%:*}"
    rest="${entry#*:}"                 # everything after the first colon
    package="${rest%%:*}"
    purpose="${rest#*:}"
    if have "$binary"; then
        ok "$(label "$binary") found"
    else
        warn "$(label "$binary") missing — needed for $purpose (package: $package)"
        # systemd is never installed this way: it is the init system, and a
        # machine without it needs a different fix than an apt command.
        [[ "$package" == "systemd" ]] || missing_optional+=("$package")
    fi
done

# Deduplicate, because two binaries can come from one package.
if (( ${#missing_optional[@]} )); then
    mapfile -t missing_optional < <(printf '%s\n' "${missing_optional[@]}" | sort -u)
fi

if (( CHECK_ONLY )); then
    section "Summary"
    if (( missing_count == 0 )); then
        ok "Everything required is present."
        exit 0
    fi
    fail "$missing_count required item(s) missing."
    exit 1
fi

# =============================================================================
# 2. Install whatever is missing
# =============================================================================
if (( ${#missing_packages[@]} > 0 )); then
    section "Installing missing packages"

    # Deduplicate: util-linux and iproute2 each appear more than once above.
    # printf prints one package per line, sort -u removes duplicates, and
    # mapfile reads the result back into an array.
    mapfile -t unique_packages < <(printf '%s\n' "${missing_packages[@]}" | sort -u)
    say "Packages to install: ${unique_packages[*]}"

    uses_apt || die "This system does not use apt. Install these manually: ${unique_packages[*]}"

    if (( ! ASSUME_YES )) && ! confirm "Install them with sudo apt-get?"; then
        die "Installation cancelled. Install manually: sudo apt-get install ${unique_packages[*]}"
    fi

    sudo apt-get update
    sudo apt-get install -y "${unique_packages[@]}"
    ok "Packages installed"

    # Verify rather than trust: apt can succeed while still not providing the
    # binary we wanted, for instance if a package name changed.
    for entry in "${REQUIRED[@]}"; do
        binary="${entry%%:*}"
        have "$binary" || die "'$binary' is still missing after installation."
    done
else
    ok "All required packages already present"
fi

# =============================================================================
# 3. Build the Python environment
# =============================================================================
# The virtual environment lives inside the checkout (.runtime/venv) so that
# deleting the project directory removes every trace of it, and so that two
# checkouts cannot interfere with each other.
# =============================================================================
# -----------------------------------------------------------------------------
# Optional extras. Stratus runs without these, so they are offered rather than
# required, and declining is a normal answer.
# -----------------------------------------------------------------------------
if (( ${#missing_optional[@]} )); then
    section "Optional extras"
    say "  These are not required. Each one enables a feature:"
    say ""
    for package in "${missing_optional[@]}"; do
        case "$package" in
            nodejs)  note "  nodejs  — host Node.js projects" ;;
            npm)     note "  npm     — install dependencies for Node.js projects" ;;
            ufw)     note "  ufw     — open firewall ports for hosted apps automatically" ;;
            rsync)   note "  rsync   — faster incremental backups" ;;
            xdg-utils) note "  xdg-utils — open hosted apps in a browser from the terminal" ;;
            *)       note "  $package" ;;
        esac
    done
    say ""
    note "  Install command: $(install_command "${missing_optional[@]}")"
    say ""
    if (( ASSUME_YES )) || confirm "Install these optional packages now?"; then
        install_packages "${missing_optional[@]}" \
            && ok "Optional packages installed" \
            || warn "Some optional packages could not be installed; Stratus still runs without them."
    else
        note "Skipped. Run the command above whenever you want them."
    fi
fi

section "Python environment"

# An environment can exist and still be unusable. Debian and Kali package
# `ensurepip` separately from `venv`, so `python3 -m venv` can succeed and
# produce an environment with no pip inside it at all. Checking for the
# directory is therefore not enough — pip itself has to answer.
venv_has_pip() {
    [[ -x "$STRATUS_PYTHON" ]] && "$STRATUS_PYTHON" -m pip --version >/dev/null 2>&1
}

if venv_has_pip; then
    ok "Environment already exists at .runtime/venv"
else
    if [[ -x "$STRATUS_PYTHON" ]]; then
        warn "The existing environment has no working pip — rebuilding it"
    else
        info "Creating virtual environment…"
    fi

    # --clear empties an existing environment rather than failing on it.
    python3 -m venv --clear "$STRATUS_VENV" 2>/dev/null \
        || python3 -m venv "$STRATUS_VENV" \
        || die "Could not create the virtual environment. Install python3-venv: $(install_command python3-venv)"

    # Some distributions create the environment without pip. ensurepip puts it
    # there without touching the system Python.
    if ! venv_has_pip; then
        info "Bootstrapping pip inside the environment…"
        "$STRATUS_PYTHON" -m ensurepip --upgrade >/dev/null 2>&1 || true
    fi

    if ! venv_has_pip; then
        say ""
        fail "This Python cannot create an environment with pip in it."
        note "That means the standard library's ensurepip module is not installed."
        note "Install it, then run this again:"
        say ""
        say "    $(install_command python3-venv python3-pip)"
        say ""
        note "On Debian and Kali the package is sometimes version-specific, e.g."
        note "python3.13-venv. Check with: apt search '^python3.*-venv$'"
        die "pip is unavailable inside the virtual environment"
    fi
    ok "Created $STRATUS_VENV"
fi

info "Installing Python dependencies…"
# Called as `python -m pip` rather than through the pip script, because that
# script hard-codes an interpreter path and breaks if the environment moves.
"$STRATUS_PYTHON" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
"$STRATUS_PYTHON" -m pip install --quiet --disable-pip-version-check -r "$STRATUS_ROOT/requirements.txt" \
    || die "Dependency installation failed. Check your network connection."
ok "Dependencies installed"

# Confirm the two libraries that everything else depends on actually import.
"$STRATUS_PYTHON" -c "import cryptography, textual" 2>/dev/null \
    && ok "cryptography and textual import correctly" \
    || die "Dependencies installed but cannot be imported."

# =============================================================================
# 4. Permissions and directories
# =============================================================================
section "Permissions and directories"

# find ... -exec chmod: make every shell script executable. Without this the
# scripts can only be run as `bash script.sh`, which is a common and confusing
# stumbling block after copying a project between machines.
find "$STRATUS_ROOT/shell" -name '*.sh' -type f -exec chmod +x {} +
chmod +x "$STRATUS_ROOT/stratus.sh"
ok "Shell scripts marked executable"

# 700 = owner may read, write and enter; nobody else may look inside.
# The device identity and its private key live here.
mkdir -p "$STRATUS_CONFIG_DIR" "$STRATUS_RUNTIME_DIR"
chmod 700 "$STRATUS_CONFIG_DIR" "$STRATUS_RUNTIME_DIR"
ok "Created $STRATUS_CONFIG_DIR and $STRATUS_RUNTIME_DIR (private to you)"

# =============================================================================
# 5. Confinement capability
# =============================================================================
section "Application confinement"

if have systemd-run && [[ -n "${XDG_RUNTIME_DIR:-}" ]] && systemctl --user is-system-running >/dev/null 2>&1; then
    ok "systemd user services available — guest applications will be confined"
else
    warn "systemd user services unavailable on this session"
    note "Guest applications would run with your own file access."
    note "This is normal over plain SSH. On a desktop login it should work."
    note "To enable it for a headless user: sudo loginctl enable-linger $(id -un)"
fi

section "Installation complete"
ok "Stratus is ready."
say ""
say "  Next steps:"
say "    ./stratus.sh doctor     check everything is healthy"
say "    ./stratus.sh            host a server or join one"
say ""
