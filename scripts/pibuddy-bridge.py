#!/usr/bin/env python3
"""PiBuddy BLE bridge — run this on your laptop when it doesn't share a
network with the Pi.

It keeps one persistent Bluetooth LE connection to the buddy and serves
the same HTTP API locally, so the Claude Code hooks don't change at all:
point them at the bridge instead of the Pi.

    pip install bleak aiohttp          # once
    python3 scripts/pibuddy-bridge.py  # keep running (see README for a
                                       # login service)

Then pair the hooks against localhost:

    python3 scripts/install-hooks.py --url http://127.0.0.1:8766

Options:
    --name PiBuddy      BLE device name to scan for (default PiBuddy)
    --address AA:BB:…   skip scanning, connect to this address
    --port 8766         local HTTP port for the hooks
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web  # noqa: E402

from pibuddy.bleproto import CentralCore, chunks  # noqa: E402
from pibuddy.ble import UART_RX, UART_SERVICE, UART_TX  # noqa: E402

log = logging.getLogger("pibuddy.bridge")

SCAN_TIMEOUT = 10.0
RECONNECT_DELAY = 5.0


class Bridge:
    def __init__(self, name: str, address: str | None, port: int) -> None:
        self.name = name
        self.address = address
        self.port = port
        self.client = None
        self.core = CentralCore(self._send_line)

    # ---------------------------------------------------------- BLE side

    async def _send_line(self, line: bytes) -> None:
        if self.client is None or not self.client.is_connected:
            raise ConnectionError("not connected to the buddy")
        for piece in chunks(line):
            await self.client.write_gatt_char(UART_RX, piece, response=False)

    async def ble_loop(self) -> None:
        from bleak import BleakClient, BleakScanner

        while True:
            try:
                address = self.address
                if address is None:
                    log.info("scanning for '%s'…", self.name)
                    device = await BleakScanner.find_device_by_name(
                        self.name, timeout=SCAN_TIMEOUT
                    )
                    if device is None:
                        log.info("buddy not found; is it in range and running with --ble?")
                        await asyncio.sleep(RECONNECT_DELAY)
                        continue
                    address = device.address

                log.info("connecting to %s…", address)
                async with BleakClient(address) as client:
                    self.client = client
                    await client.start_notify(
                        UART_TX, lambda _, data: self.core.feed(bytes(data))
                    )
                    self.core.connected = True
                    log.info("connected — hooks on http://127.0.0.1:%d now reach the buddy", self.port)
                    while client.is_connected:
                        await asyncio.sleep(1)
            except Exception as exc:
                log.warning("BLE connection lost/failed: %s", exc)
            finally:
                self.core.connected = False
                self.client = None
            await asyncio.sleep(RECONNECT_DELAY)

    # --------------------------------------------------------- HTTP side

    def build_app(self) -> web.Application:
        routes = web.RouteTableDef()

        async def read_json(request: web.Request) -> dict:
            try:
                payload = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise web.HTTPBadRequest(text="invalid JSON")
            if not isinstance(payload, dict):
                raise web.HTTPBadRequest(text="expected a JSON object")
            return payload

        @routes.post("/api/event")
        async def event(request: web.Request) -> web.Response:
            payload = await read_json(request)
            if not self.core.connected:
                # Same fail-open contract as an unreachable Pi.
                return web.json_response({"ok": False, "reason": "ble disconnected"})
            try:
                await self.core.send_event(payload)
            except Exception as exc:
                return web.json_response({"ok": False, "reason": str(exc)})
            return web.json_response({"ok": True})

        @routes.post("/api/approval")
        async def approval(request: web.Request) -> web.Response:
            payload = await read_json(request)
            try:
                wait = float(request.query.get("wait", 45))
            except ValueError:
                wait = 45.0
            if not self.core.connected:
                return web.json_response({"decision": "none", "reason": "ble disconnected"})
            try:
                decision = await self.core.request_approval(
                    payload, wait, asyncio.get_running_loop()
                )
            except Exception as exc:
                log.warning("approval over BLE failed: %s", exc)
                decision = "none"
            return web.json_response({"decision": decision})

        @routes.get("/api/status")
        async def status(request: web.Request) -> web.Response:
            return web.json_response(
                {"bridge": True, "ble_connected": self.core.connected}
            )

        app = web.Application()
        app.add_routes(routes)
        return app

    async def run(self) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        # Localhost only: the bridge is a personal sidecar, not a LAN service.
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        log.info("bridge listening on http://127.0.0.1:%d", self.port)
        try:
            await self.ble_loop()
        finally:
            await runner.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="PiBuddy BLE bridge")
    parser.add_argument("--name", default="PiBuddy", help="BLE device name to scan for")
    parser.add_argument("--address", help="connect to this BLE address, skip scanning")
    parser.add_argument("--port", type=int, default=8766, help="local HTTP port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        import bleak  # noqa: F401
    except ImportError:
        print("The bridge needs bleak: pip install bleak aiohttp", file=sys.stderr)
        return 1

    try:
        asyncio.run(Bridge(args.name, args.address, args.port).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
