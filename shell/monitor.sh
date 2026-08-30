#!/usr/bin/env bash
# =============================================================================
# monitor.sh — watch hosted applications and the server process
# =============================================================================
# Reads resource usage straight from the kernel through /proc, checks that each
# application is still answering on its port, and can restart the ones that
# have died. Designed to be run by hand for a live view, or on a timer for
# unattended supervision.
#
#   ./stratus.sh monitor              print a report once
#   ./stratus.sh monitor --watch      refresh every 5 seconds
#   ./stratus.sh monitor --repair     restart applications that have crashed
#   ./stratus.sh monitor --once --repair   what the systemd timer runs
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

WATCH=0
REPAIR=0
INTERVAL=5

while [[ $# -gt 0 ]]; do
    case "$1" in
        -w|--watch)  WATCH=1; shift ;;
        -r|--repair) REPAIR=1; shift ;;
        -o|--once)   WATCH=0; shift ;;
        -i|--interval) INTERVAL="${2:-5}"; shift 2 ;;
        -h|--help)   sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

require_linux
server_configured || die "No server configured on this machine. Run ./stratus.sh first."

# -----------------------------------------------------------------------------
# cpu_percent <pid> — average CPU use of a process since it started.
#
# /proc/<pid>/stat field 14 is utime and field 15 is stime: the CPU time the
# process has spent in user and kernel mode, counted in clock ticks. Field 22
# is starttime, the moment the process began, also in ticks since boot.
#
#   cpu% = 100 * (utime + stime) / (uptime_ticks - starttime)
#
# getconf CLK_TCK gives the number of ticks per second (100 on Linux).
# -----------------------------------------------------------------------------
cpu_percent() {
    local pid="$1"
    [[ -r "/proc/$pid/stat" ]] || { printf '0.0\n'; return; }
    local ticks uptime_seconds
    ticks="$(getconf CLK_TCK)"
    uptime_seconds="$(awk '{ print $1 }' /proc/uptime)"
    awk -v ticks="$ticks" -v uptime="$uptime_seconds" '{
        # Field 2 is the process name in parentheses and may itself contain
        # spaces, so start counting fields after the closing bracket.
        for (i = 1; i <= NF; i++) if ($i ~ /\)$/) { offset = i; break }
        utime     = $(offset + 12)
        stime     = $(offset + 13)
        starttime = $(offset + 20)
        elapsed = uptime - (starttime / ticks)
        if (elapsed <= 0) { print "0.0"; exit }
        printf "%.1f\n", 100 * ((utime + stime) / ticks) / elapsed
    }' "/proc/$pid/stat" 2>/dev/null || printf '0.0\n'
}

# directory_bytes <path> — total size of everything under a directory.
# du -s summarises, -b counts real bytes rather than disk blocks.
directory_bytes() {
    local path="$1"
    [[ -d "$path" ]] || { printf '0\n'; return; }
    du -sb "$path" 2>/dev/null | awk '{ print $1 }' || printf '0\n'
}

# restart_application <id> <name> — bring a dead application back.
# Confined applications are systemd units, so systemctl is asked first; if the
# unit is unknown the Python control plane is asked instead.
restart_application() {
    local deployment_id="$1" name="$2"
    warn "Restarting $name…"
    if have systemctl && systemctl --user cat "stratus-${deployment_id}.service" >/dev/null 2>&1; then
        if systemctl --user restart "stratus-${deployment_id}.service" 2>/dev/null; then
            ok "$name restarted by systemd"
            return 0
        fi
    fi
    local port token
    port="$(server_field port)"
    token="$(server_field token)"
    if [[ -x "$STRATUS_PYTHON" ]]; then
        ( cd "$STRATUS_ROOT" && "$STRATUS_PYTHON" -m core.client \
            --host 127.0.0.1 --port "${port:-5050}" --token "$token" restart "$deployment_id" >/dev/null 2>&1 ) \
            && { ok "$name restarted"; return 0; }
    fi
    fail "Could not restart $name"
    return 1
}

