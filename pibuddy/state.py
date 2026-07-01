"""Session tracking and the buddy state machine.

Events arrive from Claude Code hooks (one JSON payload per hook firing,
identified by ``hook_event_name`` and ``session_id``). Any number of
terminals/machines can feed the same buddy, so state is tracked per
session and then aggregated into a single displayed mood.

Mood priority (highest wins):
    attention > dizzy > heart > celebrate > busy > idle > sleep
"""

from __future__ import annotations

import datetime
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

# Buddy moods, mirroring the upstream claude-desktop-buddy animation states.
SLEEP = "sleep"
IDLE = "idle"
BUSY = "busy"
ATTENTION = "attention"
CELEBRATE = "celebrate"
DIZZY = "dizzy"
HEART = "heart"

# Sessions with no events for this long are considered gone.
SESSION_TTL = 15 * 60
# A session is "busy" for this long after its last working event, so the
# buddy doesn't flicker to idle between tool calls.
BUSY_LINGER = 90
CELEBRATE_SECS = 4.0
HEART_SECS = 3.0
DIZZY_SECS = 3.0
# Approvals answered faster than this trigger the heart animation.
FAST_APPROVAL_SECS = 5.0

# Attention escalation tiers (seconds unanswered -> tier 0/1/2).
ESCALATE_T1 = 30.0
ESCALATE_T2 = 60.0

XP_PER_TOOL = 5
XP_PER_STOP = 50
XP_PER_LEVEL = 1000

