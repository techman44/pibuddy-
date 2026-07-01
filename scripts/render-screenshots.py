#!/usr/bin/env python3
"""Render the README/PR screenshot storyboard (no hardware needed).

    python3 scripts/render-screenshots.py [outdir]

Draws every stage of the PiBuddy flow headlessly (SDL dummy driver) at
a consistent size and writes PNGs to docs/screenshots/ by default.
"""

from __future__ import annotations

import datetime
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pygame  # noqa: E402

from pibuddy.config import Config  # noqa: E402
from pibuddy.display import Display  # noqa: E402
from pibuddy.state import DayStats, StateStore  # noqa: E402

SIZE = (960, 600)
SMALL = (480, 320)


def ev(store, name, sid="s1", **extra):
    store.apply_event({"hook_event_name": name, "session_id": sid, **extra})


def shoot(out, name, setup, size=SIZE, t_offset=0.55, **cfg):
    pygame.init()
    store = StateStore()
    d = Display(
        store,
        Config(fullscreen=False, width=size[0], height=size[1], token="demo", **cfg),
    )
    d._open_window()
    d._started = time.monotonic() - t_offset
    snap = setup(d, store)
    d._draw(snap if snap is not None else store.snapshot())
    pygame.image.save(d.canvas, str(out / name))
    pygame.quit()
    print("wrote", name)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/screenshots")
    out.mkdir(parents=True, exist_ok=True)

    # 1. Asleep: no sessions anywhere.
    shoot(out, "01-sleeping.png", lambda d, s: None, t_offset=2.1)

    # 2. A session starts and Claude gets to work.
    def busy(d, store):
        ev(store, "SessionStart", cwd="/home/dean/webapp")
        ev(store, "UserPromptSubmit", prompt="add dark mode")
        ev(store, "PreToolUse", tool_name="Bash", tool_input={"command": "npm test"})

    shoot(out, "02-working.png", busy, t_offset=0.7)

    # 3. Claude needs permission: attention!
    def attention(d, store):
        ev(store, "SessionStart")
        ev(store, "Notification", message="Claude needs your permission to use Bash")

    shoot(out, "03-attention.png", attention, t_offset=0.25)

    # 4. Ignored for 72s: escalated, red pulsing edge.
    def escalated(d, store):
        store.add_approval(
            {"session_id": "s1", "tool_name": "Bash",
             "tool_input": {"command": "terraform apply -auto-approve"}}
        )
        snap = store.snapshot()
        object.__setattr__(snap, "attention_age", 72.0)
        return snap

    shoot(out, "04-escalated.png", escalated, t_offset=0.18)

    # 5. The rich approval screen.
    def approval(d, store):
        store.add_approval(
            {
                "session_id": "f00dcafe",
                "tool_name": "Bash",
                "tool_input": {
                    "command": "npm run deploy -- --prod && git tag v2.1.0 && git push --tags",
                    "description": "Deploy the site to production and tag the release",
                },
                "pibuddy_context": (
                    "The build passed and all 91 tests are green. The staging check "
                    "looks good, so I'll deploy to production and tag this as v2.1.0."
                ),
            }
        )

    shoot(out, "05-approval.png", approval)
    shoot(out, "05b-approval-small.png", approval, size=SMALL)

    # 6. Approved fast -> task finishes -> celebrate.
    def celebrate(d, store):
        store.xp = 12_000  # crown + bow tie
        ev(store, "UserPromptSubmit")
        ev(store, "Stop")

    shoot(out, "06-celebrate.png", celebrate, t_offset=0.8)

    # 7. Several terminals at once: buddy grid.
    def grid(d, store):
        ev(store, "SessionStart", sid="a", cwd="/home/dean/pibuddy")
        ev(store, "SessionStart", sid="b", cwd="/home/dean/webapp")
        ev(store, "Notification", sid="b", message="Claude needs your permission")
        ev(store, "SessionStart", sid="c", cwd="/home/dean/api-server")
        ev(store, "PreToolUse", sid="c", tool_name="Bash", tool_input={"command": "ls"})
        ev(store, "SessionStart", sid="d", cwd="/home/dean/dotfiles")
        for sess in store._sessions.values():
            if sess.session_id in ("a", "d"):
                sess.busy_until = 0

    shoot(out, "07-buddy-grid.png", grid, grid=True, t_offset=1.1)

    # 8. The activity feed.
    def feed(d, store):
        ev(store, "SessionStart", cwd="/home/dean/pibuddy")
        ev(store, "UserPromptSubmit", prompt="port buddy to the raspberry pi")
        ev(store, "PreToolUse", tool_name="Read", tool_input={"file_path": "pibuddy/state.py"})
        ev(store, "PreToolUse", tool_name="Bash", tool_input={"command": "python -m pytest -q"})
        ev(store, "Notification", message="Claude needs your permission to use Bash")
        req = store.add_approval(
            {"session_id": "s1", "tool_name": "Bash",
             "tool_input": {"command": "git push -u origin main"}}
        )
        store.resolve_approval("allow", req.request_id)
        store.discard_approval(req)
        ev(store, "Stop")
        d.screen_index = 1

    shoot(out, "08-activity-feed.png", feed)

    # 9. Stats: streak, hour chart, QR pairing code.
    def stats(d, store):
        store.xp = 5210
        today = datetime.date.today()
        for i in range(6):
            day = (today - datetime.timedelta(days=i)).isoformat()
            store._daily[day] = DayStats(tools=30 - i, prompts=8, stops=5, sessions=2, xp=400)
        store._hour_hist = [0, 0, 0, 0, 0, 0, 1, 2, 5, 9, 14, 11, 6, 8, 13, 17, 12, 7, 4, 6, 3, 2, 1, 0]
        ev(store, "SessionStart", cwd="/home/dean/pibuddy")
        d.screen_index = 2

    shoot(out, "09-stats-qr.png", stats)

    # 10. Long-press settings menu (with the touch Exit button).
    def settings(d, store):
        ev(store, "SessionStart")
        d.overlay = "settings"

    shoot(out, "10-settings.png", settings)

    # 11. Asleep at night: ambient clock.
    def clockmode(d, store):
        d.weather_text = "18°"
        d._last_interaction = 0

    shoot(out, "11-clock.png", clockmode)


if __name__ == "__main__":
    main()
