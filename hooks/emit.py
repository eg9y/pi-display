#!/usr/bin/env python3
"""Claude Code hook → claude-display bridge forwarder.

Registered in ~/.claude/settings.json for SessionStart, UserPromptSubmit,
Notification, Stop. Reads the hook event JSON from stdin, tags it with the
hook name from argv, attaches the controlling TTY of the parent Claude
process so the bridge can map tmux-pane → session_id, fires it at the
bridge as a Unix datagram, exits 0.
"""

import json
import os
import socket
import subprocess
import sys

SOCKET_PATH = "/tmp/claude_display.sock"


def ancestor_tty() -> str | None:
    """Resolve the controlling TTY of the parent Claude process.

    Returns the bare device name (e.g. `ttys006`) so it matches the
    bridge's `tmux #{pane_tty}` lookup after `/dev/` is stripped.

    Strategy, fastest-first — every hook event runs this synchronously so
    Claude is blocked until we return:

    1. `os.ttyname(fd)` on stderr/stdout. Claude inherits these to the
       terminal in interactive mode, so they share the pane's tty. One
       syscall, no subprocess. This is the common case and costs ~0ms.

    2. Single `ps -A` snapshot, walked in-Python. The previous version
       forked one `ps -p <pid>` per ancestor (up to 8) which added
       hundreds of ms to *every* hook; one snapshot is 5-30ms total.
    """
    for fd in (2, 1, 0):
        try:
            name = os.ttyname(fd)
        except OSError:
            continue
        if name:
            return name.rsplit("/", 1)[-1]

    try:
        r = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,tty="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None

    parents: dict[int, int] = {}
    ttys: dict[int, str] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        parents[pid] = ppid
        ttys[pid] = parts[2] if len(parts) >= 3 else ""

    pid = os.getppid()
    for _ in range(16):
        if pid <= 1:
            return None
        tty = ttys.get(pid, "")
        if tty and tty not in ("?", "??"):
            return tty
        next_pid = parents.get(pid)
        if next_pid is None or next_pid == pid:
            return None
        pid = next_pid
    return None


def main() -> None:
    hook_name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    payload["src"] = "hook"
    payload["type"] = hook_name
    tty = ancestor_tty()
    if tty:
        payload["tty"] = tty
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.sendto(json.dumps(payload).encode("utf-8"), SOCKET_PATH)
    except OSError:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
