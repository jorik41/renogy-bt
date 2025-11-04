#!/usr/bin/env python3
"""Test ESPHome native API connection and handshake"""
import socket
import struct
import sys

HOST = "192.168.1.28"
PORT = 6053

def read_varint(sock):
    """Read a protobuf varint from socket"""
    result = 0
    shift = 0
    while True:
        data = sock.recv(1)
        if not data:
            return None
        byte = data[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
    return result

def write_varint(value):
    """Encode a varint for protobuf"""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def send_message(sock, msg_type, data=b''):
    """Send an ESPHome API message (modern protocol: aioesphomeapi 42.x+)"""
    # Modern message format: 0x00 + varint(payload_length) + varint(type) + payload
    # Note: length field is ONLY the payload size, not including msg_type
    msg = bytearray([0x00])
    msg += write_varint(len(data))  # Length is only payload size
    msg += write_varint(msg_type)
    msg += data
    sock.send(msg)
    print(f"→ Sent message type {msg_type}, payload length {len(data)}")

def recv_message(sock):
    """Receive an ESPHome API message (modern protocol: aioesphomeapi 42.x+)"""
    # Read preamble
    preamble = sock.recv(1)
    if not preamble or preamble[0] != 0x00:
        print(f"✗ Invalid preamble: {preamble.hex() if preamble else 'empty'}")
        return None, None
    
    # Read length (payload size only, does NOT include msg_type)
    length = read_varint(sock)
    if length is None:
        print("✗ Failed to read message length")
        return None, None
    
    # Read message type
    msg_type = read_varint(sock)
    if msg_type is None:
        print("✗ Failed to read message type")
        return None, None
    
    # Read payload (length bytes)
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    
    print(f"← Received message type {msg_type}, payload length {length}, data length {len(data)}")
    return msg_type, data

def test_connection():
    """Test ESPHome API connection"""
    print(f"Connecting to {HOST}:{PORT}...")
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((HOST, PORT))
        print("✓ TCP connection established")
        
        # Step 1: Send HelloRequest (type 1)
        print("\n=== Step 1: Sending HelloRequest ===")
        send_message(sock, 1, b'\x0a\x06client')  # client_info field
        
        # Step 2: Expect HelloResponse (type 2)
        print("\n=== Step 2: Waiting for HelloResponse ===")
        msg_type, data = recv_message(sock)
        if msg_type == 2:
            print("✓ Received HelloResponse")
            print(f"  Data (hex): {data.hex()}")
            print(f"  Data (repr): {repr(data)}")
        else:
            print(f"✗ Expected HelloResponse (2), got type {msg_type}")
            if data:
                print(f"  Data: {data.hex()}")
        
        # Step 3: Send ConnectRequest (type 3)
        print("\n=== Step 3: Sending ConnectRequest ===")
        send_message(sock, 3, b'')  # Empty password
        
        # Step 4: Expect ConnectResponse (type 4)
        print("\n=== Step 4: Waiting for ConnectResponse ===")
        msg_type, data = recv_message(sock)
        if msg_type == 4:
            print("✓ Received ConnectResponse")
            print(f"  Data (hex): {data.hex()}")
            if len(data) > 0:
                print(f"  Connection status byte: {data[0]}")
        else:
            print(f"✗ Expected ConnectResponse (4), got type {msg_type}")
            if data:
                print(f"  Data: {data.hex()}")
        
        # Try to receive more messages
        print("\n=== Waiting for additional messages ===")
        sock.settimeout(3)
        try:
            while True:
                msg_type, data = recv_message(sock)
                if msg_type is None:
                    break
                print(f"  Extra message type {msg_type}: {data.hex()}")
        except socket.timeout:
            print("  (timeout - no more messages)")
        
        sock.close()
        print("\n✓ Connection closed")
        
    except socket.timeout:
        print("✗ Connection timeout")
        return 1
    except ConnectionRefusedError:
        print("✗ Connection refused")
        return 1
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(test_connection())
