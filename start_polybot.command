#!/bin/bash
# Double-click this file to start PolyBot with auto-restart
# It will open in Terminal and keep running

cd "$(dirname "$0")"

# Check if already running
if pgrep -f "uvicorn server:app" > /dev/null; then
    echo "PolyBot is already running!"
    echo "To stop it, run: pkill -f 'uvicorn server:app'"
    read -p "Press Enter to exit..."
    exit 1
fi

echo "Starting PolyBot..."
chmod +x scripts/run_server_forever.sh
./scripts/run_server_forever.sh
