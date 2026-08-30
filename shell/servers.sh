#!/usr/bin/env bash
# =============================================================================
# servers.sh — list and inspect the servers on this machine
# =============================================================================
# A person can keep several servers: one for a class, one for a project team,
# one on a drive they carry between machines. Only one runs at a time. This
# script shows what exists, which one is running, and finds servers sitting on
# a drive that this machine has no record of.
#
#   ./stratus.sh servers              list known servers
#   ./stratus.sh servers --scan PATH  find servers on a drive
#   ./stratus.sh servers --forget N   remove entry N from the list
#   ./stratus.sh servers --delete N   ERASE entry N and all of its data
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

REGISTRY="$STRATUS_CONFIG_DIR/servers.json"
ACTION="list"
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--scan)   ACTION="scan"; TARGET="${2:-}"; shift 2 ;;
        -f|--forget) ACTION="forget"; TARGET="${2:-}"; shift 2 ;;
        -d|--delete) ACTION="delete"; TARGET="${2:-}"; shift 2 ;;
        -h|--help)   sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1 (try --help)" ;;
    esac
done

require_linux

# The registry is JSON written by the Python side. python3 reads it rather than
# a shell JSON parser, because python3 is already required by the project.
registry_rows() {
    [[ -r "$REGISTRY" ]] || return 1
    python3 - "$REGISTRY" <<'PYTHON' 2>/dev/null
import json, sys
from pathlib import Path
try:
    entries = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
for entry in entries:
    storage = entry.get("storage", "")
    print("|".join([
        entry.get("name", "unnamed"),
        storage,
        str(entry.get("port", 5050)),
        "yes" if Path(storage).is_dir() else "no",
    ]))
PYTHON
}

# =============================================================================
# list
# =============================================================================
list_servers() {
    section "Servers on this machine"

    local rows
    if ! rows="$(registry_rows)" || [[ -z "$rows" ]]; then
        note "No servers yet. Create one with: ./stratus.sh"
        return 0
    fi

    # Which one, if any, is running right now.
    local running_storage=""
    if server_configured; then
        local pid
        pid="$(server_field pid)"
        pid_alive "$pid" && running_storage="$(server_field storage)"
    fi

    printf '  %-3s %-26s %-8s %-10s %s\n' "#" "SERVER" "PORT" "STATE" "STORAGE"
    printf '  %s\n' "$(printf '─%.0s' $(seq 1 78))"

    local index=1
    while IFS='|' read -r name storage port available; do
        [[ -z "$name" ]] && continue
        local state size=""
        if [[ "$storage" == "$running_storage" ]]; then
            state="${C_GREEN}running${C_RESET}"
        elif [[ "$available" == "yes" ]]; then
            state="${C_DIM}stopped${C_RESET}"
        else
            state="${C_YELLOW}no drive${C_RESET}"
        fi
        printf '  %-3s %-26.26s %-8s %-19b %s\n' "$index" "$name" "$port" "$state" "$storage"

        # Show what the server actually holds, when its drive is present.
        if [[ "$available" == "yes" && -f "$storage/platform/stratus.db" ]] && have sqlite3; then
            local devices apps
            devices="$(sqlite3 "$storage/platform/stratus.db" 'SELECT COUNT(*) FROM devices;' 2>/dev/null || echo '?')"
            apps="$(sqlite3 "$storage/platform/stratus.db" 'SELECT COUNT(*) FROM deployments;' 2>/dev/null || echo '?')"
            size="$(du -sh "$storage" 2>/dev/null | awk '{ print $1 }')"
            note "      ${devices} member(s) · ${apps} application(s) · ${size:-unknown} on disk"
        fi
        index=$(( index + 1 ))
    done <<<"$rows"

    say ""
    if [[ -n "$running_storage" ]]; then
        note "Only one server runs at a time. Stop the running one before starting another."
    else
        note "Start one with: ./stratus.sh"
    fi
}

