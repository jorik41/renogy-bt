# Renogy-BT Service Improvements

## Issues Fixed

1. **Port Binding Failures**: Added SO_REUSEPORT to socket options to prevent "Address already in use" errors
2. **Orphan Processes**: Added automatic cleanup of old processes before service start
3. **Bluetooth Adapter Issues**: Ensured hci0 adapter is UP before starting
4. **Multiple Instances**: Added PID file enforcement and conflict prevention
5. **Poor Restart Behavior**: Improved systemd restart policy with better limits

## Installation

### Quick Install (from Pi)

```bash
cd /home/jorik/renogy-bt

# Backup current service
sudo systemctl stop renogy-bt.service
sudo cp /etc/systemd/system/renogy-bt.service /etc/systemd/system/renogy-bt.service.bak

# Apply socket fix
cd /home/jorik/renogy-bt
patch -p1 < fix_esphome_socket.patch

# Install helper scripts
chmod +x scripts/*.sh

# Install improved service file
sudo cp systemd/renogy-bt-improved.service /etc/systemd/system/renogy-bt.service

# Reload and start
sudo systemctl daemon-reload
sudo systemctl start renogy-bt.service
sudo systemctl status renogy-bt.service
```

### Manual Cleanup (if issues persist)

```bash
# Stop service
sudo systemctl stop renogy-bt.service

# Run cleanup script
sudo /home/jorik/renogy-bt/scripts/cleanup_orphans.sh

# Ensure BT is ready
sudo /home/jorik/renogy-bt/scripts/ensure_bt_ready.sh

# Start service
sudo systemctl start renogy-bt.service
```

## Verification

Check that everything is working:

```bash
# Service status
systemctl status renogy-bt.service

# Check port is in use by correct process
sudo ss -tlnp | grep 6053

# Monitor logs for BLE advertisements
journalctl -u renogy-bt.service -f | grep "BLE advertisement"

# Check BLE adapter
hciconfig hci0
```

## Rollback

If you need to rollback:

```bash
sudo systemctl stop renogy-bt.service
sudo cp /etc/systemd/system/renogy-bt.service.bak /etc/systemd/system/renogy-bt.service
sudo systemctl daemon-reload
sudo systemctl start renogy-bt.service
```
