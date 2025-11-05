#!/usr/bin/env python3
"""
Mock ESPHome API Server for Testing
Implements minimal handshake for testing the comprehensive test suite
"""
import asyncio
import sys
from typing import Optional

class MockESPHomeProtocol(asyncio.Protocol):
    """Minimal ESPHome protocol implementation for testing"""
    
    def __init__(self, device_name: str = "test.device", mac_address: str = "AA:BB:CC:DD:EE:FF"):
        self.device_name = device_name
        self.mac_address = mac_address
        self.transport: Optional[asyncio.Transport] = None
        self.buffer = b''
    
    @staticmethod
    def encode_varint(value: int) -> bytes:
        """Encode an integer as a protobuf varint"""
        result = bytearray()
        while value > 0x7F:
            result.append((value & 0x7F) | 0x80)
            value >>= 7
        result.append(value & 0x7F)
        return bytes(result)
    
    @staticmethod
    def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
        """Decode a varint from bytes"""
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
        raise ValueError("Incomplete varint")
    
    def make_packet(self, msg_type: int, payload: bytes) -> bytes:
        """Create an ESPHome API packet"""
        type_bytes = self.encode_varint(msg_type)
        length = len(payload)
        packet = bytearray([0x00])  # preamble
        packet.extend(self.encode_varint(length))
        packet.extend(type_bytes)
        packet.extend(payload)
        return bytes(packet)
    
    def connection_made(self, transport: asyncio.Transport) -> None:
        """Called when connection is established"""
        self.transport = transport
        peer = transport.get_extra_info("peername")
        print(f"Connection from {peer}")
    
    def connection_lost(self, exc: Optional[BaseException]) -> None:
        """Called when connection is closed"""
        print("Connection closed")
    
    def data_received(self, data: bytes) -> None:
        """Process incoming data"""
        self.buffer += data
        
        while self.buffer:
            # Try to parse a message
            if len(self.buffer) < 3:
                return
            
            try:
                offset = 0
                
                # Check preamble
                if self.buffer[offset] != 0x00:
                    print(f"Invalid preamble: {self.buffer[offset]:02x}")
                    self.transport.close()
                    return
                offset += 1
                
                # Read length
                length, consumed = self.decode_varint(self.buffer, offset)
                offset += consumed
                
                # Read message type
                msg_type, consumed = self.decode_varint(self.buffer, offset)
                offset += consumed
                
                # Check if we have full payload
                if offset + length > len(self.buffer):
                    return  # Wait for more data
                
                payload = self.buffer[offset:offset + length]
                self.buffer = self.buffer[offset + length:]
                
                # Process the message
                self.handle_message(msg_type, payload)
                
            except ValueError:
                return  # Wait for more data
    
    def handle_message(self, msg_type: int, payload: bytes) -> None:
        """Handle a received message"""
        print(f"Received message type {msg_type}, payload length {len(payload)}")
        
        if msg_type == 1:  # HelloRequest
            self.handle_hello_request(payload)
        elif msg_type == 3:  # ConnectRequest
            self.handle_connect_request(payload)
        elif msg_type == 9:  # DeviceInfoRequest
            self.handle_device_info_request()
        elif msg_type == 66:  # SubscribeBluetoothLEAdvertisementsRequest
            self.handle_ble_subscribe()
        else:
            print(f"Unhandled message type {msg_type}")
    
    def send_message(self, msg_type: int, payload: bytes) -> None:
        """Send a message to the client"""
        if self.transport:
            packet = self.make_packet(msg_type, payload)
            self.transport.write(packet)
            print(f"Sent message type {msg_type}, payload length {len(payload)}")
    
    def handle_hello_request(self, payload: bytes) -> None:
        """Handle HelloRequest and send HelloResponse"""
        # HelloResponse (type 2)
        # Field 1 (api_version_major): 1
        # Field 2 (api_version_minor): 12
        # Field 3 (name): device_name
        # Field 4 (server_info): "mock-server/1.0"
        
        response = bytearray()
        # api_version_major (field 1, varint)
        response.extend(b'\x08\x01')
        # api_version_minor (field 2, varint)
        response.extend(b'\x10\x0c')
        # name (field 3, string)
        name_bytes = self.device_name.encode('utf-8')
        response.extend(b'\x1a')
        response.extend(self.encode_varint(len(name_bytes)))
        response.extend(name_bytes)
        # server_info (field 4, string)
        server_info = b"mock-server/1.0"
        response.extend(b'\x22')
        response.extend(self.encode_varint(len(server_info)))
        response.extend(server_info)
        
        self.send_message(2, bytes(response))
    
    def handle_connect_request(self, payload: bytes) -> None:
        """Handle ConnectRequest and send ConnectResponse"""
        # ConnectResponse (type 4)
        # Field 1 (invalid): false (0)
        response = b'\x08\x00'
        self.send_message(4, response)
    
    def handle_device_info_request(self) -> None:
        """Handle DeviceInfoRequest and send DeviceInfoResponse"""
        # DeviceInfoResponse (type 10)
        response = bytearray()
        
        # uses_password (field 1, bool): false
        response.extend(b'\x08\x00')
        # name (field 3, string)
        name_bytes = self.device_name.encode('utf-8')
        response.extend(b'\x1a')
        response.extend(self.encode_varint(len(name_bytes)))
        response.extend(name_bytes)
        # mac_address (field 4, string)
        mac_bytes = self.mac_address.encode('utf-8')
        response.extend(b'\x22')
        response.extend(self.encode_varint(len(mac_bytes)))
        response.extend(mac_bytes)
        # esphome_version (field 5, string)
        version = b"2024.12.0"
        response.extend(b'\x2a')
        response.extend(self.encode_varint(len(version)))
        response.extend(version)
        # model (field 8, string)
        model = b"Mock ESPHome Device"
        response.extend(b'\x42')
        response.extend(self.encode_varint(len(model)))
        response.extend(model)
        # bluetooth_proxy_feature_flags (field 17, uint32)
        # Passive scan (1) + raw advertisements (32) + state/mode (64) = 97
        response.extend(b'\x88\x01\x61')
        
        self.send_message(10, bytes(response))
    
    def handle_ble_subscribe(self) -> None:
        """Handle BLE subscription and send scanner state"""
        # Send BluetoothScannerStateResponse (type 87)
        # Field 1 (state): RUNNING (1)
        # Field 2 (mode): PASSIVE (1)
        # Field 3 (configured_mode): PASSIVE (1)
        response = b'\x08\x01\x10\x01\x18\x01'
        self.send_message(87, bytes(response))
        
        # Optionally send a mock advertisement
        # BluetoothLERawAdvertisementsResponse (type 69)
        print("BLE subscription handled")

