"""HTTP server the Claude Code hooks (and your phone) talk to.

Runs on a background thread with its own asyncio loop so the pygame UI
can own the main thread. Endpoints:

* ``POST /api/event``    — fire-and-forget hook events (never blocks Claude)
* ``POST /api/approval`` — a blocking PreToolUse hook; the response is held
  until the user taps approve/deny (touchscreen or phone) or the wait
  times out.
* ``POST /api/decide``   — resolve a pending approval by id (phone page)
* ``GET  /api/status``   — full status JSON
* ``GET  /``             — mobile remote-control page (status + approve/deny)

If configured, a background task relays long-unanswered attention to an
ntfy-style push URL, and the service is advertised over mDNS/zeroconf.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
import threading
import time

import aiohttp
from aiohttp import web

from .bleproto import clamp_wait
from .state import StateStore, escalation_tier

log = logging.getLogger("pibuddy.server")

POLL_INTERVAL = 0.1
RELAY_CHECK_INTERVAL = 10.0

PHONE_PAGE = """<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PiBuddy</title>
<style>
  body { font-family: system-ui, sans-serif; background: #1c1a22; color: #ebe8f0;
         margin: 0; padding: 1rem; }
  h1 { font-size: 1.2rem; color: #f0a05a; }
  .mood { font-size: 2.5rem; margin: .2rem 0; }
  .card { background: #282632; border-radius: 12px; padding: 1rem; margin: .8rem 0; }
  .tool { font-weight: 700; font-size: 1.1rem; }
  .detail { color: #9694a5; word-break: break-all; margin: .4rem 0 .8rem; }
  button { border: 0; border-radius: 10px; padding: .9rem 0; width: 48%;
           font-size: 1.05rem; font-weight: 700; }
  .ok { background: #5fbe78; color: #0c1e12; }
  .no { background: #e15f55; color: #230c0c; float: right; }
  .muted { color: #9694a5; font-size: .9rem; }
</style></head>
<body>
<h1>PiBuddy remote</h1>
<div class="mood" id="mood">…</div>
<div class="muted" id="summary"></div>
<div id="approvals"></div>
<script>
const token = new URLSearchParams(location.search).get("token")
            || localStorage.getItem("pibuddy_token") || "";
if (token) localStorage.setItem("pibuddy_token", token);
const EMOJI = {sleep:"😴", idle:"🙂", busy:"🛠️", attention:"❗",
               celebrate:"🎉", dizzy:"😵", heart:"🧡"};
async function api(path, opts) {
  const headers = Object.assign({"X-PiBuddy-Token": token}, (opts||{}).headers);
  const r = await fetch(path, Object.assign({}, opts, {headers}));
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
async function decide(id, decision) {
  await api("/api/decide", {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({request_id: id, decision})});
  refresh();
}
async function refresh() {
  try {
    const s = await api("/api/status");
    document.getElementById("mood").textContent =
      (EMOJI[s.mood] || "") + " " + s.mood;
    document.getElementById("summary").textContent =
      `${s.sessions} session(s) · level ${s.level} · ${s.events_seen} events`;
    const box = document.getElementById("approvals");
    box.innerHTML = "";
    for (const a of s.approvals) {
      const card = document.createElement("div");
      card.className = "card";
      card.innerHTML = `<div class="tool"></div><div class="desc"></div>
        <div class="detail"></div><div class="ctx muted"></div>`;
      card.querySelector(".tool").textContent = "Claude wants: " + a.tool_name;
      card.querySelector(".desc").textContent = a.description || "";
      card.querySelector(".detail").textContent = a.detail || "";
      card.querySelector(".ctx").textContent =
        a.context ? "Claude: “" + a.context + "”" : "";
      const ok = document.createElement("button"); ok.className = "ok";
      ok.textContent = "Approve"; ok.onclick = () => decide(a.request_id, "allow");
      const no = document.createElement("button"); no.className = "no";
      no.textContent = "Deny"; no.onclick = () => decide(a.request_id, "deny");
      card.append(ok, no); box.append(card);
    }
  } catch (e) {
    document.getElementById("mood").textContent = "⚠️ " + e.message;
  }
}
refresh(); setInterval(refresh, 2000);
</script>
</body></html>
"""


def _authorized(request: web.Request, token: str) -> bool:
    if not token:
        return True
    supplied = request.headers.get("X-PiBuddy-Token") or request.query.get("token") or ""
    return hmac.compare_digest(supplied, token)


async def _read_json(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise web.HTTPBadRequest(text="invalid JSON")
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return payload


def build_app(store: StateStore, token: str = "", ntfy_url: str = "", relay_after: int = 180) -> web.Application:
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

        wait = clamp_wait(request.query.get("wait"))

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

    @routes.post("/api/decide")
    async def decide(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            raise web.HTTPUnauthorized(text="bad token")
        payload = await _read_json(request)
        decision = str(payload.get("decision", ""))
        if decision not in ("allow", "deny"):
            raise web.HTTPBadRequest(text="decision must be allow or deny")
        rid = payload.get("request_id")
        resolved = store.resolve_approval(decision, str(rid) if rid else None)
        if resolved is None:
            raise web.HTTPNotFound(text="no matching pending approval")
        return web.json_response({"ok": True, "request_id": resolved.request_id})

    @routes.get("/api/status")
    async def status(request: web.Request) -> web.Response:
        if not _authorized(request, token):
            raise web.HTTPUnauthorized(text="bad token")
        snap = store.snapshot()
        return web.json_response(
            {
                "mood": snap.mood,
                "sessions": len(snap.sessions),
                "approvals_waiting": snap.approvals_waiting,
                "attention_age": round(snap.attention_age, 1),
                "escalation": escalation_tier(snap.attention_age),
                "xp": snap.xp,
                "level": snap.level,
                "events_seen": snap.events_seen,
                "streak_days": snap.streak_days,
                "today": vars(snap.today),
                "approvals": [
                    {
                        "request_id": a.request_id,
                        "tool_name": a.tool_name,
                        "detail": a.detail,
                        "description": a.description,
                        "context": a.context,
                        "session_id": a.session_id,
                    }
                    for a in snap.approvals
                ],
            }
        )

    @routes.get("/")
    async def root(request: web.Request) -> web.Response:
        return web.Response(text=PHONE_PAGE, content_type="text/html")

    app = web.Application()
    app.add_routes(routes)

    if ntfy_url:
        relay = AttentionRelay(store, ntfy_url, relay_after)

        async def start_relay(app):
            app["relay_task"] = asyncio.create_task(relay.run())

        async def stop_relay(app):
            app["relay_task"].cancel()

        app.on_startup.append(start_relay)
        app.on_cleanup.append(stop_relay)

    return app


class AttentionRelay:
    """Push a notification when attention goes unanswered too long."""

    def __init__(self, store: StateStore, url: str, after: int) -> None:
        self.store = store
        self.url = url
        self.after = max(30, after)
        self._notified_start = 0.0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(RELAY_CHECK_INTERVAL)
            try:
                await self._check()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # relay is best-effort
                log.warning("attention relay failed: %s", exc)

    async def _check(self) -> None:
        snap = self.store.snapshot()
        if snap.attention_age < self.after:
            return
        episode_start = time.time() - snap.attention_age
        if abs(episode_start - self._notified_start) < RELAY_CHECK_INTERVAL:
            return  # already notified for this episode
        self._notified_start = episode_start

        if snap.approvals:
            a = snap.approvals[0]
            body = f"Claude is waiting for approval: {a.tool_name} {a.detail}"[:300]
        else:
            body = "A Claude Code session needs your attention"
        async with aiohttp.ClientSession() as session:
            await session.post(
                self.url,
                data=body.encode(),
                headers={"Title": "PiBuddy", "Priority": "high", "Tags": "warning"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
        log.info("relayed attention notification (unanswered %ds)", int(snap.attention_age))


class ServerThread:
    """Runs the aiohttp app on a daemon thread."""

    def __init__(
        self,
        store: StateStore,
        host: str,
        port: int,
        token: str = "",
        ntfy_url: str = "",
        relay_after: int = 180,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.token = token
        self.ntfy_url = ntfy_url
        self.relay_after = relay_after
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._zeroconf = None
        self.error: BaseException | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="pibuddy-server", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10) or self.error:
            raise RuntimeError(f"server failed to start: {self.error}")
        self._advertise()

    def stop(self) -> None:
        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _advertise(self) -> None:
        """Best-effort mDNS advertisement as pibuddy._http._tcp."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            return
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            address = s.getsockname()[0]
            s.close()
            info = ServiceInfo(
                "_http._tcp.local.",
                "pibuddy._http._tcp.local.",
                addresses=[socket.inet_aton(address)],
                port=self.port,
                properties={"path": "/"},
            )
            self._zeroconf = Zeroconf()
            self._zeroconf.register_service(info)
            log.info("advertised via mDNS as pibuddy._http._tcp (%s:%d)", address, self.port)
        except Exception as exc:
            log.info("mDNS advertisement unavailable: %s", exc)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        runner = web.AppRunner(
            build_app(self.store, self.token, self.ntfy_url, self.relay_after)
        )
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
