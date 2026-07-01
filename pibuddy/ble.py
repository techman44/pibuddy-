"""EXPERIMENTAL: advertise PiBuddy as a BLE hardware buddy.

Claude Desktop (macOS/Windows) talks to hardware buddies over Bluetooth
LE using the Nordic UART Service with line-buffered JSON — the protocol
documented in anthropics/claude-desktop-buddy's REFERENCE.md. This
module uses BlueZ (via the `bluezero` package) to make the Pi look like
such a device, feeding received messages into the same StateStore the
webhook path uses, so one PiBuddy can serve Claude Code hooks and the
Claude Desktop app at the same time.

Status: UNTESTED scaffold — it needs real hardware plus a paired Claude
Desktop to validate, and the upstream message schema may evolve. Enable
with --ble after `pip install bluezero`; failures are logged and never
take down the main app.
"""

from __future__ import annotations

import json
import logging
import threading

from .state import StateStore

log = logging.getLogger("pibuddy.ble")

# Nordic UART Service (what the upstream buddies use).
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # desktop -> buddy
UART_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # buddy -> desktop


class BleBridge:
    """Best-effort BLE peripheral translating desktop messages to events."""

    def __init__(self, store: StateStore, name: str = "PiBuddy") -> None:
        self.store = store
        self.name = name
        self._buffer = b""
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pibuddy-ble", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            from bluezero import adapter, peripheral
        except ImportError:
            log.warning("--ble requested but bluezero is not installed (pip install bluezero)")
            return
        try:
            dongle = list(adapter.Adapter.available())[0]
            device = peripheral.Peripheral(dongle.address, local_name=self.name)
            device.add_service(srv_id=1, uuid=UART_SERVICE, primary=True)
            device.add_characteristic(
                srv_id=1, chr_id=1, uuid=UART_RX, value=[], notifying=False,
                flags=["write", "write-without-response"],
                write_callback=self._on_write,
            )
            device.add_characteristic(
                srv_id=1, chr_id=2, uuid=UART_TX, value=[], notifying=False,
                flags=["notify"],
            )
            log.info("BLE buddy advertising as '%s' (experimental)", self.name)
            device.publish()  # blocks, running the GLib main loop
        except Exception as exc:
            log.warning("BLE buddy failed to start: %s", exc)

    def _on_write(self, value, options) -> None:
        self._buffer += bytes(value)
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        # Map upstream desktop messages onto hook-shaped events as best we
        # can; unknown message types still register as generic activity.
        kind = str(msg.get("type", ""))
        sid = str(msg.get("session_id") or "claude-desktop")
        if kind in ("approval", "approval_request"):
            self.store.apply_event(
                {"hook_event_name": "Notification", "session_id": sid,
                 "message": "Claude needs your permission"}
            )
        elif kind in ("state", "status") and msg.get("busy"):
            self.store.apply_event({"hook_event_name": "PreToolUse", "session_id": sid})
        elif kind in ("done", "complete"):
            self.store.apply_event({"hook_event_name": "Stop", "session_id": sid})
        else:
            self.store.apply_event({"hook_event_name": "SessionStart", "session_id": sid})
