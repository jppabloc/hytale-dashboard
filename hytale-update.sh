#!/bin/bash
# Hytale Server Update Script
# Usage: hytale-update.sh check|update
# - check: Download latest version, compare with installed, output JSON
# - update: Perform full server update (stop, stage, swap, start)
#
# Install: sudo cp hytale-update.sh /usr/local/sbin/hytale-update.sh && sudo chmod 755 /usr/local/sbin/hytale-update.sh
# Sudoers: hytale ALL=(ALL) NOPASSWD: /usr/local/sbin/hytale-update.sh

set -euo pipefail

SERVER_DIR="/opt/hytale-server"
DOWNLOADER_DIR="${SERVER_DIR}/.downloader"
DOWNLOADER_BIN="${DOWNLOADER_DIR}/hytale-downloader-linux-amd64"
DOWNLOADER_URL="https://downloader.hytale.com/hytale-downloader.zip"
VERSION_FILE="${SERVER_DIR}/last_version.txt"
LATEST_VERSION_FILE="${SERVER_DIR}/.latest_version"
CREDENTIALS_FILE="${SERVER_DIR}/.hytale-downloader-credentials.json"
GAME_ZIP="${DOWNLOADER_DIR}/game.zip"
DOWNLOAD_LOG="${DOWNLOADER_DIR}/download.log"
SERVICE_NAME="hytale.service"

# Files/dirs to preserve during update
PRESERVE=(
    "mods"
    "universe"
    "logs"
    "backups"
    "config.json"
    "bans.json"
    "permissions.json"
    "whitelist.json"
    "auth.enc"
    ".hytale-downloader-credentials.json"
    "last_version.txt"
    ".latest_version"
    ".update_after_backup"
    ".downloader"
    "start.sh"
    ".console_pipe"
)

json_output() {
    local current="$1" latest="$2" available="$3" msg="$4"
    printf '{"current":"%s","latest":"%s","update_available":%s,"message":"%s"}\n' \
        "$current" "$latest" "$available" "$msg"
}

json_error() {
    printf '{"error":"%s"}\n' "$1"
    exit 1
}

get_current_version() {
    if [[ -f "$VERSION_FILE" ]]; then
        cat "$VERSION_FILE" | tr -d '[:space:]'
    else
        echo "unknown"
    fi
}

ensure_downloader() {
    mkdir -p "$DOWNLOADER_DIR"

    if [[ -x "$DOWNLOADER_BIN" ]]; then
        return 0
    fi

    local tmp_dir
    tmp_dir="$(mktemp -d /tmp/hytale-downloader-XXXXXX)"
    local zip_file="${tmp_dir}/hytale-downloader.zip"

    if ! curl -fsSL "$DOWNLOADER_URL" -o "$zip_file" 2>/dev/null; then
        rm -rf "$tmp_dir"
        json_error "Downloader konnte nicht heruntergeladen werden"
    fi

    if ! unzip -q "$zip_file" -d "$tmp_dir" 2>/dev/null; then
        rm -rf "$tmp_dir"
        json_error "Downloader-ZIP konnte nicht entpackt werden"
    fi

    local bin_path
    bin_path="$(find "$tmp_dir" -type f -name 'hytale-downloader-linux-amd64' -print -quit)"

    if [[ -z "$bin_path" ]]; then
        rm -rf "$tmp_dir"
        json_error "Downloader-Binary nicht im Archiv gefunden"
    fi

    cp "$bin_path" "$DOWNLOADER_BIN"
    chmod +x "$DOWNLOADER_BIN"
    rm -rf "$tmp_dir"
}

query_latest_version() {
    # Use -print-version flag to get available version without downloading
    local output
    output="$("$DOWNLOADER_BIN" -print-version -credentials-path "$CREDENTIALS_FILE" 2>&1)" || true
    echo "$output" > "$DOWNLOAD_LOG"
    # The output should contain the version string (trim whitespace)
    local version
    version="$(echo "$output" | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+[^ ]*' | head -n 1 | tr -d '[:space:]')"
    if [[ -z "$version" ]]; then
        # Fallback: try the whole trimmed output
        version="$(echo "$output" | tail -n 1 | tr -d '[:space:]')"
    fi
    echo "$version"
}

download_game() {
    # Download game.zip for the actual update
    local attempt max_attempts=3
    for attempt in $(seq 1 $max_attempts); do
        rm -f "$GAME_ZIP"
        "$DOWNLOADER_BIN" -download-path "$GAME_ZIP" -credentials-path "$CREDENTIALS_FILE" > "$DOWNLOAD_LOG" 2>&1 || true

        if [[ -f "$GAME_ZIP" ]]; then
            if unzip -tq "$GAME_ZIP" >/dev/null 2>&1; then
                return 0
            fi
        fi

        if [[ $attempt -lt $max_attempts ]]; then
            sleep 5
        fi
    done

    json_error "Download fehlgeschlagen nach ${max_attempts} Versuchen"
}

