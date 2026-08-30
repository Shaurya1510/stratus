#!/usr/bin/env bash
# =============================================================================
# doctor.sh — diagnose a Stratus installation
# =============================================================================
# Answers the question "why is it not working?" with evidence instead of
# guesswork. Runs twelve independent checks covering the installation, the
# running server, storage, networking and confinement, then prints a summary.
#
#   ./stratus.sh doctor           run every check
#   ./stratus.sh doctor --quiet   print only problems
#
# Exit status: 0 when everything passed, 1 when any check failed. That makes it
# usable in a pipeline: `./stratus.sh doctor && ./stratus.sh start`
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

QUIET=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        -q|--quiet) QUIET=1; shift ;;
        -h|--help)  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

# Counters. Bash has no return value richer than an exit code, so results are
# accumulated in these globals as each check runs.
PASSED=0
FAILED=0
WARNED=0

# check_pass/check_fail/check_warn record a result and print it. In quiet mode
# only failures and warnings are shown, so a healthy system prints almost
# nothing and a broken one prints exactly what is wrong.
check_pass() { PASSED=$(( PASSED + 1 )); (( QUIET )) || ok "$1"; }
check_warn() { WARNED=$(( WARNED + 1 )); warn "$1"; }
check_fail() { FAILED=$(( FAILED + 1 )); fail "$1"; }

require_linux

say ""
say "${C_BOLD}Stratus diagnostic report${C_RESET}"
say "${C_DIM}$(date '+%Y-%m-%d %H:%M:%S')  on  $(hostname)${C_RESET}"

# =============================================================================
# 1. Operating system
# =============================================================================
section "1. System"

check_pass "Operating system: $(distro_name)"
check_pass "Kernel: $(uname -r) ($(uname -m))"

# uptime -p prints "up 3 hours, 20 minutes"; the fallback reads /proc/uptime
# directly and converts the seconds to hours with awk.
if have uptime; then
    check_pass "Uptime: $(uptime -p 2>/dev/null || awk '{ printf "up %.1f hours", $1/3600 }' /proc/uptime)"
fi

# =============================================================================
# 2. Required commands
# =============================================================================
section "2. Required commands"

for binary in python3 tar lsblk findmnt ip ss sqlite3 awk sed grep; do
    if have "$binary"; then
        check_pass "$(label "$binary") $(command -v "$binary")"
    else
        check_fail "$(label "$binary") not found — run: ./stratus.sh install"
    fi
done

# =============================================================================
# 3. Python environment
# =============================================================================
section "3. Python environment"

if [[ -x "$STRATUS_PYTHON" ]]; then
    check_pass "Virtual environment: $("$STRATUS_PYTHON" --version 2>&1)"
    if "$STRATUS_PYTHON" -c "import cryptography, textual" 2>/dev/null; then
        check_pass "cryptography and textual import correctly"
    else
        check_fail "Dependencies missing — run: ./stratus.sh install"
    fi
else
    check_fail "No virtual environment at .runtime/venv — run: ./stratus.sh install"
fi

# =============================================================================
# 4. Device identity
# =============================================================================
# The identity file holds this device's private encryption key. It must exist
# and it must not be readable by anyone else on the machine.
# =============================================================================
section "4. Device identity"

identity_file="$STRATUS_CONFIG_DIR/device.json"
if [[ -f "$identity_file" ]]; then
    # stat -c '%a' prints the permission bits as an octal number, e.g. 600.
    permissions="$(stat -c '%a' "$identity_file")"
    if [[ "$permissions" == "600" ]]; then
        check_pass "Identity file present and private (mode $permissions)"
    else
        check_warn "Identity file mode is $permissions, expected 600 — fixing"
        chmod 600 "$identity_file"
    fi
    # Read the display name without printing any key material.
    name="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['display_name'])" "$identity_file" 2>/dev/null || echo unknown)"
    check_pass "This device identifies as: $name"
else
    check_warn "No device identity yet — created automatically on first join"
fi

# =============================================================================
# 5. Configured server
# =============================================================================
section "5. Server configuration"

