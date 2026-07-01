# PiBuddy

A Raspberry Pi port of [anthropics/claude-desktop-buddy](https://github.com/anthropics/claude-desktop-buddy):
an animated desk companion that reacts to your **Claude Code** sessions — driven
by webhooks from Claude Code's hooks instead of BLE, so any machine on your
network (or several at once) can feed it.

The buddy sleeps when nothing is happening, blinks and idles while sessions are
open, sweats while Claude runs tools, bounces with a "!" when something needs
your permission — and with a touchscreen you can **approve or deny the pending
tool call right on the Pi**.

## How it works

```
 your laptop(s)                          raspberry pi
┌───────────────────┐   HTTP (LAN)   ┌──────────────────────────┐
│ Claude Code       │  ────────────▶ │ pibuddy daemon           │
│  hooks (curl)     │   /api/event   │  aiohttp server          │
│                   │ ◀────────────  │  session state machine   │
│  PreToolUse waits │  /api/approval │  pygame touchscreen UI   │
└───────────────────┘    decision    └──────────────────────────┘
```

* Every Claude Code hook event (`SessionStart`, `PreToolUse`, `Stop`, …) is
  forwarded fire-and-forget by `hooks/pibuddy-event.sh` — with a 2 s timeout in
  the background, so a powered-off Pi never slows a session down.
* Optionally, a blocking `PreToolUse` hook (`hooks/pibuddy-approve.sh`) holds
  the tool call while the Pi shows an Approve / Deny overlay. Tap a button and
  the decision flows back as a `permissionDecision`. If nobody taps in time,
  the hook stays silent and Claude Code falls back to its normal terminal
  prompt.
* Multiple sessions are tracked independently (one status dot each) and
  aggregated into a single mood: `attention > dizzy > heart > celebrate >
  busy > idle > sleep` — the same seven states as the original Buddy.

## The screens

Swipe horizontally to switch between three screens; a modal approval overlay
takes over any of them when a permission is waiting:

1. **Pet** — the buddy, big, with a status caption. Triple-tap it for a
   surprise.
2. **Activity** — a scrollable live feed of prompts, tool calls, notifications
   and approval verdicts from all sessions.
3. **Stats** — level and XP progress (tool calls and finished tasks earn XP),
   session/event counters, and the URLs your hooks should post to.

Every dimension is computed from the panel's actual resolution at startup, so
the same code runs on a 3.5" 480×320 SPI hat, the official 7" 800×480, or a
10" 1280×800 panel — landscape or portrait (`--rotate 90`).

## Setup

### On the Pi

```bash
git clone https://github.com/techman44/pibuddy- pibuddy && cd pibuddy
sudo apt install python3-pygame python3-aiohttp python3-pil   # or: pip install -r requirements.txt
python3 -m pibuddy                    # fullscreen on the attached display
```

Useful flags (also settable in `~/.config/pibuddy/config.json`):

```
--port 8765            webhook port
--token SECRET         require this shared secret from hooks (recommended)
--rotate 90            rotate the UI for portrait panels
--window 800x480       run windowed (development on a desktop)
--character-pack DIR   use an upstream-format GIF character pack
--dim-after 120        seconds before the screen dims when idle
```

To start on boot, see `systemd/pibuddy.service`. On Pi OS Lite the UI renders
straight to the framebuffer via KMS/DRM (`SDL_VIDEODRIVER=kmsdrm`), no desktop
needed.

### On each machine where you run Claude Code

```bash
python3 scripts/install-hooks.py --url http://<pi-address>:8765 --token SECRET
```

That copies the hook scripts to `~/.claude/pibuddy/` and merges the hook
entries into `~/.claude/settings.json` (a backup is written first). To also
route permission prompts to the touchscreen for specific tools:

```bash
python3 scripts/install-hooks.py --url http://<pi>:8765 --token SECRET --approvals 'Bash'
```

`--approvals` takes a Claude Code hook matcher (e.g. `Bash|Write|Edit`). Note
that the hook fires for *every* matching tool call, including ones Claude Code
would have auto-allowed — start with a narrow matcher. Remove everything with
`--uninstall`.

## Character packs

Drop-in compatible with upstream GIF character packs
(`sleep.gif`, `idle.gif`, `busy.gif`, `attention.gif`, `celebrate.gif`,
`dizzy.gif`, `heart.gif` + optional `manifest.json`); missing states fall back
sensibly. Packs are scaled nearest-neighbor to keep the pixel-art look. Without
a pack you get **Pip**, the built-in vector pet, which is crisp at any
resolution.

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/event` | any Claude Code hook payload; updates mood/feed/XP |
| `POST /api/approval?wait=45` | blocks until Approve/Deny is tapped or the wait elapses; returns `{"decision": "allow"\|"deny"\|"none"}` |
| `GET /api/status` | current mood, session count, XP — handy for debugging |

If a `--token` is set, requests must carry it in an `X-PiBuddy-Token` header.
Treat the token as LAN-level protection; don't expose the port to the internet.

## Development

```bash
pip install -r requirements.txt pytest pytest-asyncio
python3 -m pytest tests/          # state machine, server, headless render tests
python3 -m pibuddy --window 800x480   # run locally; mouse simulates touch
```
