"""FastAPI web server for Agent Hub.

This is the human's window into the hub and the place to type instructions to
agents. It is a thin layer over the shared-filesystem store: every agent action
also works without it (via hubcli / HubClient). The server only needs to run on
one host that can see the shared filesystem.

Live updates use Server-Sent Events (SSE) rather than WebSockets: SSE rides on
ordinary HTTP, survives proxies, and reconnects automatically in the browser.
The hub data itself is polled from the filesystem (NFS-friendly; inotify is not
reliable over NFS).

Auth: a single shared token. Browsers send it as the `X-Hub-Token` header for
API calls and as a `?token=` query param for the SSE stream (EventSource cannot
set headers). If the hub has no token configured, auth is disabled.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Query, Header, Body
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .store import HubStore

WEB_DIR = Path(__file__).parent / "web"


def create_app(root: str | Path) -> FastAPI:
    store = HubStore(root)
    store.init()  # idempotent: ensures dirs + default channel exist
    app = FastAPI(title="Agent Hub", version="1.0")

    # -- never serve stale UI --------------------------------------------
    # The web assets change often during development; tell browsers to always
    # revalidate so a plain refresh always gets the latest HTML/CSS/JS.
    @app.middleware("http")
    async def no_cache_ui(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    # -- background auto-pruner (retention) ------------------------------
    @app.on_event("startup")
    async def _start_pruner():
        ret = (store.get_config() or {}).get("retention") or {}
        keep_last = ret.get("keep_last")
        max_age_days = ret.get("max_age_days")
        if keep_last is None and max_age_days is None:
            return  # retention disabled
        interval = float(ret.get("interval_sec") or 3600)
        max_age = max_age_days * 86400 if max_age_days else None
        archive = ret.get("archive", True)

        async def loop():
            while True:
                try:
                    result = store.prune_all(keep_last=keep_last,
                                             max_age=max_age, archive=archive)
                    total = sum(result.values())
                    if total:
                        # Log server-side only. Posting into a channel here would
                        # create a feedback loop: the notice becomes a message
                        # that gets pruned next cycle, triggering another notice.
                        print(f"[retention] pruned {total} message(s) "
                              f"(keep_last={keep_last}, max_age_days={max_age_days}): "
                              f"{result}", flush=True)
                except Exception as e:
                    print(f"[retention] error: {e}", flush=True)
                await asyncio.sleep(interval)

        asyncio.create_task(loop())

    # -- auth ------------------------------------------------------------
    def check_token(provided: str | None):
        token = store.token
        if not token:
            return  # auth disabled
        if provided != token:
            raise HTTPException(status_code=401, detail="Invalid or missing token")

    # -- meta ------------------------------------------------------------
    @app.get("/api/health")
    def health():
        cfg = store.get_config() or {}
        return {"ok": True, "root": str(store.root),
                "auth_required": bool(cfg.get("token")), "version": cfg.get("version")}

    @app.post("/api/auth/check")
    def auth_check(token: str = Body(..., embed=True)):
        check_token(token)
        return {"ok": True}

    # -- channels --------------------------------------------------------
    @app.get("/api/channels")
    def channels(x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.list_channels()

    @app.post("/api/channels")
    def make_channel(payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "name required")
        created = store.ensure_channel(name, description=payload.get("description", ""))
        return {"name": created}

    @app.get("/api/channels/{channel}/messages")
    def channel_messages(channel: str, since: float = 0.0, limit: int = 200,
                         x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.read_channel(channel, since_ts=since, limit=limit)

    @app.post("/api/channels/{channel}/messages")
    def post_channel(channel: str, payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        name = payload.get("author_name") or "human"
        m = store.post_channel(
            channel, text,
            author=payload.get("author") or f"human:{name}",
            author_name=name,
            author_kind=payload.get("author_kind", "human"),
            host=payload.get("host", "web"))
        return m.to_dict()

    # -- firehose: all activity merged -----------------------------------
    @app.get("/api/firehose")
    def firehose(since: float = 0.0, limit: int = 200,
                 x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.firehose(since_ts=since, limit=limit)

    # -- broadcast: one instruction to all (or by capability) ------------
    @app.get("/api/broadcast")
    def read_broadcast(since: float = 0.0, limit: int = 200,
                       x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.read_broadcast(since_ts=since, limit=limit)

    @app.post("/api/broadcast")
    def post_broadcast(payload: dict = Body(...),
                       x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        name = payload.get("author_name") or "human"
        author = payload.get("author") or f"human:{name}"
        capability = (payload.get("capability") or "").strip()
        if capability:
            sent = store.broadcast_to_capability(
                capability, text, author=author, author_name=name,
                author_kind=payload.get("author_kind", "human"), host="web",
                online_only=bool(payload.get("online_only")))
            return {"targeted_capability": capability,
                    "delivered": len(sent),
                    "agents": [m.to for m in sent]}
        m = store.post_broadcast(
            text, author=author, author_name=name,
            author_kind=payload.get("author_kind", "human"), host="web")
        return m.to_dict()

    # -- agents + directed instructions ----------------------------------
    @app.get("/api/agents")
    def agents(window: float = 30.0, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.list_agents(online_window=window)

    @app.get("/api/agents/{agent_id}/inbox")
    def agent_inbox(agent_id: str, since: float = 0.0, limit: int = 200,
                    x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.read_inbox(agent_id, since_ts=since, limit=limit)

    @app.post("/api/agents/{agent_id}/inbox")
    def send_instruction(agent_id: str, payload: dict = Body(...),
                         x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        text = (payload.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text required")
        name = payload.get("author_name") or "human"
        m = store.post_inbox(
            agent_id, text,
            author=payload.get("author") or f"human:{name}",
            author_name=name,
            author_kind=payload.get("author_kind", "human"),
            host=payload.get("host", "web"))
        return m.to_dict()

    # -- live stream (SSE) -----------------------------------------------
    @app.get("/api/stream")
    async def stream(request: Request, token: str | None = Query(default=None)):
        check_token(token)

        async def event_gen():
            # Per-connection cursors. Start "now" so we stream only new activity;
            # the UI loads history separately via the REST endpoints.
            start = time.time()
            cursors: dict[str, float] = {}
            inbox_cursors: dict[str, float] = {}
            broadcast_cursor = start
            yield _sse({"type": "hello", "ts": start})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # New channel messages.
                    for ch in store.list_channels():
                        name = ch["name"]
                        since = cursors.get(name, start)
                        for m in store.read_channel(name, since_ts=since):
                            cursors[name] = max(cursors.get(name, start), m["ts"])
                            yield _sse({"type": "message", "message": m})
                    # New directed instructions (so the UI can flag them).
                    for a in store.list_agents(online_window=10_000):
                        aid = a["id"]
                        since = inbox_cursors.get(aid, start)
                        for m in store.read_inbox(aid, since_ts=since):
                            inbox_cursors[aid] = max(inbox_cursors.get(aid, start), m["ts"])
                            yield _sse({"type": "inbox", "message": m})
                    # New broadcasts (instructions to all agents).
                    for m in store.read_broadcast(since_ts=broadcast_cursor):
                        broadcast_cursor = max(broadcast_cursor, m["ts"])
                        yield _sse({"type": "broadcast", "message": m})
                    # Presence snapshot.
                    yield _sse({"type": "agents", "agents": store.list_agents()})
                except Exception as e:  # never kill the stream on a transient FS error
                    yield _sse({"type": "error", "detail": str(e)})
                await asyncio.sleep(1.0)

        return StreamingResponse(event_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # -- static UI -------------------------------------------------------
    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app


def _sse(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def run_server(root: str | Path, host: str = "127.0.0.1", port: int = 8787):
    import uvicorn
    app = create_app(root)
    cfg = HubStore(root).get_config() or {}
    print(f"Agent Hub serving {root}")
    print(f"  UI:    http://{host}:{port}/")
    if cfg.get("token"):
        print(f"  Token: {cfg['token']}  (enter it in the UI to connect)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
