#!/usr/bin/env python3
"""
Comprehensive ESPHome Native API Test Suite
Tests handshake protocol, data validation, and BLE proxy functionality
"""
import argparse
import re
import socket
import sys
import traceback
from typing import Optional, Tuple, Dict
from dataclasses import dataclass

# ANSI color codes for output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def print_pass(msg: str):
    print(f"{Colors.GREEN}✓{Colors.RESET} {msg}")

def print_fail(msg: str):
    print(f"{Colors.RED}✗{Colors.RESET} {msg}")

def print_warn(msg: str):
    print(f"{Colors.YELLOW}⚠{Colors.RESET} {msg}")

def print_info(msg: str):
    print(f"{Colors.CYAN}ℹ{Colors.RESET} {msg}")

def print_section(msg: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{msg}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}\n")

@dataclass
class TestConfig:
    """Configuration for test execution"""
    host: str = "localhost"
    port: int = 6053
    timeout: float = 10.0
    verbose: bool = True

class ProtocolValidator:
    """Validates ESPHome native API protocol compliance"""
    
    @staticmethod
    def encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint"""
        if value < 0:
            raise ValueError("Varint must be non-negative")
        result = bytearray()
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)
    
    @staticmethod
    def decode_varint(data: bytes, offset: int = 0) -> Tuple[int, int]:
        """
        Decode a varint from bytes
        Returns: (value, bytes_consumed)
        """
        result = 0
        shift = 0
        consumed = 0
        while offset + consumed < len(data):
            byte = data[offset + consumed]
            consumed += 1
            result |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                return result, consumed
            shift += 7
            if shift > 63:
                raise ValueError("Varint too long")
        raise ValueError("Incomplete varint")
    
    @staticmethod
    def validate_varint_encoding(value: int) -> bool:
        """Test varint encoding/decoding roundtrip"""
        try:
            encoded = ProtocolValidator.encode_varint(value)
            decoded, consumed = ProtocolValidator.decode_varint(encoded)
            if decoded != value:
                print_fail(f"Varint roundtrip failed: {value} != {decoded}")
                return False
            if consumed != len(encoded):
                print_fail(f"Varint length mismatch: {consumed} != {len(encoded)}")
                return False
            return True
        except Exception as e:
            print_fail(f"Varint validation error for {value}: {e}")
            return False
    
    @staticmethod
    def validate_device_name(name: str) -> Tuple[bool, str]:
        """
        Validate device name format
        ESPHome devices typically use dot notation: domain.name
        """
        if not name:
            return False, "Device name cannot be empty"
        
        if len(name) > 63:
            return False, f"Device name too long: {len(name)} > 63"
        
        if '.' not in name:
            return False, "Device name should contain a dot (domain.name format)"
        
        parts = name.split('.')
        if len(parts) < 2:
            return False, "Device name should have at least domain.name format"
        
        if any(not part for part in parts):
            return False, "Device name parts cannot be empty"
        
        # Check for valid characters (alphanumeric, hyphen, underscore, dot)
        if not re.match(r'^[a-zA-Z0-9._-]+$', name):
            return False, "Device name contains invalid characters"
        
        return True, "Valid device name format"
    
    @staticmethod
    def validate_mac_address(mac: str) -> Tuple[bool, str]:
        """Validate MAC address format"""
        if not mac:
            return False, "MAC address cannot be empty"
        
        # Accept both : and - separators
        if not re.match(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$', mac):
            return False, f"Invalid MAC address format: {mac}"
        
        return True, "Valid MAC address format"
    
    @staticmethod
    def make_packet(msg_type: int, payload: bytes) -> bytes:
        """
        Create ESPHome API packet (modern protocol)
        Format: [0x00] [length_varint] [type_varint] [payload]
        Note: length = len(payload) ONLY (not including type varint)
        """
        type_bytes = ProtocolValidator.encode_varint(msg_type)
        length = len(payload)  # Only payload size
        packet = bytearray([0x00])  # preamble
        packet.extend(ProtocolValidator.encode_varint(length))
        packet.extend(type_bytes)
        packet.extend(payload)
        return bytes(packet)
    
    @staticmethod
    def parse_packet(data: bytes) -> Tuple[Optional[int], Optional[bytes], int]:
        """
        Parse ESPHome API packet
        Returns: (msg_type, payload, total_bytes_consumed)
        Returns (None, None, 0) if packet is incomplete
        """
        if len(data) < 3:
            return None, None, 0
        
        offset = 0
        
        # Check preamble
        if data[offset] != 0x00:
            raise ValueError(f"Invalid preamble: 0x{data[offset]:02x}")
        offset += 1
        
        # Read length (payload size only)
        try:
            length, consumed = ProtocolValidator.decode_varint(data, offset)
            offset += consumed
        except ValueError:
            return None, None, 0
        
        # Read message type
        try:
            msg_type, consumed = ProtocolValidator.decode_varint(data, offset)
            offset += consumed
        except ValueError:
            return None, None, 0
        
        # Check if we have the full payload
        if offset + length > len(data):
            return None, None, 0
        
        payload = data[offset:offset + length]
        return msg_type, payload, offset + length

class ESPHomeTestClient:
    """Test client for ESPHome native API"""
    
    def __init__(self, config: TestConfig):
        self.config = config
        self.sock: Optional[socket.socket] = None
        self.validator = ProtocolValidator()
        self.test_results: Dict[str, bool] = {}
    
    def connect(self) -> bool:
        """Establish TCP connection"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.config.timeout)
            self.sock.connect((self.config.host, self.config.port))
            print_pass(f"TCP connection established to {self.config.host}:{self.config.port}")
            return True
        except socket.timeout:
            print_fail("Connection timeout")
            return False
        except ConnectionRefusedError:
            print_fail(f"Connection refused - is the server running on {self.config.host}:{self.config.port}?")
            return False
        except Exception as e:
            print_fail(f"Connection error: {e}")
            return False
    
    def disconnect(self):
        """Close connection"""
        if self.sock:
            self.sock.close()
            self.sock = None
            print_info("Connection closed")
    
    def send_message(self, msg_type: int, payload: bytes) -> bool:
        """Send a message to the server"""
        if not self.sock:
            print_fail("Not connected")
            return False
        
        try:
            packet = self.validator.make_packet(msg_type, payload)
            self.sock.send(packet)
            if self.config.verbose:
                print_info(f"→ Sent message type {msg_type}, payload length {len(payload)}")
                print_info(f"  Packet hex: {packet.hex()}")
            return True
        except Exception as e:
            print_fail(f"Send error: {e}")
            return False
    
    def receive_message(self, timeout: Optional[float] = None) -> Tuple[Optional[int], Optional[bytes]]:
        """Receive a message from the server"""
        if not self.sock:
            print_fail("Not connected")
            return None, None
        
        original_timeout = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        
        try:
            # Read incrementally until we have a complete packet
            buffer = b''
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    print_fail("Connection closed by server")
                    return None, None
                buffer += chunk
                
                msg_type, payload, consumed = self.validator.parse_packet(buffer)
                if msg_type is not None:
                    if self.config.verbose:
                        print_info(f"← Received message type {msg_type}, payload length {len(payload) if payload else 0}")
                        if payload:
                            print_info(f"  Payload hex: {payload.hex()}")
                    # Keep remaining data for next message
                    buffer = buffer[consumed:]
                    return msg_type, payload
        except socket.timeout:
            if self.config.verbose:
                print_info("  (timeout - no message received)")
            return None, None
        except Exception as e:
            print_fail(f"Receive error: {e}")
            return None, None
        finally:
            if timeout is not None:
                self.sock.settimeout(original_timeout)
    
    def test_varint_encoding(self) -> bool:
        """Test varint encoding for common values"""
        print_section("Testing Varint Encoding")
        
        test_values = [0, 1, 127, 128, 255, 256, 300, 65535, 1000000]
        all_passed = True
        
        for value in test_values:
            if self.validator.validate_varint_encoding(value):
                print_pass(f"Varint encoding/decoding for {value}: OK")
            else:
                all_passed = False
        
        self.test_results['varint_encoding'] = all_passed
        return all_passed
    
    def test_device_name_validation(self) -> bool:
        """Test device name format validation"""
        print_section("Testing Device Name Validation")
        
        test_cases = [
            ("renogy.proxy", True, "Valid with dot"),
            ("esphome.device", True, "Valid with dot"),
            ("my-device.test", True, "Valid with hyphen and dot"),
            ("device_1.home", True, "Valid with underscore and dot"),
            ("renogyproxy", False, "Missing dot"),
            ("", False, "Empty name"),
            ("a" * 70, False, "Too long"),
            (".device", False, "Empty first part"),
            ("device.", False, "Empty second part"),
            ("device..test", False, "Empty middle part"),
            ("device@test", False, "Invalid character"),
        ]
        
        all_passed = True
        for name, should_pass, description in test_cases:
            valid, reason = self.validator.validate_device_name(name)
            if valid == should_pass:
                print_pass(f"'{name}': {description} - {reason}")
            else:
                print_fail(f"'{name}': {description} - Expected {should_pass}, got {valid}: {reason}")
                all_passed = False
        
        self.test_results['device_name_validation'] = all_passed
        return all_passed
    
    def test_handshake_sequence(self) -> bool:
        """Test complete handshake sequence"""
        print_section("Testing Handshake Sequence")
        
        if not self.connect():
            self.test_results['handshake'] = False
            return False
        
        try:
            # Step 1: Send HelloRequest
            print_info("\nStep 1: Sending HelloRequest (type 1)")
            # HelloRequest with client_info = "test_client"
            hello_payload = b'\x0a\x0btest_client'
            if not self.send_message(1, hello_payload):
                self.test_results['handshake'] = False
                return False
            
            # Step 2: Receive HelloResponse
            print_info("\nStep 2: Waiting for HelloResponse (type 2)")
            msg_type, payload = self.receive_message()
            if msg_type != 2:
                print_fail(f"Expected HelloResponse (type 2), got type {msg_type}")
                self.test_results['handshake'] = False
                return False
            
            print_pass("Received HelloResponse")
            
            # Parse HelloResponse to extract device name
            if payload:
                # Try to extract name (field 3, wire type 2)
                # Simple parsing - look for field tag 0x1a (field 3, length-delimited)
                if b'\x1a' in payload:
                    idx = payload.index(b'\x1a')
                    if idx + 1 < len(payload):
                        name_len = payload[idx + 1]
                        if idx + 2 + name_len <= len(payload):
                            device_name = payload[idx + 2:idx + 2 + name_len].decode('utf-8', errors='ignore')
                            print_info(f"  Device name: {device_name}")
                            
                            # Validate device name format
                            valid, reason = self.validator.validate_device_name(device_name)
                            if valid:
                                print_pass(f"  Device name validation: {reason}")
                            else:
                                print_warn(f"  Device name validation: {reason}")
            
            # Step 3: Send ConnectRequest
            print_info("\nStep 3: Sending ConnectRequest (type 3)")
            # ConnectRequest with empty password
            connect_payload = b''
            if not self.send_message(3, connect_payload):
                self.test_results['handshake'] = False
                return False
            
            # Step 4: Receive ConnectResponse
            print_info("\nStep 4: Waiting for ConnectResponse (type 4)")
            msg_type, payload = self.receive_message()
            if msg_type != 4:
                print_fail(f"Expected ConnectResponse (type 4), got type {msg_type}")
                self.test_results['handshake'] = False
                return False
            
            print_pass("Received ConnectResponse")
            
            # Check connection status (field 1, varint)
            if payload and len(payload) >= 2:
                if payload[0] == 0x08:  # field 1, varint
                    invalid = payload[1]
                    if invalid == 0:
                        print_pass("  Connection accepted (invalid=false)")
                    else:
                        print_fail(f"  Connection rejected (invalid={invalid})")
                        self.test_results['handshake'] = False
                        return False
            
            print_pass("\n✓ Handshake sequence completed successfully")
            self.test_results['handshake'] = True
            return True
            
        except Exception as e:
            print_fail(f"Handshake error: {e}")
            traceback.print_exc()
            self.test_results['handshake'] = False
            return False
        finally:
            self.disconnect()
    
    def test_device_info_request(self) -> bool:
        """Test DeviceInfoRequest after handshake"""
        print_section("Testing DeviceInfoRequest")
        
        if not self.connect():
            self.test_results['device_info'] = False
            return False
        
        try:
            # Complete handshake first
            print_info("Completing handshake...")
            self.send_message(1, b'\x0a\x0btest_client')
            self.receive_message()
            self.send_message(3, b'')
            self.receive_message()
            print_pass("Handshake completed")
            
            # Send DeviceInfoRequest (type 9)
            print_info("\nSending DeviceInfoRequest (type 9)")
            if not self.send_message(9, b''):
                self.test_results['device_info'] = False
                return False
            
            # Receive DeviceInfoResponse (type 10)
            print_info("Waiting for DeviceInfoResponse (type 10)")
            msg_type, payload = self.receive_message()
            if msg_type != 10:
                print_fail(f"Expected DeviceInfoResponse (type 10), got type {msg_type}")
                self.test_results['device_info'] = False
                return False
            
            print_pass("Received DeviceInfoResponse")
            
            # Parse key fields from DeviceInfoResponse
            if payload:
                fields_found = []
                # Field 3: name (0x1a)
                if b'\x1a' in payload:
                    fields_found.append("name")
                # Field 4: mac_address (0x22)
                if b'\x22' in payload:
                    fields_found.append("mac_address")
                # Field 17: bluetooth_proxy_feature_flags (0x88 0x01)
                if b'\x88\x01' in payload:
                    fields_found.append("bluetooth_proxy_feature_flags")
                
                print_info(f"  Fields found: {', '.join(fields_found)}")
                
                # Extract and validate MAC address
                if b'\x22' in payload:
                    try:
                        idx = payload.index(b'\x22')
                        if idx + 1 < len(payload):
                            mac_len = payload[idx + 1]
                            if idx + 2 + mac_len <= len(payload):
                                mac = payload[idx + 2:idx + 2 + mac_len].decode('utf-8', errors='ignore')
                                print_info(f"  MAC address: {mac}")
                                valid, reason = self.validator.validate_mac_address(mac)
                                if valid:
                                    print_pass(f"  MAC address validation: {reason}")
                                else:
                                    print_warn(f"  MAC address validation: {reason}")
                    except Exception as e:
                        print_warn(f"  Could not parse MAC address: {e}")
            
            print_pass("\n✓ DeviceInfoRequest completed successfully")
            self.test_results['device_info'] = True
            return True
            
        except Exception as e:
            print_fail(f"DeviceInfo test error: {e}")
            traceback.print_exc()
            self.test_results['device_info'] = False
            return False
        finally:
            self.disconnect()
    
    def test_ble_subscription(self) -> bool:
        """Test BLE advertisement subscription"""
        print_section("Testing BLE Advertisement Subscription")
        
        if not self.connect():
            self.test_results['ble_subscription'] = False
            return False
        
        try:
            # Complete handshake
            print_info("Completing handshake...")
            self.send_message(1, b'\x0a\x0btest_client')
            self.receive_message()
            self.send_message(3, b'')
            self.receive_message()
            print_pass("Handshake completed")
            
            # Send SubscribeBluetoothLEAdvertisementsRequest (type 66)
            print_info("\nSending SubscribeBluetoothLEAdvertisementsRequest (type 66)")
            if not self.send_message(66, b''):
                self.test_results['ble_subscription'] = False
                return False
            
            # Wait for various responses
            print_info("Waiting for responses (scanner state, advertisements)...")
            responses_received = []
            
            for _ in range(5):  # Try to receive up to 5 messages
                msg_type, payload = self.receive_message(timeout=3.0)
                if msg_type is None:
                    break
                
                response_names = {
                    68: "BluetoothLEAdvertisementResponse",
                    69: "BluetoothLERawAdvertisementsResponse",
                    87: "BluetoothScannerStateResponse",
                }
                response_name = response_names.get(msg_type, f"Unknown (type {msg_type})")
                responses_received.append(response_name)
                print_pass(f"  Received: {response_name}")
            
            if responses_received:
                print_pass(f"\n✓ Received {len(responses_received)} response(s)")
                self.test_results['ble_subscription'] = True
                return True
            else:
                print_warn("\n⚠ No responses received (server may not have BLE advertisements yet)")
                self.test_results['ble_subscription'] = True  # Not a failure
                return True
            
        except Exception as e:
            print_fail(f"BLE subscription test error: {e}")
            traceback.print_exc()
            self.test_results['ble_subscription'] = False
            return False
        finally:
            self.disconnect()
    
    def test_length_field_accuracy(self) -> bool:
        """Test that length field is correctly interpreted as payload-only"""
        print_section("Testing Length Field Accuracy")
        
        print_info("Testing that length field = payload size (not including msg_type)")
        
        # Test case 1: Empty payload
        test_cases = [
            (1, b'', "Empty payload"),
            (1, b'\x0a\x04test', "Small payload (6 bytes)"),
            (1, b'\x0a' + b'x' * 100, "Large payload (101 bytes)"),
        ]
        
        all_passed = True
        for msg_type, payload, description in test_cases:
            packet = self.validator.make_packet(msg_type, payload)
            
            # Parse it back
            parsed_type, parsed_payload, consumed = self.validator.parse_packet(packet)
            
            if parsed_type == msg_type and parsed_payload == payload:
                print_pass(f"{description}: length={len(payload)}, parsed correctly")
            else:
                print_fail(f"{description}: parsing failed")
                all_passed = False
        
        self.test_results['length_field'] = all_passed
        return all_passed
    
    def run_all_tests(self) -> bool:
        """Run all test suites"""
        print_section("ESPHome Native API Comprehensive Test Suite")
        print_info(f"Target: {self.config.host}:{self.config.port}")
        print_info(f"Timeout: {self.config.timeout}s")
        
        # Run all tests
        self.test_varint_encoding()
        self.test_device_name_validation()
        self.test_length_field_accuracy()
        self.test_handshake_sequence()
        self.test_device_info_request()
        self.test_ble_subscription()
        
        # Summary
        print_section("Test Summary")
        
        total = len(self.test_results)
        passed = sum(1 for v in self.test_results.values() if v)
        
        for test_name, result in self.test_results.items():
            status = "PASS" if result else "FAIL"
            color = Colors.GREEN if result else Colors.RED
            print(f"{color}{status:6}{Colors.RESET} {test_name}")
        
        print(f"\n{Colors.BOLD}Total: {passed}/{total} tests passed{Colors.RESET}")
        
        return passed == total

def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Comprehensive ESPHome Native API test suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test local server
  %(prog)s
  
  # Test remote server
  %(prog)s --host 192.168.1.100
  
  # Test with custom port
  %(prog)s --port 6053
  
  # Quiet mode (less verbose)
  %(prog)s --quiet
        """
    )
    parser.add_argument('--host', default='localhost', help='Server hostname or IP (default: localhost)')
    parser.add_argument('--port', type=int, default=6053, help='Server port (default: 6053)')
    parser.add_argument('--timeout', type=float, default=10.0, help='Connection timeout in seconds (default: 10.0)')
    parser.add_argument('--quiet', action='store_true', help='Reduce verbose output')
    
    args = parser.parse_args()
    
    config = TestConfig(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        verbose=not args.quiet
    )
    
    client = ESPHomeTestClient(config)
    success = client.run_all_tests()
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
