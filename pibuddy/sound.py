"""Tiny synthesized sound effects (no audio assets needed).

Everything degrades to silence: if the mixer can't initialize (no audio
device) or sound is disabled, every call is a no-op.
"""

from __future__ import annotations

import array
import logging
import math

import pygame

log = logging.getLogger("pibuddy.sound")

RATE = 22050
VOLUME = 0.35


class Sounds:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self._ready = False
        self._cache: dict[str, pygame.mixer.Sound] = {}

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        if self.enabled:
            self.chirp()
        return self.enabled

    # Effects ----------------------------------------------------------

    def chirp(self) -> None:
        """Attention: two rising notes."""
        self._play("chirp", [(880, 70), (1320, 90)])

    def alert(self) -> None:
        """Escalated attention: insistent triple beep."""
        self._play("alert", [(1100, 80), (0, 40), (1100, 80), (0, 40), (1500, 120)])

    def success(self) -> None:
        """Approval given / task done: happy major arpeggio."""
        self._play("success", [(660, 60), (830, 60), (990, 100)])

    def deny(self) -> None:
        """Denied: descending buzz."""
        self._play("deny", [(440, 90), (330, 130)])

    # Internals --------------------------------------------------------

    def _play(self, name: str, notes: list[tuple[int, int]]) -> None:
        if not self.enabled or not self._init():
            return
        sound = self._cache.get(name)
        if sound is None:
            sound = self._synth(notes)
            if sound is None:
                return
            self._cache[name] = sound
        sound.play()

    def _init(self) -> bool:
        if self._ready:
            return True
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(frequency=RATE, size=-16, channels=1)
            self._ready = True
        except pygame.error as exc:
            log.info("no audio output, sounds disabled (%s)", exc)
            self.enabled = False
        return self._ready

    @staticmethod
    def _synth(notes: list[tuple[int, int]]) -> pygame.mixer.Sound | None:
        samples = array.array("h")
        amp = int(32767 * VOLUME)
        for freq, ms in notes:
            n = int(RATE * ms / 1000)
            for i in range(n):
                if freq == 0:
                    samples.append(0)
                    continue
                # Sine with a short fade in/out to avoid clicks.
                envelope = min(1.0, i / 200, (n - i) / 200)
                samples.append(int(amp * envelope * math.sin(2 * math.pi * freq * i / RATE)))
        try:
            return pygame.mixer.Sound(buffer=samples.tobytes())
        except pygame.error:
            return None