if server_configured; then
    server_name="$(server_field name)"
    server_port="$(server_field port)"
    server_storage="$(server_field storage)"
    server_pid="$(server_field pid)"
    check_pass "Configured server: ${server_name:-unnamed}"
    check_pass "Control port: ${server_port:-5050}"
    check_pass "Storage root: ${server_storage:-unknown}"

    # =========================================================================
    # 6. Is it actually running?
    # =========================================================================
    section "6. Server process"

    if pid_alive "$server_pid"; then
        rss="$(pid_rss_kb "$server_pid")"
        check_pass "Running as PID $server_pid, using $(human_size $(( rss * 1024 ))) of memory"
    else
        check_warn "Recorded PID $server_pid is not running (server stopped)"
    fi

    if port_listening "${server_port:-5050}"; then
        check_pass "Control port ${server_port:-5050} is accepting connections"
    else
        check_warn "Nothing is listening on port ${server_port:-5050}"
    fi

    if port_listening 5051; then
        check_pass "Discovery port 5051 is open"
    else
        check_warn "Discovery port 5051 is closed — clients must enter the address manually"
    fi

    # =========================================================================
    # 7. Storage
    # =========================================================================
    section "7. Storage"

    if [[ -d "$server_storage" ]]; then
        check_pass "Storage directory exists"

        # df -P forces POSIX output, keeping every entry on one line so awk can
        # read fixed fields. NR==2 skips the header row.
        read -r used_percent available <<<"$(df -P "$server_storage" | awk 'NR == 2 { print $5, $4 }')"
        available_bytes=$(( available * 1024 ))
        if [[ "${used_percent%\%}" -ge 95 ]]; then
            check_fail "Storage is $used_percent full, only $(human_size $available_bytes) free"
        elif [[ "${used_percent%\%}" -ge 85 ]]; then
            check_warn "Storage is $used_percent full ($(human_size $available_bytes) free)"
        else
            check_pass "Storage $used_percent used, $(human_size $available_bytes) free"
        fi

        # The filesystem type decides whether symlinks and permissions work.
        filesystem="$(findmnt -no FSTYPE --target "$server_storage" 2>/dev/null || echo unknown)"
        case "$filesystem" in
            ext4|xfs|btrfs|zfs) check_pass "Filesystem: $filesystem" ;;
            exfat|vfat|ntfs)    check_warn "Filesystem is $filesystem — no Unix permissions; runtimes are kept on the system disk" ;;
            *)                  check_warn "Filesystem: $filesystem" ;;
        esac

        # The server refuses to start if the expected drive is not mounted, so
        # compare the recorded UUID with the one mounted there now.
        storage_config="$server_storage/config/storage.json"
        if [[ -f "$storage_config" ]]; then
            expected_uuid="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('storage_uuid',''))" "$storage_config" 2>/dev/null)"
            actual_uuid="$(findmnt -no UUID --target "$server_storage" 2>/dev/null || true)"
            if [[ -z "$expected_uuid" || "$expected_uuid" == "custom" ]]; then
                check_pass "Storage UUID not pinned (a plain directory was chosen)"
            elif [[ "$expected_uuid" == "$actual_uuid" ]]; then
                check_pass "Storage UUID matches (${expected_uuid:0:12}…)"
            else
                check_fail "Wrong drive mounted: expected ${expected_uuid:0:12}…, found ${actual_uuid:-none}"
            fi
        fi

        database="$(platform_db)"
        if [[ -f "$database" ]]; then
            size=$(stat -c '%s' "$database")
            check_pass "Platform database present ($(human_size "$size"))"
        else
            check_warn "Platform database not created yet"
        fi
    else
        check_fail "Storage directory is missing: $server_storage"
    fi

    # =========================================================================
    # 8. Hosted applications
    # =========================================================================
    section "8. Hosted applications"

    if apps="$(db_query 'SELECT name, status, port, pid FROM deployments;')" && [[ -n "$apps" ]]; then
        # IFS='|' splits each row on the separator sqlite3 was told to use.
        # read -r assigns the fields to named variables one row at a time.
        while IFS='|' read -r name status port pid; do
            [[ -z "$name" ]] && continue
            if [[ "$status" == "RUNNING" ]] && pid_alive "$pid"; then
                check_pass "$(label "$name") running on port $port"
            elif [[ "$status" == "RUNNING" ]]; then
                check_fail "$(label "$name") marked running but PID $pid is dead"
            else
                check_pass "$(label "$name") $status"
            fi
        done <<<"$apps"
    else
        check_warn "No applications deployed yet"
    fi
