# ESPHome API Testing Tools

This directory contains comprehensive testing tools for validating the ESPHome Native API handshake protocol and Bluetooth proxy functionality.

## Tools Overview

### 1. `comprehensive_esphome_test.py`
**Comprehensive test suite for ESPHome Native API compliance**

This is the main testing tool that validates all aspects of the ESPHome API handshake protocol, including:

- ✅ **Varint Encoding/Decoding**: Tests protobuf varint implementation correctness
- ✅ **Device Name Format**: Validates that device names follow the required `domain.name` format with a dot
- ✅ **Message Length Field**: Ensures length field accurately represents payload size (not including msg_type)
- ✅ **Handshake Sequence**: Tests complete HelloRequest/HelloResponse and ConnectRequest/ConnectResponse flow
- ✅ **DeviceInfo Request**: Validates device information retrieval and field parsing
- ✅ **BLE Advertisement Subscription**: Tests Bluetooth proxy advertisement subscription and scanner state
- ✅ **Protocol Compliance**: Validates message structure, preamble, and packet format
- ✅ **MAC Address Format**: Checks MAC address format validity
- ✅ **Error Handling**: Tests edge cases and malformed data

#### Usage

```bash
# Test local server (default: localhost:6053)
python3 tools/comprehensive_esphome_test.py

# Test remote server
python3 tools/comprehensive_esphome_test.py --host 192.168.1.100

# Test with custom port
python3 tools/comprehensive_esphome_test.py --port 6053

# Quiet mode (less verbose output)
python3 tools/comprehensive_esphome_test.py --quiet

# Full options
python3 tools/comprehensive_esphome_test.py --help
```

#### Test Output

The test suite provides color-coded output:
- ✓ **Green checkmarks**: Tests that pass
- ✗ **Red X marks**: Tests that fail
- ⚠ **Yellow warnings**: Non-critical issues
- ℹ **Blue info**: Informational messages

Example output:
```
======================================================================
Testing Handshake Sequence
======================================================================

✓ TCP connection established to localhost:6053
ℹ Step 1: Sending HelloRequest (type 1)
ℹ Step 2: Waiting for HelloResponse (type 2)
✓ Received HelloResponse
✓ Device name validation: Valid device name format
...
```

### 2. `mock_esphome_server.py`
**Mock ESPHome API server for testing**

A minimal ESPHome API server implementation that can be used to test clients or validate protocol behavior.

#### Usage

```bash
# Start server on default port
python3 tools/mock_esphome_server.py

# Custom configuration
python3 tools/mock_esphome_server.py --host 0.0.0.0 --port 6054 \
    --name "test.device" --mac "AA:BB:CC:DD:EE:FF"
```

### 3. `test_esphome_api.py`
**Simple handshake test script**

A lightweight test script for basic handshake validation. Use `comprehensive_esphome_test.py` for more thorough testing.

### 4. `esphome_protocol_guide.py`
**Protocol documentation and examples**

Educational tool showing the ESPHome Native API protocol structure with concrete examples.

## Common Issues and Solutions

### Issue: "Device name should contain a dot"

**Problem**: ESPHome devices should use the format `domain.name` (e.g., `renogy.proxy`, `esphome.device`).

**Solution**: Update your device name in `config.ini`:
```ini
[home_assistant_proxy]
device_name = renogy.proxy  # ✓ Correct - has a dot
# device_name = renogyproxy  # ✗ Wrong - missing dot
```

### Issue: "Length field validation failed"

**Problem**: The modern ESPHome protocol (aioesphomeapi 42.x+) uses a length field that represents ONLY the payload size, not including the message type varint.

**Solution**: Ensure your implementation follows this format:
```
[0x00] [length_varint] [type_varint] [payload]
       ^^^^^^^^^^^^^^^
       Payload size only (not including type_varint)
```

### Issue: "Invalid preamble"

**Problem**: All ESPHome API messages must start with `0x00` as the preamble byte.

**Solution**: Verify your packet construction:
```python
packet = bytearray([0x00])  # Preamble
packet.extend(encode_varint(len(payload)))  # Length
packet.extend(encode_varint(msg_type))      # Type
packet.extend(payload)                      # Payload
```

### Issue: "Connection refused"

**Problem**: The ESPHome API server is not running or is not accessible.

**Solution**:
1. Check if `renogy_bt_proxy.py` is running
2. Verify the correct port (default: 6053)
3. Check firewall rules
4. Verify `[home_assistant_proxy]` is enabled in `config.ini`

