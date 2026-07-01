"""Entry point: `python -m pibuddy`."""

from __future__ import annotations

import logging
import sys

from . import config as config_mod
from .display import Display
from .persist import Persistence
from .server import ServerThread
from .state import StateStore


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    config = config_mod.load(argv)

    store = StateStore()
    persistence = Persistence(store)
    persistence.load()
    persistence.start()

    server = ServerThread(
        store,
        config.host,
        config.port,
        config.token,
        ntfy_url=config.ntfy_url,
        relay_after=config.relay_after,
    )
    server.start()

    if config.ble:
        from .ble import BleBridge

        BleBridge(store).start()

    try:
        Display(store, config).run()
    finally:
        server.stop()
        persistence.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
