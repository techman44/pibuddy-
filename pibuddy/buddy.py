"""The built-in vector buddy: Clawd, the square orange fellow.

Modeled on the Claude mascot the desktop buddy uses — an orange rounded
square with two vertical bar eyes and stubby feet. Everything is drawn
with pygame primitives and scaled from the rect it is given, so the same
character looks right on a 240px SPI display and a 10-inch 1280x800
panel. GIF character packs (see characters.py) can replace this renderer
entirely.
"""

from __future__ import annotations

import math

import pygame

from . import state as st

# Anthropic's signature orange.
BODY = (217, 119, 87)
BODY_DARK = (181, 92, 63)
EYE = (26, 22, 20)
WHITE = (250, 248, 244)

BODY_TINTS = {
    st.SLEEP: (150, 110, 100),
    st.ATTENTION: (235, 140, 70),
    st.DIZZY: (205, 125, 130),
}

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
    """Draw Clawd in `mood` at animation time `t` (seconds), inside rect."""
    u = min(rect.width, rect.height)  # scale unit
    cx = rect.centerx
    cy = rect.centery + int(u * 0.04)

    side = int(u * 0.52)  # Clawd is a square
    foot_h = max(4, int(u * 0.05))

    # Per-mood body motion.
    dy = 0
    tilt = 0
    breathe = 1.0 + 0.012 * math.sin(t * 2.2)
    if mood == st.ATTENTION:
        dy = -abs(int(_wobble(t, 9, u * 0.035)))
    elif mood == st.CELEBRATE:
        dy = -abs(int(_wobble(t, 7, u * 0.055)))
    elif mood == st.HEART:
        dy = -abs(int(_wobble(t, 5, u * 0.02)))
    elif mood == st.DIZZY:
        tilt = int(_wobble(t, 10, u * 0.04))
    elif mood == st.SLEEP:
        breathe = 1.0 + 0.028 * math.sin(t * 1.1)
    elif mood == st.BUSY:
        tilt = int(_wobble(t, 5, u * 0.012))

    bw = int(side * breathe)
    bh = int(side * (2 - breathe))
    body = pygame.Rect(0, 0, bw, bh)
    body.center = (cx + tilt, cy + dy)
    radius = max(4, int(side * 0.22))

    # Shadow.
    shadow = pygame.Rect(0, 0, int(bw * 0.9), max(3, int(u * 0.045)))
    shadow.center = (cx, cy + side // 2 + foot_h + int(u * 0.05))
    pygame.draw.ellipse(surface, (60, 55, 70), shadow)

    color = BODY_TINTS.get(mood, BODY)

    # Feet: two stubs that shuffle when excited, tucked away when asleep.
    if mood != st.SLEEP:
        step = _wobble(t, 8, foot_h * 0.6) if mood in (st.CELEBRATE, st.BUSY) else 0
        for i, fx in enumerate((-bw // 4, bw // 4)):
            lift = int(step) if i == 0 else -int(step)
            foot = pygame.Rect(0, 0, max(6, int(bw * 0.2)), foot_h * 2)
            foot.center = (body.centerx + fx, body.bottom + foot_h // 2 - max(0, lift))
            pygame.draw.rect(surface, BODY_DARK, foot, border_radius=foot_h)

    # Body.
    pygame.draw.rect(surface, color, body, border_radius=radius)

    eye_y = body.centery - int(bh * 0.08)
    eye_dx = int(bw * 0.19)
    eye_w = max(3, int(side * 0.09))
    eye_h = max(6, int(side * 0.26))
    lw = max(2, u // 100)

    if mood == st.SLEEP:
        _closed_eyes(surface, body, eye_dx, eye_y, eye_w, lw)
        _zzz(surface, body, u, t)
    elif mood == st.DIZZY:
        _spiral_eyes(surface, body, eye_dx, eye_y, eye_h, lw, t)
    elif mood == st.CELEBRATE:
        _happy_eyes(surface, body, eye_dx, eye_y, eye_h, lw)
        _confetti(surface, rect, u, t)
    elif mood == st.HEART:
        _happy_eyes(surface, body, eye_dx, eye_y, eye_h, lw)
        _hearts(surface, body, u, t)
    elif mood == st.ATTENTION:
        _bar_eyes(surface, body, eye_dx, eye_y, int(eye_w * 1.4), int(eye_h * 1.15), t)
        _exclaim(surface, body, u, t)
    elif mood == st.BUSY:
        _bar_eyes(surface, body, eye_dx, eye_y, eye_w, int(eye_h * 0.75), t, dart=True)
        _sweat(surface, body, u, t)
    else:  # idle
        _bar_eyes(surface, body, eye_dx, eye_y, eye_w, eye_h, t)


def _bar_eyes(surface, body, dx, y, w, h, t, dart=False):
    """Clawd's signature vertical bar eyes (blink every ~4s)."""
    if (t % 4.0) > 3.85:
        lw = max(2, w // 2)
        for sx in (-dx, dx):
            cx = body.centerx + sx
            pygame.draw.line(surface, EYE, (cx - w, y), (cx + w, y), lw)
        return
    px = int(math.sin(t * (6 if dart else 0.7)) * w * (1.2 if dart else 0.8))
    for sx in (-dx, dx):
        eye = pygame.Rect(0, 0, w, h)
        eye.center = (body.centerx + sx + px, y)
        pygame.draw.rect(surface, EYE, eye, border_radius=w // 2)


def _happy_eyes(surface, body, dx, y, h, lw):
    r = int(h * 0.55)
    for sx in (-dx, dx):
        c = (body.centerx + sx, y + r // 2)
        rect = pygame.Rect(0, 0, r * 2, r * 2)
        rect.center = c
        pygame.draw.arc(surface, EYE, rect, math.radians(20), math.radians(160), lw + 2)


def _closed_eyes(surface, body, dx, y, w, lw):
    for sx in (-dx, dx):
        c = (body.centerx + sx, y - w)
        rect = pygame.Rect(0, 0, w * 3, w * 3)
        rect.center = c
        pygame.draw.arc(surface, EYE, rect, math.radians(200), math.radians(340), lw + 1)


def _spiral_eyes(surface, body, dx, y, h, lw, t):
    r = int(h * 0.5)
    for i, sx in enumerate((-dx, dx)):
        c = (body.centerx + sx, y)
        start = t * 6 + i * math.pi
        for k in range(3):
            rr = max(2, int(r * (0.4 + 0.35 * k)))
            rect = pygame.Rect(0, 0, rr * 2, rr * 2)
            rect.center = c
            a = start + k * 1.2
            pygame.draw.arc(surface, EYE, rect, a, a + 4.2, lw)


def _zzz(surface, body, u, t):
    for i in range(3):
        phase = (t * 0.4 + i * 0.33) % 1.0
        size = 0.6 + 0.4 * (i / 2)
        x = body.right - int(u * 0.02) + int(phase * u * 0.12) + i * int(u * 0.05)
        y = body.top - int(phase * u * 0.18) - i * int(u * 0.04)
        glyph = pygame.font.Font(None, max(12, int(u * 0.11 * size))).render("z", True, (200, 205, 225))
        surface.blit(glyph, (x, y))


def _exclaim(surface, body, u, t):
    if int(t * 3) % 2 == 0:
        font = pygame.font.Font(None, max(18, int(u * 0.22)))
        glyph = font.render("!", True, (230, 70, 60))
        surface.blit(glyph, (body.centerx - glyph.get_width() // 2, body.top - int(u * 0.24)))


def _sweat(surface, body, u, t):
    phase = (t * 0.9) % 1.0
    r = max(2, int(u * 0.025))
    x = body.right - int(u * 0.02)
    y = body.top + int(u * 0.06) + int(phase * u * 0.12)
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
