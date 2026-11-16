"""BLE protocol tests: framing, both endpoint handlers, and a full
loopback approval round-trip (CentralCore <-> PeripheralHandler wired
directly together, simulating the radio)."""

import asyncio
import json
import time

import pytest

from pibuddy import state as st
import threading

from pibuddy.bleproto import (
    CentralCore,
    LineBuffer,
    MAX_FRAME,
    MIN_CHUNK_SIZE,
    PeripheralHandler,
    chunks,
    clamp_wait,
    encode,
    parse,
    safe_chunk_size,
    slim_payload,
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


def test_clamp_wait():
    assert clamp_wait(45) == 45.0
    assert clamp_wait("30") == 30.0
    assert clamp_wait(600) == 120.0  # server-side maximum
    assert clamp_wait(0) == 1.0
    assert clamp_wait("nan") == 45.0  # garbage -> default, never nan
    assert clamp_wait(float("inf")) == 45.0
    assert clamp_wait(None) == 45.0
    assert clamp_wait("bogus") == 45.0


def test_buffers_reset_on_reconnect():
    # Peripheral side: a partial line from a dropped link must not
    # corrupt the first message of the next connection.
    store = StateStore()
    handler = PeripheralHandler(store, lambda line: None)
    handler.receive(b'{"kind":"event","payl')  # link dies mid-frame
    handler.reset()
    handler.receive(encode({"kind": "event", "payload": {
        "hook_event_name": "UserPromptSubmit", "session_id": "s"}}))
    assert store.snapshot().mood == st.BUSY

    # Central side: same story for notifications.
    core = CentralCore(send_line=lambda line: None)
    core.feed(b'{"kind":"deci')
    core.reset()
    got = []
    core._pending["a1"] = _FakeFuture(got)
    core.feed(encode({"kind": "decision", "id": "a1", "decision": "allow"}))
    assert got == ["allow"]


class _FakeFuture:
    def __init__(self, sink):
        self._sink = sink
        self._done = False

    def done(self):
        return self._done

    def set_result(self, value):
        self._done = True
        self._sink.append(value)


@pytest.mark.asyncio
async def test_fail_pending_unblocks_approvals_on_disconnect():
    core = CentralCore(send_line=lambda line: None)  # decision never arrives
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(
        core.request_approval({"tool_name": "Bash"}, wait=60, loop=loop)
    )
    await asyncio.sleep(0.05)
    assert core._pending  # in flight
    core.fail_pending()  # BLE dropped
    decision = await asyncio.wait_for(task, timeout=1)
    assert decision == "none"  # answered immediately, not after 60s


def test_approval_slots_bounded():
    store = StateStore()
    replies = []
    handler = PeripheralHandler(store, replies.append)
    handler._approval_slots = threading.Semaphore(1)
    handler.receive(encode({"kind": "approval", "id": "one", "wait": 5,
                            "payload": {"session_id": "s", "tool_name": "Bash"}}))
    # Slot occupied: the second request is refused immediately.
    handler.receive(encode({"kind": "approval", "id": "two", "wait": 5,
                            "payload": {"session_id": "s", "tool_name": "Write"}}))
    deadline = time.monotonic() + 2
    while not replies and time.monotonic() < deadline:
        time.sleep(0.02)
    assert parse(replies[0][:-1]) == {"kind": "decision", "id": "two", "decision": "none"}
    store.resolve_approval("deny")  # release the waiter thread


def test_slim_payload_trims_but_keeps_shape():
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "s1",
        "tool_name": "Write",
        "tool_input": {"file_path": "/x.txt", "content": "A" * 200_000},
        "pibuddy_context": "B" * 5000,
    }
    slim = slim_payload(payload)
    assert slim["tool_name"] == "Write"
    assert slim["tool_input"]["file_path"] == "/x.txt"
    assert len(slim["tool_input"]["content"]) < 2000
    assert len(slim["pibuddy_context"]) < 2000
    assert len(encode({"kind": "approval", "id": "x", "payload": slim})) < MAX_FRAME


