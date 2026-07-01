"""Resolution-independent, touch-first UI.

Three swipeable screens (pet / activity feed / stats) plus a modal
approval overlay. Every dimension is derived from the actual display
size at startup, so the same code runs on a 3.5" 480x320 SPI hat and a
10" 1280x800 DSI panel, portrait or landscape (see --rotate).

Touch handling: SDL reports touchscreens both as FINGER* events and as
synthesized mouse events; we listen to FINGER* plus real-mouse-only
events so a desktop mouse works in development windows too.
"""

from __future__ import annotations

import datetime
import logging
import math
import socket
import time

import pygame

from . import buddy as vector_buddy
from . import state as st
from .characters import CharacterPack
from .state import StateStore, Snapshot

log = logging.getLogger("pibuddy.display")

BG = (28, 26, 34)
BG_PANEL = (40, 38, 50)
FG = (235, 232, 240)
MUTED = (150, 148, 165)
ACCENT = (240, 160, 90)
GOOD = (95, 190, 120)
BAD = (225, 95, 85)
ATTN = (250, 190, 60)

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


class Display:
    def __init__(self, store: StateStore, config) -> None:
        self.store = store
        self.config = config
        self.pack: CharacterPack | None = None
        self.screen_index = 0
        self.feed_scroll = 0.0
        self._fonts: dict[int, pygame.font.Font] = {}
        self._pointer_down: tuple[float, float, float] | None = None  # x, y, t
        self._drag_last_y: float | None = None
        self._recent_taps: list[float] = []
        self._last_interaction = time.monotonic()
        self._started = time.monotonic()

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

        clock = pygame.time.Clock()
        running = True
        while running:
            snap = self.store.snapshot()
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

            self._draw(snap)
            if self.rotate:
                rotated = pygame.transform.rotate(self.canvas, self.rotate)
                self.physical.blit(rotated, (0, 0))
            pygame.display.flip()
            clock.tick(self.config.fps)
        pygame.quit()

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
            self._drag_last_y = y
            return

        if is_motion and self._drag_last_y is not None:
            if SCREENS[self.screen_index] == "feed" and snap.approval is None:
                self.feed_scroll -= y - self._drag_last_y
            self._drag_last_y = y
            return

        if not is_up or self._pointer_down is None:
            return

        x0, y0, t0 = self._pointer_down
        self._pointer_down = None
        self._drag_last_y = None
        dx, dy = x - x0, y - y0

        if snap.approval is not None:
            if self._hit_approval_buttons(x, y):
                return

        moved = math.hypot(dx, dy)
        if moved <= max(8, self.unit * TAP_MAX_PX_FRACTION) and now - t0 <= TAP_MAX_SECS:
            self._on_tap(x, y, now, snap)
            return
        if abs(dx) > self.w * SWIPE_FRACTION and abs(dx) > abs(dy) and snap.approval is None:
            if dx < 0:
                self.screen_index = min(self.screen_index + 1, len(SCREENS) - 1)
            else:
                self.screen_index = max(self.screen_index - 1, 0)

    def _on_tap(self, x: float, y: float, now: float, snap: Snapshot) -> None:
        if SCREENS[self.screen_index] == "pet" and snap.approval is None:
            # Triple-tap easter egg: make the buddy dizzy.
            self._recent_taps = [t for t in self._recent_taps if now - t < 1.0]
            self._recent_taps.append(now)
            if len(self._recent_taps) >= 3:
                self._recent_taps.clear()
                self.store.trigger_dizzy()

    def _hit_approval_buttons(self, x: float, y: float) -> bool:
        approve, deny = self._approval_button_rects()
        if approve.collidepoint(x, y):
            self.store.resolve_current_approval("allow")
            return True
        if deny.collidepoint(x, y):
            self.store.resolve_current_approval("deny")
            return True
        return False

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
        if snap.approval is not None:
            self._draw_approval(snap)
        else:
            dim = self._dim_level()
            if dim > 0 and snap.mood in (st.SLEEP, st.IDLE):
                veil = pygame.Surface((self.w, self.h))
                veil.fill((0, 0, 0))
                veil.set_alpha(int(210 * dim))
                self.canvas.blit(veil, (0, 0))

    def _draw_header(self, snap: Snapshot) -> None:
        pygame.draw.rect(self.canvas, BG_PANEL, (0, 0, self.w, self.header_h))
        f = self.font(int(self.header_h * 0.55))
        title = f.render(f"PiBuddy · Lv {snap.level}", True, FG)
        self.canvas.blit(
            title, (int(self.unit * 0.03), (self.header_h - title.get_height()) // 2)
        )
        # One dot per session, colored by its state.
        r = max(4, int(self.header_h * 0.16))
        x = self.w - int(self.unit * 0.03) - r
        now = time.monotonic()
        for sess in snap.sessions[:10]:
            if sess.needs_attention:
                color = ATTN
            elif now < sess.busy_until:
                color = (120, 180, 240)
            else:
                color = MUTED
            pygame.draw.circle(self.canvas, color, (x, self.header_h // 2), r)
            x -= r * 3

    def _draw_pet(self, snap: Snapshot) -> None:
        t = time.monotonic() - self._started
        if self.pack:
            self.pack.draw(self.canvas, self.stage, snap.mood, t)
        else:
            vector_buddy.draw(self.canvas, self.stage, snap.mood, t)

        caption = MOOD_CAPTIONS.get(snap.mood, "")
        n = len(snap.sessions)
        if n and snap.mood in (st.IDLE, st.BUSY):
            caption = f"{n} session{'s' if n != 1 else ''} · {caption}"
        if snap.approvals_waiting > 1:
            caption = f"{snap.approvals_waiting} approvals waiting"
        f = self.font(int(self.unit * 0.06))
        text = f.render(caption, True, MUTED)
        self.canvas.blit(
            text,
            (self.w // 2 - text.get_width() // 2, self.stage.bottom - text.get_height()),
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
        f_big = self.font(int(self.unit * 0.14))
        f = self.font(int(self.unit * 0.06))
        y = self.stage.top + pad

        level = f_big.render(f"Level {snap.level}", True, ACCENT)
        self.canvas.blit(level, (pad, y))
        y += level.get_height() + pad // 2

        # XP progress bar toward the next level.
        into = snap.xp % st.XP_PER_LEVEL
        bar = pygame.Rect(pad, y, self.w - pad * 2, max(10, int(self.unit * 0.04)))
        pygame.draw.rect(self.canvas, BG_PANEL, bar, border_radius=bar.height // 2)
        fill = bar.copy()
        fill.width = max(bar.height, int(bar.width * into / st.XP_PER_LEVEL))
        pygame.draw.rect(self.canvas, ACCENT, fill, border_radius=bar.height // 2)
        y += bar.height + pad // 2
        xp_label = f.render(f"{into} / {st.XP_PER_LEVEL} xp", True, MUTED)
        self.canvas.blit(xp_label, (pad, y))
        y += xp_label.get_height() + pad

        uptime = int(time.monotonic() - self._started)
        lines = [
            f"Sessions: {len(snap.sessions)}",
            f"Events seen: {snap.events_seen}",
            f"Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m",
            f"Character: {self.pack.name if self.pack else 'Pip (built-in)'}",
            "",
            "Send hooks to:",
        ]
        lines += [
            f"  http://{addr}:{self.config.port}" for addr in _local_addresses()
        ]
        for line in lines:
            if y > self.stage.bottom - f.get_height():
                break
            label = f.render(line, True, FG if not line.startswith("  ") else GOOD)
            self.canvas.blit(label, (pad, y))
            y += int(f.get_height() * 1.25)

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
    # Approval overlay
    # ------------------------------------------------------------------

    def _approval_button_rects(self) -> tuple[pygame.Rect, pygame.Rect]:
        btn_h = max(48, int(self.h * 0.2))
        btn_w = int(self.w * 0.4)
        gap = int(self.w * 0.06)
        y = self.h - btn_h - int(self.h * 0.06)
        approve = pygame.Rect(self.w // 2 - btn_w - gap // 2, y, btn_w, btn_h)
        deny = pygame.Rect(self.w // 2 + gap // 2, y, btn_w, btn_h)
        return approve, deny

    def _draw_approval(self, snap: Snapshot) -> None:
        veil = pygame.Surface((self.w, self.h))
        veil.fill((10, 8, 14))
        veil.set_alpha(235)
        self.canvas.blit(veil, (0, 0))

        req = snap.approval
        pad = int(self.unit * 0.05)
        f_small = self.font(int(self.unit * 0.055))
        f_tool = self.font(int(self.unit * 0.1))
        f_detail = self.font(int(self.unit * 0.06))

        y = pad
        head = f_small.render("Claude wants to use:", True, ATTN)
        self.canvas.blit(head, (pad, y))
        y += head.get_height() + pad // 2

        tool = f_tool.render(req.tool_name, True, FG)
        self.canvas.blit(tool, (pad, y))
        y += tool.get_height() + pad // 2

        approve_rect, _ = self._approval_button_rects()
        detail_bottom = approve_rect.top - pad
        for line in _wrap(req.detail, f_detail, self.w - pad * 2):
            if y + f_detail.get_height() > detail_bottom:
                break
            label = f_detail.render(line, True, MUTED)
            self.canvas.blit(label, (pad, y))
            y += int(f_detail.get_height() * 1.15)

        elapsed = int(time.monotonic() - req.created)
        stamp = f_small.render(
            f"session {req.session_id[:8]} · waiting {elapsed}s", True, MUTED
        )
        self.canvas.blit(stamp, (pad, detail_bottom - stamp.get_height()))

        approve, deny = self._approval_button_rects()
        f_btn = self.font(int(approve.height * 0.42))
        pygame.draw.rect(self.canvas, GOOD, approve, border_radius=approve.height // 5)
        pygame.draw.rect(self.canvas, BAD, deny, border_radius=deny.height // 5)
        a_label = f_btn.render("Approve", True, (12, 30, 18))
        d_label = f_btn.render("Deny", True, (35, 12, 12))
        self.canvas.blit(a_label, a_label.get_rect(center=approve.center))
        self.canvas.blit(d_label, d_label.get_rect(center=deny.center))


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
