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
import getpass
import json
import time
from pathlib import Path
from typing import Any


def os_username() -> str:
    """The OS account name, used as the default display name (issue #67).
    Falls back to 'user' if it can't be determined (no hardcoded person)."""
    try:
        name = getpass.getuser()
    except Exception:
        name = ""
    return name.strip() or "user"

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

    # -- whoami: default display name for the human (issue #67) ----------
    @app.get("/api/whoami")
    def whoami(x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return {"name": os_username()}

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

    def _with_reactions(msgs: list[dict]) -> list[dict]:
        """Attach aggregated emoji reactions (issue #61) to a list of messages."""
        by_id = store.reactions_for([m.get("id") for m in msgs if m.get("id")])
        for m in msgs:
            r = by_id.get(m.get("id"))
            if r:
                m["reactions"] = r
        return msgs

    @app.get("/api/channels/{channel}/messages")
    def channel_messages(channel: str, since: float = 0.0, limit: int = 200,
                         x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return _with_reactions(store.read_channel(channel, since_ts=since, limit=limit))

    @app.get("/api/channels/{channel}/thread/{parent_id}")
    def channel_thread(channel: str, parent_id: str,
                       x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        th = store.read_thread(channel, parent_id)
        th["parent"] = _with_reactions([th["parent"]])[0] if th["parent"] else None
        th["replies"] = _with_reactions(th["replies"])
        return th

    # -- reactions (issue #61) -------------------------------------------
    @app.get("/api/messages/{msg_id}/reactions")
    def get_reactions(msg_id: str, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.get_reactions(msg_id)

    @app.post("/api/messages/{msg_id}/reactions")
    def post_reaction(msg_id: str, payload: dict = Body(...),
                      x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        emoji = (payload.get("emoji") or "").strip()
        author = payload.get("author") or "human"
        author_name = payload.get("author_name") or author
        op = (payload.get("op") or "toggle").lower()
        try:
            if op == "add":
                r = store.add_reaction(msg_id, emoji, author, author_name=author_name)
            elif op == "remove":
                r = store.remove_reaction(msg_id, emoji, author)
            else:
                r = store.toggle_reaction(msg_id, emoji, author, author_name=author_name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"msg_id": msg_id, "reactions": r}

    @app.post("/api/channels/{channel}/messages")
    def post_channel(channel: str, payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        text = (payload.get("text") or "").strip()
        meta = payload.get("meta") or {}
        if not text and not meta.get("image"):
            raise HTTPException(400, "text or image required")
        name = payload.get("author_name") or "human"
        m = store.post_channel(
            channel, text,
            author=payload.get("author") or f"human:{name}",
            author_name=name,
            author_kind=payload.get("author_kind", "human"),
            host=payload.get("host", "web"), meta=meta,
            reply_to=payload.get("reply_to"))   # threaded replies (#64)
        return m.to_dict()

    # -- firehose: all activity merged -----------------------------------
    @app.get("/api/firehose")
    def firehose(since: float = 0.0, limit: int = 200,
                 x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return _with_reactions(store.firehose(since_ts=since, limit=limit))

    # -- full-text search (issue #51) -----------------------------------
    @app.get("/api/search")
    def search(q: str, channel: str | None = None, limit: int = 50,
               include_tasks: bool = True,
               x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        channels = [c.strip() for c in channel.split(",") if c.strip()] if channel else None
        return store.search_messages(q, channels=channels, limit=limit,
                                     include_tasks=include_tasks)

    # -- @mentions: messages that mention a viewer (issue #52) -----------
    @app.get("/api/mentions")
    def mentions(name: str, since: float = 0.0, limit: int = 200,
                 x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        from .store import collect_mentions
        return collect_mentions(store, name, since_ts=since, limit=limit)

    # -- broadcast: one instruction to all (or by capability) ------------
    @app.get("/api/broadcast")
    def read_broadcast(since: float = 0.0, limit: int = 200,
                       x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return _with_reactions(store.read_broadcast(since_ts=since, limit=limit))

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
    def agents(window: float = 30.0, retire_after: float | None = None,
               x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.list_agents(online_window=window, retire_after=retire_after)

    # -- task board: durable dispatch state for the UI -------------------
    @app.get("/api/tasks")
    def tasks(status: str | None = None, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.list_tasks(status=status)

    # -- research pipeline (issue #24) ----------------------------------
    @app.post("/api/research")
    def research_run(payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        import os as _os
        from . import research as R
        question = (payload.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "question required")
        key = None if payload.get("no_search") else _os.environ.get("AGORA_SEARCH_KEY")
        out = R.research(question, urls=payload.get("urls"), search_key=key,
                         max_sources=int(payload.get("max_sources", 5)))
        if payload.get("save_kb", True):
            name = payload.get("author") or "researcher"
            e = store.kb_add(f"Research: {question}", body=out["report_md"],
                             tags=["research"], kind="note",
                             author=name, author_name=name)
            out["kb_id"] = e["id"]
        return out

    # -- agent web access (issue #19) -----------------------------------
    @app.get("/api/web/fetch")
    def web_fetch(url: str, timeout: float = 12.0,
                  x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        from . import web_access as web
        return web.fetch_url(url, timeout=timeout)

    @app.get("/api/web/search")
    def web_search(q: str, backend: str | None = None, limit: int = 5,
                   x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        from . import web_access as web
        return web.search(q, backend=backend, limit=limit)

    # -- advisory locks (issue #10) -------------------------------------
    @app.get("/api/locks")
    def locks_list(x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.list_locks()

    @app.post("/api/locks")
    def lock_acquire(payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        resource = (payload.get("resource") or "").strip()
        if not resource:
            raise HTTPException(400, "resource required")
        owner = payload.get("owner") or payload.get("author_name") or "web"
        return store.acquire_lock(resource, owner=owner, owner_name=owner,
                                  note=payload.get("note", ""))

    @app.post("/api/locks/release")
    def lock_release(payload: dict = Body(...),
                     x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        resource = (payload.get("resource") or "").strip()
        if not resource:
            raise HTTPException(400, "resource required")
        owner = payload.get("owner") or payload.get("author_name") or "web"
        ok = store.release_lock(resource, owner=owner, force=bool(payload.get("force")))
        return {"ok": ok}

    # -- projects (issue #22) -------------------------------------------
    @app.get("/api/projects")
    def projects_list(x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.project_list()

    @app.get("/api/projects/{project_id}")
    def project_get(project_id: str, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        p = store.project_get(project_id)
        if p is None:
            raise HTTPException(404, "no such project")
        return p

    @app.post("/api/projects")
    def project_new(payload: dict = Body(...),
                    x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        pid = (payload.get("id") or payload.get("name") or "").strip()
        if not pid:
            raise HTTPException(400, "id or name required")
        name = payload.get("name") or pid
        author = payload.get("author") or payload.get("author_name") or "web"
        return store.project_new(pid, name=name, goal=payload.get("goal", ""),
                                 owner=payload.get("owner", ""), created_by=author)

    @app.post("/api/projects/{project_id}/add")
    def project_add(project_id: str, payload: dict = Body(...),
                    x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        if store.project_get(project_id, rollup=False) is None:
            raise HTTPException(404, "no such project")
        if payload.get("task"):
            store.project_add_task(project_id, payload["task"])
        if payload.get("channel"):
            store.project_add_channel(project_id, payload["channel"])
        if payload.get("milestone"):
            store.project_add_milestone(project_id, payload["milestone"],
                                        done=bool(payload.get("done")))
        if payload.get("set_milestone"):
            store.project_set_milestone(project_id, payload["set_milestone"],
                                        bool(payload.get("done")))
        if payload.get("owner") is not None or payload.get("goal") is not None:
            store.project_update(project_id, owner=payload.get("owner"),
                                 goal=payload.get("goal"))
        return store.project_get(project_id)

    @app.delete("/api/projects/{project_id}")
    def project_delete(project_id: str, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        if not store.project_delete(project_id):
            raise HTTPException(404, "no such project")
        return {"ok": True}

    # -- shared knowledge base (issue #25) ------------------------------
    @app.get("/api/kb")
    def kb_list(q: str | None = None, tag: str | None = None,
                limit: int | None = None,
                x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        if q:
            entries = store.kb_search(q, tag=tag, limit=limit)
        else:
            entries = store.kb_list(tag=tag, limit=limit)
        return {"entries": entries, "tags": store.kb_tags()}

    @app.get("/api/kb/{entry_id}")
    def kb_get(entry_id: str, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        e = store.kb_get(entry_id)
        if e is None:
            raise HTTPException(404, "no such KB entry")
        return e

    @app.post("/api/kb")
    def kb_add(payload: dict = Body(...),
               x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        title = (payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "title required")
        tags = payload.get("tags") or []
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t.strip()]
        name = payload.get("author_name") or payload.get("author") or "web"
        return store.kb_add(
            title, body=payload.get("body", ""), tags=tags,
            kind=payload.get("kind", "note"), url=payload.get("url", ""),
            author=name, author_name=name, entry_id=payload.get("id"))

    @app.delete("/api/kb/{entry_id}")
    def kb_delete(entry_id: str, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        if not store.kb_delete(entry_id):
            raise HTTPException(404, "no such KB entry")
        return {"ok": True}

    # -- system utilization / efficiency panel (issue #6) ---------------
    @app.get("/api/usage")
    def usage(window: float = 30.0, x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.usage_stats(online_window=window)

    # -- agent communication graph (issue #5) ---------------------------
    @app.get("/api/graph")
    def graph(x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return store.comm_graph()

    @app.get("/api/agents/{agent_id}/inbox")
    def agent_inbox(agent_id: str, since: float = 0.0, limit: int = 200,
                    x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        return _with_reactions(store.read_inbox(agent_id, since_ts=since, limit=limit))

    @app.post("/api/agents/{agent_id}/inbox")
    def send_instruction(agent_id: str, payload: dict = Body(...),
                         x_hub_token: str | None = Header(default=None)):
        check_token(x_hub_token)
        text = (payload.get("text") or "").strip()
        meta = payload.get("meta") or {}
        if not text and not meta.get("image"):
            raise HTTPException(400, "text or image required")
        name = payload.get("author_name") or "human"
        m = store.post_inbox(
            agent_id, text,
            author=payload.get("author") or f"human:{name}",
            author_name=name,
            author_kind=payload.get("author_kind", "human"),
            host=payload.get("host", "web"), meta=meta,
            reply_to=payload.get("reply_to"))   # threaded replies (#64)
        return m.to_dict()

    # -- spawn a new agent (HUMAN-ONLY, via the UI) ----------------------
    @app.post("/api/agents/spawn")
    def spawn_agent(payload: dict = Body(...),
                    x_hub_token: str | None = Header(default=None)):
        """Create + connect a brand-new live agent (claude session + bridge).

        Intended for USERS only: spawning is exposed solely here (token-gated)
        and via the browser UI — there is no hubcli/HubClient/bridge path to it,
        so a connected agent following the normal protocol has no tool to call
        it. (Under a single shared token this is a convention, not a hard
        boundary — kept simple per the project owner.) Every spawn is logged
        server-side, and the shell-out is argv-exec locally / shlex-quoted over
        ssh with validated inputs."""
        check_token(x_hub_token)
        from .spawn import run_spawn
        name = (payload.get("name") or "").strip()
        path = (payload.get("path") or "").strip()
        machine = (payload.get("machine") or "").strip()
        session = (payload.get("session") or "").strip()
        tasks = (payload.get("tasks") or "").strip()
        if not name or not path:
            raise HTTPException(400, "name and path are required")
        # Don't clobber an agent that's already online under this name.
        existing = store.get_agent(name)
        if existing and (time.time() - existing.get("last_seen", 0)) <= 30 \
                and existing.get("status") != "offline":
            raise HTTPException(409, f"an agent named '{name}' is already online")
        try:
            plan = run_spawn(name, path, machine, session, tasks,
                             hub_root=str(store.root))
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(500, f"spawn failed: {e}")
        # Announce in #general so everyone sees a new agent is starting up.
        store.post_channel(
            "general",
            f"🚀 spawning new agent '{plan['name']}' on {plan['target']} "
            f"(tmux {plan['session']}) — it will announce itself when ready.",
            author="system:spawn", author_name="system", author_kind="system",
            host="web")
        return {"ok": True, "name": plan["name"], "session": plan["session"],
                "bridge_session": plan["bridge_session"], "target": plan["target"]}

    # -- image / file uploads --------------------------------------------
    @app.post("/api/upload")
    def upload(payload: dict = Body(...),
               x_hub_token: str | None = Header(default=None)):
        """Accept a base64-encoded file (no multipart dependency), store it in
        the hub's uploads dir, and return its URL for use as a message image."""
        check_token(x_hub_token)
        import base64
        b64 = payload.get("data_base64") or ""
        if "," in b64 and b64.strip().startswith("data:"):
            b64 = b64.split(",", 1)[1]   # strip a data: URL prefix if present
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raise HTTPException(400, "invalid base64 data")
        if not raw:
            raise HTTPException(400, "empty upload")
        if len(raw) > 25 * 1024 * 1024:
            raise HTTPException(413, "file too large (max 25 MB)")
        filename = payload.get("filename") or "file"
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
        url = store.save_upload(raw, ext)
        return {"url": url, "filename": filename, "bytes": len(raw)}

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
            reaction_cursor = start
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
                    # New reaction changes (issue #61): emit the message's current
                    # aggregated reactions, once per changed message in the batch.
                    rxn_events = store.read_reaction_events(since_ts=reaction_cursor)
                    changed = []
                    for ev in rxn_events:
                        reaction_cursor = max(reaction_cursor, ev["ts"])
                        if ev["msg_id"] not in changed:
                            changed.append(ev["msg_id"])
                    for mid in changed:
                        yield _sse({"type": "reaction", "msg_id": mid,
                                    "reactions": store.get_reactions(mid)})
                    # Presence snapshot.
                    yield _sse({"type": "agents", "agents": store.list_agents()})
                    # Task-board snapshot (durable dispatch state, live).
                    yield _sse({"type": "tasks", "tasks": store.list_tasks()})
                    # Advisory locks snapshot (issue #10).
                    yield _sse({"type": "locks", "locks": store.list_locks()})
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

    # Serve uploaded images/files (stored under HUB_ROOT/uploads, outside git).
    store.uploads_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/uploads", StaticFiles(directory=str(store.uploads_dir)), name="uploads")

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
