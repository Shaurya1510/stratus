#!/usr/bin/env bash
# =============================================================================
# common.sh — shared shell library for every Stratus script
# =============================================================================
# This file is never run on its own. Every other script in shell/ starts by
# sourcing it, which gives them one consistent set of colours, logging, checks
# and helpers instead of each script inventing its own.
#
# Sourcing means "run this file's contents inside my own shell", so the
# functions and variables defined here become part of the calling script.
# =============================================================================

# -----------------------------------------------------------------------------
# Guard against being sourced twice.
# If STRATUS_COMMON_LOADED is already set, return immediately. ${VAR:-} means
# "the value of VAR, or an empty string if it is unset" — without :- the script
# would abort under `set -u` when the variable does not exist yet.
# -----------------------------------------------------------------------------
[[ -n "${STRATUS_COMMON_LOADED:-}" ]] && return 0
STRATUS_COMMON_LOADED=1

# -----------------------------------------------------------------------------
# Locate the project root.
# BASH_SOURCE[0] is the path of *this* file (unlike $0, which is the path of the
# script the user actually ran). We take its directory, go up two levels
# (lib -> shell -> project root) and resolve it to an absolute path with pwd.
# -----------------------------------------------------------------------------
STRATUS_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STRATUS_SHELL_DIR="$(cd "$STRATUS_LIB_DIR/.." && pwd)"
STRATUS_ROOT="$(cd "$STRATUS_SHELL_DIR/.." && pwd)"
export STRATUS_ROOT STRATUS_SHELL_DIR

# Where the platform keeps per-user state (identity, active server pointer).
STRATUS_CONFIG_DIR="${STRATUS_CONFIG_DIR:-$HOME/.config/stratus}"
STRATUS_RUNTIME_DIR="${STRATUS_RUNTIME_DIR:-$HOME/.local/share/stratus}"
STRATUS_VENV="${STRATUS_VENV:-$STRATUS_ROOT/.runtime/venv}"
STRATUS_PYTHON="${STRATUS_PYTHON:-$STRATUS_VENV/bin/python}"
STRATUS_ACTIVE_SERVER="$STRATUS_CONFIG_DIR/active-server.json"

# =============================================================================
# Colour output
# =============================================================================
# Colour is only enabled when stdout is a terminal (-t 1) and the user has not
# set NO_COLOR. Piping output to a file or another program therefore produces
# clean text with no escape sequences in it.
# tput reads the terminal's capability database, which is more portable than
# hard-coding escape codes like \033[31m.
# =============================================================================
if [[ -t 1 && -z "${NO_COLOR:-}" ]] && command -v tput >/dev/null 2>&1; then
    C_RESET="$(tput sgr0)"
    C_BOLD="$(tput bold)"
    C_DIM="$(tput dim)"
    C_RED="$(tput setaf 1)"
    C_GREEN="$(tput setaf 2)"
    C_YELLOW="$(tput setaf 3)"
    C_BLUE="$(tput setaf 4)"
    C_CYAN="$(tput setaf 6)"
else
    C_RESET="" C_BOLD="" C_DIM="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN=""
fi

# =============================================================================
# Logging helpers
# =============================================================================
# printf is used instead of echo throughout: echo's handling of backslashes and
# leading dashes differs between shells, while printf behaves identically
# everywhere. '%s\n' prints the argument literally, so a filename containing a
# percent sign or a backslash cannot corrupt the output.
# Warnings and errors go to file descriptor 2 (stderr) so that a caller can
# capture normal output with $(...) without swallowing the diagnostics.
# =============================================================================

say()  { printf '%s\n' "$*"; }
info() { printf '%s\n' "${C_BLUE}::${C_RESET} $*"; }
ok()   { printf '%s\n' "  ${C_GREEN}✔${C_RESET} $*"; }
warn() { printf '%s\n' "  ${C_YELLOW}!${C_RESET} $*" >&2; }
fail() { printf '%s\n' "  ${C_RED}✘${C_RESET} $*" >&2; }
note() { printf '%s\n' "    ${C_DIM}$*${C_RESET}"; }

# A titled section separator, used to structure long script output.
section() {
    printf '\n%s\n' "${C_BOLD}${C_CYAN}$*${C_RESET}"
    printf '%s\n' "${C_DIM}$(printf '─%.0s' $(seq 1 60))${C_RESET}"
}

# die prints an error and terminates the whole script with a non-zero status.
# Non-zero exit codes are how one script tells another that it failed, which is
# what makes `stratus.sh doctor && stratus.sh start` behave correctly.
die() {
    fail "$*"
    exit 1
}

# =============================================================================
# Command and privilege checks
# =============================================================================

# have <command> — true if the command exists in PATH. `command -v` is the
# POSIX way to test this; `which` is an external binary that is not present on
# every minimal system.
have() { command -v "$1" >/dev/null 2>&1; }

# need <command> [package] — abort unless the command exists, naming the
# package to install when it does not.
need() {
    local binary="$1" package="${2:-$1}"
    have "$binary" || die "'$binary' is required but not installed. Try: sudo apt install $package"
}

# Refuse to run as root. Stratus deliberately runs unprivileged: it writes into
# the user's own home and uses systemd *user* services, and running as root
# would create root-owned files the normal user could no longer delete.
require_not_root() {
    [[ "${EUID:-$(id -u)}" -ne 0 ]] || die "Do not run Stratus as root. Run it as your normal user."
}

