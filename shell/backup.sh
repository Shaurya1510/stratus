#!/usr/bin/env bash
# =============================================================================
# backup.sh — back up and restore Stratus server data
# =============================================================================
# The storage drive holds everything that cannot be recreated: device records,
# encrypted messages and files, deployed applications and their data. The code
# can be cloned again from Git; this cannot.
#
#   ./stratus.sh backup                      back up to the default location
#   ./stratus.sh backup --to /mnt/usb        back up somewhere specific
#   ./stratus.sh backup --list               show existing backups
#   ./stratus.sh backup --verify FILE        check an archive is intact
#   ./stratus.sh backup --restore FILE       restore (asks first)
#   ./stratus.sh backup --keep 10            how many backups to retain
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

ACTION="create"
DESTINATION=""
ARCHIVE=""
KEEP=7

while [[ $# -gt 0 ]]; do
    case "$1" in
        -t|--to)      DESTINATION="${2:-}"; shift 2 ;;
        -l|--list)    ACTION="list"; shift ;;
        -v|--verify)  ACTION="verify"; ARCHIVE="${2:-}"; shift 2 ;;
        -r|--restore) ACTION="restore"; ARCHIVE="${2:-}"; shift 2 ;;
        -k|--keep)    KEEP="${2:-7}"; shift 2 ;;
        -h|--help)    sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

need tar
server_configured || die "No server configured. Run ./stratus.sh first."

STORAGE="$(server_field storage)"
SERVER_NAME="$(server_field name)"
[[ -d "$STORAGE" ]] || die "Storage directory not found: $STORAGE"

# Backups live beside the data by default, but --to can send them to a USB
# stick or a network mount, which is what you actually want for real safety.
DESTINATION="${DESTINATION:-$STORAGE/backups}"

# Everything worth preserving. Runtime files are deliberately excluded: virtual
# environments and node_modules are large, machine-specific, and rebuildable.
BACKUP_ITEMS=(config platform users chat drive deployments app-data logs)