@pytest.mark.asyncio
async def test_huge_write_approval_roundtrips_after_slimming():
    """A Write approval with 200KB of content must still reach the buddy
    (slimmed) instead of being dropped by the peripheral's line guard."""
    store = StateStore()
    loop = asyncio.get_running_loop()
    core = CentralCore(send_line=None)
    handler = PeripheralHandler(
        store, lambda line: [loop.call_soon_threadsafe(core.feed, p) for p in chunks(line)]
    )

    async def central_send(line):
        for piece in chunks(line):
            handler.receive(piece)

    core.send_line = central_send

    async def tap():
        for _ in range(200):
            await asyncio.sleep(0.02)
            if store.snapshot().approvals:
                store.resolve_approval("allow")
                return
        raise AssertionError("approval never appeared")

    tapping = asyncio.ensure_future(tap())
    decision = await core.request_approval(
        {"session_id": "s", "tool_name": "Write",
         "tool_input": {"file_path": "/big.txt", "content": "A" * 200_000}},
        wait=5, loop=loop,
    )
    await tapping
    assert decision == "allow"


@pytest.mark.asyncio
async def test_pathological_approval_fails_open_immediately():
    """If a frame can't be made to fit even after slimming, the bridge
    answers 'none' at once instead of holding the hook to timeout."""
    core = CentralCore(send_line=lambda line: None)
    loop = asyncio.get_running_loop()
    # 40 keys x 1500-char strings survive slimming but exceed MAX_FRAME.
    payload = {f"k{i}": "x" * 1400 for i in range(40)}
    t0 = loop.time()
    decision = await core.request_approval(payload, wait=60, loop=loop)
    assert decision == "none"
    assert loop.time() - t0 < 1.0  # immediate, not 60s
    assert not core._pending


def test_safe_chunk_size_clamps():
    assert safe_chunk_size(None) == MIN_CHUNK_SIZE
    assert safe_chunk_size("garbage") == MIN_CHUNK_SIZE
    assert safe_chunk_size(0) == MIN_CHUNK_SIZE
    assert safe_chunk_size(3) == MIN_CHUNK_SIZE  # never below the BLE minimum
    assert safe_chunk_size(100) == 100
    assert safe_chunk_size(512) == 180  # never above our frame chunk cap
    # 20-byte chunks still reassemble fine.
    msg = {"kind": "event", "payload": {"hook_event_name": "Stop", "session_id": "s1"}}
    buf = LineBuffer()
    seen = []
    for piece in chunks(encode(msg), MIN_CHUNK_SIZE):
        assert len(piece) <= MIN_CHUNK_SIZE
        seen += buf.feed(piece)
    assert parse(seen[0]) == msg


@pytest.mark.asyncio
async def test_bridge_send_line_serializes_frames():
    """Concurrent frames must not interleave chunks on the UART stream."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "pibuddy_bridge_ser",
        Path(__file__).resolve().parent.parent / "scripts" / "pibuddy-bridge.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    bridge = mod.Bridge(name="PiBuddy", address=None, port=8766)
    bridge._chunk_size = 5  # force many chunks per frame

    written: list[bytes] = []

    class FakeClient:
        is_connected = True

        async def write_gatt_char(self, uuid, data, response=False):
            written.append(bytes(data))
            await asyncio.sleep(0)  # yield, inviting interleaving

    bridge.client = FakeClient()
    line_a = b"A" * 23 + b"\n"
    line_b = b"B" * 23 + b"\n"
    await asyncio.gather(bridge._send_line(line_a), bridge._send_line(line_b))

    stream = b"".join(written)
    # Each frame arrives contiguously: no B bytes inside A's frame or vice versa.
    frames = stream.split(b"\n")[:-1]
    assert sorted(frames) == sorted([b"A" * 23, b"B" * 23])


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
