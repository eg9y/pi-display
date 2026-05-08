#!/usr/bin/env python3
"""
Pi Agent Hardware Monitor — Bridge Daemon

Tails active Pi agent session transcripts at
~/.pi/agent/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl and pushes
real token usage, model, cost, and status to an ESP32 over WebSocket.

Pi sessions are documented, public JSONL — no proxy, no hooks, no hacks.

Usage:
    python pi_agent_bridge.py --ip 192.168.1.42
    python pi_agent_bridge.py --ip 192.168.1.42 --project ~/Projects/foo
    python pi_agent_bridge.py --ip 192.168.1.42 --sessions agent-1,agent-2
    python pi_agent_bridge.py --ip 192.168.1.42 --demo
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import websocket

PI_SESSIONS_DIR: Path = Path.home() / ".pi" / "agent" / "sessions"
POLL_INTERVAL: float = 0.2
IDLE_AFTER_DONE_SEC: float = 30.0
SESSION_SWITCH_GRACE_SEC: float = 2.0
LOG_PATH: str = "/tmp/pi_agent_display.log"


def log(tag: str, data: Any) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {tag} {json.dumps(data, default=str)[:800]}\n")
    except OSError:
        pass


def pretty_model(raw: str) -> str:
    if not raw or raw == "—":
        return raw
    name = raw
    lower = name.lower()

    # moonshotai/kimi-k2.6 → Kimi K2.6
    if "moonshotai/" in lower or "kimi" in lower:
        model_part = name.split("/")[-1] if "/" in name else name
        return model_part.replace("kimi-", "Kimi ").replace("kimi", "Kimi")

    # claude-sonnet-4-5 → Sonnet 4.5
    if lower.startswith("claude-"):
        name = name[len("claude-"):]
        parts = name.split("-")
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
            return f"{parts[0].title()} {parts[1]}.{parts[2]}"
        if len(parts) >= 2 and parts[1].isdigit():
            return f"{parts[0].title()} {parts[1]}"
        return name.replace("-", " ").title()

    # gpt-4o → GPT-4o
    if lower.startswith("gpt-"):
        return name.upper()

    # Generic: strip provider prefix, title-case
    if "/" in name:
        name = name.split("/")[-1]
    return name.replace("-", " ").title()


# ── Session state ───────────────────────────────────────────
@dataclass
class SessionState:
    tokens_in: int = 0          # Input from latest assistant msg
    tokens_out: int = 0         # Cumulative output
    cost: float = 0.0           # Cumulative cost
    model: str = "—"
    status: str = "idle"
    tool_name: str = ""
    session_path: Path | None = None
    last_event_ts: float = 0.0
    last_done_ts: float = 0.0
    pending_done_event: bool = False

    def reset_counters(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = 0.0

    def display_payload(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "status": self.status,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost": round(self.cost, 6),
            "model": pretty_model(self.model),
        }
        if self.status == "tool_use" and self.tool_name:
            d["tool_name"] = self.tool_name
        return d


# ── Session-file discovery ──────────────────────────────────
def encode_cwd(cwd: Path) -> str:
    abs_path = os.path.abspath(str(cwd.expanduser()))
    inner = abs_path.lstrip("/").replace("/", "-")
    return f"--{inner}--"


def find_active_session(restrict_to: Path | None = None) -> Path | None:
    if restrict_to is not None:
        roots = [PI_SESSIONS_DIR / encode_cwd(restrict_to)]
    else:
        if not PI_SESSIONS_DIR.exists():
            return None
        roots = [
            p for p in PI_SESSIONS_DIR.iterdir()
            if p.is_dir() and p.name.startswith("--")
        ]

    candidates: list[tuple[float, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        for f in root.glob("*.jsonl"):
            try:
                candidates.append((f.stat().st_mtime, f))
            except (FileNotFoundError, PermissionError, OSError):
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


# ── JSONL tailer ────────────────────────────────────────────
def tail_jsonl(path: Path, should_continue: Callable[[], bool],
               on_tick: Callable[[], None] | None = None) -> Iterator[dict[str, Any]]:
    """
    Yield parsed JSON objects appended to `path`.
    Starts from end-of-file (we only care about new activity).
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


