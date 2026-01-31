#!/bin/bash
#===============================================================================
# Hytale Server Wrapper with FIFO Console Support
#
# This wrapper script provides:
# - FIFO pipe for dashboard console commands
# - Staged update support (like official start.sh)
# - AOT cache support for faster startup
# - Proper working directory handling (Server/universe/)
#
# Install:
#   sudo cp start-hytale.sh /opt/hytale-server/start-wrapper.sh
#   sudo chmod 755 /opt/hytale-server/start-wrapper.sh
#   sudo chown hytale:hytale /opt/hytale-server/start-wrapper.sh
#
# Update systemd service ExecStart to use this wrapper instead of start.sh
#===============================================================================

HYTALE_DIR="/opt/hytale-server"
PIPE="${HYTALE_DIR}/.console_pipe"

cd "$HYTALE_DIR"

# Create FIFO pipe if it doesn't exist
if [ ! -p "$PIPE" ]; then
    mkfifo "$PIPE"
    chmod 660 "$PIPE"
    chown hytale:hytale "$PIPE" 2>/dev/null || true
fi

# Cleanup on exit
cleanup() {
    echo "[Wrapper] Shutting down..."
    rm -f "$PIPE"
    kill 0 2>/dev/null
    wait
}
trap cleanup EXIT INT TERM

# Main server loop (supports staged updates via exit code 8)
while true; do
    APPLIED_UPDATE=false

    # Apply staged update if present (like official start.sh)
    if [ -f "updater/staging/Server/HytaleServer.jar" ]; then
        echo "[Wrapper] Applying staged update..."
        cp -f updater/staging/Server/HytaleServer.jar Server/
        [ -f "updater/staging/Server/HytaleServer.aot" ] && cp -f updater/staging/Server/HytaleServer.aot Server/
        [ -d "updater/staging/Server/Licenses" ] && rm -rf Server/Licenses && cp -r updater/staging/Server/Licenses Server/
        [ -f "updater/staging/Assets.zip" ] && cp -f updater/staging/Assets.zip ./
        # Don't overwrite this wrapper with official start.sh
        # [ -f "updater/staging/start.sh" ] && cp -f updater/staging/start.sh ./
        [ -f "updater/staging/start.bat" ] && cp -f updater/staging/start.bat ./
        rm -rf updater/staging
        APPLIED_UPDATE=true
    fi

    # Change to Server/ directory (required since Hytale 2026.01)
    # This ensures universe data is created in Server/universe/
    cd "${HYTALE_DIR}/Server"

    # JVM arguments for AOT cache (faster startup)
    JVM_ARGS=""
    if [ -f "HytaleServer.aot" ]; then
        echo "[Wrapper] Using AOT cache for faster startup"
        JVM_ARGS="-XX:AOTCache=HytaleServer.aot"
    fi

    # Memory settings from environment or defaults
    MEMORY_MIN="${HYTALE_MEMORY_MIN:-2G}"
    MEMORY_MAX="${HYTALE_MEMORY_MAX:-4G}"

    # Backup settings from environment or defaults
    BACKUP_FREQUENCY="${HYTALE_BACKUP_FREQUENCY:-30}"

    # Default server arguments
    if [ "$BACKUP_FREQUENCY" -gt 0 ]; then
        DEFAULT_ARGS="--assets ../Assets.zip --backup --backup-dir backups --backup-frequency $BACKUP_FREQUENCY"
    else
        DEFAULT_ARGS="--assets ../Assets.zip"
    fi

    echo "[Wrapper] Starting Hytale Server..."
    echo "[Wrapper] Memory: ${MEMORY_MIN} - ${MEMORY_MAX}"
    echo "[Wrapper] Console pipe: ${PIPE}"

    # Start server with FIFO pipe for stdin
    START_TIME=$(date +%s)
    tail -f "$PIPE" | java \
        -Xms${MEMORY_MIN} \
        -Xmx${MEMORY_MAX} \
        $JVM_ARGS \
        -jar HytaleServer.jar \
        $DEFAULT_ARGS "$@"
    EXIT_CODE=$?
    ELAPSED=$(( $(date +%s) - START_TIME ))

    # Return to main dir for next iteration
    cd "$HYTALE_DIR"

    # Exit code 8 = restart for update
    if [ $EXIT_CODE -eq 8 ]; then
        echo "[Wrapper] Restarting to apply update..."
        continue
    fi

    # Warn on crash shortly after update
    if [ $EXIT_CODE -ne 0 ] && [ "$APPLIED_UPDATE" = true ] && [ $ELAPSED -lt 30 ]; then
        echo ""
        echo "[Wrapper] ERROR: Server exited with code $EXIT_CODE within ${ELAPSED}s of starting."
        echo "[Wrapper] This may indicate the update failed to start correctly."
        echo "[Wrapper] Your previous files are in the updater/backup/ folder."
        echo ""
    fi

    exit $EXIT_CODE
done
