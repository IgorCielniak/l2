#!/usr/bin/env python3
"""Print L2 compiler daemon events from its Unix-socket event stream.

The socket defaults to the daemon of this checkout (the one main.py uses,
`--daemon-name` / L2_DAEMON_NAME select another instance); `main.py
--daemon-events --follow` prints the same stream as JSON lines.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default=None, help="daemon socket (default: this checkout's daemon)")
    parser.add_argument("--daemon-name", default=None)
    parser.add_argument("--pattern", default="*")
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    import l2_client as client
    socket_path = args.socket or client._instance(args.daemon_name).socket
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        client._connect_unix(conn, socket_path)
        conn.sendall((json.dumps({"cmd": "subscribe_events", "pattern": args.pattern, "replay": args.replay}) + "\n").encode())
        with conn.makefile("r", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                event = row.get("event", {})
                print(f"{event.get('event', '?')}: {json.dumps(event.get('payload', {}), sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
