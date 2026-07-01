"""Persist XP and daily stats across restarts.

A small JSON file (default ~/.local/share/pibuddy/state.json), written
atomically by a background thread whenever the store has changes, and
once more on shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

from .state import StateStore

log = logging.getLogger("pibuddy.persist")

DEFAULT_PATH = Path("~/.local/share/pibuddy/state.json").expanduser()
SAVE_INTERVAL = 60.0


class Persistence:
    def __init__(self, store: StateStore, path: Path | str = DEFAULT_PATH) -> None:
        self.store = store
        self.path = Path(path).expanduser()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("ignoring unreadable state file %s: %s", self.path, exc)
            return
        self.store.import_state(data)
        log.info("restored stats from %s", self.path)

    def save(self) -> None:
        data = self.store.export_state()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("could not save state to %s: %s", self.path, exc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pibuddy-persist", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.save()

    def _run(self) -> None:
        while not self._stop.wait(SAVE_INTERVAL):
            if self.store.dirty:
                self.save()
