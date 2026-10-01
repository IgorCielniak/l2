#!/usr/bin/env python3
"""Print L2 compiler daemon events from its Unix-socket event stream."""

from __future__ import annotations

import argparse
import json
import socket


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="build/.l2_daemon.sock")
    parser.add_argument("--pattern", default="*")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(args.socket)
        conn.sendall((json.dumps({"cmd": "subscribe_events", "pattern": args.pattern, "replay": args.replay}) + "\n").encode())
        with conn.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                event = row.get("event", {})
                print(f"{event.get('event', '?')}: {json.dumps(event.get('payload', {}), sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
