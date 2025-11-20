#!/bin/bash
# Ensure Bluetooth adapter is ready before starting service

ADAPTER="${1:-hci0}"
MAX_RETRIES=5
RETRY_DELAY=2

echo "Ensuring Bluetooth adapter $ADAPTER is ready..."

for i in $(seq 1 $MAX_RETRIES); do
    # Check if adapter exists
    if ! hciconfig $ADAPTER > /dev/null 2>&1; then
        echo "Adapter $ADAPTER not found, waiting... (attempt $i/$MAX_RETRIES)"
        sleep $RETRY_DELAY
        continue
    fi
    
    # Bring adapter up
    hciconfig $ADAPTER up 2>/dev/null
    
    # Verify it's up
    if hciconfig $ADAPTER | grep -q "UP RUNNING"; then
        echo "Adapter $ADAPTER is UP and ready"
        exit 0
    fi
    
    echo "Adapter $ADAPTER not ready, retrying... (attempt $i/$MAX_RETRIES)"
    sleep $RETRY_DELAY
done

echo "ERROR: Failed to bring up adapter $ADAPTER after $MAX_RETRIES attempts"
exit 1