# =============================================================================
# Create
# =============================================================================
create_backup() {
    section "Backing up $SERVER_NAME"

    mkdir -p "$DESTINATION" || die "Cannot write to $DESTINATION"
    [[ -w "$DESTINATION" ]] || die "No permission to write to $DESTINATION"

    # A UTC timestamp sorts chronologically as plain text, which makes pruning
    # and listing trivial later.
    local stamp archive
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    archive="$DESTINATION/stratus-${stamp}.tar.gz"

    # Only include directories that exist; tar aborts on a missing path.
    local -a present=()
    local item
    for item in "${BACKUP_ITEMS[@]}"; do
        [[ -e "$STORAGE/$item" ]] && present+=("$item")
    done
    (( ${#present[@]} > 0 )) || die "No Stratus data found in $STORAGE"

    info "Including: ${present[*]}"

    # Check there is room before starting, using the uncompressed size as a
    # deliberately pessimistic estimate.
    local needed available
    needed="$(du -sb "${present[@]/#/$STORAGE/}" 2>/dev/null | awk '{ total += $1 } END { print total }')"
    available="$(df -P "$DESTINATION" | awk 'NR == 2 { print $4 * 1024 }')"
    info "Data size: $(human_size "$needed"), free at destination: $(human_size "$available")"
    if (( available < needed )); then
        die "Not enough space at $DESTINATION (need up to $(human_size "$needed"))"
    fi

    info "Creating $(basename "$archive")…"
    # -C changes directory first so paths inside the archive are relative,
    # which lets it be restored anywhere. The excludes drop regenerable files.
    tar -C "$STORAGE" \
        --exclude='*/venv' \
        --exclude='*/node_modules' \
        --exclude='*/__pycache__' \
        --exclude='*.log.*.gz' \
        -czf "$archive" "${present[@]}" 2>/dev/null \
        || die "Backup failed while writing $archive"

    local size
    size="$(stat -c '%s' "$archive")"
    ok "Created $(basename "$archive") ($(human_size "$size"))"

    # A backup nobody has verified is only a hope. Read it back immediately.
    info "Verifying…"
    if tar -tzf "$archive" >/dev/null 2>&1; then
        ok "Archive verified — $(tar -tzf "$archive" 2>/dev/null | wc -l) entries readable"
    else
        rm -f "$archive"
        die "Archive failed verification and was deleted. Check the destination drive."
    fi

    # A checksum lets a future restore prove the file has not rotted or been
    # altered since it was written.
    if have sha256sum; then
        sha256sum "$archive" | awk '{ print $1 }' > "${archive}.sha256"
        ok "Checksum written to $(basename "$archive").sha256"
    fi

    prune_backups
    say ""
    ok "Backup complete: $archive"
}

# =============================================================================
# Prune
# =============================================================================
# Keeps the newest $KEEP archives and deletes the rest. Without this, backups
# silently fill the drive they are meant to protect.
# =============================================================================
prune_backups() {
    local -a archives=()
    while IFS= read -r -d '' file; do
        archives+=("$file")
    done < <(find "$DESTINATION" -maxdepth 1 -name 'stratus-*.tar.gz' -print0 2>/dev/null | sort -zr)

    local total=${#archives[@]}
    (( total <= KEEP )) && { note "$total backup(s) kept (limit $KEEP)"; return 0; }

    info "Pruning old backups (keeping newest $KEEP of $total)…"
    local index
    for (( index = KEEP; index < total; index++ )); do
        rm -f "${archives[index]}" "${archives[index]}.sha256"
        note "Removed $(basename "${archives[index]}")"
    done
    ok "Pruned $(( total - KEEP )) old backup(s)"
}

# =============================================================================
# List
# =============================================================================
list_backups() {
    section "Backups in $DESTINATION"

    [[ -d "$DESTINATION" ]] || { note "No backup directory yet."; return 0; }

    local found=0 total=0
    printf '  %-34s %10s %20s\n' "ARCHIVE" "SIZE" "CREATED"
    printf '  %s\n' "$(printf '─%.0s' $(seq 1 66))"

    while IFS= read -r archive; do
        [[ -z "$archive" ]] && continue
        local size created
        size="$(stat -c '%s' "$archive")"
        created="$(stat -c '%y' "$archive" | cut -d'.' -f1)"
        printf '  %-34s %10s %20s\n' "$(basename "$archive")" "$(human_size "$size")" "$created"
        total=$(( total + size ))
        found=$(( found + 1 ))
    done <<<"$(find "$DESTINATION" -maxdepth 1 -name 'stratus-*.tar.gz' 2>/dev/null | sort -r)"

    say ""
    if (( found == 0 )); then
        note "No backups yet. Create one with: ./stratus.sh backup"
    else
        note "$found backup(s), $(human_size "$total") in total"
    fi
}

# =============================================================================
# Verify
# =============================================================================
verify_backup() {
    [[ -n "$ARCHIVE" ]] || die "Give an archive: ./stratus.sh backup --verify FILE"
    [[ -f "$ARCHIVE" ]] || die "No such file: $ARCHIVE"

    section "Verifying $(basename "$ARCHIVE")"

    if tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
        ok "Archive structure is readable"
        note "$(tar -tzf "$ARCHIVE" 2>/dev/null | wc -l) entries"
    else
        die "Archive is corrupt or not a gzip tar file"
    fi

    if [[ -f "${ARCHIVE}.sha256" ]] && have sha256sum; then
        local expected actual
        expected="$(cat "${ARCHIVE}.sha256")"
        actual="$(sha256sum "$ARCHIVE" | awk '{ print $1 }')"
        if [[ "$expected" == "$actual" ]]; then
            ok "Checksum matches — the file is byte-for-byte unchanged"
        else
            die "Checksum mismatch. This archive has been altered or damaged."
        fi
    else
        warn "No checksum file alongside this archive"
    fi

    say ""
    note "Contents:"
    tar -tzf "$ARCHIVE" 2>/dev/null | awk -F/ '{ print $1 }' | sort | uniq -c \
        | awk '{ printf "    %-20s %s file(s)\n", $2, $1 }'
}

# =============================================================================
# Restore
# =============================================================================
# Restoring overwrites live data, so it stops the server first, moves the
# existing data aside rather than deleting it, and only then unpacks.
# =============================================================================
restore_backup() {
    [[ -n "$ARCHIVE" ]] || die "Give an archive: ./stratus.sh backup --restore FILE"
    [[ -f "$ARCHIVE" ]] || die "No such file: $ARCHIVE"

    section "Restoring $(basename "$ARCHIVE")"

    tar -tzf "$ARCHIVE" >/dev/null 2>&1 || die "That archive is corrupt. Refusing to restore."
    ok "Archive verified"

    warn "This will replace the current contents of $STORAGE"
    say ""
    confirm "Restore over the existing server data?" || { note "Cancelled."; exit 0; }

    # Refuse to restore underneath a running server; the database would be
    # replaced while open and the result would be inconsistent.
    local server_pid
    server_pid="$(server_field pid)"
    if pid_alive "$server_pid"; then
        die "The server is running as PID $server_pid. Stop it first, then restore."
    fi

    # Move the current data aside instead of deleting it. If the restore turns
    # out to be the wrong archive, the previous state is still on disk.
    local safety="$STORAGE.replaced-$(date -u +%Y%m%dT%H%M%SZ)"
    info "Moving current data to $(basename "$safety")…"
    mkdir -p "$safety"
    local item
    for item in "${BACKUP_ITEMS[@]}"; do
        [[ -e "$STORAGE/$item" ]] && mv "$STORAGE/$item" "$safety/"
    done

    info "Unpacking…"
    tar -C "$STORAGE" -xzf "$ARCHIVE" || die "Restore failed. Your previous data is in $safety"

    ok "Restore complete"
    note "Previous data kept at: $safety"
    note "Delete it once you have confirmed the restore is good."
}

case "$ACTION" in
    create)  create_backup ;;
    list)    list_backups ;;
    verify)  verify_backup ;;
    restore) restore_backup ;;
esac
