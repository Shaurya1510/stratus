#!/usr/bin/env bash
# =============================================================================
# service.sh — run Stratus as a systemd service
# =============================================================================
# Without this, the server only lives as long as the terminal that started it.
# This script writes a systemd user unit so the server starts at boot, restarts
# if it crashes, and can be controlled with ordinary systemctl commands. It
# also installs a timer that runs the monitor and log rotation on a schedule.
#
#   ./stratus.sh service install     generate and enable the units
#   ./stratus.sh service start|stop|restart|status
#   ./stratus.sh service logs        follow the journal
#   ./stratus.sh service uninstall   remove the units
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

# User units live here. Using a *user* unit rather than a system one means no
# root is needed and the service runs with exactly the permissions Stratus
# should have — the same ones the person using it already has.
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_UNIT="stratus-server.service"
MONITOR_UNIT="stratus-monitor.service"
MONITOR_TIMER="stratus-monitor.timer"

ACTION="${1:-status}"

# --help is answered before any environment checks, so it works everywhere.
case "$ACTION" in
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

require_linux
require_not_root
need systemctl systemd

# Without a user manager there is nothing to install units into. This is the
# normal state on a bare SSH login unless lingering has been enabled.
systemctl --user is-system-running >/dev/null 2>&1 \
    || warn "No systemd user session detected. Enable it with: sudo loginctl enable-linger $(id -un)"

# =============================================================================
# install — write the unit files
# =============================================================================
install_units() {
    server_configured || die "No server configured yet. Run ./stratus.sh and host a server first."

    local name storage token port
    name="$(server_field name)"
    storage="$(server_field storage)"
    token="$(server_field token)"
    port="$(server_field port)"
    port="${port:-5050}"

    section "Installing systemd units"
    mkdir -p "$UNIT_DIR"

    # -------------------------------------------------------------------------
    # The server unit.
    #
    # A quoted heredoc ('EOF') is used so that $variables inside the template
    # are NOT expanded by bash — except the few we substitute deliberately
    # afterwards. Here we do want expansion, so the delimiter is unquoted and
    # each value is inserted directly.
    #
    # Restart=on-failure with RestartSec means a crashed server comes back
    # automatically, which is the main reason to use systemd at all.
    # -------------------------------------------------------------------------
    # The join token is written to its own file instead of into the unit, so
    # it never appears in `systemctl cat`, in the journal, or in the process
    # list of every other program this user runs.
    local token_file="$STRATUS_CONFIG_DIR/tokens/service.token"
    mkdir -p "$(dirname "$token_file")"
    ( umask 077; printf '%s' "$token" > "$token_file" )
    chmod 600 "$token_file"

    # Only this device may administer the server from this machine.
    local admin_device
    admin_device="$(python3 -c 'import json,pathlib,sys
path = pathlib.Path.home() / ".config" / "stratus" / "device.json"
try: sys.stdout.write(json.loads(path.read_text(encoding="utf-8"))["id"])
except Exception: sys.stdout.write("")' 2>/dev/null || true)"

    cat > "$UNIT_DIR/$SERVICE_UNIT" <<EOF
[Unit]
Description=Stratus server — $name
Documentation=file://$STRATUS_ROOT/docs/GUIDE.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$STRATUS_ROOT
ExecStart=$STRATUS_PYTHON -m core.server \\
    --bind 0.0.0.0 \\
    --port $port \\
    --storage $storage \\
    --token-file $token_file \\
    --admin-device $admin_device \\
    --server-name $name
Restart=on-failure
RestartSec=5
TimeoutStopSec=20

# Keep the journal readable: identify our messages clearly.
SyslogIdentifier=stratus

# The server needs its own storage and config, and nothing else.
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=default.target
EOF
    # The unit file contains the join token, so it must not be world-readable.
    chmod 600 "$UNIT_DIR/$SERVICE_UNIT"
    ok "Wrote $SERVICE_UNIT"

    # -------------------------------------------------------------------------
    # The maintenance unit: one shot, does its work, exits.
    # Type=oneshot tells systemd not to expect a long-running process.
    # -------------------------------------------------------------------------
    cat > "$UNIT_DIR/$MONITOR_UNIT" <<EOF
[Unit]
Description=Stratus periodic maintenance (monitor, log rotation)

[Service]
Type=oneshot
WorkingDirectory=$STRATUS_ROOT
ExecStart=$STRATUS_SHELL_DIR/monitor.sh --once --repair
ExecStart=$STRATUS_SHELL_DIR/logs.sh --rotate
SyslogIdentifier=stratus-maintenance
EOF
    ok "Wrote $MONITOR_UNIT"

    # -------------------------------------------------------------------------
    # The timer that triggers it.
    # OnBootSec waits after boot before the first run; OnUnitActiveSec sets the
    # gap between runs. Persistent=true catches up on a missed run if the
    # machine was switched off when it was due.
    # -------------------------------------------------------------------------
    cat > "$UNIT_DIR/$MONITOR_TIMER" <<EOF
[Unit]
Description=Run Stratus maintenance every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Persistent=true
Unit=$MONITOR_UNIT

[Install]
WantedBy=timers.target
EOF
    ok "Wrote $MONITOR_TIMER"

    # systemd caches unit files; it must be told to re-read them.
    systemctl --user daemon-reload
    ok "Reloaded systemd"

    systemctl --user enable "$SERVICE_UNIT" >/dev/null 2>&1 && ok "Server enabled at login"
    systemctl --user enable --now "$MONITOR_TIMER" >/dev/null 2>&1 && ok "Maintenance timer enabled"

    say ""
    note "Start the server now with: ./stratus.sh service start"
    note "To keep it running after you log out: sudo loginctl enable-linger $(id -un)"
}

