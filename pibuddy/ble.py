"""BLE peripheral: lets laptops (and Claude Desktop) reach the buddy
without sharing a network.

Advertises the Nordic UART Service via BlueZ (`bluezero`). Two clients
speak to it:

* the PiBuddy laptop bridge (`scripts/pibuddy-bridge.py`), which forwards
  Claude Code hook events and approval requests over BLE using the
  protocol in bleproto.py — full feature parity with the HTTP path,
  including touch approvals flowing back as notifications;
* the Claude Desktop app, whose upstream-style messages are mapped to
  hook events best-effort.

Enable with --ble (needs `pip install bluezero` and BlueZ, i.e. a Pi).
The radio layer needs real hardware; protocol logic lives in bleproto.py
and is unit-tested. Failures here are logged and never take down the app.
"""

from __future__ import annotations

import logging
import threading

from .bleproto import MIN_CHUNK_SIZE, PeripheralHandler, chunks
from .state import StateStore

log = logging.getLogger("pibuddy.ble")

# Nordic UART Service (same UUIDs as the upstream buddies).
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # central -> buddy
UART_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # buddy -> central


class BlePeripheral:
    def __init__(self, store: StateStore, name: str = "PiBuddy") -> None:
        self.store = store
        self.name = name
        self.handler = PeripheralHandler(store, self._send_line)
        self._tx = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pibuddy-ble", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            from bluezero import adapter, peripheral
        except ImportError:
            log.warning("--ble requested but bluezero is not installed (pip install bluezero)")
            return
        try:
            dongles = list(adapter.Adapter.available())
            if not dongles:
                log.warning("--ble requested but no Bluetooth adapter found")
                return
            device = peripheral.Peripheral(dongles[0].address, local_name=self.name)
            device.add_service(srv_id=1, uuid=UART_SERVICE, primary=True)
            device.add_characteristic(
                srv_id=1, chr_id=1, uuid=UART_RX, value=[], notifying=False,
                flags=["write", "write-without-response"],
                write_callback=self._on_write,
            )
            device.add_characteristic(
                srv_id=1, chr_id=2, uuid=UART_TX, value=[], notifying=False,
                flags=["notify"],
                notify_callback=self._on_notify_toggle,
            )
            log.info("BLE: advertising as '%s' (Nordic UART)", self.name)
            device.publish()  # blocks, running the GLib main loop
        except Exception as exc:
            log.warning("BLE peripheral failed: %s", exc)

    def _on_write(self, value, options) -> None:
        try:
            self.handler.receive(bytes(value))
        except Exception as exc:
            log.warning("BLE receive error: %s", exc)

    def _on_notify_toggle(self, notifying, characteristic) -> None:
        self._tx = characteristic if notifying else None
        # A new (or gone) central: drop any partial line from the old link
        # so its bytes can't corrupt the first message of the next one.
        self.handler.reset()
        log.info("BLE: central %s notifications", "enabled" if notifying else "disabled")

    def _send_line(self, line: bytes) -> None:
        """Emit one reply line as TX notifications.

        Called from approval-waiter threads, but dbus-python is not
        thread-safe without explicit init, so the actual set_value calls
        are marshalled onto the GLib main loop (which device.publish()
        runs in the BLE thread). Sending a whole line per idle callback
        also keeps concurrent replies from interleaving chunks.
        """
        tx = self._tx
        if tx is None:
            log.info("BLE: no subscribed central, dropping reply")
            return
        # 20-byte pieces: bluezero can't see the negotiated MTU, and the
        # BLE minimum is accepted everywhere; the bridge reassembles.
        pieces = [list(piece) for piece in chunks(line, MIN_CHUNK_SIZE)]

        def emit():
            current = self._tx
            if current is not None:
                for piece in pieces:
                    current.set_value(piece)
            return False  # one-shot idle callback

        try:
            from gi.repository import GLib

            GLib.idle_add(emit)
        except ImportError:  # not on a GLib system (e.g. tests)
            emit()
