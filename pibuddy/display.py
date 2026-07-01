"""Resolution-independent, touch-first UI.

Three swipeable screens (pet / activity feed / stats) plus overlays:
a modal approval prompt (with queue navigation when several are
pending), a per-session detail card, and a long-press settings menu
with an exit button. When the buddy sleeps and the screen dims, an
ambient clock takes over.

Every dimension is derived from the actual display size at startup, so
the same code runs on a 3.5" 480x320 SPI hat and a 10" 1280x800 DSI
panel, portrait or landscape (see --rotate).

Touch handling: SDL reports touchscreens both as FINGER* events and as
synthesized mouse events; we listen to FINGER* plus real-mouse-only
events so a desktop mouse works in development windows too.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import socket
import threading
import time
import urllib.request

import pygame

from . import buddy as vector_buddy
from . import state as st
from .characters import CharacterPack
from .sound import Sounds
from .state import StateStore, Snapshot, escalation_tier, session_mood

log = logging.getLogger("pibuddy.display")

BG = (28, 26, 34)
BG_PANEL = (40, 38, 50)
FG = (235, 232, 240)
MUTED = (150, 148, 165)
ACCENT = (240, 160, 90)
GOOD = (95, 190, 120)
BAD = (225, 95, 85)
ATTN = (250, 190, 60)
URGENT = (235, 80, 70)

KIND_COLORS = {
    "tool": (120, 180, 240),
    "prompt": (240, 160, 90),
    "note": MUTED,
    "done": GOOD,
    "approval": ATTN,
}

MOOD_CAPTIONS = {
    st.SLEEP: "zzz…",
    st.IDLE: "waiting for something to do",
    st.BUSY: "working…",
    st.ATTENTION: "needs your attention!",
    st.CELEBRATE: "done!",
    st.DIZZY: "whoa…",
    st.HEART: "thanks!",
}

SCREENS = ("pet", "feed", "stats")
SWIPE_FRACTION = 0.12  # of screen width
TAP_MAX_PX_FRACTION = 0.02
TAP_MAX_SECS = 0.5
LONG_PRESS_SECS = 1.2
ALERT_REPEAT_SECS = 15.0


def _local_addresses() -> list[str]:
    addrs = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        addrs.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    hostname = socket.gethostname()
    if hostname:
        addrs.append(f"{hostname}.local")
    return addrs


def _make_qr(text: str) -> list[list[bool]] | None:
    try:
        import segno
    except ImportError:
        return None
    qr = segno.make(text, error="m")
    return [[bool(v) for v in row] for row in qr.matrix]


class Display:
    def __init__(self, store: StateStore, config) -> None:
        self.store = store
        self.config = config
        self.pack: CharacterPack | None = None
        self.sounds = Sounds(enabled=getattr(config, "sound", False))
        self.grid_enabled = bool(getattr(config, "grid", False))
        self.screen_index = 0
        self.feed_scroll = 0.0
        self.approval_index = 0
        self.overlay: str | None = None  # None | "settings" | "sessions"
        self.weather_text = ""
        self._fonts: dict[int, pygame.font.Font] = {}
        self._pointer_down: tuple[float, float, float] | None = None  # x, y, t
        self._pointer_moved = 0.0
        self._drag_last_y: float | None = None
        self._recent_taps: list[float] = []
        self._last_interaction = time.monotonic()
        self._started = time.monotonic()
        self._prev_mood = st.SLEEP
        self._last_alert = 0.0
        self._qr_cache: tuple[str, pygame.Surface] | None = None
        self._settings_rects: list[tuple[pygame.Rect, str]] = []

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _open_window(self) -> None:
        pygame.init()
        pygame.mouse.set_visible(not self.config.fullscreen)
        if self.config.fullscreen:
            self.physical = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.physical = pygame.display.set_mode((self.config.width, self.config.height))
        pygame.display.set_caption("PiBuddy")

        pw, ph = self.physical.get_size()
        self.rotate = self.config.rotate % 360
        if self.rotate in (90, 270):
            self.w, self.h = ph, pw
        else:
            self.w, self.h = pw, ph
        if self.rotate:
            self.canvas = pygame.Surface((self.w, self.h))
        else:
            self.canvas = self.physical
        log.info("display %dx%d (rotate %d) -> logical %dx%d", pw, ph, self.rotate, self.w, self.h)

        # Layout derived from logical size.
        self.unit = min(self.w, self.h)
        self.header_h = max(28, int(self.h * 0.09))
        self.footer_h = max(16, int(self.h * 0.05))
        self.stage = pygame.Rect(
            0, self.header_h, self.w, self.h - self.header_h - self.footer_h
        )

    def font(self, size: int) -> pygame.font.Font:
        size = max(10, size)
        if size not in self._fonts:
            self._fonts[size] = pygame.font.Font(None, size)
        return self._fonts[size]

    # ------------------------------------------------------------------
    # Coordinate handling
    # ------------------------------------------------------------------

    def _to_logical(self, px: float, py: float) -> tuple[float, float]:
        if self.rotate == 0:
            return px, py
        if self.rotate == 90:
            return self.w - 1 - py, px
        if self.rotate == 180:
            return self.w - 1 - px, self.h - 1 - py
        return py, self.h - 1 - px  # 270

    def _pointer_pos(self, event) -> tuple[float, float] | None:
        if event.type in (pygame.FINGERDOWN, pygame.FINGERUP, pygame.FINGERMOTION):
            pw, ph = self.physical.get_size()
            return self._to_logical(event.x * pw, event.y * ph)
        if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
            if getattr(event, "touch", False):
                return None  # synthesized from a finger event we already saw
            return self._to_logical(*event.pos)
        return None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._open_window()
        if self.config.character_pack:
            from .characters import load_pack

            self.pack = load_pack(self.config.character_pack)
            if self.pack:
                log.info("loaded character pack '%s'", self.pack.name)
        self._start_weather()

        clock = pygame.time.Clock()
        running = True
        while running:
            snap = self.store.snapshot()
            self._sound_cues(snap)
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == pygame.KEYDOWN and event.key in (
                    pygame.K_ESCAPE,
                    pygame.K_q,
                ):
                    running = False
                else:
                    self._handle_pointer(event, snap)
            self._check_long_press(snap)

            self._draw(snap)
            if self.rotate:
                rotated = pygame.transform.rotate(self.canvas, self.rotate)
                self.physical.blit(rotated, (0, 0))
            pygame.display.flip()
            clock.tick(self.config.fps)
        pygame.quit()

    def _sound_cues(self, snap: Snapshot) -> None:
        now = time.monotonic()
        if snap.mood == st.ATTENTION and self._prev_mood != st.ATTENTION:
            self.sounds.chirp()
            self._last_alert = now
        elif snap.mood == st.ATTENTION and escalation_tier(snap.attention_age) >= 2:
            if now - self._last_alert >= ALERT_REPEAT_SECS:
                self.sounds.alert()
                self._last_alert = now
        elif snap.mood == st.CELEBRATE and self._prev_mood != st.CELEBRATE:
            self.sounds.success()
        self._prev_mood = snap.mood

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    def _handle_pointer(self, event, snap: Snapshot) -> None:
        pos = self._pointer_pos(event)
        if pos is None:
            return
        x, y = pos
        now = time.monotonic()
        is_down = event.type in (pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN)
        is_up = event.type in (pygame.FINGERUP, pygame.MOUSEBUTTONUP)
        is_motion = event.type in (pygame.FINGERMOTION, pygame.MOUSEMOTION)
        if event.type == pygame.MOUSEBUTTONDOWN and event.button != 1:
            return
        if event.type == pygame.MOUSEBUTTONUP and event.button != 1:
            return
        if is_motion and self._pointer_down is None:
            return

        woke = self._wake()
        if is_down:
            if woke:
                self._pointer_down = None  # consume the waking touch
                return
            self._pointer_down = (x, y, now)
            self._pointer_moved = 0.0
            self._drag_last_y = y
            return

        if is_motion and self._drag_last_y is not None:
            x0, y0, _ = self._pointer_down
            self._pointer_moved = max(self._pointer_moved, math.hypot(x - x0, y - y0))
            if (
                SCREENS[self.screen_index] == "feed"
                and not snap.approvals
                and self.overlay is None
            ):
                self.feed_scroll -= y - self._drag_last_y
            self._drag_last_y = y
            return

        if not is_up or self._pointer_down is None:
            return

        x0, y0, t0 = self._pointer_down
        self._pointer_down = None
        self._drag_last_y = None
        dx, dy = x - x0, y - y0
        moved = math.hypot(dx, dy)
        is_tap = moved <= max(8, self.unit * TAP_MAX_PX_FRACTION) and now - t0 <= TAP_MAX_SECS
        is_swipe = abs(dx) > self.w * SWIPE_FRACTION and abs(dx) > abs(dy)

        # Approval overlay eats input first.
        if snap.approvals:
            if is_tap and self._hit_approval_buttons(x, y):
                return
            if is_swipe and len(snap.approvals) > 1:
                self.approval_index += 1 if dx < 0 else -1
            return

        if self.overlay == "settings":
            if is_tap:
                self._hit_settings(x, y)
            return
        if self.overlay == "sessions":
            if is_tap:
                self.overlay = None
            return

        if is_tap:
            self._on_tap(x, y, now, snap)
            return
        if is_swipe:
            if dx < 0:
                self.screen_index = min(self.screen_index + 1, len(SCREENS) - 1)
            else:
                self.screen_index = max(self.screen_index - 1, 0)

    def _check_long_press(self, snap: Snapshot) -> None:
        if self._pointer_down is None or snap.approvals or self.overlay is not None:
            return
        x0, y0, t0 = self._pointer_down
        if (
            time.monotonic() - t0 >= LONG_PRESS_SECS
            and self._pointer_moved <= max(10, self.unit * 0.03)
        ):
            self._pointer_down = None
            self._drag_last_y = None
            self.overlay = "settings"

    def _on_tap(self, x: float, y: float, now: float, snap: Snapshot) -> None:
        if y <= self.header_h and snap.sessions:
            self.overlay = "sessions"
            return
        if SCREENS[self.screen_index] == "pet":
            # Triple-tap easter egg: make the buddy dizzy.
            self._recent_taps = [t for t in self._recent_taps if now - t < 1.0]
            self._recent_taps.append(now)
            if len(self._recent_taps) >= 3:
                self._recent_taps.clear()
                self.store.trigger_dizzy()

    def _hit_approval_buttons(self, x: float, y: float) -> bool:
        approve, terminal, deny = self._approval_button_rects()
        snap = self.store.snapshot()
        selected = None
        if snap.approvals:
            selected = snap.approvals[self.approval_index % len(snap.approvals)]
        rid = selected.request_id if selected else None
        if approve.collidepoint(x, y):
            if self.store.resolve_approval("allow", rid):
                self.sounds.success()
            self.approval_index = 0
            return True
        if deny.collidepoint(x, y):
            if self.store.resolve_approval("deny", rid):
                self.sounds.deny()
            self.approval_index = 0
            return True
        if terminal.collidepoint(x, y):
            # "pass" -> the hook stays silent and the terminal shows its
            # normal prompt with the full option list.
            self.store.resolve_approval("pass", rid)
            self.approval_index = 0
            return True
        return False

    def _hit_settings(self, x: float, y: float) -> None:
        for rect, action in self._settings_rects:
            if not rect.collidepoint(x, y):
                continue
            if action == "sound":
                self.sounds.toggle()
            elif action == "grid":
                self.grid_enabled = not self.grid_enabled
            elif action == "dim":
                self._last_interaction = time.monotonic() - self.config.dim_after - 10
                self.overlay = None
            elif action == "reset":
                self.store.reset_stats()
            elif action == "exit":
                pygame.event.post(pygame.event.Event(pygame.QUIT))
            elif action == "close":
                self.overlay = None
            return
        self.overlay = None  # tap outside the panel closes

    def _wake(self) -> bool:
        """Register interaction; returns True if the screen was dimmed."""
        was_dimmed = self._dim_level() > 0.5
        self._last_interaction = time.monotonic()
        return was_dimmed

    def _dim_level(self) -> float:
        idle_for = time.monotonic() - self._last_interaction
        if idle_for < self.config.dim_after:
            return 0.0
        return min(1.0, (idle_for - self.config.dim_after) / 5.0)

    # ------------------------------------------------------------------
    # Weather (optional, for the ambient clock)
    # ------------------------------------------------------------------

    def _start_weather(self) -> None:
        lat = getattr(self.config, "latitude", None)
        lon = getattr(self.config, "longitude", None)
        if lat is None or lon is None:
            return

        def poll():
            url = (
                "https://api.open-meteo.com/v1/forecast"
                f"?latitude={lat}&longitude={lon}&current_weather=true"
            )
            while True:
                try:
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = json.loads(resp.read())
                    cw = data.get("current_weather", {})
                    if "temperature" in cw:
                        self.weather_text = f"{round(cw['temperature'])}°"
                except Exception as exc:  # network is best-effort
                    log.debug("weather fetch failed: %s", exc)
                time.sleep(1800)

        threading.Thread(target=poll, name="pibuddy-weather", daemon=True).start()

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def _draw(self, snap: Snapshot) -> None:
        self.canvas.fill(BG)
        self._draw_header(snap)
        screen = SCREENS[self.screen_index]
        if screen == "pet":
            self._draw_pet(snap)
        elif screen == "feed":
            self._draw_feed(snap)
        else:
            self._draw_stats(snap)
        self._draw_screen_dots()

        if snap.approvals:
            self._draw_approval(snap)
            self._draw_escalation_edge(snap)
            return
        if self.overlay == "settings":
            self._draw_settings()
            return
        if self.overlay == "sessions":
            self._draw_sessions(snap)
            return

        if snap.mood == st.ATTENTION:
            self._draw_escalation_edge(snap)
            return  # never dim while attention is needed
        dim = self._dim_level()
        if dim > 0 and snap.mood in (st.SLEEP, st.IDLE):
            veil = pygame.Surface((self.w, self.h))
            veil.fill((0, 0, 0))
            veil.set_alpha(int(225 * dim))
            self.canvas.blit(veil, (0, 0))
            if dim >= 1.0 and snap.mood == st.SLEEP:
                self._draw_clock()

    def _draw_escalation_edge(self, snap: Snapshot) -> None:
        tier = escalation_tier(snap.attention_age)
        if tier == 0:
            return
        t = time.monotonic() - self._started
        pulse = (math.sin(t * (4 if tier == 1 else 8)) + 1) / 2
        color = ATTN if tier == 1 else URGENT
        thickness = max(4, int(self.unit * 0.02))
        edge = pygame.Surface((self.w, self.h), pygame.SRCALPHA)
        alpha = int(90 + 140 * pulse)
        for i in range(thickness):
            fade = 1.0 - i / thickness
            pygame.draw.rect(
                edge,
                (*color, int(alpha * fade)),
                pygame.Rect(i, i, self.w - 2 * i, self.h - 2 * i),
                width=1,
            )
        self.canvas.blit(edge, (0, 0))

    def _draw_header(self, snap: Snapshot) -> None:
        pygame.draw.rect(self.canvas, BG_PANEL, (0, 0, self.w, self.header_h))
        f = self.font(int(self.header_h * 0.55))
        title = f.render(f"PiBuddy · Lv {snap.level}", True, FG)
        self.canvas.blit(
            title, (int(self.unit * 0.03), (self.header_h - title.get_height()) // 2)
        )
        # One dot per session, colored by its state (tap for details).
        r = max(4, int(self.header_h * 0.16))
        x = self.w - int(self.unit * 0.03) - r
        now = time.monotonic()
        for sess in snap.sessions[:10]:
            mood = session_mood(sess, now)
            color = ATTN if mood == st.ATTENTION else (120, 180, 240) if mood == st.BUSY else MUTED
            pygame.draw.circle(self.canvas, color, (x, self.header_h // 2), r)
            x -= r * 3

    def _draw_pet(self, snap: Snapshot) -> None:
        t = time.monotonic() - self._started
        if self.grid_enabled and len(snap.sessions) >= 2 and not self.pack:
            self._draw_buddy_grid(snap, t)
            return
        intensity = 1.0 + 0.6 * escalation_tier(snap.attention_age)
        if self.pack:
            self.pack.draw(self.canvas, self.stage, snap.mood, t)
        else:
            vector_buddy.draw(
                self.canvas, self.stage, snap.mood, t, intensity=intensity, level=snap.level
            )

        caption = MOOD_CAPTIONS.get(snap.mood, "")
        n = len(snap.sessions)
        if n and snap.mood in (st.IDLE, st.BUSY):
            caption = f"{n} session{'s' if n != 1 else ''} · {caption}"
        if snap.mood == st.ATTENTION and escalation_tier(snap.attention_age) >= 1:
            caption = f"waiting {int(snap.attention_age)}s — hello?!"
        f = self.font(int(self.unit * 0.06))
        text = f.render(caption, True, MUTED)
        self.canvas.blit(
            text,
            (self.w // 2 - text.get_width() // 2, self.stage.bottom - text.get_height()),
        )

    def _draw_buddy_grid(self, snap: Snapshot, t: float) -> None:
        """One mini-Clawd per session."""
        sessions = snap.sessions[:9]
        n = len(sessions)
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        cell_w = self.stage.width // cols
        cell_h = self.stage.height // rows
        now = time.monotonic()
        f = self.font(max(12, int(min(cell_w, cell_h) * 0.09)))
        for i, sess in enumerate(sessions):
            cell = pygame.Rect(
                self.stage.left + (i % cols) * cell_w,
                self.stage.top + (i // cols) * cell_h,
                cell_w,
                cell_h,
            )
            mood = session_mood(sess, now)
            # Stagger each buddy's animation so they don't move in lockstep.
            vector_buddy.draw(self.canvas, cell.inflate(-8, -f.get_height() * 2), mood,
                              t + i * 1.7, level=snap.level)
            name = sess.cwd.rstrip("/").rsplit("/", 1)[-1] or sess.session_id[:8]
            label = f.render(name, True, FG if mood != st.IDLE else MUTED)
            self.canvas.blit(
                label, (cell.centerx - label.get_width() // 2, cell.bottom - label.get_height() - 2)
            )

    def _draw_feed(self, snap: Snapshot) -> None:
        row_h = max(24, int(self.unit * 0.085))
        f_text = self.font(int(row_h * 0.62))
        f_time = self.font(int(row_h * 0.5))
        area = self.stage
        max_scroll = max(0.0, len(snap.log) * row_h - area.height)
        self.feed_scroll = max(0.0, min(self.feed_scroll, max_scroll))

        if not snap.log:
            msg = f_text.render("No activity yet — waiting for hook events", True, MUTED)
            self.canvas.blit(msg, msg.get_rect(center=area.center))
            return

        clip = self.canvas.get_clip()
        self.canvas.set_clip(area)
        y = area.top - self.feed_scroll
        time_w = f_time.size("00:00:00")[0] + int(self.unit * 0.04)
        for entry in snap.log:
            if y + row_h > area.top and y < area.bottom:
                stamp = datetime.datetime.fromtimestamp(entry.when).strftime("%H:%M:%S")
                color = KIND_COLORS.get(entry.kind, MUTED)
                self.canvas.blit(
                    f_time.render(stamp, True, MUTED),
                    (int(self.unit * 0.03), y + (row_h - f_time.get_height()) // 2),
                )
                pygame.draw.circle(
                    self.canvas,
                    color,
                    (time_w + int(self.unit * 0.02), y + row_h // 2),
                    max(3, row_h // 8),
                )
                text = entry.text.replace("\n", " ")
                text_x = time_w + int(self.unit * 0.05)
                avail = self.w - text_x - int(self.unit * 0.03)
                label = f_text.render(text, True, FG)
                while label.get_width() > avail and len(text) > 4:
                    text = text[: int(len(text) * 0.9)] + "…"
                    label = f_text.render(text, True, FG)
                self.canvas.blit(label, (text_x, y + (row_h - label.get_height()) // 2))
            y += row_h
        self.canvas.set_clip(clip)

    def _draw_stats(self, snap: Snapshot) -> None:
        pad = int(self.unit * 0.06)
        f_big = self.font(int(self.unit * 0.12))
        f = self.font(int(self.unit * 0.055))
        y = self.stage.top + pad // 2

        level = f_big.render(f"Level {snap.level}", True, ACCENT)
        self.canvas.blit(level, (pad, y))
        streak = f.render(
            f"{snap.streak_days} day streak" if snap.streak_days else "", True, GOOD
        )
        self.canvas.blit(streak, (pad + level.get_width() + pad, y + level.get_height() // 3))
        y += level.get_height() + pad // 3

        # XP progress bar toward the next level.
        into = snap.xp % st.XP_PER_LEVEL
        bar = pygame.Rect(pad, y, self.w - pad * 2, max(10, int(self.unit * 0.035)))
        pygame.draw.rect(self.canvas, BG_PANEL, bar, border_radius=bar.height // 2)
        fill = bar.copy()
        fill.width = max(bar.height, int(bar.width * into / st.XP_PER_LEVEL))
        pygame.draw.rect(self.canvas, ACCENT, fill, border_radius=bar.height // 2)
        y += bar.height + pad // 2

        today = snap.today
        lines = [
            f"Today: {today.tools} tools · {today.prompts} prompts · {today.stops} tasks done",
            f"Sessions: {len(snap.sessions)} live · {today.sessions} today · {snap.events_seen} events",
            f"Character: {self.pack.name if self.pack else 'Clawd (built-in)'}",
        ]
        for line in lines:
            label = f.render(line, True, FG)
            self.canvas.blit(label, (pad, y))
            y += int(f.get_height() * 1.3)

        # Busiest-hours mini chart.
        peak = max(snap.hour_hist) or 1
        chart_h = max(16, int(self.unit * 0.1))
        chart_w = min(self.w - pad * 2, int(self.w * 0.55))
        bw = chart_w // 24
        base = y + chart_h
        for hour, count in enumerate(snap.hour_hist):
            hgt = max(2, int(chart_h * count / peak)) if count else 2
            color = ACCENT if count == peak and count else (90, 88, 105)
            pygame.draw.rect(
                self.canvas, color, (pad + hour * bw, base - hgt, max(2, bw - 2), hgt)
            )
        y = base + int(f.get_height() * 0.5)
        cap = f.render("activity by hour", True, MUTED)
        self.canvas.blit(cap, (pad, y))
        y += int(f.get_height() * 1.5)

        addr = _local_addresses()
        url = f"http://{addr[0]}:{self.config.port}" if addr else ""
        for line in ("Pair a laptop / open phone remote:", f"  {url}"):
            label = f.render(line, True, GOOD if line.startswith("  ") else FG)
            self.canvas.blit(label, (pad, y))
            y += int(f.get_height() * 1.25)

        # QR code for the phone page, right-hand side if it fits.
        if url:
            self._blit_qr(url, pad)

    def _blit_qr(self, url: str, pad: int) -> None:
        target = url
        token = getattr(self.config, "token", "")
        if token:
            target = f"{url}/?token={token}"
        if self._qr_cache is None or self._qr_cache[0] != target:
            matrix = _make_qr(target)
            if matrix is None:
                return
            n = len(matrix)
            scale = max(2, int(self.unit * 0.35) // n)
            quiet = scale * 2
            size = n * scale + quiet * 2
            surf = pygame.Surface((size, size))
            surf.fill(FG)
            for r, row in enumerate(matrix):
                for c, val in enumerate(row):
                    if val:
                        pygame.draw.rect(
                            surf, (0, 0, 0),
                            (quiet + c * scale, quiet + r * scale, scale, scale),
                        )
            self._qr_cache = (target, surf)
        surf = self._qr_cache[1]
        x = self.w - surf.get_width() - pad
        y = self.stage.bottom - surf.get_height() - pad // 2
        if x > self.w * 0.55:  # only when there's room next to the text
            self.canvas.blit(surf, (x, y))

    def _draw_screen_dots(self) -> None:
        r = max(3, int(self.unit * 0.012))
        gap = r * 5
        total = gap * (len(SCREENS) - 1)
        x = self.w // 2 - total // 2
        cy = self.h - self.footer_h // 2
        for i in range(len(SCREENS)):
            color = FG if i == self.screen_index else (90, 88, 105)
            pygame.draw.circle(self.canvas, color, (x + i * gap, cy), r)

    # ------------------------------------------------------------------
    # Ambient clock (sleep + fully dimmed)
    # ------------------------------------------------------------------

    def _draw_clock(self) -> None:
        now = datetime.datetime.now()
        f_time = self.font(int(self.unit * 0.28))
        f_date = self.font(int(self.unit * 0.07))
        dim_fg = (110, 108, 125)
        clock = f_time.render(now.strftime("%H:%M"), True, dim_fg)
        self.canvas.blit(clock, clock.get_rect(center=(self.w // 2, int(self.h * 0.42))))
        date_line = now.strftime("%A %d %B")
        if self.weather_text:
            date_line += f"  ·  {self.weather_text}"
        date = f_date.render(date_line, True, (80, 78, 92))
        self.canvas.blit(date, date.get_rect(center=(self.w // 2, int(self.h * 0.62))))

    # ------------------------------------------------------------------
    # Sessions overlay
    # ------------------------------------------------------------------

    def _draw_sessions(self, snap: Snapshot) -> None:
        veil = pygame.Surface((self.w, self.h))
        veil.fill((10, 8, 14))
        veil.set_alpha(210)
        self.canvas.blit(veil, (0, 0))

        pad = int(self.unit * 0.05)
        f_head = self.font(int(self.unit * 0.07))
        f = self.font(int(self.unit * 0.05))
        f_small = self.font(int(self.unit * 0.042))
        y = pad
        head = f_head.render(f"{len(snap.sessions)} active session(s)", True, FG)
        self.canvas.blit(head, (pad, y))
        y += head.get_height() + pad

        now = time.monotonic()
        card_h = int(f.get_height() * 1.4 + f_small.get_height() * 2.6)
        for sess in snap.sessions[:6]:
            mood = session_mood(sess, now)
            color = ATTN if mood == st.ATTENTION else (120, 180, 240) if mood == st.BUSY else MUTED
            card = pygame.Rect(pad, y, self.w - pad * 2, card_h)
            pygame.draw.rect(self.canvas, BG_PANEL, card, border_radius=pad // 2)
            pygame.draw.circle(
                self.canvas, color, (card.left + pad, card.top + card_h // 2), max(5, int(self.unit * 0.015))
            )
            tx = card.left + pad * 2
            name = sess.cwd or sess.session_id
            mins = int((now - sess.started) / 60) if sess.started else 0
            self.canvas.blit(
                f.render(f"{name}   ·   {mood} · {mins}m", True, FG), (tx, card.top + int(f.get_height() * 0.3))
            )
            detail = []
            if sess.last_tool:
                detail.append(f"last tool: {sess.last_tool}")
            if sess.last_prompt:
                detail.append(f'"{sess.last_prompt[:80]}"')
            self.canvas.blit(
                f_small.render("   ".join(detail), True, MUTED),
                (tx, card.top + int(f.get_height() * 1.5)),
            )
            y += card_h + pad // 2
        hint = f_small.render("tap anywhere to close", True, MUTED)
        self.canvas.blit(hint, hint.get_rect(center=(self.w // 2, self.h - pad)))

    # ------------------------------------------------------------------
    # Settings overlay (long-press)
    # ------------------------------------------------------------------

    def _draw_settings(self) -> None:
        veil = pygame.Surface((self.w, self.h))
        veil.fill((10, 8, 14))
        veil.set_alpha(210)
        self.canvas.blit(veil, (0, 0))

        pad = int(self.unit * 0.05)
        items = [
            ("sound", f"Sound: {'on' if self.sounds.enabled else 'off'}"),
            ("grid", f"Buddy grid: {'on' if self.grid_enabled else 'off'}"),
            ("dim", "Dim screen now"),
            ("reset", "Reset stats"),
            ("exit", "Exit PiBuddy"),
            ("close", "Close menu"),
        ]
        btn_h = max(44, int((self.h - pad * (len(items) + 1)) / len(items)))
        btn_w = min(self.w - pad * 2, int(self.unit * 1.4))
        x = self.w // 2 - btn_w // 2
        y = pad
        f = self.font(int(btn_h * 0.45))
        self._settings_rects = []
        for action, label in items:
            rect = pygame.Rect(x, y, btn_w, btn_h)
            color = BAD if action == "exit" else BG_PANEL
            pygame.draw.rect(self.canvas, color, rect, border_radius=btn_h // 5)
            text = f.render(label, True, FG)
            self.canvas.blit(text, text.get_rect(center=rect.center))
            self._settings_rects.append((rect, action))
            y += btn_h + pad // 2

    # ------------------------------------------------------------------
    # Approval overlay
    # ------------------------------------------------------------------

    def _approval_button_rects(self) -> tuple[pygame.Rect, pygame.Rect, pygame.Rect]:
        """Approve / Decide-in-terminal / Deny."""
        btn_h = max(48, int(self.h * 0.18))
        gap = int(self.w * 0.03)
        btn_w = (self.w - gap * 4) // 3
        y = self.h - btn_h - int(self.h * 0.05)
        approve = pygame.Rect(gap, y, btn_w, btn_h)
        terminal = pygame.Rect(gap * 2 + btn_w, y, btn_w, btn_h)
        deny = pygame.Rect(gap * 3 + btn_w * 2, y, btn_w, btn_h)
        return approve, terminal, deny

    def _draw_approval(self, snap: Snapshot) -> None:
        veil = pygame.Surface((self.w, self.h))
        veil.fill((10, 8, 14))
        veil.set_alpha(245)
        self.canvas.blit(veil, (0, 0))

        total = len(snap.approvals)
        req = snap.approvals[self.approval_index % total]
        pad = int(self.unit * 0.045)
        f_small = self.font(int(self.unit * 0.05))
        f_tool = self.font(int(self.unit * 0.09))
        f_body = self.font(int(self.unit * 0.055))

        approve_rect, _, _ = self._approval_button_rects()
        bottom = approve_rect.top - pad
        y = pad

        heading = "Claude wants to use:"
        if total > 1:
            heading = f"Claude wants to use:   ({self.approval_index % total + 1} of {total} — swipe)"
        head = f_small.render(heading, True, ATTN)
        self.canvas.blit(head, (pad, y))
        y += head.get_height() + pad // 3

        tool_line = req.tool_name
        elapsed = int(time.monotonic() - req.created)
        tool = f_tool.render(tool_line, True, FG)
        self.canvas.blit(tool, (pad, y))
        stamp = f_small.render(
            f"session {req.session_id[:8]} · {elapsed}s", True, MUTED
        )
        self.canvas.blit(
            stamp, (self.w - stamp.get_width() - pad, y + tool.get_height() - stamp.get_height())
        )
        y += tool.get_height() + pad // 3

        # What Claude says this call is for.
        if req.description:
            y = self._wrapped_block(req.description, f_body, ATTN, pad, y, bottom, max_lines=2)

        # The exact command/input, in its own panel.
        if req.detail:
            y = self._panel_block(req.detail, f_body, pad, y, bottom, max_frac=0.34)

        # Options the tool is asking you to choose between (answer in
        # the terminal — the Pi can only allow/deny the call itself).
        for question, options in req.questions:
            y = self._wrapped_block(f"? {question}", f_body, FG, pad, y, bottom, max_lines=2)
            for opt in options[:4]:
                y = self._wrapped_block(f"   ◦ {opt}", f_body, MUTED, pad, y, bottom, max_lines=1)

        # Context: the last thing Claude said before asking.
        if req.context and y < bottom - f_body.get_height() * 2:
            y += pad // 3
            y = self._wrapped_block("Claude's last message:", f_small, MUTED, pad, y, bottom, max_lines=1)
            y = self._wrapped_block(req.context, f_body, (190, 188, 205), pad, y, bottom)

        approve, terminal, deny = self._approval_button_rects()
        f_btn = self.font(int(approve.height * 0.36))
        for rect, color, label, fg in (
            (approve, GOOD, "Approve", (12, 30, 18)),
            (terminal, BG_PANEL, "Terminal…", FG),
            (deny, BAD, "Deny", (35, 12, 12)),
        ):
            pygame.draw.rect(self.canvas, color, rect, border_radius=rect.height // 5)
            text = f_btn.render(label, True, fg)
            self.canvas.blit(text, text.get_rect(center=rect.center))

    def _wrapped_block(
        self, text: str, font: pygame.font.Font, color, pad: int, y: int, bottom: int,
        max_lines: int = 99,
    ) -> int:
        for i, line in enumerate(_wrap(text, font, self.w - pad * 2)):
            if i >= max_lines or y + font.get_height() > bottom:
                break
            self.canvas.blit(font.render(line, True, color), (pad, y))
            y += int(font.get_height() * 1.12)
        return y

    def _panel_block(
        self, text: str, font: pygame.font.Font, pad: int, y: int, bottom: int, max_frac: float
    ) -> int:
        lines = _wrap(text, font, self.w - pad * 3)
        line_h = int(font.get_height() * 1.15)
        max_h = min(int(self.h * max_frac), bottom - y - pad // 2)
        n = max(1, min(len(lines), max_h // line_h))
        if n <= 0 or y >= bottom:
            return y
        panel = pygame.Rect(pad, y, self.w - pad * 2, n * line_h + pad // 2)
        pygame.draw.rect(self.canvas, BG_PANEL, panel, border_radius=pad // 3)
        ty = y + pad // 4
        for line in lines[:n]:
            if n < len(lines) and line is lines[n - 1]:
                line = line[: max(1, len(line) - 1)] + "…"
            self.canvas.blit(font.render(line, True, FG), (pad + pad // 2, ty))
            ty += line_h
        return panel.bottom + pad // 2


def _wrap(text: str, font: pygame.font.Font, width: int) -> list[str]:
    words = text.replace("\n", " ").split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if font.size(trial)[0] <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        # Hard-break words longer than the line.
        while font.size(word)[0] > width and len(word) > 1:
            cut = max(1, int(len(word) * width / max(1, font.size(word)[0])))
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines
