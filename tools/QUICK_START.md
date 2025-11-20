# ESPHome API Test Suite - Quick Start

## What This Test Suite Does

This comprehensive test suite validates the ESPHome Native API handshake protocol and ensures your Bluetooth proxy implementation is correct.

### Key Tests

1. **Varint Encoding** - Validates protobuf varint encoding/decoding
2. **Device Name Format** - Ensures device name has required dot format (e.g., `renogy.proxy`)
3. **Length Field** - Verifies length field is payload-only (modern protocol)
4. **Handshake** - Tests HelloRequest/HelloResponse, ConnectRequest/ConnectResponse
5. **Device Info** - Validates device information and MAC address format
6. **BLE Subscription** - Tests Bluetooth advertisement subscription

## Quick Commands

```bash
# Test your running proxy
python3 tools/comprehensive_esphome_test.py

# Run with mock server (integration test)
python3 tools/run_integration_test.py

# Test remote server
python3 tools/comprehensive_esphome_test.py --host 192.168.1.100

# Quiet mode
python3 tools/comprehensive_esphome_test.py --quiet
```

## Common Issues Fixed

### ❌ Device Name Without Dot
**Problem:** Device name `renogyproxy` fails validation  
**Fix:** Use `renogy.proxy` (must contain a dot)

### ❌ Wrong Length Field
**Problem:** Length includes msg_type varint  
**Fix:** Length = payload size only (modern protocol)

### ❌ Invalid MAC Format
**Problem:** MAC address format not validated  
**Fix:** Must be `AA:BB:CC:DD:EE:FF` format

## Test Output Example

```
======================================================================
Testing Handshake Sequence
======================================================================

✓ TCP connection established to localhost:6053
ℹ Step 1: Sending HelloRequest (type 1)
✓ Received HelloResponse
✓ Device name validation: Valid device name format
✓ Connection accepted (invalid=false)
✓ Handshake sequence completed successfully
```

## Exit Codes

- `0` = All tests passed ✓
- `1` = One or more tests failed ✗

## See Also

- [tools/README_TESTING.md](README_TESTING.md) - Complete documentation
- [../README.md](../README.md) - Main project README
- [esphome_protocol_guide.py](esphome_protocol_guide.py) - Protocol reference
