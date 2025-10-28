# Running as a systemd service

The repository ships with a ready-to-use unit file that keeps the Renogy poller
and Home Assistant Bluetooth proxy running in the background and brings it back
after reboot.

1. Copy the unit into place (adjust the path or username if your checkout lives
   somewhere else):

   ```bash
   sudo install -m 644 systemd/renogy-bt.service /etc/systemd/system/renogy-bt.service
   ```

2. Reload the systemd manager, enable the service so it starts automatically on
   boot, and start it immediately:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now renogy-bt.service
   ```

3. Confirm the proxy is listening on port `6053` and retrying the Renogy
   connection in the background:

   ```bash
   systemctl status renogy-bt.service
   journalctl -u renogy-bt.service -n 50
   ```

The unit already waits for `network-online.target` and `bluetooth.target`, runs
as user `jorik`, and restarts the process if it crashes. If you use a different
username or installation path, edit the `User`, `Group`, `WorkingDirectory`, or
`ExecStart` entries before installing the service.
