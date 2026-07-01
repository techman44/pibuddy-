"""PiBuddy's BLE wire protocol — transport-agnostic, fully testable.

Both ends exchange newline-delimited JSON over the Nordic UART Service
(the same GATT service the upstream claude-desktop-buddy uses). BLE
writes/notifications are small (typically 20–512 bytes per packet), so
messages are chunked on send and reassembled by line buffering on
receive.

Messages (laptop bridge -> Pi):
    {"kind": "event",    "payload": {<hook payload>}}
    {"kind": "approval", "id": "…", "wait": 45, "payload": {<hook payload>}}
    {"kind": "ping"}

Messages (Pi -> laptop bridge):
    {"kind": "decision", "id": "…", "decision": "allow"|"deny"|"pass"|"none"}
    {"kind": "pong"}

Anything without a "kind" is treated as a Claude Desktop upstream-style
message ({"type": …}) and mapped to hook-shaped events best-effort, so
the same peripheral can also serve the Desktop app.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Iterator

from .state import StateStore

log = logging.getLogger("pibuddy.bleproto")

# Upper bound on payload bytes per BLE write/notification. The bridge
# clamps this down to the size the connection actually negotiated (see
# safe_chunk_size) — often just 20 bytes on older stacks; receivers
# reassemble by newline so any chunk size works.
CHUNK_SIZE = 180
# The BLE minimum (ATT_MTU 23 - 3 overhead): always safe to send.
MIN_CHUNK_SIZE = 20
MAX_LINE = 64 * 1024  # drop anything absurd rather than buffer forever
MAX_APPROVAL_WAIT = 120.0
DEFAULT_APPROVAL_WAIT = 45.0
# How many approval requests a peripheral will hold concurrently.
MAX_PENDING_APPROVALS = 16


def clamp_wait(value: object) -> float:
    """Parse an approval wait into [1, MAX_APPROVAL_WAIT] seconds.

    Garbage, nan and inf all fall back to the default — every layer
    (HTTP server, BLE peripheral, laptop bridge) must agree on this so
    timeout budgets stay consistent end to end.
    """
    try:
        wait = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_WAIT
    if not math.isfinite(wait):
        return DEFAULT_APPROVAL_WAIT
    return max(1.0, min(wait, MAX_APPROVAL_WAIT))


def encode(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def chunks(data: bytes, size: int = CHUNK_SIZE) -> Iterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def safe_chunk_size(reported: object) -> int:
    """Clamp a transport-reported max write size to something sane.

    Unknown/garbage values fall back to the BLE minimum of 20 bytes,
    which every stack accepts.
    """
    try:
        size = int(reported)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return MIN_CHUNK_SIZE
    if size < MIN_CHUNK_SIZE:
        return MIN_CHUNK_SIZE
    return min(size, CHUNK_SIZE)


class LineBuffer:
    """Reassemble newline-delimited messages from arbitrary chunks."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        if len(self._buf) > MAX_LINE and b"\n" not in self._buf:
            log.warning("dropping oversized BLE line (%d bytes)", len(self._buf))
            self._buf = b""
            return []
        lines = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if line.strip():
                lines.append(line)
        return lines


