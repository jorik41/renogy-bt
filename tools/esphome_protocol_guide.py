#!/usr/bin/env python3
"""
ESPHome Native API Protocol Analyzer
Toont wat je Pi moet implementeren voor correcte handshake
"""

print("=" * 70)
print("ESPHome Native API Protocol - Wat je moet implementeren")
print("=" * 70)

print("\n1. BERICHT STRUCTUUR:")
print("   Elk bericht: [0x00] [length_varint] [type_varint] [data]")
print("   - 0x00 = preamble")
print("   - length = totale lengte van type+data (varint encoded)")
print("   - type = message type ID (varint encoded)")
print("   - data = protobuf encoded payload")

print("\n2. HANDSHAKE SEQUENCE:")
print("   Client → Server: HelloRequest (type 1)")
print("   Server → Client: HelloResponse (type 2)")
print("   Client → Server: ConnectRequest (type 3)")
print("   Server → Client: ConnectResponse (type 4)")

print("\n3. CONCRETE BYTES DIE JE ONTVANGT:")
hello_request = bytes([0x00, 0x09, 0x01, 0x0a, 0x06, 0x63, 0x6c, 0x69, 0x65, 0x6e, 0x74])
print(f"\n   HelloRequest van HA:")
print(f"   Hex: {' '.join(f'{b:02x}' for b in hello_request)}")
print(f"   - 0x00    = preamble")
print(f"   - 0x09    = length (9 bytes)")
print(f"   - 0x01    = type (HelloRequest)")
print(f"   - 0x0a    = protobuf field tag (field 1, wire type 2)")
print(f"   - 0x06    = string length (6)")
print(f"   - 'client' = client_info string")

print("\n4. WAT JE MOET TERUGSTUREN:")
print("\n   HelloResponse (minimaal):")
hello_response = bytes([0x00, 0x0c, 0x02, 0x0a, 0x07, 0x70, 0x69, 0x7a, 0x65, 0x72, 0x6f, 0x32])
print(f"   Hex: {' '.join(f'{b:02x}' for b in hello_response)}")
print(f"   - 0x00         = preamble")
print(f"   - 0x0c         = length (12 bytes)")
print(f"   - 0x02         = type (HelloResponse)")
print(f"   - 0x0a 0x07    = protobuf: field 1 (api_version), length 7")
print(f"   - 'pizero2'    = device name")
print(f"   Of eenvoudiger: {hello_response.hex()}")

print("\n   ConnectResponse (na ConnectRequest):")
connect_response = bytes([0x00, 0x02, 0x04, 0x08, 0x00])
print(f"   Hex: {' '.join(f'{b:02x}' for b in connect_response)}")
print(f"   - 0x00    = preamble")
print(f"   - 0x02    = length (2 bytes)")
print(f"   - 0x04    = type (ConnectResponse)")
print(f"   - 0x08 0x00 = invalid=false (connectie OK)")

print("\n5. VARINT ENCODING:")
print("   - Waarden < 128: gewoon de byte zelf")
print("   - Waarden >= 128: LSB eerst, bit 7 = continuation bit")
print("   Voorbeelden:")
print("   - 1 → 0x01")
print("   - 127 → 0x7f")
print("   - 128 → 0x80 0x01")
print("   - 300 → 0xac 0x02")

print("\n6. PYTHON HELPER CODE:")
print("""
def encode_varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)

def decode_varint(sock):
    result = 0
    shift = 0
    while True:
        byte = sock.recv(1)[0]
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result
        shift += 7

def send_message(sock, msg_type, data=b''):
    msg = bytes([0x00])  # preamble
    msg_data = encode_varint(msg_type) + data
    msg += encode_varint(len(msg_data))
    msg += msg_data
    sock.send(msg)
""")

print("\n7. MINIMALE SERVER IMPLEMENTATIE:")
print("""
import socket

def handle_client(conn):
    # Wacht op HelloRequest (type 1)
    preamble = conn.recv(1)  # 0x00
    length = decode_varint(conn)
    msg_type = decode_varint(conn)
    data = conn.recv(length - 1)  # rest van bericht
    
    if msg_type == 1:  # HelloRequest
        # Stuur HelloResponse
        send_message(conn, 2, b'\\x0a\\x08mydevice\\x12\\x051.0.0')
    
    # Wacht op ConnectRequest (type 3)
    preamble = conn.recv(1)
    length = decode_varint(conn)
    msg_type = decode_varint(conn)
    data = conn.recv(length - 1)
    
    if msg_type == 3:  # ConnectRequest
        # Stuur ConnectResponse (success)
        send_message(conn, 4, b'\\x08\\x00')

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind(('0.0.0.0', 6053))
sock.listen(1)
while True:
    conn, addr = sock.accept()
    handle_client(conn)
""")

print("\n" + "=" * 70)
print("DEBUG TIP: Gebruik 'python3 test_esphome_api.py' om te testen")
print("=" * 70)