do_check() {
    local current
    current="$(get_current_version)"

    ensure_downloader

    local latest
    latest="$(query_latest_version)"

    # Store latest version
    echo "$latest" > "$LATEST_VERSION_FILE"

    if [[ -z "$latest" || "$latest" == "unknown" ]]; then
        json_output "$current" "unknown" "false" "Version konnte nicht ermittelt werden"
        return
    fi

    if [[ "$current" == "$latest" ]]; then
        json_output "$current" "$latest" "false" "Server ist aktuell"
    else
        json_output "$current" "$latest" "true" "Update verfuegbar"
    fi
}

do_update() {
    local current
    current="$(get_current_version)"

    ensure_downloader

    # Get latest version
    local latest
    latest="$(query_latest_version)"

    if [[ -z "$latest" || "$latest" == "unknown" ]]; then
        json_error "Neue Version konnte nicht ermittelt werden"
    fi

    if [[ "$current" == "$latest" && "$current" != "unknown" ]]; then
        json_output "$current" "$latest" "false" "Server ist bereits aktuell"
        return
    fi

    # Download game.zip if not already present
    if [[ ! -f "$GAME_ZIP" ]]; then
        download_game
    fi

    # Stop server
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    sleep 2

    # Extract game.zip to temp directory
    local extract_dir
    extract_dir="$(mktemp -d "${SERVER_DIR}/.update_extract_XXXXXX")"
    if ! unzip -q "$GAME_ZIP" -d "$extract_dir" 2>/dev/null; then
        rm -rf "$extract_dir"
        systemctl start "$SERVICE_NAME" 2>/dev/null || true
        json_error "game.zip konnte nicht entpackt werden"
    fi

    # Find the actual content root (might be nested in a subdirectory)
    local content_root="$extract_dir"
    if [[ -d "${extract_dir}/Server" || -f "${extract_dir}/Assets.zip" ]]; then
        content_root="$extract_dir"
    else
        local subdirs
        subdirs=($(find "$extract_dir" -mindepth 1 -maxdepth 1 -type d))
        if [[ ${#subdirs[@]} -eq 1 ]]; then
            content_root="${subdirs[0]}"
        fi
    fi

    # Create staging directory
    local staging_dir="${SERVER_DIR}/.update_staging"
    rm -rf "$staging_dir"
    mkdir -p "$staging_dir"

    # Copy new files to staging (skip preserved items)
    shopt -s dotglob nullglob
    for item in "$content_root"/*; do
        local basename
        basename="$(basename "$item")"
        local skip=false
        for preserve in "${PRESERVE[@]}"; do
            if [[ "$basename" == "$preserve" ]]; then
                skip=true
                break
            fi
        done
        if [[ "$skip" == "false" ]]; then
            cp -a "$item" "$staging_dir/"
        fi
    done
    shopt -u dotglob nullglob

    # Create backup of current files being replaced
    local backup_dir="${SERVER_DIR}/.update_backup_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$backup_dir"

    shopt -s dotglob nullglob
    for item in "$staging_dir"/*; do
        local basename
        basename="$(basename "$item")"
        if [[ -e "${SERVER_DIR}/${basename}" ]]; then
            mv "${SERVER_DIR}/${basename}" "$backup_dir/"
        fi
    done
    shopt -u dotglob nullglob

    # Move staged files to server root
    shopt -s dotglob nullglob
    for item in "$staging_dir"/*; do
        mv "$item" "$SERVER_DIR/"
    done
    shopt -u dotglob nullglob

    # Update version file
    echo "$latest" > "$VERSION_FILE"
    echo "$latest" > "$LATEST_VERSION_FILE"

    # Cleanup
    rm -rf "$extract_dir" "$staging_dir" "$GAME_ZIP" "$DOWNLOAD_LOG"

    # Remove auto-update flag if present
    rm -f "${SERVER_DIR}/.update_after_backup"

    # Start server
    systemctl start "$SERVICE_NAME" 2>/dev/null || true

    json_output "$latest" "$latest" "false" "Update auf ${latest} erfolgreich"
}

# --- Main ---
case "${1:-}" in
    check)
        do_check
        ;;
    update)
        do_update
        ;;
    *)
        echo "Usage: $0 {check|update}" >&2
        exit 1
        ;;
esac
