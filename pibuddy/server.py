"""HTTP server the Claude Code hooks talk to.

Runs on a background thread with its own asyncio loop so the pygame UI
can own the main thread. Two kinds of endpoint:

* ``POST /api/event``    — fire-and-forget hook events (never blocks Claude)
* ``POST /api/approval`` — a blocking PreToolUse hook; the response is held
  until the user taps approve/deny on the touchscreen or the wait times out.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading

from aiohttp import web

from .state import StateStore

log = logging.getLogger("pibuddy.server")

MAX_APPROVAL_WAIT = 120.0
DEFAULT_APPROVAL_WAIT = 45.0
POLL_INTERVAL = 0.1


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    supplied = request.headers.get("X-PiBuddy-Token", "")
    return hmac.compare_digest(supplied, token)


async def _read_json(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="invalid JSON")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return payload


def build_app(store: StateStore, token: str = "") -> web.Application:
    routes = web.RouteTableDef()

    @routes.post("/api/event")
    async def event(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            raise web.HTTPUnauthorized(text="bad token")
        payload = await _read_json(request)
        store.apply_event(payload)
        return web.json_response({"ok": True})

    @routes.post("/api/approval")
    async def approval(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            raise web.HTTPUnauthorized(text="bad token")
        payload = await _read_json(request)
        # The approval also counts as activity for the session.
        store.apply_event({**payload, "hook_event_name": "PreToolUse"})

        try:
            wait = float(request.query.get("wait", DEFAULT_APPROVAL_WAIT))
        except ValueError:
            wait = DEFAULT_APPROVAL_WAIT
        wait = max(1.0, min(wait, MAX_APPROVAL_WAIT))

        req = store.add_approval(payload)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        try:
            while req.decision is None and loop.time() < deadline:
                await asyncio.sleep(POLL_INTERVAL)
        finally:
            store.discard_approval(req)

        return web.json_response(
            {
                "decision": req.decision or "none",
                "request_id": req.request_id,
            }
        )

    @routes.get("/api/status")
    async def status(request: web.Request) -> web.Response:
        snap = store.snapshot()
        return web.json_response(
            {
                "mood": snap.mood,
                "sessions": len(snap.sessions),
                "approvals_waiting": snap.approvals_waiting,
                "xp": snap.xp,
                "level": snap.level,
                "events_seen": snap.events_seen,
            }
        )

    @routes.get("/")
    async def root(request: web.Request) -> web.Response:
        return web.Response(text="PiBuddy is listening. POST hook events to /api/event\n")

    app = web.Application()
    app.add_routes(routes)
    return app


class ServerThread:
    """Runs the aiohttp app on a daemon thread."""

    def __init__(self, store: StateStore, host: str, port: int, token: str = "") -> None:
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self.error: BaseException | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pibuddy-server", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10) or self.error:
            raise RuntimeError(f"server failed to start: {self.error}")

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        runner = web.AppRunner(build_app(self.store, self.token))
        try:
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, self.host, self.port)
            loop.run_until_complete(site.start())
            log.info("listening on http://%s:%d", self.host, self.port)
            self._started.set()
            loop.run_forever()
        except BaseException as exc:  # surface bind errors etc. to the main thread
            self.error = exc
            self._started.set()
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()