# ── Entry type detection ────────────────────────────────────
def is_user_message(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "message":
        return False
    msg = entry.get("message") or {}
    return msg.get("role") == "user"


def is_assistant_message(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "message":
        return False
    msg = entry.get("message") or {}
    return msg.get("role") == "assistant"


def is_tool_result(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "message":
        return False
    msg = entry.get("message") or {}
    return msg.get("role") == "toolResult"


def extract_tool_names(msg: dict[str, Any]) -> list[str]:
    content = msg.get("content") or []
    if not isinstance(content, list):
        return []
    return [
        b["name"] for b in content
        if isinstance(b, dict) and b.get("type") == "toolCall" and b.get("name")
    ]


def is_model_change(entry: dict[str, Any]) -> bool:
    return entry.get("type") == "model_change"


# ── State machine ───────────────────────────────────────────
def apply_line(entry: dict[str, Any], state: SessionState) -> bool:
    """
    Parse a single Pi session entry and update state.
    Returns True if the display should be pushed.
    """
    now = time.time()
    state.last_event_ts = now

    # Track model changes quietly
    if is_model_change(entry):
        model_id = entry.get("modelId")
        if isinstance(model_id, str) and model_id:
            state.model = model_id
        return False

    # User message = new prompt → reset counters if coming from idle/done
    if is_user_message(entry):
        if state.status in ("idle", "done"):
            state.reset_counters()
        state.status = "thinking"
        state.tool_name = ""
        return True

    # Tool result → model will respond next → thinking
    if is_tool_result(entry):
        state.status = "thinking"
        state.tool_name = ""
        return True

    # Assistant message → update stats and derive status from stopReason
    if is_assistant_message(entry):
        msg = entry.get("message") or {}

        model = msg.get("model")
        if isinstance(model, str) and model:
            state.model = model

        usage = msg.get("usage") or {}
        msg_in = int(usage.get("input", 0) or 0)
        msg_out = int(usage.get("output", 0) or 0)
        msg_cr = int(usage.get("cacheRead", 0) or 0)
        msg_cw = int(usage.get("cacheWrite", 0) or 0)

        # "IN:" on the OLED = total input side of the latest assistant msg
        state.tokens_in = msg_in + msg_cr + msg_cw
        state.tokens_out += msg_out

        cost_info = usage.get("cost") or {}
        state.cost += float(cost_info.get("total", 0.0) or 0.0)

        stop_reason = msg.get("stopReason", "")
        tool_names = extract_tool_names(msg)

        if tool_names or stop_reason == "toolUse":
            state.status = "tool_use"
            state.tool_name = tool_names[-1] if tool_names else ""
        elif stop_reason in ("stop", "length"):
            state.tool_name = ""
            state.status = "done"
            state.last_done_ts = now
            state.pending_done_event = True
        elif stop_reason == "error":
            state.tool_name = ""
            state.status = "error"
        elif stop_reason == "aborted":
            state.tool_name = ""
            state.status = "done"
            state.last_done_ts = now
            state.pending_done_event = True
        else:
            state.tool_name = ""

        return True

    return False


# ── WebSocket client ────────────────────────────────────────
class WSClient:
    def __init__(self, ip: str, port: int) -> None:
        self.url = f"ws://{ip}:{port}/"
        self.ws: websocket.WebSocket | None = None
        self.connected = False
        self._send_lock = threading.Lock()
        self._on_message: Callable[[dict[str, Any]], None] | None = None
        self._listener_thread: threading.Thread | None = None
        # Cache per slot so reconnects replay every tile
        self._last_state_by_slot: dict[int, dict[str, Any]] = {}

    def connect(self) -> None:
        backoff = 1.0
        while True:
            try:
                ws = websocket.WebSocket()
                ws.connect(self.url, timeout=5)
                ws.settimeout(None)
                self.ws = ws
                self.connected = True
                print(f"[pi-bridge] connected to {self.url}")
                for slot in sorted(self._last_state_by_slot):
                    try:
                        ws.send(json.dumps(self._last_state_by_slot[slot]))
                    except Exception as e:
                        print(f"[pi-bridge] resync failed (slot {slot}): {e}")
                        break
                return
            except Exception as e:
                print(
                    f"[pi-bridge] cannot reach {self.url}: {e}; "
                    f"retrying in {backoff:.0f}s"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def send(self, payload: dict[str, Any]) -> None:
        log("OUT", payload)
        data = json.dumps(payload)
        if "event" not in payload:
            slot_key = payload.get("slot", 0)
            if isinstance(slot_key, int):
                self._last_state_by_slot[slot_key] = payload
        with self._send_lock:
            for _attempt in range(2):
                if not self.connected:
                    self.connect()
                try:
                    assert self.ws is not None
                    self.ws.send(data)
                    return
                except Exception as e:
                    print(f"[pi-bridge] send failed: {e}; reconnecting")
                    self.connected = False
                    try:
                        if self.ws:
                            self.ws.close()
                    except Exception:
                        pass

    def start_listener(self, on_message: Callable[[dict[str, Any]], None]) -> None:
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
                print(f"[pi-bridge] recv failed: {e}")
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
                except Exception as exc:
                    print(f"[pi-bridge] listener error: {exc}")


# ── Focus dispatch ──────────────────────────────────────────
def resolve_session(slot: int, sessions: list[str]) -> str:
    if 1 <= slot <= len(sessions):
        return sessions[slot - 1]
    return f"agent-{slot}"


def focus_agent(slot: int, terminal_app: str, sessions: list[str]) -> None:
    target = resolve_session(slot, sessions)
    print(f"[pi-bridge] focus → tmux session '{target}' (in {terminal_app})")
    result = subprocess.run(
        ["tmux", "switch-client", "-t", target],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"[pi-bridge] tmux switch-client failed: {err or 'unknown error'}")
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


# ── Slot worker ─────────────────────────────────────────────
def _send_slot(ws_client: WSClient, slot: int, payload: dict[str, Any]) -> None:
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
    state = SessionState()
    label = f"slot {slot} ({tmux_session or 'global'})"
    _send_slot(ws_client, slot, {
        "session": "start",
        "status": "idle",
        "model": pretty_model(state.model),
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
    })

    def project_for_slot() -> Path | None:
        if tmux_session is not None:
            return tmux_session_cwd(tmux_session)
        return restrict_to

    def resolve_transcript() -> Path | None:
        project = project_for_slot()
        return find_active_session(project) if project else find_active_session(None)

    while not stop.is_set():
        active = resolve_transcript()
        if active is None:
            time.sleep(1.0)
            continue

        if active != state.session_path:
            print(f"[pi-bridge] {label} → following {active}")
            state.session_path = active
            state.reset_counters()
            state.status = "idle"
            _send_slot(ws_client, slot, {
                "session": "start",
                "status": "idle",
                "model": pretty_model(state.model),
                "tokens_in": 0,
                "tokens_out": 0,
                "cost": 0.0,
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
            return resolve_transcript() == state.session_path

        def on_tick() -> None:
            if (
                state.status == "done"
                and state.last_done_ts > 0
                and time.time() - state.last_done_ts > IDLE_AFTER_DONE_SEC
            ):
                state.status = "idle"
                state.last_done_ts = 0
                _send_slot(ws_client, slot, state.display_payload())

        try:
            for entry in tail_jsonl(active, should_continue, on_tick=on_tick):
                log("IN:jsonl", {
                    "slot": slot,
                    "type": entry.get("type"),
                    "role": (entry.get("message") or {}).get("role"),
                })
                changed = apply_line(entry, state)
                if changed:
                    _send_slot(ws_client, slot, state.display_payload())

                if state.pending_done_event:
                    _send_slot(ws_client, slot, {"event": "session_done"})
                    state.pending_done_event = False

                if (
                    state.status == "done"
                    and state.last_done_ts > 0
                    and time.time() - state.last_done_ts > IDLE_AFTER_DONE_SEC
                ):
                    state.status = "idle"
                    _send_slot(ws_client, slot, state.display_payload())
                    state.last_done_ts = 0
        except (FileNotFoundError, PermissionError, OSError):
            pass


# ── Orchestrator ────────────────────────────────────────────
def run(
    ws_client: WSClient,
    restrict_to: Path | None,
    sessions: list[str],
) -> None:
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
    print(f"[pi-bridge] demo mode ({num_slots} slots)")

    demo_tools = ["Read", "Grep", "Edit", "Bash", "Write"]
    states: list[SessionState] = []

    for slot in range(1, num_slots + 1):
        s = SessionState(model="claude-sonnet-4-5" if slot % 2 else "gpt-4o")
        states.append(s)
        _send_slot(ws_client, slot, {
            "session": "start",
            "status": "idle",
            "model": pretty_model(s.model),
            "tokens_in": 0,
            "tokens_out": 0,
            "cost": 0.0,
        })

    time.sleep(1.0)

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
            s.tokens_in = 3200 + 400 * slot + tick * 150
            s.tokens_out += 140 + 30 * slot
            s.cost += 0.0001 * (slot + tick)
            _send_slot(ws_client, slot, s.display_payload())
            if s.status == "done" and phase == 11:
                _send_slot(ws_client, slot, {"event": "session_done"})
        time.sleep(0.5)

    for slot, s in enumerate(states, start=1):
        s.status = "idle"
        _send_slot(ws_client, slot, s.display_payload())
    print("[pi-bridge] demo complete")


# ── Entry ──────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser(description="Pi Agent → ESP32 bridge")
    ap.add_argument("--ip", required=True, help="ESP32 IP address")
    ap.add_argument("--port", type=int, default=81, help="ESP32 WebSocket port")
    ap.add_argument("--project", type=Path, default=None,
                    help="Restrict to one project directory")
    ap.add_argument("--demo", action="store_true", help="Send synthetic data and exit")
    ap.add_argument("--terminal", default="Ghostty",
                    help="macOS terminal app to raise on focus events")
    ap.add_argument("--sessions", default="",
                    help="Comma-separated tmux session names mapped to slots 1..N")
    args = ap.parse_args()

    sessions = [s.strip() for s in args.sessions.split(",") if s.strip()]
    if sessions:
        mapping = ", ".join(f"K{i+1}→{name}" for i, name in enumerate(sessions))
        print(f"[pi-bridge] slot mapping: {mapping}")

    ws_client = WSClient(args.ip, args.port)
    ws_client.connect()
    ws_client.start_listener(make_inbound_handler(args.terminal, sessions))

    if args.demo:
        slots = max(2, len(sessions)) if sessions else 2
        run_demo(ws_client, num_slots=slots)
        return

    try:
        run(ws_client, args.project, sessions)
    except KeyboardInterrupt:
        print("\n[pi-bridge] stopped")


if __name__ == "__main__":
    main()
