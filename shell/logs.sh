#!/usr/bin/env bash
# =============================================================================
# logs.sh — rotate, prune and analyse Stratus logs
# =============================================================================
# Application logs grow without limit until something truncates them. This
# script rotates them when they get large, compresses the old copies, deletes
# ones past the retention window, and reports what the logs actually contain.
#
#   ./stratus.sh logs                summary of every log
#   ./stratus.sh logs --rotate       rotate anything over the size limit
#   ./stratus.sh logs --analyse      error counts, busiest applications
#   ./stratus.sh logs --tail NAME    follow one application's log
#   ./stratus.sh logs --errors       show recent errors from every log
# =============================================================================
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/common.sh"

MAX_SIZE_MB=10        # rotate a log once it passes this size
KEEP_ROTATIONS=5      # how many compressed generations to keep
RETENTION_DAYS=30     # delete compressed logs older than this

ACTION="summary"
TARGET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -r|--rotate)  ACTION="rotate"; shift ;;
        -a|--analyse|--analyze) ACTION="analyse"; shift ;;
        -e|--errors)  ACTION="errors"; shift ;;
        -t|--tail)    ACTION="tail"; TARGET="${2:-}"; shift 2 ;;
        -h|--help)    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

server_configured || die "No server configured. Run ./stratus.sh first."
STORAGE="$(server_field storage)"
[[ -d "$STORAGE" ]] || die "Storage directory not found: $STORAGE"

# -----------------------------------------------------------------------------
# Collect every log file into an array.
#
# `find -print0` separates names with a NUL byte and `read -d ''` reads them
# back the same way. NUL is the only character that cannot appear in a filename,
# so this is the one safe way to pass a list of arbitrary paths through a
# pipeline — a plain `for file in $(find ...)` breaks on spaces.
# -----------------------------------------------------------------------------
collect_logs() {
    local -a found=()
    while IFS= read -r -d '' file; do
        found+=("$file")
    done < <(find "$STORAGE" -type f -name '*.log' -print0 2>/dev/null)
    printf '%s\n' "${found[@]:-}"
}

# =============================================================================
# Summary
# =============================================================================
show_summary() {
    section "Log files under $STORAGE"

    local total=0 count=0
    printf '  %-42s %10s %12s\n' "LOG FILE" "SIZE" "MODIFIED"
    printf '  %s\n' "$(printf '─%.0s' $(seq 1 68))"

    while IFS= read -r file; do
        [[ -z "$file" || ! -f "$file" ]] && continue
        local size modified relative
        size="$(stat -c '%s' "$file")"
        modified="$(stat -c '%y' "$file" | cut -d'.' -f1)"
        # Show the path relative to the storage root to keep the table narrow.
        relative="${file#"$STORAGE"/}"
        printf '  %-42.42s %10s %12s\n' "$relative" "$(human_size "$size")" "${modified#* }"
        total=$(( total + size ))
        count=$(( count + 1 ))
    done <<<"$(collect_logs)"

    say ""
    note "$count log file(s), $(human_size "$total") in total"

    # Warn about anything that has grown past the rotation threshold.
    local oversized
    oversized="$(find "$STORAGE" -type f -name '*.log' -size +"${MAX_SIZE_MB}"M 2>/dev/null | wc -l)"
    if (( oversized > 0 )); then
        warn "$oversized log(s) exceed ${MAX_SIZE_MB} MB — run: ./stratus.sh logs --rotate"
    fi

    local archives
    archives="$(find "$STORAGE" -type f -name '*.log.*.gz' 2>/dev/null | wc -l)"
    (( archives > 0 )) && note "$archives compressed archive(s) retained"
}