else
    # No server is *running*. That is not the same as never having made one:
    # stopping a server clears the active marker but keeps it in the registry,
    # and saying "not configured" would suggest the data had been lost.
    registry_file="$STRATUS_CONFIG_DIR/servers.json"
    known=0
    if [[ -r "$registry_file" ]]; then
        known="$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$registry_file" 2>/dev/null || echo 0)"
    fi
    if (( known > 0 )); then
        check_pass "$known server(s) on this machine, none running right now"
        python3 - "$registry_file" <<'PYTHON' 2>/dev/null
import json, sys
from pathlib import Path
for entry in json.load(open(sys.argv[1], encoding="utf-8")):
    storage = entry.get("storage", "")
    state = "ready" if Path(storage).is_dir() else "drive not connected"
    print(f"    {entry.get('name', 'unnamed'):<28} {state}")
PYTHON
        note "Start one with ./stratus.sh and choose 'Open an Existing Server'."
    else
        check_warn "No server has been created on this machine yet"
        note "Run ./stratus.sh and choose 'Host a Server' to create one."
    fi

    section "6-8. Server process, storage and applications"
    note "Skipped because no server is running."
fi

# =============================================================================
# 9. Networking
# =============================================================================
section "9. Networking"

address="$(lan_address)"
if [[ "$address" == "127.0.0.1" ]]; then
    check_warn "No local network address found — this machine may be offline"
else
    check_pass "This host is reachable at $address"
fi

# ip -brief shows one line per interface: name, state, addresses.
if have ip; then
    interfaces="$(ip -brief link show up 2>/dev/null | awk '$1 != "lo" { print $1 }' | tr '\n' ' ')"
    [[ -n "$interfaces" ]] && check_pass "Active interfaces: $interfaces" \
                           || check_warn "No active network interfaces besides loopback"
fi

if have ufw; then
    firewall="$(ufw status 2>/dev/null | awk 'NR == 1 { print $2 }' || echo unknown)"
    if [[ "$firewall" == "active" ]]; then
        if ufw status 2>/dev/null | grep -q "5050"; then
            check_pass "Firewall active and port 5050 allowed"
        else
            check_warn "Firewall active but port 5050 is not allowed — run: ./stratus.sh netcheck --fix"
        fi
    else
        check_pass "Firewall inactive (nothing blocked)"
    fi
fi

# =============================================================================
# 10. Confinement
# =============================================================================
section "10. Application confinement"

if have systemd-run && [[ -n "${XDG_RUNTIME_DIR:-}" ]] && systemctl --user is-system-running >/dev/null 2>&1; then
    check_pass "systemd user services available — guest applications are confined"
    units="$(systemctl --user list-units 'stratus-*' --no-legend 2>/dev/null | wc -l)"
    check_pass "Stratus service units currently active: $units"
else
    check_warn "systemd user services unavailable — guest applications would run unconfined"
    note "Enable with: sudo loginctl enable-linger $(id -un)"
fi

# =============================================================================
# Summary
# =============================================================================
section "Summary"

say "  ${C_GREEN}passed${C_RESET}   $PASSED"
say "  ${C_YELLOW}warnings${C_RESET} $WARNED"
say "  ${C_RED}failed${C_RESET}   $FAILED"
say ""

if (( FAILED > 0 )); then
    fail "$FAILED check(s) failed. Fix these before using Stratus."
    exit 1
fi
if (( WARNED > 0 )); then
    say "  ${C_YELLOW}Stratus will run, but some features are unavailable.${C_RESET}"
    exit 0
fi
ok "Everything is healthy."
exit 0
