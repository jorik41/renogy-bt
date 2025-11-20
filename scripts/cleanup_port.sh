#!/bin/bash
# Safe cleanup: only kill processes on port 6053 if they exist
PORT=6053

# Find processes using the port
PIDS=$(ss -tlnp 2>/dev/null | grep ":$PORT " | grep -oP "pid=\K[0-9]+" | sort -u)

if [ -n "$PIDS" ]; then
    for pid in $PIDS; do
        # Check if this is actually a renogy process
        if ps -p $pid -o cmd= 2>/dev/null | grep -q "renogy_bt_proxy.py"; then
            echo "Cleaning up orphan process $pid on port $PORT"
            kill -9 $pid 2>/dev/null || true
        fi
    done
    sleep 1
fi

exit 0
