import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pibuddy.server import build_app
from pibuddy.state import StateStore


@pytest.fixture
def store():
    return StateStore()


async def make_client(store, token=""):
    server = TestServer(build_app(store, token))
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_event_updates_state(store):
    client = await make_client(store)
    try:
        resp = await client.post(
            "/api/event",
            json={"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hi"},
        )
        assert resp.status == 200
        assert store.snapshot().mood == "busy"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_bad_json_rejected(store):
    client = await make_client(store)
    try:
        resp = await client.post("/api/event", data="not json")
        assert resp.status == 400
        resp = await client.post("/api/event", json=[1, 2, 3])
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_token_enforced(store):
    client = await make_client(store, token="sekrit")
    try:
        resp = await client.post("/api/event", json={"hook_event_name": "Stop"})
        assert resp.status == 401
        resp = await client.post(
            "/api/event",
            json={"hook_event_name": "Stop", "session_id": "x"},
            headers={"X-PiBuddy-Token": "sekrit"},
        )
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approval_roundtrip(store):
    client = await make_client(store)
    try:
        async def answer_when_visible():
            for _ in range(100):
                await asyncio.sleep(0.05)
                if store.snapshot().approval is not None:
                    store.resolve_current_approval("allow")
                    return
            raise AssertionError("approval never appeared")

        answering = asyncio.ensure_future(answer_when_visible())
        resp = await client.post(
            "/api/approval?wait=5",
            json={
                "hook_event_name": "PreToolUse",
                "session_id": "s1",
                "tool_name": "Bash",
                "tool_input": {"command": "make deploy"},
            },
        )
        await answering
        body = await resp.json()
        assert body["decision"] == "allow"
        # Approval is cleaned up afterwards.
        assert store.snapshot().approval is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_approval_timeout_returns_none(store):
    client = await make_client(store)
    try:
        resp = await client.post(
            "/api/approval?wait=1",
            json={"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash"},
        )
        body = await resp.json()
        assert body["decision"] == "none"
        assert store.snapshot().approval is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_endpoint(store):
    client = await make_client(store)
    try:
        await client.post("/api/event", json={"hook_event_name": "SessionStart", "session_id": "a"})
        resp = await client.get("/api/status")
        body = await resp.json()
        assert body["sessions"] == 1
        assert body["mood"] == "busy"
    finally:
        await client.close()
