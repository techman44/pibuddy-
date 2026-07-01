"""The built-in vector buddy, "Pip".

Everything is drawn with pygame primitives and scaled from the rect it is
given, so the same character looks right on a 240px SPI display and a
10-inch 1280x800 panel. GIF character packs (see characters.py) can
replace this renderer entirely.
"""

from __future__ import annotations

import math

import pygame

from . import state as st

BODY_COLORS = {
    st.SLEEP: (110, 120, 150),
    st.IDLE: (240, 160, 90),
    st.BUSY: (240, 160, 90),
    st.ATTENTION: (250, 190, 60),
    st.CELEBRATE: (250, 140, 120),
    st.DIZZY: (170, 150, 220),
    st.HEART: (245, 130, 150),
}
OUTLINE = (40, 35, 45)
EYE = (40, 35, 45)
WHITE = (250, 248, 244)

CONFETTI = [
    (240, 100, 100),
    (100, 180, 240),
    (250, 210, 90),
    (140, 220, 140),
    (200, 140, 240),
]


def _wobble(t: float, speed: float, amount: float) -> float:
    return math.sin(t * speed) * amount


def draw(surface: pygame.Surface, rect: pygame.Rect, mood: str, t: float) -> None:
    """Draw the buddy in `mood` at animation time `t` (seconds), inside rect."""
    u = min(rect.width, rect.height)  # scale unit
    cx = rect.centerx
    cy = rect.centery + int(u * 0.06)

    body_w = int(u * 0.62)
    body_h = int(u * 0.54)

    # Per-mood body motion.
    dy = 0
    tilt_x = 0
    breathe = 1.0 + 0.015 * math.sin(t * 2.2)
    if mood == st.ATTENTION:
        dy = -abs(int(_wobble(t, 9, u * 0.03)))
    elif mood == st.CELEBRATE:
        dy = -abs(int(_wobble(t, 7, u * 0.05)))
    elif mood == st.DIZZY:
        tilt_x = int(_wobble(t, 10, u * 0.04))
    elif mood == st.SLEEP:
        breathe = 1.0 + 0.03 * math.sin(t * 1.1)
    elif mood == st.BUSY:
        tilt_x = int(_wobble(t, 5, u * 0.015))

    bw = int(body_w * breathe)
    bh = int(body_h * (2 - breathe))
    body = pygame.Rect(0, 0, bw, bh)
    body.center = (cx + tilt_x, cy + dy)

    # Shadow.
    shadow = pygame.Rect(0, 0, int(bw * 0.8), max(3, int(u * 0.05)))
    shadow.center = (cx, cy + body_h // 2 + int(u * 0.08))
    pygame.draw.ellipse(surface, (0, 0, 0), shadow.inflate(2, 2), width=0)
    pygame.draw.ellipse(surface, (60, 55, 70), shadow)

    # Body.
    color = BODY_COLORS.get(mood, BODY_COLORS[st.IDLE])
    pygame.draw.ellipse(surface, color, body)
    pygame.draw.ellipse(surface, OUTLINE, body, width=max(2, u // 90))

    eye_y = body.centery - int(bh * 0.12)
    eye_dx = int(bw * 0.18)
    eye_r = max(3, int(u * 0.045))
    lw = max(2, u // 100)

    if mood == st.SLEEP:
        _closed_eyes(surface, body, eye_dx, eye_y, eye_r, lw)
        _zzz(surface, body, u, t)
    elif mood == st.DIZZY:
        _spiral_eyes(surface, body, eye_dx, eye_y, eye_r, lw, t)
        _mouth(surface, body, u, "wavy", t)
    elif mood == st.CELEBRATE:
        _happy_eyes(surface, body, eye_dx, eye_y, eye_r, lw)
        _mouth(surface, body, u, "open", t)
        _confetti(surface, rect, u, t)
    elif mood == st.HEART:
        _happy_eyes(surface, body, eye_dx, eye_y, eye_r, lw)
        _mouth(surface, body, u, "smile", t)
        _hearts(surface, body, u, t)
    elif mood == st.ATTENTION:
        _open_eyes(surface, body, eye_dx, eye_y, int(eye_r * 1.3), t, dart=True)
        _mouth(surface, body, u, "o", t)
        _exclaim(surface, body, u, t)
    elif mood == st.BUSY:
        _open_eyes(surface, body, eye_dx, eye_y, eye_r, t, dart=True)
        _mouth(surface, body, u, "flat", t)
        _sweat(surface, body, u, t)
    else:  # idle
        _open_eyes(surface, body, eye_dx, eye_y, eye_r, t)
        _mouth(surface, body, u, "smile", t)


def _open_eyes(surface, body, dx, y, r, t, dart=False):
    # Blink roughly every 4 seconds.
    if (t % 4.0) > 3.85:
        lw = max(2, r // 3)
        for sx in (-dx, dx):
            pygame.draw.line(surface, EYE, (body.centerx + sx - r, y), (body.centerx + sx + r, y), lw)
        return
    if dart:
        px = int(math.sin(t * 6) * r * 0.5)
    else:
        px = int(math.sin(t * 0.7) * r * 0.4)
    for sx in (-dx, dx):
        pygame.draw.circle(surface, WHITE, (body.centerx + sx, y), int(r * 1.35))
        pygame.draw.circle(surface, EYE, (body.centerx + sx + px, y), r)


def _happy_eyes(surface, body, dx, y, r, lw):
    for sx in (-dx, dx):
        c = (body.centerx + sx, y + r)
        rect = pygame.Rect(0, 0, int(r * 2.6), int(r * 2.6))
        rect.center = c
        pygame.draw.arc(surface, EYE, rect, math.radians(20), math.radians(160), lw + 1)


def _closed_eyes(surface, body, dx, y, r, lw):
    for sx in (-dx, dx):
        c = (body.centerx + sx, y - r // 2)
        rect = pygame.Rect(0, 0, int(r * 2.6), int(r * 2.6))
        rect.center = c
        pygame.draw.arc(surface, EYE, rect, math.radians(200), math.radians(340), lw + 1)


def _spiral_eyes(surface, body, dx, y, r, lw, t):
    for i, sx in enumerate((-dx, dx)):
        c = (body.centerx + sx, y)
        start = t * 6 + i * math.pi
        for k in range(3):
            rr = int(r * (0.4 + 0.35 * k))
            rect = pygame.Rect(0, 0, rr * 2, rr * 2)
            rect.center = c
            a = start + k * 1.2
            pygame.draw.arc(surface, EYE, rect, a, a + 4.2, lw)


def _mouth(surface, body, u, kind, t):
    mx, my = body.centerx, body.centery + int(body.height * 0.18)
    w = int(u * 0.12)
    lw = max(2, u // 100)
    if kind == "smile":
        rect = pygame.Rect(0, 0, w, int(w * 0.8))
        rect.center = (mx, my - w // 4)
        pygame.draw.arc(surface, EYE, rect, math.radians(200), math.radians(340), lw + 1)
    elif kind == "flat":
        pygame.draw.line(surface, EYE, (mx - w // 2, my), (mx + w // 2, my), lw + 1)
    elif kind == "o":
        pygame.draw.circle(surface, EYE, (mx, my), max(3, int(u * 0.03)), lw + 1)
    elif kind == "open":
        rect = pygame.Rect(0, 0, w, int(w * 0.8))
        rect.center = (mx, my)
        pygame.draw.ellipse(surface, EYE, rect)
    elif kind == "wavy":
        pts = [
            (mx - w // 2 + i * w // 6, my + int(math.sin(i * 2 + t * 8) * u * 0.012))
            for i in range(7)
        ]
        pygame.draw.lines(surface, EYE, False, pts, lw + 1)


def _zzz(surface, body, u, t):
    font = pygame.font.Font(None, max(14, int(u * 0.11)))
    for i in range(3):
        phase = (t * 0.4 + i * 0.33) % 1.0
        size = 0.6 + 0.4 * (i / 2)
        x = body.right - int(u * 0.02) + int(phase * u * 0.12) + i * int(u * 0.05)
        y = body.top - int(phase * u * 0.18) - i * int(u * 0.04)
        glyph = pygame.font.Font(None, max(12, int(u * 0.11 * size))).render("z", True, (200, 205, 225))
        surface.blit(glyph, (x, y))
    del font


def _exclaim(surface, body, u, t):
    if int(t * 3) % 2 == 0:
        font = pygame.font.Font(None, max(18, int(u * 0.22)))
        glyph = font.render("!", True, (230, 70, 60))
        surface.blit(glyph, (body.centerx - glyph.get_width() // 2, body.top - int(u * 0.24)))


def _sweat(surface, body, u, t):
    phase = (t * 0.9) % 1.0
    r = max(2, int(u * 0.025))
    x = body.right - int(u * 0.04)
    y = body.top + int(u * 0.08) + int(phase * u * 0.12)
    pygame.draw.circle(surface, (120, 190, 240), (x, y), r)
    pygame.draw.polygon(
        surface,
        (120, 190, 240),
        [(x - r, y), (x + r, y), (x, y - int(r * 2.2))],
    )


def _confetti(surface, rect, u, t):
    for i in range(18):
        # Deterministic pseudo-random trajectory per particle.
        seed = math.sin(i * 12.9898) * 43758.5453
        fx = seed - math.floor(seed)
        seed2 = math.sin(i * 78.233) * 12543.123
        fy = seed2 - math.floor(seed2)
        phase = (t * (0.4 + fy * 0.4) + fx) % 1.0
        x = rect.left + int(fx * rect.width)
        y = rect.top + int(phase * rect.height)
        size = max(2, int(u * (0.012 + fy * 0.015)))
        color = CONFETTI[i % len(CONFETTI)]
        angle = t * 4 + i
        dx = int(math.cos(angle) * size)
        dy = int(math.sin(angle) * size)
        pygame.draw.line(surface, color, (x - dx, y - dy), (x + dx, y + dy), max(2, size // 2))


def _hearts(surface, body, u, t):
    for i in range(4):
        phase = (t * 0.5 + i * 0.25) % 1.0
        x = body.centerx + int(math.sin((phase + i) * 6) * u * 0.14)
        y = body.top - int(phase * u * 0.3)
        s = max(3, int(u * 0.035 * (1.0 - phase * 0.5)))
        _heart_shape(surface, (x, y), s, (235, 80, 110))


def _heart_shape(surface, center, s, color):
    x, y = center
    pygame.draw.circle(surface, color, (x - s // 2, y), s // 2 + 1)
    pygame.draw.circle(surface, color, (x + s // 2, y), s // 2 + 1)
    pygame.draw.polygon(surface, color, [(x - s, y), (x + s, y), (x, y + int(s * 1.4))])