# =============================================================================
# Rotation
# =============================================================================
# A log is renamed to name.log.1, the previous .1 becomes .2, and so on. The
# oldest generation falls off the end. Renaming rather than deleting means a
# process that still holds the file open keeps writing to the renamed file, so
# nothing is lost mid-write; the application creates a fresh log next time.
# =============================================================================
rotate_logs() {
    section "Rotating logs larger than ${MAX_SIZE_MB} MB"

    local rotated=0
    while IFS= read -r -d '' file; do
        local size
        size="$(stat -c '%s' "$file")"
        info "Rotating ${file#"$STORAGE"/} ($(human_size "$size"))"

        # Shift the existing generations down, oldest first, so nothing is
        # overwritten before it has been moved.
        local generation
        for (( generation = KEEP_ROTATIONS - 1; generation >= 1; generation-- )); do
            if [[ -f "${file}.${generation}.gz" ]]; then
                mv -f "${file}.${generation}.gz" "${file}.$(( generation + 1 )).gz"
            fi
        done

        cp "$file" "${file}.1"
        # Truncating with : > keeps the same inode, so any process holding the
        # file open continues to write into it rather than into a deleted file.
        : > "$file"
        gzip -f "${file}.1"
        ok "Rotated to ${file##*/}.1.gz"
        rotated=$(( rotated + 1 ))
    done < <(find "$STORAGE" -type f -name '*.log' -size +"${MAX_SIZE_MB}"M -print0 2>/dev/null)

    (( rotated == 0 )) && ok "Nothing needed rotating"

    # Delete archives past the retention window.
    local expired
    expired="$(find "$STORAGE" -type f -name '*.log.*.gz' -mtime +"$RETENTION_DAYS" 2>/dev/null | wc -l)"
    if (( expired > 0 )); then
        find "$STORAGE" -type f -name '*.log.*.gz' -mtime +"$RETENTION_DAYS" -delete 2>/dev/null
        ok "Deleted $expired archive(s) older than $RETENTION_DAYS days"
    fi

    # Drop generations beyond the keep limit.
    find "$STORAGE" -type f -name "*.log.*.gz" 2>/dev/null | while IFS= read -r archive; do
        local generation="${archive##*.log.}"
        generation="${generation%%.gz}"
        if [[ "$generation" =~ ^[0-9]+$ ]] && (( generation > KEEP_ROTATIONS )); then
            rm -f "$archive"
            note "Removed old generation: ${archive##*/}"
        fi
    done
}

# =============================================================================
# Analysis
# =============================================================================
# This is the part that turns raw logs into information, using the classic text
# processing chain: grep to select lines, sed to normalise them, awk to count
# and sort to rank.
# =============================================================================
analyse_logs() {
    section "Log analysis"

    local files
    files="$(collect_logs)"
    [[ -z "$files" ]] && { note "No logs to analyse."; return 0; }

    # ---- error counts per file ---------------------------------------------
    say ""
    say "  ${C_BOLD}Errors and warnings by application${C_RESET}"
    printf '  %-40s %8s %10s\n' "LOG" "ERRORS" "WARNINGS"
    printf '  %s\n' "$(printf '─%.0s' $(seq 1 60))"

    while IFS= read -r file; do
        [[ -z "$file" || ! -f "$file" ]] && continue
        local errors warnings relative
        # -c counts matching lines, -i ignores case, -E enables alternation.
        # `|| true` stops grep's "no matches" exit code from killing the script
        # under `set -e`.
        errors="$(grep -ciE 'error|exception|traceback|fatal|failed' "$file" 2>/dev/null || true)"
        warnings="$(grep -ciE 'warning|warn' "$file" 2>/dev/null || true)"
        relative="${file#"$STORAGE"/}"
        if (( errors > 0 )); then
            printf '  %-40.40s %b%8s%b %10s\n' "$relative" "$C_RED" "$errors" "$C_RESET" "$warnings"
        else
            printf '  %-40.40s %8s %10s\n' "$relative" "$errors" "$warnings"
        fi
    done <<<"$files"

    # ---- most frequent error messages ---------------------------------------
    say ""
    say "  ${C_BOLD}Most frequent error messages${C_RESET}"
    # The pipeline: find error lines, strip timestamps and numbers so that
    # otherwise-identical messages group together, count duplicates with
    # `sort | uniq -c`, then rank them.
    if ! grep -hiE 'error|exception|failed' $(printf '%s ' $files) 2>/dev/null \
        | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9:,\.]+//g; s/\b[0-9]+\b/N/g; s/^[[:space:]]+//' \
        | sort | uniq -c | sort -rn | head -8 \
        | awk '{ count = $1; $1 = ""; printf "    %4d x %s\n", count, substr($0, 2, 90) }'
    then
        note "No errors found."
    fi

    # ---- platform activity ---------------------------------------------------
    if db_query 'SELECT 1;' >/dev/null 2>&1; then
        say ""
        say "  ${C_BOLD}Platform activity${C_RESET}"
        local devices messages files_count deployments
        devices="$(db_query 'SELECT COUNT(*) FROM devices;')"
        messages="$(db_query 'SELECT COUNT(*) FROM encrypted_messages;')"
        files_count="$(db_query 'SELECT COUNT(*) FROM encrypted_files;')"
        deployments="$(db_query 'SELECT COUNT(*) FROM deployments;')"
        note "Devices registered      : ${devices:-0}"
        note "Encrypted messages      : ${messages:-0}"
        note "Encrypted files stored  : ${files_count:-0}"
        note "Applications deployed   : ${deployments:-0}"

        say ""
        say "  ${C_BOLD}Most recently active devices${C_RESET}"
        db_query "SELECT display_name, last_seen FROM devices ORDER BY last_seen DESC LIMIT 5;" \
            | awk -F'|' '{ printf "    %-30s last seen %s\n", $1, $2 }'

        # Deployment events are the platform's own audit trail.
        say ""
        say "  ${C_BOLD}Recent deployment events${C_RESET}"
        db_query "SELECT event, COUNT(*) FROM deployment_events GROUP BY event ORDER BY COUNT(*) DESC LIMIT 6;" \
            | awk -F'|' '{ printf "    %-20s %s\n", $1, $2 }'
    fi
}

# =============================================================================
# Errors only
# =============================================================================
show_errors() {
    section "Recent errors"
    local found=0
    while IFS= read -r file; do
        [[ -z "$file" || ! -f "$file" ]] && continue
        local matches
        matches="$(grep -inE 'error|exception|traceback|fatal' "$file" 2>/dev/null | tail -5 || true)"
        if [[ -n "$matches" ]]; then
            found=1
            say ""
            say "  ${C_BOLD}${file#"$STORAGE"/}${C_RESET}"
            # Print each match indented, with its line number, trimmed to width.
            printf '%s\n' "$matches" | awk -F: '{
                line = $1; $1 = ""
                printf "    %b%6s%b  %s\n", "'"$C_DIM"'", line, "'"$C_RESET"'", substr($0, 2, 100)
            }'
        fi
    done <<<"$(collect_logs)"
    (( found )) || ok "No errors found in any log."
}

# =============================================================================
# Tail one application
# =============================================================================
tail_log() {
    [[ -n "$TARGET" ]] || die "Give an application name: ./stratus.sh logs --tail NAME"

    # Ask the database where that application's log lives.
    local source_path
    source_path="$(db_query "SELECT source_path FROM deployments WHERE name = '${TARGET//\'/\'\'}' LIMIT 1;")"
    if [[ -z "$source_path" ]]; then
        die "No application called '$TARGET'. Run ./stratus.sh logs to list what exists."
    fi

    local log_file="${source_path%/source}/runtime.log"
    [[ -f "$log_file" ]] || die "No log yet for $TARGET (it may never have started)."

    info "Following $log_file — press Ctrl+C to stop"
    say ""
    # -F keeps following even if the file is rotated out from under us.
    tail -F -n 40 "$log_file"
}

case "$ACTION" in
    summary) show_summary ;;
    rotate)  rotate_logs ;;
    analyse) analyse_logs ;;
    errors)  show_errors ;;
    tail)    tail_log ;;
esac