# =============================================================================
# scan — find servers on a drive this machine has no record of
# =============================================================================
scan_drive() {
    [[ -n "$TARGET" ]] || die "Give a path: ./stratus.sh servers --scan /media/you/USB"
    [[ -d "$TARGET" ]] || die "No such directory: $TARGET"

    section "Scanning $TARGET"

    local root="$TARGET/StratusData/servers"
    if [[ ! -d "$root" ]]; then
        note "No Stratus data on that drive."
        return 0
    fi

    local found=0
    while IFS= read -r -d '' config; do
        local directory name
        directory="$(dirname "$(dirname "$config")")"
        name="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('server_name','?'))" "$config" 2>/dev/null || echo '?')"
        ok "$name"
        note "  $directory"
        found=$(( found + 1 ))
    done < <(find "$root" -mindepth 3 -maxdepth 3 -path '*/config/storage.json' -print0 2>/dev/null)

    say ""
    if (( found == 0 )); then
        note "No servers found."
    else
        note "$found server(s) found. Open one from the Stratus start screen; you will"
        note "need its join token, which is never stored on the drive."
    fi
}

# =============================================================================
# forget
# =============================================================================
# Resolve the number the user typed into NAME and STORAGE.
select_server() {
    [[ "$TARGET" =~ ^[0-9]+$ ]] || die "Give the number shown by './stratus.sh servers'"
    local rows row
    rows="$(registry_rows)" || die "No servers are registered."
    row="$(printf '%s\n' "$rows" | sed -n "${TARGET}p")"
    [[ -n "$row" ]] || die "There is no server number $TARGET."
    NAME="$(cut -d'|' -f1 <<<"$row")"
    STORAGE="$(cut -d'|' -f2 <<<"$row")"
}

# Drop an entry from the registry without touching the drive.
drop_from_registry() {
    python3 - "$REGISTRY" "$1" <<'PYTHON'
import json, sys
path, storage = sys.argv[1], sys.argv[2]
entries = json.load(open(path, encoding="utf-8"))
json.dump([item for item in entries if item.get("storage") != storage],
          open(path, "w", encoding="utf-8"), indent=2)
PYTHON
}

forget_server() {
    local storage name
    select_server; name="$NAME"; storage="$STORAGE"

    say ""
    warn "This removes '$name' from the list on this machine."
    note "The data at $storage is NOT deleted."
    confirm "Forget it?" || { note "Cancelled."; exit 0; }

    drop_from_registry "$storage"
    ok "Removed '$name' from the list"
}

# =============================================================================
# delete — erase a server and everything it holds
# =============================================================================
delete_server() {
    local storage name
    select_server; name="$NAME"; storage="$STORAGE"

    # Refuse while it is running: the process would keep writing into a
    # directory that is being removed underneath it.
    if server_configured && [[ "$(server_field storage)" == "$storage" ]]; then
        pid_alive "$(server_field pid)" && die "'$name' is running. Stop it first: ./stratus.sh service --stop"
    fi

    # Never remove a tree that has not been positively identified as a server.
    if [[ -d "$storage" ]]; then
        [[ -f "$storage/config/storage.json" ]] || die "$storage does not look like a Stratus server."
        [[ "$(basename "$(dirname "$storage")")" == "servers" ]] || die "$storage is not in a StratusData/servers directory."
    fi

    say ""
    warn "This ERASES '$name' and everything in it. It cannot be undone."
    if [[ -d "$storage" ]]; then
        note "  $storage"
        if [[ -f "$storage/platform/stratus.db" ]] && have sqlite3; then
            note "  $(sqlite3 "$storage/platform/stratus.db" 'SELECT COUNT(*) FROM devices;' 2>/dev/null || echo '?') member(s), \
$(sqlite3 "$storage/platform/stratus.db" 'SELECT COUNT(*) FROM deployments;' 2>/dev/null || echo '?') application(s), \
$(du -sh "$storage" 2>/dev/null | awk '{ print $1 }') on disk"
        fi
    else
        note "  The drive is not connected, so only the list entry can be removed."
    fi

    say ""
    note "Type the server name exactly to confirm."
    local typed
    read -r -p "  Server name: " typed
    [[ "$typed" == "$name" ]] || { note "That does not match. Nothing was deleted."; exit 0; }

    if [[ -d "$storage" ]]; then
        rm -rf -- "$storage"
        ok "Erased $storage"
    fi
    drop_from_registry "$storage"

    # A stale active-server record would otherwise point at a directory that
    # no longer exists.
    if server_configured && [[ "$(server_field storage)" == "$storage" ]]; then
        rm -f "$STRATUS_CONFIG_DIR/active-server.json"
    fi
    ok "Deleted '$name'"
}

case "$ACTION" in
    list)   list_servers ;;
    scan)   scan_drive ;;
    forget) forget_server ;;
    delete) delete_server ;;
esac
