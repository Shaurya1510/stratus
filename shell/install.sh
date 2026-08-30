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
for entry in "${OPTIONAL[@]}"; do
    binary="${entry%%:*}"
    rest="${entry#*:}"                 # everything after the first colon
    package="${rest%%:*}"
    purpose="${rest#*:}"
    if have "$binary"; then
        ok "$(label "$binary") found"
    else
        warn "$(label "$binary") missing — needed for $purpose (package: $package)"
    fi
done

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
section "Python environment"

if [[ -x "$STRATUS_PYTHON" ]]; then
    ok "Environment already exists at .runtime/venv"
else
    info "Creating virtual environment…"
    python3 -m venv "$STRATUS_VENV" || die "Could not create the virtual environment. Install python3-venv."
    ok "Created $STRATUS_VENV"
fi

info "Installing Python dependencies…"
"$STRATUS_VENV/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
"$STRATUS_VENV/bin/pip" install --quiet --disable-pip-version-check -r "$STRATUS_ROOT/requirements.txt" \
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
