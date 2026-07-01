"""Headless render smoke tests: draw every mood and screen at several
resolutions/rotations and make sure nothing crashes and pixels change."""

import os

import pytest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import pygame  # noqa: E402

from pibuddy import buddy, state as st  # noqa: E402
from pibuddy.config import Config  # noqa: E402
from pibuddy.display import Display, _wrap  # noqa: E402
from pibuddy.state import StateStore  # noqa: E402

MOODS = (st.SLEEP, st.IDLE, st.BUSY, st.ATTENTION, st.CELEBRATE, st.DIZZY, st.HEART)
# 3.5" hat, official 7", 10" panel, tiny square, portrait
SIZES = ((480, 320), (800, 480), (1280, 800), (240, 240), (320, 480))


@pytest.fixture(autouse=True)
def pygame_session():
    pygame.init()
    yield
    pygame.quit()


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("mood", MOODS)
def test_vector_buddy_draws_at_every_size(size, mood):
    surface = pygame.Surface(size)
    surface.fill((0, 0, 0))
    for t in (0.0, 1.3, 3.9):
        buddy.draw(surface, surface.get_rect(), mood, t)
    assert pygame.transform.average_color(surface)[:3] != (0, 0, 0)


def make_display(size, rotate=0):
    store = StateStore()
    config = Config(fullscreen=False, width=size[0], height=size[1], rotate=rotate)
    d = Display(store, config)
    d._open_window()
    return d, store


@pytest.mark.parametrize("size", SIZES)
def test_all_screens_render(size):
    d, store = make_display(size)
    store.apply_event({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "hello"})
    store.apply_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        }
    )
    for index in range(3):
        d.screen_index = index
        d._draw(store.snapshot())


@pytest.mark.parametrize("size", ((800, 480), (1280, 800)))
def test_approval_overlay_and_touch(size):
    d, store = make_display(size)
    req = store.add_approval(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf build && make all VERY_LONG_FLAG=" + "x" * 200},
        }
    )
    d._draw(store.snapshot())
    approve_rect, terminal_rect, deny_rect = d._approval_button_rects()
    assert d._hit_approval_buttons(*approve_rect.center)
    assert req.decision == "allow"
    # Buttons are big enough for fingers (>= 48px).
    assert approve_rect.height >= 48
    assert not approve_rect.colliderect(terminal_rect)
    assert not terminal_rect.colliderect(deny_rect)


@pytest.mark.parametrize("rotate", (0, 90, 180, 270))
def test_rotation_roundtrip(rotate):
    d, _ = make_display((800, 480), rotate=rotate)
    # A point in physical space must land inside the logical canvas.
    pw, ph = d.physical.get_size()
    for px, py in ((0, 0), (pw - 1, 0), (0, ph - 1), (pw - 1, ph - 1), (pw // 2, ph // 2)):
        lx, ly = d._to_logical(px, py)
        assert 0 <= lx <= d.w - 1
        assert 0 <= ly <= d.h - 1
    # Corners map to distinct corners (it's a bijection on corners).
    corners = {d._to_logical(px, py) for px, py in ((0, 0), (pw - 1, 0), (0, ph - 1), (pw - 1, ph - 1))}
    assert len(corners) == 4
    d._draw(d.store.snapshot())


def test_wrap_never_loses_text():
    pygame.display.set_mode((100, 100))
    font = pygame.font.Font(None, 20)
    text = "one two three supercalifragilisticexpialidocious" * 3
    lines = _wrap(text, font, 120)
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")
