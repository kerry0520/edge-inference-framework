"""Bounded TCP discovery for the previously authorized phone address only.

This checks connection acceptance, not ADB identity. Verify open ports with the
already paired adb client, then verify the phone model before executing tests.
No application payload, pairing reset, or other hosts are used here.
"""
import argparse
import collections
import concurrent.futures
import datetime
import json
import socket
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", choices=["192.168.5.30"], required=True)
    parser.add_argument("--first", type=int, default=30000)
    parser.add_argument("--last", type=int, default=49999)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 30000 <= args.first <= args.last <= 65535:
        parser.error("Only the authorized dynamic range is permitted")
    if args.output.exists():
        parser.error("Output already exists; preserve previous observations")

    def check(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return port, sock.connect_ex((args.ip, port))

    started = time.monotonic()
    opened = []
    codes = collections.Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=128) as executor:
        for port, code in executor.map(check, range(args.first, args.last + 1)):
            codes[code] += 1
            if code == 0:
                opened.append(port)
                print(f"TCP accepts: {args.ip}:{port}", flush=True)
    record = dict(
        checked_at=datetime.datetime.now().astimezone().isoformat(),
        host=args.ip, first=args.first, last=args.last,
        concurrent_connections=128, timeout_seconds=0.5,
        ports_attempted=args.last - args.first + 1,
        open_ports=opened, elapsed_seconds=round(time.monotonic() - started, 3),
        socket_connect_ex_result_counts={str(code): count for code, count in sorted(codes.items())},
        nonzero_result_interpretation="No TCP acceptance; distinguish timeout/refusal via raw OS result code, not closed-port claims",
        scope="single previously authorized phone IP; TCP acceptance only",
        adb_identity_verified=False, application_payload_sent=False,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
