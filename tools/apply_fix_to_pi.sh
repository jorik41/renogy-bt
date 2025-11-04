#!/bin/bash
# Apply ESPHome handshake fix to Pi Zero at 192.168.1.28

set -e

PI_HOST="192.168.1.28"
PI_USER="pi"  # Change if different
REPO_PATH="~/renogy-bt"  # Change if different

echo "========================================================================"
echo "ESPHome Handshake Fix - Remote Application Script"
echo "========================================================================"
echo ""
echo "This script will:"
echo "1. SSH to your Pi Zero at $PI_HOST"
echo "2. Apply the handshake fix"
echo "3. Restart the service"
echo ""
echo "Prerequisites:"
echo "- SSH access to pi@$PI_HOST (with key or you'll be prompted for password)"
echo "- renogy-bt repository at $REPO_PATH on the Pi"
echo ""
read -p "Press ENTER to continue or Ctrl+C to cancel..."

echo ""
echo "Step 1: Checking SSH access..."
if ! ssh -o ConnectTimeout=5 -o BatchMode=yes ${PI_USER}@${PI_HOST} "echo 'SSH OK'" 2>/dev/null; then
    echo "ERROR: Cannot connect to ${PI_USER}@${PI_HOST}"
    echo "Make sure:"
    echo "  - Pi is powered on and connected"
    echo "  - SSH is enabled"
    echo "  - You have SSH key setup or can enter password"
    exit 1
fi
echo "✓ SSH access confirmed"

echo ""
echo "Step 2: Checking repository..."
if ! ssh ${PI_USER}@${PI_HOST} "cd ${REPO_PATH} && pwd" >/dev/null 2>&1; then
    echo "ERROR: Repository not found at ${REPO_PATH}"
    echo "Please update the REPO_PATH variable in this script"
    exit 1
fi
echo "✓ Repository found"

echo ""
echo "Step 3: Pulling latest changes..."
ssh ${PI_USER}@${PI_HOST} "cd ${REPO_PATH} && git fetch origin && git checkout codex/add-ha-bt-proxy-support && git pull origin codex/add-ha-bt-proxy-support"
echo "✓ Code updated"

echo ""
echo "Step 4: Verifying fix is present..."
if ssh ${PI_USER}@${PI_HOST} "grep -q 'pos_before_msg_type = self._pos' ${REPO_PATH}/renogybt/esphome_api_server.py"; then
    echo "✓ Fix is present in the code"
else
    echo "⚠ WARNING: Fix not found in code. Applying manually..."
    
    # Create patch on Pi and apply it
    ssh ${PI_USER}@${PI_HOST} << 'ENDSSH'
cd ~/renogy-bt
cat > /tmp/esphome_fix.patch << 'EOF'
--- a/renogybt/esphome_api_server.py
+++ b/renogybt/esphome_api_server.py
@@ -133,6 +133,9 @@ class ESPHomeAPIProtocol(asyncio.Protocol):
                 self._close_transport()
                 return
 
+            # Remember position before reading msg_type to calculate its size
+            pos_before_msg_type = self._pos
+            
             msg_type = self._read_varuint()
             if msg_type == -1:
                 logger.error("Failed to read message type; closing connection")
@@ -140,12 +143,17 @@ class ESPHomeAPIProtocol(asyncio.Protocol):
                 self._close_transport()
                 return
 
+            # Calculate how many bytes the msg_type varint consumed
+            msg_type_len = self._pos - pos_before_msg_type
+
             if length == 0:
                 self._remove_from_buffer()
                 self._process_packet(msg_type, b"")
                 continue
 
-            packet = self._read(length)
+            # length includes msg_type, so subtract it to get payload length
+            payload_len = length - msg_type_len
+            packet = self._read(payload_len)
             if packet is None:
                 return  # Wait for the rest of the packet
 
EOF
patch -p1 < /tmp/esphome_fix.patch
rm /tmp/esphome_fix.patch
ENDSSH
    echo "✓ Patch applied"
fi

echo ""
echo "Step 5: Finding and restarting service..."
# Try to find and restart the service
if ssh ${PI_USER}@${PI_HOST} "systemctl is-active --quiet renogy-bt 2>/dev/null"; then
    echo "Found systemd service 'renogy-bt', restarting..."
    ssh ${PI_USER}@${PI_HOST} "sudo systemctl restart renogy-bt"
    echo "✓ Service restarted"
elif ssh ${PI_USER}@${PI_HOST} "pgrep -f esphome_proxy_example.py >/dev/null"; then
    echo "Found running esphome_proxy_example.py process, restarting..."
    ssh ${PI_USER}@${PI_HOST} "pkill -f esphome_proxy_example.py; cd ${REPO_PATH} && nohup python3 esphome_proxy_example.py > /tmp/renogy-proxy.log 2>&1 &"
    sleep 2
    echo "✓ Process restarted"
else
    echo "⚠ Could not find running service. Please restart manually:"
    echo "   ssh ${PI_USER}@${PI_HOST}"
    echo "   cd ${REPO_PATH}"
    echo "   python3 esphome_proxy_example.py"
fi

echo ""
echo "========================================================================"
echo "✓ FIX APPLIED!"
echo "========================================================================"
echo ""
echo "Now test the connection:"
echo "  python3 /home/jorik41/test_esphome_api.py"
echo ""
echo "You should see:"
echo "  ✓ Received HelloResponse"
echo "  ✓ Received ConnectResponse"
echo ""
echo "If it works, add the device in Home Assistant:"
echo "  Settings -> Devices & Services -> Add Integration -> ESPHome"
echo ""
