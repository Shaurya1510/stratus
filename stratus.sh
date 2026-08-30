#!/usr/bin/env bash
# =============================================================================
# stratus.sh — the single entry point for everything Stratus does
# =============================================================================
# Every command a user needs goes through this script. It works out where the
# project lives, makes sure the Python environment exists, then dispatches to
# either a shell script in shell/ or a Python module in core/.
#
# Run it with no arguments to open the full-screen interface.
# =============================================================================
set -euo pipefail

# -----------------------------------------------------------------------------
# Find the project root even when called through a symlink or from another
# directory. $0 is the path used to invoke this script; dirname strips the
# filename, cd + pwd turns whatever is left into an absolute path.
# -----------------------------------------------------------------------------
STRATUS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$STRATUS_ROOT"

source "$STRATUS_ROOT/shell/lib/common.sh"

usage() {
    cat <<'EOF'

  STRATUS — a private cloud for your local network

  SETUP
    ./stratus.sh install         Install dependencies and prepare this machine
    ./stratus.sh doctor          Diagnose problems (run this first when stuck)

  USING IT
    ./stratus.sh                 Open the full-screen interface (host or join)
    ./stratus.sh terminal        Join a server on this network
    ./stratus.sh admin           Host dashboard: policy, devices, approvals
    ./stratus.sh stop            Stop the running server and its applications

  OPERATIONS
    ./stratus.sh monitor         Live view of applications and resource use
    ./stratus.sh monitor --watch Refresh continuously
    ./stratus.sh logs            Log sizes, errors and activity
    ./stratus.sh logs --rotate   Rotate and compress large logs
    ./stratus.sh backup          Back up all server data
    ./stratus.sh netcheck        Check the network; --fix opens firewall ports
    ./stratus.sh service install Run the server automatically at boot
    ./stratus.sh servers         List the servers on this machine
    ./stratus.sh stop --status   Show what is running without stopping it

  DEVELOPMENT
    ./stratus.sh test            Run the automated test suite
    ./stratus.sh host  ...       Start a server directly (see docs)
    ./stratus.sh join  ...       Command-line client (see docs)

  Every command accepts --help for its own options.

EOF
}

# -----------------------------------------------------------------------------
# The Python environment is required by most commands but not by all of them,
# so it is created lazily rather than on every invocation.
# -----------------------------------------------------------------------------
ensure_python() {
    if [[ -x "$STRATUS_PYTHON" ]]; then
        return 0
    fi
    warn "Python environment not found — setting it up now"
    exec "$STRATUS_SHELL_DIR/install.sh"
}

# Python entry points are package modules, so they must run from the project
# root with it on the import path.
run_python() {
    ensure_python
    cd "$STRATUS_ROOT"
    exec "$STRATUS_PYTHON" -m "$@"
}

command="${1:-}"
[[ $# -gt 0 ]] && shift

case "$command" in
    # ---- shell tooling ------------------------------------------------------
    install)   exec "$STRATUS_SHELL_DIR/install.sh"  "$@" ;;
    doctor)    exec "$STRATUS_SHELL_DIR/doctor.sh"   "$@" ;;
    monitor)   exec "$STRATUS_SHELL_DIR/monitor.sh"  "$@" ;;
    logs)      exec "$STRATUS_SHELL_DIR/logs.sh"     "$@" ;;
    backup)    exec "$STRATUS_SHELL_DIR/backup.sh"   "$@" ;;
    netcheck)  exec "$STRATUS_SHELL_DIR/netcheck.sh" "$@" ;;
    service)   exec "$STRATUS_SHELL_DIR/service.sh"  "$@" ;;
    servers)   exec "$STRATUS_SHELL_DIR/servers.sh"   "$@" ;;
    stop)      exec "$STRATUS_SHELL_DIR/stop.sh"      "$@" ;;

    # ---- the platform itself ------------------------------------------------
    terminal)  require_linux; run_python core.interface.terminal "$@" ;;
    admin)     require_linux; run_python core.interface.admin    "$@" ;;
    host)      require_linux; run_python core.server             "$@" ;;
    join)      require_linux; run_python core.client             "$@" ;;

    # ---- development --------------------------------------------------------
    test)
        ensure_python
        exec "$STRATUS_PYTHON" -m unittest discover -s "$STRATUS_ROOT/tests" -v "$@"
        ;;

    -h|--help|help) usage ;;

    "")
        # No arguments: open the first-run interface.
        require_linux
        run_python core.interface.setup
        ;;

    *)
        fail "Unknown command: $command"
        usage
        exit 2
        ;;
esac
