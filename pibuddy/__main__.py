"""Entry point: `python -m pibuddy`."""

from __future__ import annotations

import logging
import sys

from . import config as config_mod
from .display import Display
from .server import ServerThread
from .state import StateStore


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = config_mod.load(argv)

    store = StateStore()
    server = ServerThread(store, config.host, config.port, config.token)
    server.start()
    try:
        Display(store, config).run()
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
