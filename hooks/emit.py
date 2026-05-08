#!/usr/bin/env python3
"""Claude Code hook → claude-display bridge forwarder.

Registered in ~/.claude/settings.json for SessionStart, UserPromptSubmit,
Notification, Stop. Reads the hook event JSON from stdin, tags it with the
hook name from argv, fires it at the bridge as a Unix datagram, exits 0.
"""

import json
import socket
import sys

SOCKET_PATH = "/tmp/claude_display.sock"


def main() -> None:
    hook_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    payload["src"] = "hook"
    payload["type"] = hook_name
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.sendto(json.dumps(payload).encode("utf-8"), SOCKET_PATH)
    except OSError:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
