"""BLE protocol tests: framing, both endpoint handlers, and a full
loopback approval round-trip (CentralCore <-> PeripheralHandler wired
directly together, simulating the radio)."""

import asyncio
import json
import time

import pytest

from pibuddy import state as st
from pibuddy.bleproto import (
    CentralCore,
    LineBuffer,
    PeripheralHandler,
    chunks,
    encode,
    parse,
)
from pibuddy.state import StateStore


def test_encode_parse_roundtrip():
    msg = {"kind": "event", "payload": {"hook_event_name": "Stop", "session_id": "s"}}
    line = encode(msg)
    assert line.endswith(b"\n")
    assert parse(line[:-1]) == msg
    assert parse(b"not json") is None
    assert parse(b"[1,2]") is None


def test_chunking_and_reassembly():
    msg = {"kind": "event", "payload": {"data": "x" * 2000}}
    line = encode(msg)
    pieces = list(chunks(line))
    assert all(len(p) <= 180 for p in pieces)
    buf = LineBuffer()
    seen = []
    for piece in pieces:
        seen += buf.feed(piece)
    assert len(seen) == 1
    assert parse(seen[0]) == msg


def test_linebuffer_multiple_messages_one_chunk():
    buf = LineBuffer()
    lines = buf.feed(encode({"kind": "ping"}) + encode({"kind": "pong"}))
    assert [parse(l)["kind"] for l in lines] == ["ping", "pong"]


def test_linebuffer_drops_oversized_garbage():
    buf = LineBuffer()
    assert buf.feed(b"x" * (70 * 1024)) == []
    # And it recovers afterwards.
    assert len(buf.feed(encode({"kind": "ping"}))) == 1


def test_peripheral_event_and_ping():
    store = StateStore()
    replies = []
    handler = PeripheralHandler(store, replies.append)
    handler.receive(encode({"kind": "event", "payload": {
        "hook_event_name": "UserPromptSubmit", "session_id": "b1", "prompt": "hi"}}))
    assert store.snapshot().mood == st.BUSY
    handler.receive(encode({"kind": "ping"}))
    assert parse(replies[0][:-1]) == {"kind": "pong"}


def test_peripheral_desktop_compat():
    store = StateStore()
    handler = PeripheralHandler(store, lambda line: None)
    handler.receive(encode({"type": "approval_request", "session_id": "desk"}))
    assert store.snapshot().mood == st.ATTENTION
    handler.receive(encode({"type": "done", "session_id": "desk"}))
    assert store.snapshot().mood == st.CELEBRATE


def test_peripheral_approval_decision_notify():
    store = StateStore()
    replies = []
    handler = PeripheralHandler(store, replies.append)
    handler.receive(encode({"kind": "approval", "id": "req7", "wait": 5, "payload": {
        "session_id": "b1", "tool_name": "Bash", "tool_input": {"command": "ls"}}}))
    # The approval shows up for the touchscreen…
    deadline = time.monotonic() + 2
    while not store.snapshot().approvals and time.monotonic() < deadline:
        time.sleep(0.02)
    assert store.snapshot().approvals[0].tool_name == "Bash"
    # …the user taps deny, and the decision is notified back.
    store.resolve_approval("deny")
    deadline = time.monotonic() + 2
    while not replies and time.monotonic() < deadline:
        time.sleep(0.02)
    assert parse(replies[0][:-1]) == {"kind": "decision", "id": "req7", "decision": "deny"}


def test_peripheral_ignores_malformed():
    store = StateStore()
    handler = PeripheralHandler(store, lambda line: None)
    handler.receive(b"garbage\n")
    handler.receive(encode({"kind": "event", "payload": "not-a-dict"}))
    handler.receive(encode({"kind": "approval", "payload": None}))
    assert store.snapshot().mood == st.SLEEP


@pytest.mark.asyncio
async def test_loopback_approval_roundtrip():
    """Both ends wired together directly — the radio replaced by function
    calls, everything else exactly as in production."""
    store = StateStore()
    loop = asyncio.get_running_loop()

    core = CentralCore(send_line=None)  # set below

    def peripheral_reply(line: bytes) -> None:
        # Pi -> laptop notifications, delivered chunked like real BLE.
        for piece in chunks(line):
            loop.call_soon_threadsafe(core.feed, piece)

    handler = PeripheralHandler(store, peripheral_reply)

    async def central_send(line: bytes) -> None:
        for piece in chunks(line):
            handler.receive(piece)

    core.send_line = central_send

    # Event flows through and moves the mood.
    await core.send_event({"hook_event_name": "UserPromptSubmit", "session_id": "x"})
    assert store.snapshot().mood == st.BUSY

    # Approval: the "user" taps approve while the bridge awaits the decision.
    async def tap_when_visible():
        for _ in range(100):
            await asyncio.sleep(0.02)
            if store.snapshot().approvals:
                store.resolve_approval("allow")
                return
        raise AssertionError("approval never appeared")

    tap = asyncio.ensure_future(tap_when_visible())
    decision = await core.request_approval(
        {"session_id": "x", "tool_name": "Bash", "tool_input": {"command": "make"}},
        wait=5,
        loop=loop,
    )
    await tap
    assert decision == "allow"
    assert store.snapshot().approvals == []


@pytest.mark.asyncio
async def test_central_timeout_returns_none():
    core = CentralCore(send_line=lambda line: None)  # replies never come
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    decision = await core.request_approval({"tool_name": "Bash"}, wait=0.1, loop=loop)
    assert decision == "none"
    assert loop.time() - t0 < 12  # bounded, not hanging


def test_bridge_http_app_builds():
    """The bridge's local HTTP app constructs (no radio needed)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "pibuddy_bridge", Path(__file__).resolve().parent.parent / "scripts" / "pibuddy-bridge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bridge = mod.Bridge(name="PiBuddy", address=None, port=8766)
    app = bridge.build_app()
    paths = {r.resource.canonical for r in app.router.routes()}
    assert {"/api/event", "/api/approval", "/api/status"} <= paths
