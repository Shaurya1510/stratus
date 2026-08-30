#!/usr/bin/env bash
# =============================================================================
# stop.sh — stop a running Stratus server and everything it started
# =============================================================================
# A server started from the interface is detached on purpose: it keeps running
# when the terminal that launched it closes, which is what you want on a machine
# people are relying on. The cost is that closing that terminal by accident
# leaves a server running with nothing on screen to stop it, and the next
# attempt to start one refuses because only one may run at a time.
#
# This is the way back. It stops the control server, the applications it is
# hosting, and clears the record of the running server so a new one can start.
#
#   ./stratus.sh stop                stop the server and its hosted apps
#   ./stratus.sh stop --apps         stop only the hosted applications
#   ./stratus.sh stop --yes          do not ask for confirmation
#   ./stratus.sh stop --status       show what is running, change nothing
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ASSUME_YES=0
APPS_ONLY=0
STATUS_ONLY=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)     ASSUME_YES=1; shift ;;
        -a|--apps)    APPS_ONLY=1; shift ;;
        -s|--status)  STATUS_ONLY=1; shift ;;
        -h|--help)    sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

require_linux

# -----------------------------------------------------------------------------
# Finding what is running.
#
# Two independent sources, because either one alone can be wrong:
#
#   * active-server.json records the process id, but a crash or a reboot leaves
#     the file behind pointing at a process that no longer exists — and a kill
#     against a recycled process id would hit something else entirely.
#   * pgrep finds the real processes, including one whose record was lost.
#
# Both are consulted and the results merged, so neither a stale file nor a
# missing one can leave a server running with no way to stop it.
# -----------------------------------------------------------------------------

recorded_pid() {
    server_configured || return 1
    local pid
    pid="$(server_field pid)" || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    pid_alive "$pid" || return 1
    printf '%s' "$pid"
}

# Every Stratus control server this user is running. `pgrep -u` restricts the
# search to our own processes: killing another user's is neither wanted nor
# permitted, and matching on the command line alone could find theirs.
running_servers() {
    pgrep -u "$(id -u)" -f "core\.server" 2>/dev/null || true
}

# Hosted applications run as transient systemd user units named stratus-<id>.
running_apps() {
    systemctl --user list-units --type=service --state=running --no-legend 'stratus-*.service' 2>/dev/null \
        | awk '{ print $1 }' | grep -v '^stratus-server\.service$' || true
}

# stop_pid <pid> — ask politely, then insist.
#
# SIGTERM first so the server closes its database cleanly and SQLite is left
# without a hot journal. Only a process that ignores it for five seconds is
# sent SIGKILL, which gives it no chance to tidy up.
stop_pid() {
    local pid="$1" waited=0
    kill -TERM "$pid" 2>/dev/null || return 1
    while (( waited < 50 )) && pid_alive "$pid"; do
        sleep 0.1
        waited=$(( waited + 1 ))
    done
    if pid_alive "$pid"; then
        warn "Process $pid ignored the stop request; forcing it"
        kill -KILL "$pid" 2>/dev/null || true
        sleep 0.3
    fi
    ! pid_alive "$pid"
}

# =============================================================================
# Report
# =============================================================================
section "What is running"

mapfile -t server_pids < <(running_servers)
mapfile -t app_units   < <(running_apps)
recorded="$(recorded_pid || true)"

if server_configured; then
    name="$(server_field name || echo 'a server')"
    if [[ -n "$recorded" ]]; then
        ok "Server '$name' is running as PID $recorded"
    else
        warn "'$name' is recorded as running, but its process is gone (stale record)"
    fi
else
    note "No server is recorded on this machine."
fi

# A server process with no record: exactly what happens when the record was
# removed, or when one was started by hand.
for pid in "${server_pids[@]}"; do
    [[ -z "$pid" || "$pid" == "$recorded" ]] && continue
    warn "Another Stratus server process is running as PID $pid (not in the record)"
done

if (( ${#app_units[@]} )); then
    ok "${#app_units[@]} hosted application(s) running:"
    for unit in "${app_units[@]}"; do note "  $unit"; done
else
    note "No hosted applications are running."
fi

if (( STATUS_ONLY )); then
    say ""
    if (( ${#server_pids[@]} == 0 )) && (( ${#app_units[@]} == 0 )); then
        note "Nothing to stop."
    else
        note "Stop it all with: ./stratus.sh stop"
    fi
    exit 0
fi

if (( ${#server_pids[@]} == 0 )) && (( ${#app_units[@]} == 0 )); then
    say ""
    # The record can outlive the process. Clearing it here is the difference
    # between "start a server" working and refusing for no visible reason.
    if server_configured && [[ -z "$recorded" ]]; then
        rm -f "$STRATUS_ACTIVE_SERVER"
        ok "Cleared the stale record; you can start a server again"
    else
        ok "Nothing is running"
    fi
    exit 0
fi

# =============================================================================
# Stop
# =============================================================================
say ""
if (( APPS_ONLY )); then
    note "This will stop the hosted applications. The server keeps running."
else
    note "This will stop the server and every application it is hosting."
    note "Nothing stored is deleted — messages, files and applications all remain."
fi

if (( ! ASSUME_YES )) && ! confirm "Stop them now?"; then
    note "Cancelled. Nothing was stopped."
    exit 0
fi

section "Stopping"

# Applications first. Stopping the server first would leave them running with
# nothing supervising them, and the next server would find their ports taken.
for unit in "${app_units[@]}"; do
    [[ -z "$unit" ]] && continue
    if systemctl --user stop "$unit" 2>/dev/null; then
        ok "Stopped $unit"
    else
        warn "Could not stop $unit"
    fi
    # A unit that failed rather than exited stays in the manager's list and
    # blocks a later start under the same name.
    systemctl --user reset-failed "$unit" >/dev/null 2>&1 || true
done

if (( ! APPS_ONLY )); then
    for pid in "${server_pids[@]}"; do
        [[ -z "$pid" ]] && continue
        if stop_pid "$pid"; then
            ok "Stopped server process $pid"
        else
            warn "Could not stop process $pid — try: kill -9 $pid"
        fi
    done

    # The service unit, if this machine runs Stratus that way.
    if systemctl --user is-active stratus-server.service >/dev/null 2>&1; then
        systemctl --user stop stratus-server.service && ok "Stopped stratus-server.service"
    fi

    if server_configured; then
        rm -f "$STRATUS_ACTIVE_SERVER"
        ok "Cleared the running-server record"
    fi
fi

section "Done"
if (( APPS_ONLY )); then
    note "The server is still running. Stop it with: ./stratus.sh stop"
else
    ok "Stratus is stopped. Start it again with: ./stratus.sh"
fi
