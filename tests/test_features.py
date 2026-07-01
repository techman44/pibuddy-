"""Tests for the feature batch: escalation, queue, stats, persistence,
remote decide, settings/session overlays, grid, clock, sounds."""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

from pibuddy import state as st  # noqa: E402
from pibuddy.config import Config  # noqa: E402
from pibuddy.display import Display  # noqa: E402
from pibuddy.persist import Persistence  # noqa: E402
from pibuddy.server import build_app  # noqa: E402
from pibuddy.sound import Sounds  # noqa: E402
from pibuddy.state import StateStore, escalation_tier, session_mood  # noqa: E402

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, secs):
        self.now += secs


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(clock):
    return StateStore(clock=clock)


def event(name, sid="s1", **extra):
    return {"hook_event_name": name, "session_id": sid, **extra}


# ---------------------------------------------------------------- state


def test_escalation_tiers():
    assert escalation_tier(0) == 0
    assert escalation_tier(29) == 0
    assert escalation_tier(30) == 1
    assert escalation_tier(59.9) == 1
    assert escalation_tier(60) == 2
    assert escalation_tier(600) == 2


def test_attention_age_from_notification(store, clock):
    store.apply_event(event("Notification", message="Claude needs your permission"))
    clock.advance(45)
    snap = store.snapshot()
    assert snap.mood == st.ATTENTION
    assert 44 <= snap.attention_age <= 46
    assert escalation_tier(snap.attention_age) == 1


def test_attention_age_from_approval(store, clock):
    store.add_approval(event("PreToolUse", tool_name="Bash"))
    clock.advance(70)
    snap = store.snapshot()
    assert escalation_tier(snap.attention_age) == 2


def test_session_mood_per_session(store, clock):
    store.apply_event(event("UserPromptSubmit", sid="a"))
    store.apply_event(event("Notification", sid="b", message="permission needed"))
    store.apply_event(event("SessionStart", sid="c"))
    clock.advance(st.BUSY_LINGER + 1)
    snap = store.snapshot()
    moods = {s.session_id: session_mood(s, clock()) for s in snap.sessions}
    assert moods["a"] == st.IDLE
    assert moods["b"] == st.ATTENTION
    assert moods["c"] == st.IDLE


def test_resolve_approval_by_id(store, clock):
    a = store.add_approval(event("PreToolUse", tool_name="Bash", sid="s1"))
    b = store.add_approval(event("PreToolUse", tool_name="Write", sid="s2"))
    resolved = store.resolve_approval("deny", b.request_id)
    assert resolved is b and b.decision == "deny"
    assert a.decision is None
    assert store.resolve_approval("allow", "nonexistent") is None
    assert store.resolve_approval("allow") is a  # oldest pending by default


def test_daily_stats_and_streak(store, clock):
    store.apply_event(event("SessionStart"))
    store.apply_event(event("PreToolUse", tool_name="Bash"))
    store.apply_event(event("UserPromptSubmit", prompt="hi"))
    store.apply_event(event("Stop"))
    snap = store.snapshot()
    assert snap.today.tools == 1
    assert snap.today.prompts == 1
    assert snap.today.stops == 1
    assert snap.today.sessions == 1
    assert snap.streak_days == 1
    assert sum(snap.hour_hist) == 1


def test_reset_stats(store):
    store.apply_event(event("PreToolUse", tool_name="Bash"))
    assert store.xp > 0
    store.reset_stats()
    snap = store.snapshot()
    assert snap.xp == 0 and snap.today.tools == 0 and sum(snap.hour_hist) == 0


def test_persistence_roundtrip(store, clock, tmp_path):
    store.apply_event(event("PreToolUse", tool_name="Bash"))
    store.apply_event(event("Stop"))
    p = Persistence(store, tmp_path / "state.json")
    p.save()

    fresh = StateStore(clock=clock)
    p2 = Persistence(fresh, tmp_path / "state.json")
    p2.load()
    snap = fresh.snapshot()
    assert snap.xp == store.xp
    assert snap.today.tools == 1
    assert snap.hour_hist == store.snapshot().hour_hist


