#!/usr/bin/env python3
"""
Claude Code Hardware Monitor — Bridge Daemon

Tails the active Claude Code session transcript at
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl and pushes
real token usage, model, cost, and status to an ESP32 over WebSocket.

Usage:
    python claude_monitor_bridge.py --ip 192.168.1.42
    python claude_monitor_bridge.py --ip 192.168.1.42 --project ~/Projects/foo
    python claude_monitor_bridge.py --ip 192.168.1.42 --demo
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterator

import websocket

PROJECTS_DIR = Path.home() / ".claude" / "projects"
POLL_INTERVAL = 0.2
IDLE_AFTER_DONE_SEC = 30.0
SESSION_SWITCH_GRACE_SEC = 2.0
SOCKET_PATH = "/tmp/claude_display.sock"
LOG_PATH = "/tmp/claude_display.log"


def log(tag: str, data: Any) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {tag} {json.dumps(data, default=str)[:800]}\n")
    except OSError:
        pass


# ── Pricing ─────────────────────────────────────────────────
# USD per token. Cache reads ≈ 10% of input rate; cache creation (5m) ≈ 125%.
PRICING = {
    "opus":   {"in": 15.0e-6, "out": 75.0e-6, "cache_read": 1.5e-6,  "cache_write": 18.75e-6},
    "sonnet": {"in":  3.0e-6, "out": 15.0e-6, "cache_read": 0.3e-6,  "cache_write":  3.75e-6},
    "haiku":  {"in":  1.0e-6, "out":  5.0e-6, "cache_read": 0.1e-6,  "cache_write":  1.25e-6},
}


def pretty_model(raw: str) -> str:
    if not raw or raw == "—":
        return raw
    name = raw
    if name.lower().startswith("claude-"):
        name = name[len("claude-"):]
    parts = name.split("-")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0]} {parts[1]}.{parts[2]}"
    return name.replace("-", " ")


def pricing_for(model: str) -> dict[str, float]:
    m = (model or "").lower()
    if "opus" in m:
        return PRICING["opus"]
    if "sonnet" in m:
        return PRICING["sonnet"]
    if "haiku" in m:
        return PRICING["haiku"]
    return PRICING["opus"]


# ── Session state ───────────────────────────────────────────
@dataclass
class SessionState:
    # Cumulative for cost (you pay per sub-call).
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Snapshot of the most recent assistant message — matches what the CLI shows.
    last_input_total: int = 0
    cost: float = 0.0
    model: str = "—"
    status: str = "idle"
    tool_name: str = ""
    session_path: Path | None = None
    last_event_ts: float = 0.0
    last_done_ts: float = 0.0
    todo_done: int = 0
    todo_total: int = 0
    todo_mtime: float = 0.0
    pending_done_event: bool = False
    pending_waiting_event: bool = False
    current_msg_out_added: int = 0

    def reset_counters(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cache_read = 0
        self.cache_write = 0
        self.last_input_total = 0
        self.cost = 0.0

    def recompute_cost(self) -> None:
        p = pricing_for(self.model)
        self.cost = (
            self.tokens_in * p["in"]
            + self.tokens_out * p["out"]
            + self.cache_read * p["cache_read"]
            + self.cache_write * p["cache_write"]
        )

    def display_payload(self) -> dict[str, Any]:
        d = {
            "status": self.status,
            "tokens_in": self.last_input_total,
            "tokens_out": self.tokens_out,
            "cost": round(self.cost, 4),
            "model": pretty_model(self.model),
            "todo_done": self.todo_done,
            "todo_total": self.todo_total,
        }
        if self.status == "tool_use" and self.tool_name:
            d["tool_name"] = self.tool_name
        return d


TODOS_DIR = Path.home() / ".claude" / "todos"


def todo_path_for(session_path: Path) -> Path:
    sid = session_path.stem
    return TODOS_DIR / f"{sid}-agent-{sid}.json"


def refresh_todos(state: "SessionState") -> bool:
    """Re-read the session's todo file if its mtime changed. Returns True if state changed."""
    if state.session_path is None:
        return False
    p = todo_path_for(state.session_path)
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        if state.todo_total or state.todo_done:
            state.todo_done = 0
            state.todo_total = 0
            state.todo_mtime = 0.0
            return True
        return False
    if mtime == state.todo_mtime:
        return False
    state.todo_mtime = mtime
    try:
        items = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(items, list):
        return False
    total = len(items)
    done = sum(
        1 for i in items
        if isinstance(i, dict) and i.get("status") == "completed"
    )
    if done == state.todo_done and total == state.todo_total:
        return False
    state.todo_done = done
    state.todo_total = total
    return True