async def run_server(host: str = "localhost", port: int = 6053, 
                     device_name: str = "test.device",
                     mac_address: str = "AA:BB:CC:DD:EE:FF") -> None:
    """Run the mock server"""
    loop = asyncio.get_running_loop()
    
    def factory():
        return MockESPHomeProtocol(device_name, mac_address)
    
    server = await loop.create_server(factory, host=host, port=port)
    
    print(f"Mock ESPHome server listening on {host}:{port}")
    print(f"Device name: {device_name}")
    print(f"MAC address: {mac_address}")
    print("Press Ctrl+C to stop")
    
    async with server:
        await server.serve_forever()

def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Mock ESPHome API server for testing")
    parser.add_argument('--host', default='localhost', help='Listen host (default: localhost)')
    parser.add_argument('--port', type=int, default=6053, help='Listen port (default: 6053)')
    parser.add_argument('--name', default='test.device', help='Device name (default: test.device)')
    parser.add_argument('--mac', default='AA:BB:CC:DD:EE:FF', help='MAC address (default: AA:BB:CC:DD:EE:FF)')
    
    args = parser.parse_args()
    
    try:
        asyncio.run(run_server(args.host, args.port, args.name, args.mac))
    except KeyboardInterrupt:
        print("\nServer stopped")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