# =============================================================================
# uninstall
# =============================================================================
uninstall_units() {
    section "Removing systemd units"

    local unit
    for unit in "$SERVICE_UNIT" "$MONITOR_TIMER" "$MONITOR_UNIT"; do
        if [[ -f "$UNIT_DIR/$unit" ]]; then
            systemctl --user disable --now "$unit" >/dev/null 2>&1 || true
            rm -f "$UNIT_DIR/$unit"
            ok "Removed $unit"
        fi
    done

    systemctl --user daemon-reload
    ok "Reloaded systemd"
    note "Your server data was not touched."
}

# =============================================================================
# status
# =============================================================================
show_status() {
    section "Stratus service status"

    if [[ ! -f "$UNIT_DIR/$SERVICE_UNIT" ]]; then
        warn "No service unit installed"
        note "Install it with: ./stratus.sh service install"
        return 0
    fi

    # is-active and is-enabled each print one word and set an exit code.
    # `|| true` keeps `set -e` from aborting when the answer is "inactive".
    local active enabled
    active="$(systemctl --user is-active "$SERVICE_UNIT" 2>/dev/null || true)"
    enabled="$(systemctl --user is-enabled "$SERVICE_UNIT" 2>/dev/null || true)"

    printf '  %-22s %s\n' "Service:" "$SERVICE_UNIT"
    if [[ "$active" == "active" ]]; then
        printf '  %-22s %b%s%b\n' "State:" "$C_GREEN" "$active" "$C_RESET"
    else
        printf '  %-22s %b%s%b\n' "State:" "$C_YELLOW" "${active:-unknown}" "$C_RESET"
    fi
    printf '  %-22s %s\n' "Starts at login:" "${enabled:-no}"

    # MainPID and memory come straight from systemd's own accounting.
    local pid memory
    pid="$(systemctl --user show -p MainPID --value "$SERVICE_UNIT" 2>/dev/null || echo 0)"
    if [[ "$pid" != "0" ]] && pid_alive "$pid"; then
        memory="$(pid_rss_kb "$pid")"
        printf '  %-22s %s (%s)\n' "Process:" "PID $pid" "$(human_size $(( memory * 1024 )))"
    fi

    say ""
    local timer_active
    timer_active="$(systemctl --user is-active "$MONITOR_TIMER" 2>/dev/null || true)"
    printf '  %-22s %s\n' "Maintenance timer:" "${timer_active:-not installed}"
    if [[ "$timer_active" == "active" ]]; then
        systemctl --user list-timers "$MONITOR_TIMER" --no-legend 2>/dev/null \
            | awk '{ printf "  %-22s %s %s\n", "Next run:", $1, $2 }'
    fi

    # Any confined guest applications currently running.
    say ""
    local units
    units="$(systemctl --user list-units 'stratus-*' --no-legend --state=running 2>/dev/null | grep -v 'stratus-server\|stratus-monitor' | wc -l)"
    printf '  %-22s %s\n' "Confined apps running:" "$units"
}

case "$ACTION" in
    install)   install_units ;;
    uninstall) uninstall_units ;;
    start)     systemctl --user start "$SERVICE_UNIT" && ok "Server started" ;;
    stop)      systemctl --user stop "$SERVICE_UNIT" && ok "Server stopped" ;;
    restart)   systemctl --user restart "$SERVICE_UNIT" && ok "Server restarted" ;;
    status)    show_status ;;
    logs)      journalctl --user -u "$SERVICE_UNIT" -f -n 50 ;;
    *)         die "Unknown action: $ACTION (install, uninstall, start, stop, restart, status, logs)" ;;
esac