# -----------------------------------------------------------------------------
# One full pass of the report.
# -----------------------------------------------------------------------------
report() {
    local policy_memory policy_cpu policy_data
    # Read the host's own limits so usage can be shown against its allowance.
    read -r policy_memory policy_cpu policy_data <<<"$(
        db_query 'SELECT max_memory_mb, max_cpu_percent, max_data_mb FROM hosting_policy WHERE id = 1;' \
        | tr '|' ' '
    )" || true
    policy_memory="${policy_memory:-0}"
    policy_cpu="${policy_cpu:-0}"
    policy_data="${policy_data:-0}"

    say ""
    say "${C_BOLD}Stratus monitor${C_RESET}   ${C_DIM}$(date '+%H:%M:%S')${C_RESET}"

    # ---- the control server itself -----------------------------------------
    section "Control server"
    local server_pid server_port
    server_pid="$(server_field pid)"
    server_port="$(server_field port)"
    if pid_alive "$server_pid"; then
        local rss cpu
        rss="$(pid_rss_kb "$server_pid")"
        cpu="$(cpu_percent "$server_pid")"
        ok "$(label "$(server_field name)") PID $server_pid · $(human_size $(( rss * 1024 ))) · ${cpu}% CPU"
        port_listening "${server_port:-5050}" \
            && ok "$(label "control port ${server_port:-5050}") listening" \
            || fail "$(label "control port ${server_port:-5050}") not listening"
    else
        fail "$(label "control server") not running"
    fi

    # ---- system load --------------------------------------------------------
    section "Host resources"
    # /proc/loadavg holds the 1, 5 and 15 minute load averages.
    local load
    load="$(awk '{ print $1", "$2", "$3 }' /proc/loadavg)"
    note "Load average (1/5/15 min): $load  across $(nproc) CPU(s)"

    # /proc/meminfo is a "key: value kB" table; awk collects the two fields it
    # needs and prints the percentage in use.
    awk '
        /^MemTotal:/     { total = $2 }
        /^MemAvailable:/ { available = $2 }
        END {
            used = total - available
            printf "    Memory: %.1f GB used of %.1f GB (%.0f%%)\n",
                   used / 1048576, total / 1048576, 100 * used / total
        }' /proc/meminfo

    # ---- hosted applications ------------------------------------------------
    section "Hosted applications"
    local rows
    rows="$(db_query 'SELECT id, name, owner, status, port, pid, data_path FROM deployments ORDER BY name;')" || true
    if [[ -z "$rows" ]]; then
        note "No applications deployed."
        return 0
    fi

    printf '  %-22s %-10s %-6s %10s %7s %10s\n' "APPLICATION" "STATUS" "PORT" "MEMORY" "CPU" "DATA"
    printf '  %s\n' "$(printf '─%.0s' $(seq 1 70))"

    local failures=0
    while IFS='|' read -r deployment_id name owner status port pid data_path; do
        [[ -z "$deployment_id" ]] && continue

        local memory_text="—" cpu_text="—" data_text="—" state="$status"

        if pid_alive "$pid"; then
            local rss cpu
            rss="$(pid_rss_kb "$pid")"
            cpu="$(cpu_percent "$pid")"
            memory_text="$(human_size $(( rss * 1024 )))"
            cpu_text="${cpu}%"

            # Compare against policy and flag anything close to its ceiling.
            if (( policy_memory > 0 )) && (( rss / 1024 > policy_memory * 90 / 100 )); then
                state="NEAR LIMIT"
            fi
        elif [[ "$status" == "RUNNING" ]]; then
            state="DEAD"
            failures=$(( failures + 1 ))
        fi

        if [[ -n "$data_path" ]]; then
            local used
            used="$(directory_bytes "$data_path")"
            data_text="$(human_size "$used")"
            if (( policy_data > 0 )) && (( used > policy_data * 1024 * 1024 * 90 / 100 )); then
                state="QUOTA NEAR"
            fi
        fi

        # Colour the row by state so problems are visible at a glance.
        local colour="$C_GREEN"
        case "$state" in
            DEAD|CRASHED|BUILD_FAILED)  colour="$C_RED" ;;
            "NEAR LIMIT"|"QUOTA NEAR"|QUOTA_EXCEEDED) colour="$C_YELLOW" ;;
            STOPPED) colour="$C_DIM" ;;
        esac

        printf '  %-22.22s %b%-10.10s%b %-6s %10s %7s %10s\n' \
               "$name" "$colour" "$state" "$C_RESET" "${port:-—}" "$memory_text" "$cpu_text" "$data_text"

        # Port reachability is a stronger signal than "the process exists":
        # a hung application still has a PID but stops accepting connections.
        if [[ "$status" == "RUNNING" ]] && pid_alive "$pid" && [[ -n "$port" ]]; then
            port_listening "$port" || note "  ↳ $name is running but not answering on port $port"
        fi

        if (( REPAIR )) && [[ "$state" == "DEAD" ]]; then
            restart_application "$deployment_id" "$name"
        fi
    done <<<"$rows"

    if (( failures > 0 )) && (( ! REPAIR )); then
        say ""
        warn "$failures application(s) stopped unexpectedly. Restart them with: ./stratus.sh monitor --repair"
    fi
}

# -----------------------------------------------------------------------------
# Either loop forever or print one report.
# trap runs a command when a signal arrives; here it restores the cursor and
# leaves cleanly when the user presses Ctrl+C, instead of dying mid-redraw.
# -----------------------------------------------------------------------------
if (( WATCH )); then
    trap 'printf "\n"; tput cnorm 2>/dev/null || true; exit 0' INT TERM
    tput civis 2>/dev/null || true          # hide the cursor while redrawing
    while true; do
        clear
        report
        say ""
        note "Refreshing every ${INTERVAL}s — press Ctrl+C to stop"
        sleep "$INTERVAL"
    done
else
    report
fi
