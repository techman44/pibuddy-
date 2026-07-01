"""GIF character pack support, compatible with anthropics/claude-desktop-buddy.

A pack is a folder of per-state GIFs (the upstream format):

    my-pet/
      manifest.json          (optional: name, background color)
      sleep.gif
      idle.gif  or  idle_0.gif, idle_1.gif, ...
      busy.gif
      attention.gif
      celebrate.gif
      dizzy.gif
      heart.gif

Frames are decoded once with Pillow and scaled to the stage at render
time. Upstream packs are 96px sprites, so we scale with nearest-neighbor
to keep the pixel-art look instead of blurring.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pygame
from PIL import Image, ImageSequence

from . import state as st

log = logging.getLogger("pibuddy.characters")

STATES = (st.SLEEP, st.IDLE, st.BUSY, st.ATTENTION, st.CELEBRATE, st.DIZZY, st.HEART)
# Which state to substitute when a pack doesn't include a GIF for one.
FALLBACKS = {
    st.HEART: st.CELEBRATE,
    st.DIZZY: st.BUSY,
    st.CELEBRATE: st.IDLE,
    st.ATTENTION: st.BUSY,
    st.BUSY: st.IDLE,
    st.SLEEP: st.IDLE,
}


class Animation:
    def __init__(self, frames: list[pygame.Surface], durations: list[float]) -> None:
        self.frames = frames
        self.durations = durations
        self.total = sum(durations) or 1.0

    def frame_at(self, t: float) -> pygame.Surface:
        t %= self.total
        for frame, dur in zip(self.frames, self.durations):
            if t < dur:
                return frame
            t -= dur
        return self.frames[-1]


class CharacterPack:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.name = path.name
        self.animations: dict[str, Animation] = {}
        self._scaled: dict[tuple[str, int], Animation] = {}

        manifest = path / "manifest.json"
        if manifest.exists():
            try:
                meta = json.loads(manifest.read_text())
                self.name = meta.get("name", self.name)
            except (json.JSONDecodeError, OSError):
                log.warning("ignoring unreadable manifest in %s", path)

        for state_name in STATES:
            gifs = sorted(path.glob(f"{state_name}*.gif"))
            if gifs:
                anim = _load_gif(gifs[0])
                if anim:
                    self.animations[state_name] = anim
        if not self.animations:
            raise ValueError(f"no usable GIFs found in {path}")

    def animation_for(self, mood: str) -> Animation:
        seen = set()
        while mood not in self.animations and mood not in seen:
            seen.add(mood)
            mood = FALLBACKS.get(mood, st.IDLE)
        return self.animations.get(mood) or next(iter(self.animations.values()))

    def draw(self, surface: pygame.Surface, rect: pygame.Rect, mood: str, t: float) -> None:
        anim = self.animation_for(mood)
        base = anim.frame_at(t)
        target_h = int(rect.height * 0.85)
        key = (mood, target_h)
        scaled = self._scaled.get(key)
        if scaled is None:
            scale = target_h / max(1, base.get_height())
            frames = [
                pygame.transform.scale_by(f, scale)  # nearest-neighbor for pixel art
                for f in anim.frames
            ]
            scaled = Animation(frames, anim.durations)
            self._scaled[key] = scaled
        frame = scaled.frame_at(t)
        pos = frame.get_rect(center=rect.center)
        surface.blit(frame, pos)


def _load_gif(path: Path) -> Animation | None:
    try:
        image = Image.open(path)
    except OSError:
        log.warning("cannot open %s", path)
        return None
    frames: list[pygame.Surface] = []
    durations: list[float] = []
    for frame in ImageSequence.Iterator(image):
        rgba = frame.convert("RGBA")
        surf = pygame.image.frombytes(rgba.tobytes(), rgba.size, "RGBA")
        frames.append(surf)
        durations.append(max(0.02, frame.info.get("duration", 100) / 1000.0))
    if not frames:
        return None
    return Animation(frames, durations)


def load_pack(path: str | Path) -> CharacterPack | None:
    p = Path(path).expanduser()
    if not p.is_dir():
        log.warning("character pack %s is not a directory", p)
        return None
    try:
        return CharacterPack(p)
    except ValueError as exc:
        log.warning("%s", exc)
        return None
