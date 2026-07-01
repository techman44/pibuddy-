"""Configuration: JSON file with CLI overrides.

Search order for the config file (first hit wins):
    --config PATH
    ~/.config/pibuddy/config.json
    /etc/pibuddy/config.json
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger("pibuddy.config")

DEFAULT_PATHS = (
    Path("~/.config/pibuddy/config.json").expanduser(),
    Path("/etc/pibuddy/config.json"),
)


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    token: str = ""
    fullscreen: bool = True
    width: int = 800
    height: int = 480
    rotate: int = 0
    fps: int = 30
    dim_after: int = 120
    character_pack: str = ""


def _load_file(path: Path | None) -> dict:
    candidates = [path] if path else list(DEFAULT_PATHS)
    for candidate in candidates:
        if candidate and candidate.exists():
            try:
                data = json.loads(candidate.read_text())
                if isinstance(data, dict):
                    log.info("loaded config from %s", candidate)
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("ignoring config %s: %s", candidate, exc)
    return {}


def load(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        prog="pibuddy", description="Claude Code desk companion for Raspberry Pi"
    )
    parser.add_argument("--config", type=Path, help="path to config.json")
    parser.add_argument("--host", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="webhook port (default 8765)")
    parser.add_argument("--token", help="shared secret hooks must send")
    parser.add_argument(
        "--window",
        metavar="WxH",
        help="run windowed at this size instead of fullscreen (e.g. 800x480)",
    )
    parser.add_argument(
        "--rotate", type=int, choices=(0, 90, 180, 270), help="rotate the UI"
    )
    parser.add_argument("--fps", type=int, help="frame rate cap (default 30)")
    parser.add_argument(
        "--dim-after", type=int, help="seconds of inactivity before dimming"
    )
    parser.add_argument(
        "--character-pack", help="path to a GIF character pack directory"
    )
    args = parser.parse_args(argv)

    data = _load_file(args.config)
    known = {f.name for f in fields(Config)}
    config = Config(**{k: v for k, v in data.items() if k in known})

    for name in ("host", "port", "token", "rotate", "fps", "character_pack"):
        value = getattr(args, name)
        if value is not None:
            setattr(config, name, value)
    if args.dim_after is not None:
        config.dim_after = args.dim_after
    if args.window:
        try:
            w, h = args.window.lower().split("x")
            config.width, config.height = int(w), int(h)
            config.fullscreen = False
        except ValueError:
            parser.error("--window expects WIDTHxHEIGHT, e.g. 800x480")
    return config
