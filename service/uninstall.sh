#!/bin/bash
# Uninstall Prabhjot's Pipeline LaunchAgent

PLIST_NAME="com.prabhjot-pipeline.dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

if [ -f "$PLIST_PATH" ]; then
    echo "Stopping Prabhjot's Pipeline..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Prabhjot's Pipeline LaunchAgent removed."
else
    echo "Prabhjot's Pipeline LaunchAgent not found."
fi
