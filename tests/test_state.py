import pytest

from pibuddy import state as st
from pibuddy.state import StateStore


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


def test_starts_asleep(store):
    assert store.snapshot().mood == st.SLEEP


def test_session_lifecycle(store, clock):
    store.apply_event(event("SessionStart", cwd="/proj"))
    assert store.snapshot().mood == st.BUSY  # just started, considered active
    clock.advance(st.BUSY_LINGER + 1)
    assert store.snapshot().mood == st.IDLE
    store.apply_event(event("SessionEnd"))
    assert store.snapshot().mood == st.SLEEP


def test_busy_while_working(store, clock):
    store.apply_event(event("UserPromptSubmit", prompt="do a thing"))
    assert store.snapshot().mood == st.BUSY
    store.apply_event(event("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
    assert store.snapshot().mood == st.BUSY


def test_stop_celebrates_then_idles(store, clock):
    store.apply_event(event("UserPromptSubmit"))
    store.apply_event(event("Stop"))
    assert store.snapshot().mood == st.CELEBRATE
    clock.advance(st.CELEBRATE_SECS + 1)
    assert store.snapshot().mood == st.IDLE


def test_permission_notification_needs_attention(store):
    store.apply_event(event("UserPromptSubmit"))
    store.apply_event(event("Notification", message="Claude needs your permission to use Bash"))
    assert store.snapshot().mood == st.ATTENTION
    # Resuming work clears it.
    store.apply_event(event("PostToolUse", tool_name="Bash"))
    assert store.snapshot().mood == st.BUSY


def test_multiple_sessions_aggregate(store, clock):
    store.apply_event(event("UserPromptSubmit", sid="a"))
    store.apply_event(event("Stop", sid="a"))
    store.apply_event(event("UserPromptSubmit", sid="b"))
    clock.advance(st.CELEBRATE_SECS + 1)
    snap = store.snapshot()
    assert snap.mood == st.BUSY  # b still working
    assert len(snap.sessions) == 2


def test_approval_flow_allow_fast_gives_heart(store, clock):
    req = store.add_approval(event("PreToolUse", tool_name="Bash", tool_input={"command": "rm -rf /tmp/x"}))
    assert store.snapshot().mood == st.ATTENTION
    assert store.snapshot().approval.tool_name == "Bash"
    clock.advance(2)
    resolved = store.resolve_current_approval("allow")
    assert resolved is req
    assert req.decision == "allow"
    store.discard_approval(req)
    assert store.snapshot().mood == st.HEART


def test_approval_deny_no_heart(store, clock):
    req = store.add_approval(event("PreToolUse", tool_name="Write"))
    store.resolve_current_approval("deny")
    store.discard_approval(req)
    assert store.snapshot().mood != st.HEART


def test_slow_approval_no_heart(store, clock):
    req = store.add_approval(event("PreToolUse", tool_name="Bash"))
    clock.advance(st.FAST_APPROVAL_SECS + 1)
    store.resolve_current_approval("allow")
    store.discard_approval(req)
    assert store.snapshot().mood != st.HEART


def test_sessions_expire(store, clock):
    store.apply_event(event("SessionStart"))
    clock.advance(st.SESSION_TTL + 1)
    assert store.snapshot().mood == st.SLEEP


def test_xp_and_levels(store, clock):
    assert store.level == 1
    for _ in range(25):
        store.apply_event(event("PreToolUse", tool_name="Bash"))
        store.apply_event(event("Stop"))
    snap = store.snapshot()
    assert snap.xp == 25 * (st.XP_PER_TOOL + st.XP_PER_STOP)
    assert snap.level == 1 + snap.xp // st.XP_PER_LEVEL
    assert snap.level > 1


def test_activity_log_records_and_orders(store, clock):
    store.apply_event(event("UserPromptSubmit", prompt="fix the tests"))
    store.apply_event(event("PreToolUse", tool_name="Bash", tool_input={"command": "pytest"}))
    store.apply_event(event("Stop"))
    log = store.snapshot().log
    assert [e.kind for e in log[:3]] == ["done", "tool", "prompt"]  # newest first
    assert "pytest" in log[1].text
    assert "fix the tests" in log[2].text


def test_dizzy_trigger(store, clock):
    store.apply_event(event("SessionStart"))
    clock.advance(st.BUSY_LINGER + 1)
    store.trigger_dizzy()
    assert store.snapshot().mood == st.DIZZY
    clock.advance(st.DIZZY_SECS + 1)
    assert store.snapshot().mood == st.IDLE


def test_unknown_and_malformed_events_do_not_crash(store):
    store.apply_event({})
    store.apply_event(event("SomethingNew"))
    store.apply_event({"hook_event_name": "PreToolUse", "tool_input": "not-a-dict"})
    assert store.snapshot().events_seen == 3