def test_persistence_ignores_garbage(store, tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    Persistence(store, path).load()  # must not raise
    store.import_state({"xp": "nope"})  # wrong-typed fields are tolerated
    # xp int() of "nope" would raise; ensure it didn't corrupt the store
    assert isinstance(store.snapshot().xp, int)


# ---------------------------------------------------------------- server


async def make_client(store, **kw):
    server = TestServer(build_app(store, **kw))
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_decide_endpoint(store):
    client = await make_client(store)
    try:
        req = store.add_approval(event("PreToolUse", tool_name="Bash"))
        resp = await client.post(
            "/api/decide", json={"request_id": req.request_id, "decision": "allow"}
        )
        assert resp.status == 200
        assert req.decision == "allow"
        # Unknown id -> 404; bad decision -> 400
        resp = await client.post("/api/decide", json={"request_id": "x", "decision": "allow"})
        assert resp.status == 404
        resp = await client.post("/api/decide", json={"decision": "maybe"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_includes_approvals_and_stats(store):
    client = await make_client(store)
    try:
        store.apply_event(event("PreToolUse", tool_name="Bash"))
        store.add_approval(event("PreToolUse", tool_name="Write", tool_input={"file_path": "/x"}))
        resp = await client.get("/api/status")
        body = await resp.json()
        assert body["approvals"][0]["tool_name"] == "Write"
        assert body["today"]["tools"] == 1
        assert "attention_age" in body and "escalation" in body
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_phone_page_served_and_query_token_auth(store):
    client = await make_client(store, token="sek")
    try:
        resp = await client.get("/")
        assert resp.status == 200
        assert "PiBuddy remote" in await resp.text()
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/status?token=sek")).status == 200
    finally:
        await client.close()


# ---------------------------------------------------------------- display


@pytest.fixture(autouse=True)
def pygame_session():
    pygame.init()
    yield
    pygame.quit()


def make_display(size=(800, 480), **cfg):
    store = StateStore()
    config = Config(fullscreen=False, width=size[0], height=size[1], **cfg)
    d = Display(store, config)
    d._open_window()
    return d, store


def test_settings_overlay_actions():
    d, store = make_display()
    d.overlay = "settings"
    d._draw(store.snapshot())
    actions = {a for _, a in d._settings_rects}
    assert {"sound", "grid", "dim", "reset", "exit", "close"} <= actions
    for rect, action in d._settings_rects:
        if action == "grid":
            d._hit_settings(*rect.center)
    assert d.grid_enabled
    # exit posts a QUIT event
    d.overlay = "settings"
    d._draw(store.snapshot())
    for rect, action in d._settings_rects:
        if action == "exit":
            d._hit_settings(*rect.center)
    assert any(e.type == pygame.QUIT for e in pygame.event.get())


def test_sessions_overlay_renders():
    d, store = make_display()
    store.apply_event(event("SessionStart", cwd="/home/dean/project"))
    store.apply_event(event("UserPromptSubmit", prompt="do stuff"))
    d.overlay = "sessions"
    d._draw(store.snapshot())


def test_buddy_grid_renders():
    d, store = make_display(grid=True)
    for sid in ("a", "b", "c"):
        store.apply_event(event("SessionStart", sid=sid, cwd=f"/proj/{sid}"))
    d._draw(store.snapshot())


def test_approval_queue_navigation_and_selected_resolve():
    d, store = make_display()
    a = store.add_approval(event("PreToolUse", tool_name="Bash", sid="s1"))
    b = store.add_approval(event("PreToolUse", tool_name="Write", sid="s2"))
    d._draw(store.snapshot())
    d.approval_index = 1  # select the second approval
    approve, _, _ = d._approval_button_rects()
    assert d._hit_approval_buttons(*approve.center)
    assert b.decision == "allow" and a.decision is None


def test_terminal_button_passes_to_terminal():
    d, store = make_display()
    req = store.add_approval(event("PreToolUse", tool_name="Bash"))
    d._draw(store.snapshot())
    _, terminal, _ = d._approval_button_rects()
    assert d._hit_approval_buttons(*terminal.center)
    assert req.decision == "pass"  # hook stays silent -> terminal prompt


def test_rich_approval_fields_and_render():
    d, store = make_display()
    store.add_approval(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {
                "command": "npm run deploy -- --prod",
                "description": "Deploy the site to production",
            },
            "pibuddy_context": "Build passed, ready to ship. I'll deploy now.",
        }
    )
    snap = store.snapshot()
    req = snap.approvals[0]
    assert req.description == "Deploy the site to production"
    assert "ready to ship" in req.context
    d._draw(snap)


def test_approval_with_question_options_renders():
    d, store = make_display()
    store.add_approval(
        {
            "session_id": "s1",
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [
                    {
                        "question": "Which database should we use?",
                        "options": [{"label": "Postgres"}, {"label": "SQLite"}],
                    }
                ]
            },
        }
    )
    snap = store.snapshot()
    assert snap.approvals[0].questions == (
        ("Which database should we use?", ("Postgres", "SQLite")),
    )
    d._draw(snap)


def test_approval_fallback_detail_from_input():
    store = StateStore()
    store.add_approval(
        {"session_id": "s", "tool_name": "Fetch", "tool_input": {"method": "GET", "target": "example"}}
    )
    req = store.snapshot().approvals[0]
    assert "method: GET" in req.detail


def test_clock_mode_renders():
    d, store = make_display()
    d.weather_text = "21°"
    d._last_interaction = 0  # long ago -> fully dimmed
    d._draw(store.snapshot())  # mood is sleep with no sessions


def test_escalation_edge_renders():
    d, store = make_display()

    class OldClock:
        def __call__(self):
            import time as _t

            return _t.monotonic()

    store.add_approval(event("PreToolUse", tool_name="Bash"))
    snap = store.snapshot()
    # Force an escalated age without waiting.
    object.__setattr__(snap, "attention_age", 75.0)
    d._draw(snap)


def test_stats_screen_with_qr():
    d, store = make_display()
    store.apply_event(event("PreToolUse", tool_name="Bash"))
    d.screen_index = 2
    d._draw(store.snapshot())


def test_sounds_are_safe_headless():
    s = Sounds(enabled=True)
    s.chirp()
    s.alert()
    s.success()
    s.deny()
    assert s.toggle() in (True, False)
