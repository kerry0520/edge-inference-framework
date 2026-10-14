"""Ask only for Android wireless-ADB connection services on the Wi-Fi link."""
import argparse
import datetime
import json
import socket
import struct
import time
from pathlib import Path


def decode_name(data, offset):
    parts, next_offset, seen = [], None, set()
    while True:
        if offset in seen or offset >= len(data):
            raise ValueError("Malformed DNS name")
        seen.add(offset)
        size = data[offset]
        if size & 0xC0 == 0xC0:
            if next_offset is None:
                next_offset = offset + 2
            offset = ((size & 0x3F) << 8) | data[offset + 1]
        elif size == 0:
            return ".".join(parts), next_offset or offset + 1
        else:
            offset += 1
            parts.append(data[offset:offset + size].decode("utf-8", "replace"))
            offset += size


def records(data):
    _, _, questions, answers, authorities, additional = struct.unpack("!6H", data[:12])
    offset = 12
    for _ in range(questions):
        _, offset = decode_name(data, offset)
        offset += 4
    found = []
    for _ in range(answers + authorities + additional):
        name, offset = decode_name(data, offset)
        kind, _, ttl, length = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        value = None
        if kind == 12:
            value, _ = decode_name(data, offset)
        elif kind == 33:
            priority, weight, port = struct.unpack("!HHH", data[offset:offset + 6])
            host, _ = decode_name(data, offset + 6)
            value = dict(priority=priority, weight=weight, port=port, host=host)
        elif kind == 1 and length == 4:
            value = socket.inet_ntoa(data[offset:offset + 4])
        if value is not None:
            found.append(dict(name=name, type=kind, ttl=ttl, value=value))
        offset += length
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface-ip", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Preserve prior results")
    name = "_adb-tls-connect._tcp.local"
    encoded = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".")) + b"\0"
    query = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + encoded + struct.pack("!HH", 12, 0x8001)
    responses, errors = [], []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((args.interface_ip, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(args.interface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(1)
        for _ in range(3):
            sock.sendto(query, ("224.0.0.251", 5353))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    data, peer = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    responses.append(dict(peer=list(peer), records=records(data)))
                except (ValueError, IndexError, struct.error) as error:
                    errors.append(str(error))
    result = dict(checked_at=datetime.datetime.now().astimezone().isoformat(),
                  question=name, interface_ipv4=args.interface_ip, queries=3,
                  unicast_response_requested=True, responses=responses,
                  parse_errors=errors, tcp_port_scan=False, adb_identity_verified=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