## Protocol Reference

### Message Format (Modern Protocol - aioesphomeapi 42.x+)

```
┌─────────┬─────────────┬──────────────┬─────────┐
│ Preamble│   Length    │ Message Type │ Payload │
│  (0x00) │  (varint)   │   (varint)   │ (bytes) │
│  1 byte │  1-5 bytes  │   1-5 bytes  │ N bytes │
└─────────┴─────────────┴──────────────┴─────────┘
           └──────┬──────┘
                  └─ Length = len(Payload) ONLY
```

### Handshake Sequence

```
Client                          Server
   │                               │
   ├── HelloRequest (type 1) ────→ │
   │                               │
   │ ←──── HelloResponse (type 2) ─┤
   │                               │
   ├── ConnectRequest (type 3) ──→ │
   │                               │
   │ ←─── ConnectResponse (type 4)─┤
   │                               │
   ├── DeviceInfoRequest (9) ────→ │
   │                               │
   │ ←─── DeviceInfoResponse (10)──┤
   │                               │
```

### Key Message Types

| Type | Name | Direction | Purpose |
|------|------|-----------|---------|
| 1 | HelloRequest | Client → Server | Initiate connection |
| 2 | HelloResponse | Server → Client | Server identification |
| 3 | ConnectRequest | Client → Server | Authentication |
| 4 | ConnectResponse | Server → Client | Connection status |
| 9 | DeviceInfoRequest | Client → Server | Query device info |
| 10 | DeviceInfoResponse | Server → Client | Device details |
| 66 | SubscribeBluetoothLEAdvertisementsRequest | Client → Server | Subscribe to BLE ads |
| 68 | BluetoothLEAdvertisementResponse | Server → Client | Legacy BLE advertisement |
| 69 | BluetoothLERawAdvertisementsResponse | Server → Client | Raw BLE advertisement |
| 87 | BluetoothScannerStateResponse | Server → Client | Scanner state update |

### Varint Encoding

Protobuf variable-length integer encoding:
- Values < 128: Single byte (e.g., `1` → `0x01`)
- Values ≥ 128: Multiple bytes with continuation bit
  - `128` → `0x80 0x01`
  - `300` → `0xac 0x02`

## Running Tests Against Your Server

### Quick Test
```bash
# Test the actual proxy
python3 ./renogy_bt_proxy.py config.ini &
PROXY_PID=$!
sleep 5
python3 tools/comprehensive_esphome_test.py
kill $PROXY_PID
```

### Automated Testing
```bash
# Run all tests with summary
python3 tools/comprehensive_esphome_test.py --quiet
echo "Exit code: $?"
```

Exit codes:
- `0`: All tests passed
- `1`: One or more tests failed

## Test Development

To add new test cases, edit `comprehensive_esphome_test.py`:

```python
def test_my_feature(self) -> bool:
    """Test description"""
    print_section("Testing My Feature")
    
    # Your test logic here
    
    self.test_results['my_feature'] = result
    return result
```

Then add it to `run_all_tests()`:
```python
def run_all_tests(self) -> bool:
    self.test_varint_encoding()
    self.test_device_name_validation()
    # ... other tests ...
    self.test_my_feature()  # Add your test
```

## References

- [ESPHome Bluetooth Proxy Documentation](https://esphome.io/components/bluetooth_proxy.html)
- [aioesphomeapi Protocol Implementation](https://github.com/esphome/aioesphomeapi)
- [Protocol Buffers Encoding](https://developers.google.com/protocol-buffers/docs/encoding)
- [Renogy BT Project README](../README.md)

## Troubleshooting

### All tests fail with "Connection refused"
- Ensure the ESPHome API server is running
- Check that the correct port is being used
- Verify firewall settings

### Handshake fails after HelloRequest
- Check device name format (must contain a dot)
- Verify packet structure (preamble, length, type, payload)
- Enable verbose mode: `--verbose` (remove `--quiet`)

### BLE subscription tests timeout
- Server may not have BLE advertisements yet (not a failure)
- Wait for BLE devices to be in range
- Check that Bluetooth adapter is working

### Tests pass locally but fail remotely
- Check network connectivity
- Verify firewall rules allow port 6053
- Ensure mDNS is working if using hostnames

## Contributing

When modifying the test suite:
1. Ensure all existing tests still pass
2. Add tests for new features
3. Update this README with new test descriptions
4. Follow the existing test pattern and naming conventions