def parse(line: bytes) -> dict | None:
    try:
        msg = json.loads(line.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None
    return msg if isinstance(msg, dict) else None


class PeripheralHandler:
    """Pi-side message handling: feed it received bytes, it drives the
    StateStore and emits reply lines through `send_line`.

    `send_line` receives one full encoded line; the BLE layer is
    responsible for chunking it into notifications.
    """

    def __init__(self, store: StateStore, send_line: Callable[[bytes], None]) -> None:
        self.store = store
        self.send_line = send_line
        self._buffer = LineBuffer()
        self._approval_slots = threading.Semaphore(MAX_PENDING_APPROVALS)

    def reset(self) -> None:
        """Drop any partial line left over from a previous connection."""
        self._buffer = LineBuffer()

    def receive(self, data: bytes) -> None:
        for line in self._buffer.feed(data):
            msg = parse(line)
            if msg is not None:
                self._handle(msg)

    # ------------------------------------------------------------------

    def _handle(self, msg: dict) -> None:
        kind = msg.get("kind")
        if kind == "event":
            payload = msg.get("payload")
            if isinstance(payload, dict):
                self.store.apply_event(payload)
        elif kind == "approval":
            self._handle_approval(msg)
        elif kind == "ping":
            self._reply({"kind": "pong"})
        elif kind is None and "type" in msg:
            self._handle_desktop(msg)

    def _handle_approval(self, msg: dict) -> None:
        payload = msg.get("payload")
        if not isinstance(payload, dict):
            return
        request_id = str(msg.get("id") or "")
        wait = clamp_wait(msg.get("wait"))

        # Bound the number of waiter threads a central can pile up.
        if not self._approval_slots.acquire(blocking=False):
            log.warning("too many pending approvals, refusing %s", request_id)
            self._reply({"kind": "decision", "id": request_id, "decision": "none"})
            return

        self.store.apply_event({**payload, "hook_event_name": "PreToolUse"})
        req = self.store.add_approval(payload)

        def wait_for_decision() -> None:
            deadline = time.monotonic() + wait
            try:
                while req.decision is None and time.monotonic() < deadline:
                    time.sleep(0.1)
            finally:
                self.store.discard_approval(req)
                self._approval_slots.release()
            self._reply(
                {"kind": "decision", "id": request_id, "decision": req.decision or "none"}
            )

        threading.Thread(
            target=wait_for_decision, name="pibuddy-ble-approval", daemon=True
        ).start()

    def _handle_desktop(self, msg: dict) -> None:
        """Map Claude Desktop upstream-style messages to hook events."""
        kind = str(msg.get("type", ""))
        sid = str(msg.get("session_id") or "claude-desktop")
        if kind in ("approval", "approval_request"):
            self.store.apply_event(
                {"hook_event_name": "Notification", "session_id": sid,
                 "message": "Claude needs your permission"}
            )
        elif kind in ("state", "status") and msg.get("busy"):
            self.store.apply_event({"hook_event_name": "PreToolUse", "session_id": sid})
        elif kind in ("done", "complete"):
            self.store.apply_event({"hook_event_name": "Stop", "session_id": sid})
        else:
            self.store.apply_event({"hook_event_name": "SessionStart", "session_id": sid})

    def _reply(self, message: dict) -> None:
        try:
            self.send_line(encode(message))
        except Exception as exc:  # never let a reply failure kill the handler
            log.warning("BLE reply failed: %s", exc)


class CentralCore:
    """Laptop-bridge-side logic: turns HTTP requests into protocol lines
    and correlates decision replies. Transport-agnostic for testing —
    the BLE layer supplies `send_line` and calls `feed` with notify data.
    """

    def __init__(self, send_line: Callable[[bytes], Any]) -> None:
        self.send_line = send_line
        self._buffer = LineBuffer()
        self._pending: dict[str, Any] = {}  # id -> asyncio.Future
        self._counter = 0
        self.connected = False

    def next_id(self) -> str:
        self._counter += 1
        return f"a{self._counter}"

    def reset(self) -> None:
        """Call on (re)connect: drop any partial line from the old link."""
        self._buffer = LineBuffer()

    def fail_pending(self) -> None:
        """Call on disconnect: answer every in-flight approval with 'none'
        immediately instead of holding the hook until its timeout."""
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result("none")
        self._pending.clear()

    async def send_event(self, payload: dict) -> None:
        await self._send({"kind": "event", "payload": payload})

    async def request_approval(self, payload: dict, wait: float, loop) -> str:
        import asyncio

        wait = clamp_wait(wait)
        rid = self.next_id()
        future = loop.create_future()
        self._pending[rid] = future
        try:
            await self._send({"kind": "approval", "id": rid, "wait": wait, "payload": payload})
            # Grace must stay under the hook's curl --max-time (wait + 5),
            # or our answer would be written to a closed socket.
            try:
                return await asyncio.wait_for(future, timeout=wait + 3)
            except asyncio.TimeoutError:
                return "none"
        finally:
            self._pending.pop(rid, None)

    def feed(self, data: bytes) -> None:
        for line in self._buffer.feed(data):
            msg = parse(line)
            if not msg:
                continue
            if msg.get("kind") == "decision":
                future = self._pending.get(str(msg.get("id")))
                if future is not None and not future.done():
                    future.set_result(str(msg.get("decision") or "none"))

    async def _send(self, message: dict) -> None:
        result = self.send_line(encode(message))
        if hasattr(result, "__await__"):
            await result