require_linux() {
    [[ "$(uname -s)" == "Linux" ]] || die "Stratus supports Linux only (found $(uname -s))."
}

# =============================================================================
# Distribution detection
# =============================================================================
# /etc/os-release is the standard, machine-readable description of a Linux
# distribution. Sourcing it in a subshell defines variables such as ID and
# PRETTY_NAME without leaking them into the caller's environment.
# =============================================================================
distro_id() {
    if [[ -r /etc/os-release ]]; then
        (. /etc/os-release && printf '%s\n' "${ID:-unknown}")
    else
        printf 'unknown\n'
    fi
}

distro_name() {
    if [[ -r /etc/os-release ]]; then
        (. /etc/os-release && printf '%s\n' "${PRETTY_NAME:-${NAME:-unknown}}")
    else
        printf 'unknown\n'
    fi
}

# True when the distribution uses apt (Debian, Ubuntu, Kali, Mint, Pop!_OS ...).
uses_apt() { have apt-get; }

# =============================================================================
# Formatting helpers
# =============================================================================

# human_size <bytes> — turn a byte count into something a person can read.
#
# The whole calculation is done inside awk rather than in bash. Bash arithmetic
# is integer-only, so dividing by 1024 repeatedly in the shell would throw away
# the fraction and report 1.6 GB as "1.0 GB". awk divides in floating point and
# keeps it.
human_size() {
    awk -v bytes="${1:-0}" 'BEGIN {
        split("B KB MB GB TB PB", units, " ")
        step = 1
        while (bytes >= 1024 && step < 6) {
            bytes /= 1024
            step++
        }
        # Whole bytes have no meaningful fraction; larger units get one decimal.
        if (step == 1) printf "%d %s\n", bytes, units[step]
        else           printf "%.1f %s\n", bytes, units[step]
    }'
}

# Draw a fixed-width label so multi-line reports line up in columns.
label() { printf '  %-34s' "$1"; }

# =============================================================================
# Interaction
# =============================================================================

# confirm <question> — ask a yes/no question, defaulting to no.
# -r stops read from interpreting backslashes; -p prints the prompt on the same
# line. When there is no terminal attached (a cron job, a pipeline) the answer
# is automatically "no", so an unattended run can never hang waiting for input.
confirm() {
    local reply
    [[ -t 0 ]] || return 1
    read -r -p "$* [y/N]: " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# =============================================================================
# Server state
# =============================================================================
# The Python control plane writes ~/.config/stratus/active-server.json when a
# server is started. These helpers read single fields out of it. python3 is used
# rather than a JSON parser like jq, because python3 is already a hard
# requirement of the project while jq would be one more thing to install.
# =============================================================================

server_configured() { [[ -r "$STRATUS_ACTIVE_SERVER" ]]; }

# server_field <key> — print one value from active-server.json, or nothing.
server_field() {
    local key="$1"
    server_configured || return 1
    python3 - "$STRATUS_ACTIVE_SERVER" "$key" <<'PYTHON' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        print(json.load(handle).get(sys.argv[2], ""))
except Exception:
    sys.exit(1)
PYTHON
}

# The SQLite database holding devices, deployments and encrypted objects.
platform_db() {
    local storage
    storage="$(server_field storage)" || return 1
    [[ -n "$storage" ]] || return 1
    printf '%s\n' "$storage/platform/stratus.db"
}

# Run a read-only SQL query against the platform database.
# Failing quietly matters here: monitoring must not crash because the server is
# mid-write or has not been created yet.
db_query() {
    local database
    database="$(platform_db)" || return 1
    [[ -f "$database" ]] || return 1
    have sqlite3 || return 1
    sqlite3 -noheader -separator '|' "$database" "$1" 2>/dev/null
}

# =============================================================================
# Process helpers
# =============================================================================

# pid_alive <pid> — true when a process with that id currently exists.
# Testing for the directory /proc/<pid> is cheaper than running ps, and reads
# the kernel's own process table directly.
pid_alive() {
    local pid="${1:-}"
    [[ -n "$pid" && "$pid" != "0" && -d "/proc/$pid" ]]
}

# pid_rss_kb <pid> — resident memory of a process in kilobytes.
# /proc/<pid>/status contains a line "VmRSS:   12345 kB"; awk matches that line
# and prints the second field.
pid_rss_kb() {
    local pid="$1"
    [[ -r "/proc/$pid/status" ]] || { printf '0\n'; return; }
    awk '/^VmRSS:/ { print $2; exit }' "/proc/$pid/status" 2>/dev/null || printf '0\n'
}

# port_listening <port> — true when something is accepting connections there.
# ss is the modern replacement for netstat; -l lists listening sockets, -t and
# -n restrict it to TCP and stop it resolving names, which keeps it fast.
port_listening() {
    local port="$1"
    have ss || return 1
    ss -ltn 2>/dev/null | awk -v port=":$port" '$4 ~ port"$" { found = 1 } END { exit !found }'
}

# =============================================================================
# Network helpers
# =============================================================================

# lan_address — the address other machines on this network should use.
# Opening a UDP socket toward an off-link address makes the kernel pick an
# outgoing interface without sending a single packet. `ip route get` reports
# which source address it would use, and awk pulls the value after "src".
lan_address() {
    local address=""
    if have ip; then
        address="$(ip route get 1.1.1.1 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i+1); exit } }')"
    fi
    [[ -z "$address" ]] && address="$(hostname -I 2>/dev/null | awk '{ print $1 }')"
    printf '%s\n' "${address:-127.0.0.1}"
}