# ── WebSocket client ────────────────────────────────────────
class WSClient:
    def __init__(self, ip: str, port: int):
        self.url = f"ws://{ip}:{port}/"
        self.ws: websocket.WebSocket | None = None
        self.connected = False
        self._send_lock = threading.Lock()
        self._on_message: Callable[[dict[str, Any]], None] | None = None
        self._listener_thread: threading.Thread | None = None
        self._last_state_payload: dict[str, Any] | None = None

    def connect(self) -> None:
        backoff = 1.0
        while True:
            try:
                ws = websocket.WebSocket()
                ws.connect(self.url, timeout=5)
                # The connect timeout becomes the default socket timeout for
                # subsequent ops in websocket-client. recv() must block
                # indefinitely or the listener thread will kill the connection
                # every 5s of idle traffic.
                ws.settimeout(None)
                self.ws = ws
                self.connected = True
                print(f"[bridge] connected to {self.url}")
                # Re-push the last known display state so the OLED catches up
                # after a reconnect (otherwise it sits on a stale "thinking").
                if self._last_state_payload is not None:
                    try:
                        ws.send(json.dumps(self._last_state_payload))
                    except Exception as e:
                        print(f"[bridge] resync send failed: {e}")
                return
            except Exception as e:
                print(f"[bridge] cannot reach {self.url}: {e}; retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def send(self, payload: dict[str, Any]) -> None:
        log("OUT", payload)
        data = json.dumps(payload)
        # Remember the last full display state so we can re-push after reconnects.
        # "event" payloads (one-shot triggers like session_done) shouldn't replay.
        if "event" not in payload:
            self._last_state_payload = payload
        with self._send_lock:
            for _ in range(2):
                if not self.connected:
                    self.connect()
                try:
                    assert self.ws is not None
                    self.ws.send(data)
                    return
                except Exception as e:
                    print(f"[bridge] send failed: {e}; reconnecting")
                    self.connected = False
                    try:
                        if self.ws:
                            self.ws.close()
                    except Exception:
                        pass

    def start_listener(self, on_message: Callable[[dict[str, Any]], None]) -> None:
        """Spawn a background thread that reads inbound WS frames and dispatches them."""
        self._on_message = on_message
        if self._listener_thread and self._listener_thread.is_alive():
            return
        t = threading.Thread(target=self._listen_loop, name="ws-listener", daemon=True)
        self._listener_thread = t
        t.start()

    def _listen_loop(self) -> None:
        while True:
            if not self.connected or self.ws is None:
                time.sleep(0.2)
                continue
            try:
                raw = self.ws.recv()
            except Exception as e:
                # Let the sender handle reconnect; we just idle.
                print(f"[bridge] recv failed: {e}")
                self.connected = False
                time.sleep(0.5)
                continue
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            log("IN:ws", ev)
            if self._on_message:
                try:
                    self._on_message(ev)
                except Exception as e:
                    print(f"[bridge] listener handler error: {e}")


# ── Focus dispatch ──────────────────────────────────────────
def resolve_session(slot: int, sessions: list[str]) -> str:
    """Map a 1-based slot to a tmux session name. Falls back to `agent-<slot>`."""
    if 1 <= slot <= len(sessions):
        return sessions[slot - 1]
    return f"agent-{slot}"


def focus_agent(slot: int, terminal_app: str, sessions: list[str]) -> None:
    """Switch the host's tmux client to the slot's session and raise the terminal window."""
    target = resolve_session(slot, sessions)
    print(f"[bridge] focus → tmux session '{target}' (in {terminal_app})")
    result = subprocess.run(
        ["tmux", "switch-client", "-t", target],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"[bridge] tmux switch-client failed: {err or 'unknown error'}")
        # Bail before raising the terminal — no point activating the window
        # if we couldn't actually switch the session inside it.
        return
    subprocess.run(
        ["osascript", "-e", f'tell application "{terminal_app}" to activate'],
        check=False,
    )


def make_inbound_handler(
    terminal_app: str, sessions: list[str]
) -> Callable[[dict[str, Any]], None]:
    def handle(ev: dict[str, Any]) -> None:
        if ev.get("event") == "focus":
            slot = ev.get("slot")
            if isinstance(slot, int) and slot >= 1:
                focus_agent(slot, terminal_app, sessions)
    return handle


def tmux_session_cwd(name: str) -> Path | None:
    """Return the cwd of the named tmux session's active pane, or None."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{pane_current_path}"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    path = r.stdout.strip()
    return Path(path).expanduser() if path else None


# ── Session-file discovery ──────────────────────────────────
def encoded_project_dir(project_path: Path) -> Path:
    abs_path = str(project_path.resolve())
    encoded = abs_path.replace("/", "-")
    return PROJECTS_DIR / encoded


def find_active_session(restrict_to: Path | None = None) -> Path | None:
    if restrict_to is not None:
        roots = [encoded_project_dir(restrict_to)]
    else:
        if not PROJECTS_DIR.exists():
            return None
        roots = [p for p in PROJECTS_DIR.iterdir() if p.is_dir()]

    candidates: list[tuple[float, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        for f in root.glob("*.jsonl"):
            try:
                candidates.append((f.stat().st_mtime, f))
            except FileNotFoundError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ── JSONL tailer ────────────────────────────────────────────
def tail_jsonl(path: Path, should_continue, on_tick=None) -> Iterator[dict[str, Any]]:
    """
    Yield parsed JSON objects appended to `path`.
    Stops when should_continue() returns False.
    Starts from end-of-file (we only care about new activity).
    `on_tick` is called once per idle poll cycle, useful for sidecar polling.
    """
    with path.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, os.SEEK_END)
        buffer = ""
        while should_continue():
            chunk = f.read()
            if not chunk:
                if on_tick is not None:
                    on_tick()
                time.sleep(POLL_INTERVAL)
                continue
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ── Line processing / state machine ─────────────────────────
def is_user_prompt(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "user":
        return False
    msg = obj.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
        return True
    return False


def is_tool_result(obj: dict[str, Any]) -> bool:
    if obj.get("type") != "user":
        return False
    msg = obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def is_assistant_message(obj: dict[str, Any]) -> bool:
    return obj.get("type") == "assistant" and isinstance(obj.get("message"), dict)


def assistant_has_tool_use(obj: dict[str, Any]) -> bool:
    content = obj["message"].get("content") or []
    if not isinstance(content, list):
        return False
    return any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)


def extract_tool_names(obj: dict[str, Any]) -> list[str]:
    """Return the names of all tool_use blocks in an assistant message."""
    content = obj["message"].get("content") or []
    if not isinstance(content, list):
        return []
    return [
        b["name"] for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name")
    ]


WAITING_TOOLS = {"ExitPlanMode", "AskUserQuestion"}


def apply_line(obj: dict[str, Any], state: SessionState) -> bool:
    """
    Returns True if the display should be pushed.
    """
    now = time.time()
    state.last_event_ts = now

    if is_user_prompt(obj):
        if state.status in ("idle", "done"):
            state.reset_counters()
        state.status = "thinking"
        return True

    if is_tool_result(obj):
        state.status = "thinking"
        return True

    if is_assistant_message(obj):
        msg = obj["message"]
        model = msg.get("model")
        if isinstance(model, str) and model:
            state.model = model
        usage = msg.get("usage") or {}
        msg_in    = int(usage.get("input_tokens", 0) or 0)
        msg_out   = int(usage.get("output_tokens", 0) or 0)
        msg_cr    = int(usage.get("cache_read_input_tokens", 0) or 0)
        msg_cw    = int(usage.get("cache_creation_input_tokens", 0) or 0)
        state.tokens_in   += msg_in
        state.tokens_out  += msg_out
        state.cache_read  += msg_cr
        state.cache_write += msg_cw
        state.last_input_total = msg_in + msg_cr + msg_cw
        state.recompute_cost()
        stop = msg.get("stop_reason", "")
        if assistant_has_tool_use(obj):
            state.status = "tool_use"
            names = extract_tool_names(obj)
            state.tool_name = names[-1] if names else ""
            if WAITING_TOOLS & set(names):
                state.pending_waiting_event = True
        elif stop == "end_turn":
            state.tool_name = ""
            state.status = "done"
            state.last_done_ts = now
            state.pending_done_event = True
        else:
            state.tool_name = ""
        return True

    return False


# ── Proxy / hook event handling ─────────────────────────────
def apply_proxy_event(ev: dict[str, Any], state: SessionState) -> bool:
    t = ev.get("type")
    now = time.time()
    state.last_event_ts = now

    if t == "message_start":
        if state.status in ("idle", "done"):
            state.reset_counters()
        model = ev.get("model")
        if isinstance(model, str) and model:
            state.model = model
        msg_in = int(ev.get("input_tokens") or 0)
        msg_cr = int(ev.get("cache_read") or 0)
        msg_cw = int(ev.get("cache_creation") or 0)
        state.tokens_in += msg_in
        state.cache_read += msg_cr
        state.cache_write += msg_cw
        state.last_input_total = msg_in + msg_cr + msg_cw
        state.current_msg_out_added = 0
        state.status = "thinking"
        state.tool_name = ""
        state.recompute_cost()
        return True

    if t == "tool_use_start":
        state.status = "tool_use"
        state.tool_name = ev.get("tool_name") or ""
        if state.tool_name in WAITING_TOOLS:
            state.pending_waiting_event = True
        return True

    if t == "message_delta":
        out_total = int(ev.get("output_tokens") or 0)
        delta = out_total - state.current_msg_out_added
        if delta > 0:
            state.tokens_out += delta
            state.current_msg_out_added = out_total
            state.recompute_cost()
        stop = ev.get("stop_reason")
        if stop == "end_turn":
            state.status = "done"
            state.tool_name = ""
            state.last_done_ts = now
            state.pending_done_event = True
        return True

    if t == "message_stop":
        return False

    return False


def apply_hook_event(ev: dict[str, Any], state: SessionState) -> bool:
    t = ev.get("type")
    now = time.time()
    state.last_event_ts = now

    if t == "SessionStart":
        state.reset_counters()
        state.status = "idle"
        state.tool_name = ""
        state.current_msg_out_added = 0
        return True

    if t == "UserPromptSubmit":
        if state.status in ("idle", "done"):
            state.reset_counters()
        state.status = "thinking"
        state.tool_name = ""
        return True

    if t == "Notification":
        state.pending_waiting_event = True
        return False

    if t == "Stop":
        state.status = "done"
        state.tool_name = ""
        state.last_done_ts = now
        state.pending_done_event = True
        return True

    return False


def run_proxy(ws_client: WSClient) -> None:
    """Listen on the Unix datagram socket and drive the display from proxy + hook events.

    Single-slot only (slot 1) — proxy mode is the legacy hook-driven path.
    Multi-slot belongs in the main `run()` JSONL tailer.
    """
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    sock.settimeout(1.0)
    print(f"[bridge] proxy mode: listening on {SOCKET_PATH}")

    state = SessionState()
    _send_slot(ws_client, 1, {
        "session": "start",
        "status": "idle",
        "model": pretty_model(state.model),
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
        "todo_done": 0,
        "todo_total": 0,
    })

    while True:
        try:
            data, _ = sock.recvfrom(65536)
        except socket.timeout:
            if (
                state.status == "done"
                and state.last_done_ts > 0
                and time.time() - state.last_done_ts > IDLE_AFTER_DONE_SEC
            ):
                state.status = "idle"
                state.last_done_ts = 0
                _send_slot(ws_client, 1, state.display_payload())
            continue
        except KeyboardInterrupt:
            return

        try:
            ev = json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            continue

        src = ev.get("src")
        log(f"IN:{src}", ev)
        changed = False
        if src == "proxy":
            changed = apply_proxy_event(ev, state)
        elif src == "hook":
            changed = apply_hook_event(ev, state)

        if changed:
            _send_slot(ws_client, 1, state.display_payload())

        if state.pending_done_event:
            _send_slot(ws_client, 1, {"event": "session_done"})
            state.pending_done_event = False

        if state.pending_waiting_event:
            _send_slot(ws_client, 1, {"event": "waiting_for_user"})
            state.pending_waiting_event = False


# ── Main loop ───────────────────────────────────────────────
def _send_slot(ws_client: WSClient, slot: int, payload: dict[str, Any]) -> None:
    """Wrap a payload with its slot tag and send it."""
    out = dict(payload)
    out["slot"] = slot
    ws_client.send(out)


def slot_worker(
    slot: int,
    tmux_session: str | None,
    restrict_to: Path | None,
    ws_client: WSClient,
    stop: threading.Event,
) -> None:
    """
    Drive one tile from one Claude session.

    If `tmux_session` is provided, we rebind the slot to whatever JSONL is
    most recently active in *that tmux pane's cwd*, refreshed periodically.
    Otherwise we follow `restrict_to` (or the most-recent session globally,
    if that's None) — the legacy single-slot path.
    """
    state = SessionState()
    label = f"slot {slot} ({tmux_session or 'global'})"
    _send_slot(ws_client, slot, {
        "session": "start", "status": "idle", "model": pretty_model(state.model),
        "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
        "todo_done": 0, "todo_total": 0,
    })

    def project_for_slot() -> Path | None:
        if tmux_session is not None:
            return tmux_session_cwd(tmux_session)
        return restrict_to

    while not stop.is_set():
        project = project_for_slot()
        active = find_active_session(project) if project else find_active_session(None)
        if active is None:
            time.sleep(1.0)
            continue

        if active != state.session_path:
            print(f"[bridge] {label} → following {active}")
            state.session_path = active
            state.reset_counters()
            state.status = "idle"
            state.todo_done = 0
            state.todo_total = 0
            state.todo_mtime = 0.0
            refresh_todos(state)
            _send_slot(ws_client, slot, {
                "session": "start", "status": "idle", "model": pretty_model(state.model),
                "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
                "todo_done": state.todo_done, "todo_total": state.todo_total,
            })

        last_check = time.time()

        def should_continue() -> bool:
            nonlocal last_check
            if stop.is_set():
                return False
            now = time.time()
            if now - last_check < SESSION_SWITCH_GRACE_SEC:
                return True
            last_check = now
            proj = project_for_slot()
            current = find_active_session(proj) if proj else find_active_session(None)
            return current == state.session_path

        def on_tick() -> None:
            if refresh_todos(state):
                _send_slot(ws_client, slot, state.display_payload())
            if (
                state.status == "done"
                and state.last_done_ts > 0
                and time.time() - state.last_done_ts > IDLE_AFTER_DONE_SEC
            ):
                state.status = "idle"
                state.last_done_ts = 0
                _send_slot(ws_client, slot, state.display_payload())

        try:
            for obj in tail_jsonl(active, should_continue, on_tick=on_tick):
                log("IN:jsonl", {"slot": slot, "type": obj.get("type"),
                                 "stop": (obj.get("message") or {}).get("stop_reason")})
                changed_payload = apply_line(obj, state)
                changed_todos = refresh_todos(state)
                if changed_payload or changed_todos:
                    _send_slot(ws_client, slot, state.display_payload())

                if state.pending_done_event:
                    _send_slot(ws_client, slot, {"event": "session_done"})
                    state.pending_done_event = False

                if state.pending_waiting_event:
                    _send_slot(ws_client, slot, {"event": "waiting_for_user"})
                    state.pending_waiting_event = False

                if (
                    state.status == "done"
                    and state.last_done_ts > 0
                    and time.time() - state.last_done_ts > IDLE_AFTER_DONE_SEC
                ):
                    state.status = "idle"
                    _send_slot(ws_client, slot, state.display_payload())
                    state.last_done_ts = 0
        except FileNotFoundError:
            pass


def run(ws_client: WSClient, restrict_to: Path | None, sessions: list[str]) -> None:
    """Spawn one tailer thread per slot. Without `sessions`, runs slot 1 only."""
    stop = threading.Event()
    threads: list[threading.Thread] = []

    if sessions:
        for i, name in enumerate(sessions, start=1):
            t = threading.Thread(
                target=slot_worker,
                args=(i, name, None, ws_client, stop),
                name=f"slot-{i}",
                daemon=True,
            )
            t.start()
            threads.append(t)
    else:
        # Legacy single-slot mode — most recent session globally (or in --project).
        t = threading.Thread(
            target=slot_worker,
            args=(1, None, restrict_to, ws_client, stop),
            name="slot-1",
            daemon=True,
        )
        t.start()
        threads.append(t)

    try:
        while True:
            time.sleep(1.0)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2.0)


# ── Demo mode ───────────────────────────────────────────────
def run_demo(ws_client: WSClient, num_slots: int = 2) -> None:
    """Drive `num_slots` synthetic agents concurrently so multi-tile rendering
    is observable without spawning real Claude sessions."""
    print(f"[bridge] demo mode ({num_slots} slots)")

    states: list[SessionState] = []
    for slot in range(1, num_slots + 1):
        s = SessionState(model="claude-opus-4-6" if slot % 2 else "claude-sonnet-4-6")
        s.todo_total = 4 + slot
        s.todo_done = 0
        states.append(s)
        _send_slot(ws_client, slot, {
            "session": "start", "status": "idle", "model": pretty_model(s.model),
            "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
            "todo_done": 0, "todo_total": s.todo_total,
        })

    time.sleep(1.0)

    demo_tools = ["Read", "Grep", "Edit", "Bash", "Agent", "Write"]

    # Each slot ramps tokens at a slightly different rate so the tiles diverge visibly.
    for tick in range(14):
        for slot, s in enumerate(states, start=1):
            phase = (tick + slot) % 14
            if phase < 5:
                s.status = "thinking"
                s.tool_name = ""
            elif phase < 11:
                s.status = "tool_use"
                s.tool_name = demo_tools[phase % len(demo_tools)]
            else:
                s.status = "done"
                s.tool_name = ""
            s.tokens_in  += 320 + 40 * slot
            s.tokens_out += 140 + 30 * slot
            s.last_input_total = s.tokens_in
            s.todo_done = min(s.todo_total, tick // 3 + slot)
            s.recompute_cost()
            _send_slot(ws_client, slot, s.display_payload())
            if s.status == "done" and phase == 11:
                _send_slot(ws_client, slot, {"event": "session_done"})
        time.sleep(0.5)

    for slot, s in enumerate(states, start=1):
        s.status = "idle"
        _send_slot(ws_client, slot, s.display_payload())
    print("[bridge] demo complete")


# ── Entry ──────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="Claude Code → ESP32 bridge")
    ap.add_argument("--ip", required=True, help="ESP32 IP address")
    ap.add_argument("--port", type=int, default=81, help="ESP32 WebSocket port (default 81)")
    ap.add_argument("--project", type=Path, default=None,
                    help="Restrict to one project directory (defaults to all projects)")
    ap.add_argument("--demo", action="store_true", help="Send synthetic data and exit")
    ap.add_argument("--proxy", action="store_true",
                    help=f"Listen on {SOCKET_PATH} for events from claude_proxy.py + hooks")
    ap.add_argument("--terminal", default="Ghostty",
                    help="macOS terminal app to raise on focus events (default: Ghostty)")
    ap.add_argument("--sessions", default="",
                    help="Comma-separated tmux session names mapped to slots 1..N "
                         "(e.g. --sessions foo,bar maps K1→foo, K2→bar). "
                         "Slots beyond the list fall back to 'agent-<slot>'.")
    args = ap.parse_args()

    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    if sessions:
        mapping = ", ".join(f"K{i+1}→{name}" for i, name in enumerate(sessions))
        print(f"[bridge] slot mapping: {mapping}")

    ws_client = WSClient(args.ip, args.port)
    ws_client.connect()
    ws_client.start_listener(make_inbound_handler(args.terminal, sessions))

    if args.demo:
        slots = max(2, len(sessions)) if sessions else 2
        run_demo(ws_client, num_slots=slots)
        return

    if args.proxy:
        try:
            run_proxy(ws_client)
        except KeyboardInterrupt:
            print("\n[bridge] stopped")
        return

    try:
        run(ws_client, args.project, sessions)
    except KeyboardInterrupt:
        print("\n[bridge] stopped")


if __name__ == "__main__":
    main()
