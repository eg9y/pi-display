#!/usr/bin/env python3
"""
Claude Code → claude-display reverse proxy.

Run this, point `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` at it, and every
outbound Claude Code API call is forwarded verbatim to api.anthropic.com while
SSE events from /v1/messages are parsed and fired as Unix datagrams to the
bridge at /tmp/claude_display.sock.

No request bodies are ever logged to disk. Binds to loopback only.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from typing import Any

import aiohttp
from aiohttp import web

UPSTREAM = "https://api.anthropic.com"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8787
SOCKET_PATH = "/tmp/claude_display.sock"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
    "host",
}


_event_sock: socket.socket | None = None


def emit(payload: dict[str, Any]) -> None:
    global _event_sock
    try:
        if _event_sock is None:
            _event_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        _event_sock.sendto(json.dumps(payload).encode("utf-8"), SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError, OSError):
        pass


class SSEParser:
    """Feed raw SSE bytes; calls on_event(event_name, parsed_json) per block."""

    def __init__(self, on_event):
        self.on_event = on_event
        self.buffer = b""
        self.event_name: str | None = None
        self.data_parts: list[bytes] = []

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            line = raw.rstrip(b"\r")
            if not line:
                self._flush()
                continue
            if line.startswith(b":"):
                continue
            if line.startswith(b"event:"):
                self.event_name = line[6:].strip().decode("utf-8", "replace")
            elif line.startswith(b"data:"):
                self.data_parts.append(line[5:].lstrip())

    def _flush(self) -> None:
        if self.event_name and self.data_parts:
            raw = b"".join(self.data_parts)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                try:
                    self.on_event(self.event_name, parsed)
                except Exception as e:
                    print(f"[proxy] sse handler error: {e}", file=sys.stderr)
        self.event_name = None
        self.data_parts = []


def handle_sse(event_name: str, data: dict[str, Any]) -> None:
    if event_name == "message_start":
        msg = data.get("message") or {}
        usage = msg.get("usage") or {}
        emit({
            "src": "proxy",
            "type": "message_start",
            "model": msg.get("model"),
            "input_tokens": usage.get("input_tokens") or 0,
            "cache_read": usage.get("cache_read_input_tokens") or 0,
            "cache_creation": usage.get("cache_creation_input_tokens") or 0,
        })
    elif event_name == "content_block_start":
        block = data.get("content_block") or {}
        if block.get("type") == "tool_use":
            emit({
                "src": "proxy",
                "type": "tool_use_start",
                "tool_name": block.get("name") or "",
            })
    elif event_name == "message_delta":
        usage = data.get("usage") or {}
        delta = data.get("delta") or {}
        emit({
            "src": "proxy",
            "type": "message_delta",
            "output_tokens": usage.get("output_tokens") or 0,
            "stop_reason": delta.get("stop_reason"),
        })
    elif event_name == "message_stop":
        emit({"src": "proxy", "type": "message_stop"})
    elif event_name == "error":
        err = data.get("error") or {}
        emit({
            "src": "proxy",
            "type": "error",
            "message": err.get("message") or "",
        })


def _filter_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    target = UPSTREAM + request.rel_url.path_qs
    body = await request.read()
    fwd_headers = _filter_headers(request.headers)

    is_stream = (
        request.method == "POST"
        and request.path.endswith("/v1/messages")
        and b'"stream":true' in body.replace(b" ", b"")
    )

    timeout = aiohttp.ClientTimeout(total=None, sock_read=600, connect=15)
    session = request.app["client"]

    try:
        upstream_resp = await session.request(
            request.method,
            target,
            headers=fwd_headers,
            data=body,
            allow_redirects=False,
            timeout=timeout,
        )
    except aiohttp.ClientError as e:
        emit({"src": "proxy", "type": "upstream_error", "message": str(e)})
        return web.Response(status=502, text=f"proxy upstream error: {e}")

    resp = web.StreamResponse(
        status=upstream_resp.status,
        reason=upstream_resp.reason,
        headers=_filter_headers(upstream_resp.headers),
    )
    await resp.prepare(request)

    parser = SSEParser(handle_sse) if is_stream else None
    try:
        async for chunk in upstream_resp.content.iter_any():
            await resp.write(chunk)
            if parser is not None:
                parser.feed(chunk)
    finally:
        upstream_resp.release()

    await resp.write_eof()
    return resp


async def on_startup(app: web.Application) -> None:
    app["client"] = aiohttp.ClientSession(auto_decompress=False)
    print(f"[proxy] listening on http://{LISTEN_HOST}:{LISTEN_PORT} → {UPSTREAM}")
    print(f"[proxy] events → unix:{SOCKET_PATH}")


async def on_cleanup(app: web.Application) -> None:
    await app["client"].close()


def main() -> None:
    app = web.Application(client_max_size=1024 * 1024 * 64)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    try:
        web.run_app(app, host=LISTEN_HOST, port=LISTEN_PORT, print=lambda *_: None)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
