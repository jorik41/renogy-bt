#!/bin/bash
# Cleanup orphan renogy-bt processes and release port 6053

PORT="${1:-6053}"

echo "Checking for processes using port $PORT..."

# Find and kill processes using the port
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP 'pid=\K[0-9]+' | sort -u)

if [ -z "$PIDS" ]; then
    echo "No processes found using port $PORT"
else
    echo "Found processes using port $PORT: $PIDS"
    for pid in $PIDS; do
        echo "Killing process $pid..."
        kill -9 $pid 2>/dev/null || true
    done
    sleep 1
fi

# Kill any orphan renogy_bt_proxy.py processes
echo "Checking for orphan renogy_bt_proxy.py processes..."
pkill -9 -f "renogy_bt_proxy.py" 2>/dev/null || true

# Clean up PID file
rm -f /run/renogy-bt.pid 2>/dev/null || true

echo "Cleanup complete"
exit 0