_WORKING_EVENTS = {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact"}


def escalation_tier(attention_age: float) -> int:
    """0 = normal, 1 = getting impatient, 2 = jumping up and down."""
    if attention_age >= ESCALATE_T2:
        return 2
    if attention_age >= ESCALATE_T1:
        return 1
    return 0


@dataclass
class Session:
    session_id: str
    cwd: str = ""
    last_seen: float = 0.0
    busy_until: float = 0.0
    needs_attention: bool = False
    attention_since: float = 0.0
    last_tool: str = ""
    last_prompt: str = ""
    started: float = 0.0


def session_mood(sess: Session, now: float) -> str:
    """The mood of a single session (used by the buddy-grid view)."""
    if sess.needs_attention:
        return ATTENTION
    if now < sess.busy_until:
        return BUSY
    return IDLE


@dataclass
class Approval:
    request_id: str
    session_id: str
    tool_name: str
    detail: str
    created: float
    decision: str | None = None  # "allow" | "deny"
    decided_at: float = 0.0


@dataclass
class LogEntry:
    """One line in the activity feed."""

    when: float  # wall-clock (time.time)
    session_id: str
    kind: str  # short label, e.g. "tool", "prompt", "note", "done"
    text: str


@dataclass
class DayStats:
    tools: int = 0
    prompts: int = 0
    stops: int = 0
    sessions: int = 0
    xp: int = 0


@dataclass
class Snapshot:
    """Immutable view of state for one render frame."""

    mood: str
    sessions: list[Session]
    approvals: list[Approval]  # pending, oldest first
    approvals_waiting: int
    attention_age: float  # seconds the oldest attention has gone unanswered
    xp: int
    level: int
    events_seen: int
    last_event_name: str
    last_event_at: float
    log: list[LogEntry]
    today: DayStats
    streak_days: int
    hour_hist: list[int]  # tool events per hour-of-day, all time

    @property
    def approval(self) -> Approval | None:
        return self.approvals[0] if self.approvals else None


class StateStore:
    """Thread-safe buddy state. The server thread writes, the UI thread reads."""

    def __init__(self, clock=time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._approvals: list[Approval] = []
        self._log: deque[LogEntry] = deque(maxlen=200)
        self.xp = 0
        self.events_seen = 0
        self.last_event_name = ""
        self.last_event_at = 0.0
        self._celebrate_until = 0.0
        self._heart_until = 0.0
        self._dizzy_until = 0.0
        self._daily: dict[str, DayStats] = {}  # ISO date -> stats
        self._hour_hist = [0] * 24
        self.dirty = False  # set on changes worth persisting

    # ------------------------------------------------------------------
    # Event ingestion (called from the server thread)
    # ------------------------------------------------------------------

    def apply_event(self, payload: dict[str, Any]) -> None:
        name = str(payload.get("hook_event_name", ""))
        sid = str(payload.get("session_id") or "unknown")
        now = self._clock()

        with self._lock:
            self.events_seen += 1
            self.last_event_name = name
            self.last_event_at = now
            self._record(name, sid, payload)

            if name == "SessionEnd":
                self._sessions.pop(sid, None)
                self._expire(now)
                return

            sess = self._sessions.setdefault(sid, Session(session_id=sid, started=now))
            sess.last_seen = now
            if payload.get("cwd"):
                sess.cwd = str(payload["cwd"])

            if name in _WORKING_EVENTS:
                sess.busy_until = now + BUSY_LINGER
                self._clear_attention(sess)
                if name == "UserPromptSubmit":
                    sess.last_prompt = str(payload.get("prompt", ""))[:200]
                if name in ("PreToolUse", "PostToolUse"):
                    sess.last_tool = str(payload.get("tool_name", ""))
                    self._add_xp(XP_PER_TOOL, now)
                    self._today().tools += 1
                    self._hour_hist[datetime.datetime.now().hour] += 1
                if name == "UserPromptSubmit":
                    self._today().prompts += 1
            elif name == "Notification":
                message = str(payload.get("message", "")).lower()
                if "permission" in message or "waiting" in message:
                    self._raise_attention(sess, now)
                    sess.busy_until = 0.0
            elif name in ("Stop", "SubagentStop"):
                sess.busy_until = 0.0
                self._clear_attention(sess)
                if name == "Stop":
                    self._celebrate_until = now + CELEBRATE_SECS
                    self._add_xp(XP_PER_STOP, now)
                    self._today().stops += 1
            elif name == "SessionStart":
                sess.busy_until = now + BUSY_LINGER
                self._today().sessions += 1

            # Hook scripts may attach token usage they extracted client-side.
            tokens = payload.get("pibuddy_tokens")
            if isinstance(tokens, (int, float)) and tokens > 0:
                self._add_xp(int(tokens) // 100, now)

            self.dirty = True
            self._expire(now)

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------

    def add_approval(self, payload: dict[str, Any]) -> Approval:
        tool = str(payload.get("tool_name", "tool"))
        tool_input = payload.get("tool_input") or {}
        detail = ""
        if isinstance(tool_input, dict):
            detail = str(
                tool_input.get("command")
                or tool_input.get("file_path")
                or tool_input.get("description")
                or ""
            )
        req = Approval(
            request_id=uuid.uuid4().hex,
            session_id=str(payload.get("session_id") or "unknown"),
            tool_name=tool,
            detail=detail,
            created=self._clock(),
        )
        with self._lock:
            self._approvals.append(req)
        return req

    def resolve_approval(self, decision: str, request_id: str | None = None) -> Approval | None:
        """Resolve a pending approval (the oldest one if no id is given).

        Called from the UI thread (touch) or the server (phone page).
        """
        now = self._clock()
        with self._lock:
            for req in self._approvals:
                if req.decision is not None:
                    continue
                if request_id is not None and req.request_id != request_id:
                    continue
                req.decision = decision
                req.decided_at = now
                if decision == "allow" and now - req.created <= FAST_APPROVAL_SECS:
                    self._heart_until = now + HEART_SECS
                verdict = "approved" if decision == "allow" else "denied"
                self._log.appendleft(
                    LogEntry(
                        when=time.time(),
                        session_id=req.session_id,
                        kind="approval",
                        text=f"{verdict}: {req.tool_name}  {req.detail}"[:300],
                    )
                )
                self.dirty = True
                return req
        return None

    # Backwards-compatible alias.
    def resolve_current_approval(self, decision: str) -> Approval | None:
        return self.resolve_approval(decision)

    def discard_approval(self, req: Approval) -> None:
        with self._lock:
            if req in self._approvals:
                self._approvals.remove(req)

    # ------------------------------------------------------------------
    # UI-side effects
    # ------------------------------------------------------------------

    def trigger_dizzy(self) -> None:
        with self._lock:
            self._dizzy_until = self._clock() + DIZZY_SECS

    def reset_stats(self) -> None:
        with self._lock:
            self.xp = 0
            self._daily.clear()
            self._hour_hist = [0] * 24
            self.dirty = True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def export_state(self) -> dict:
        with self._lock:
            self.dirty = False
            return {
                "xp": self.xp,
                "hour_hist": list(self._hour_hist),
                "daily": {day: vars(stats) for day, stats in self._daily.items()},
            }

    def import_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return

        def as_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        with self._lock:
            self.xp = as_int(data.get("xp"))
            hist = data.get("hour_hist")
            if isinstance(hist, list) and len(hist) == 24:
                self._hour_hist = [as_int(v) for v in hist]
            daily = data.get("daily")
            if isinstance(daily, dict):
                known = set(vars(DayStats()).keys())
                for day, stats in daily.items():
                    if isinstance(stats, dict):
                        self._daily[str(day)] = DayStats(
                            **{k: as_int(v) for k, v in stats.items() if k in known}
                        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        now = self._clock()
        with self._lock:
            self._expire(now)
            pending = [a for a in self._approvals if a.decision is None]
            mood = self._mood(now, bool(pending))
            attention_age = 0.0
            if pending:
                attention_age = max(attention_age, now - pending[0].created)
            for s in self._sessions.values():
                if s.needs_attention and s.attention_since:
                    attention_age = max(attention_age, now - s.attention_since)
            return Snapshot(
                mood=mood,
                sessions=[Session(**vars(s)) for s in self._sessions.values()],
                approvals=[Approval(**vars(a)) for a in pending],
                approvals_waiting=len(pending),
                attention_age=attention_age,
                xp=self.xp,
                level=self.level,
                events_seen=self.events_seen,
                last_event_name=self.last_event_name,
                last_event_at=self.last_event_at,
                log=list(self._log),
                today=DayStats(**vars(self._today())),
                streak_days=self._streak(),
                hour_hist=list(self._hour_hist),
            )

    @property
    def level(self) -> int:
        return 1 + self.xp // XP_PER_LEVEL

    # ------------------------------------------------------------------
    # Internals (call with lock held)
    # ------------------------------------------------------------------

    def _mood(self, now: float, approval_pending: bool) -> str:
        if approval_pending or any(s.needs_attention for s in self._sessions.values()):
            return ATTENTION
        if now < self._dizzy_until:
            return DIZZY
        if now < self._heart_until:
            return HEART
        if now < self._celebrate_until:
            return CELEBRATE
        if any(now < s.busy_until for s in self._sessions.values()):
            return BUSY
        if self._sessions:
            return IDLE
        return SLEEP

    def _raise_attention(self, sess: Session, now: float) -> None:
        if not sess.needs_attention:
            sess.needs_attention = True
            sess.attention_since = now

    def _clear_attention(self, sess: Session) -> None:
        sess.needs_attention = False
        sess.attention_since = 0.0

    def _today(self) -> DayStats:
        key = datetime.date.today().isoformat()
        if key not in self._daily:
            self._daily[key] = DayStats()
        return self._daily[key]

    def _streak(self) -> int:
        """Consecutive days with any activity, ending today or yesterday."""
        day = datetime.date.today()
        streak = 0
        stats = self._daily.get(day.isoformat())
        if not stats or (stats.tools + stats.prompts + stats.stops + stats.sessions) == 0:
            day -= datetime.timedelta(days=1)  # today hasn't started yet
        while True:
            stats = self._daily.get(day.isoformat())
            if not stats or (stats.tools + stats.prompts + stats.stops + stats.sessions) == 0:
                break
            streak += 1
            day -= datetime.timedelta(days=1)
        return streak

    def _record(self, name: str, sid: str, payload: dict[str, Any]) -> None:
        kind, text = "note", name
        if name == "UserPromptSubmit":
            kind, text = "prompt", str(payload.get("prompt", ""))[:300] or "(prompt)"
        elif name in ("PreToolUse", "PostToolUse"):
            tool = str(payload.get("tool_name", "tool"))
            tool_input = payload.get("tool_input") or {}
            detail = ""
            if isinstance(tool_input, dict):
                detail = str(
                    tool_input.get("command")
                    or tool_input.get("file_path")
                    or tool_input.get("description")
                    or ""
                )[:200]
            if name == "PostToolUse":
                return  # PreToolUse already logged this call
            kind, text = "tool", f"{tool}  {detail}".strip()
        elif name == "Notification":
            kind, text = "note", str(payload.get("message", ""))[:300] or "notification"
        elif name == "Stop":
            kind, text = "done", "Claude finished responding"
        elif name == "SubagentStop":
            kind, text = "done", "Subagent finished"
        elif name == "SessionStart":
            kind, text = "note", f"Session started  {payload.get('cwd', '')}"
        elif name == "SessionEnd":
            kind, text = "note", "Session ended"
        elif name == "PreCompact":
            kind, text = "note", "Compacting context"
        self._log.appendleft(LogEntry(when=time.time(), session_id=sid, kind=kind, text=text))

    def _add_xp(self, amount: int, now: float) -> None:
        before = self.level
        self.xp += amount
        self._today().xp += amount
        if self.level > before:
            self._celebrate_until = now + CELEBRATE_SECS * 2

    def _expire(self, now: float) -> None:
        dead = [k for k, s in self._sessions.items() if now - s.last_seen > SESSION_TTL]
        for k in dead:
            del self._sessions[k]
        # Drop resolved/abandoned approvals after a grace period.
        self._approvals = [
            a
            for a in self._approvals
            if a.decision is None or now - a.decided_at < 30
        ]
