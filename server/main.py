"""aiohttp server-brain — HTTP for clicks, SSE (EventSource) for streams.

State machine: a chat is either idle or generating (one exclusive turn).
POST /api/chats/{id}/messages returns 409 while a turn is running —
the server is the source of truth, the frontend is a dumb renderer.

Phase 5 — modes & deployment:
  solo / team / accounts scopes (§2) with per-user context isolation,
  onboarding wizard + /admin (§16), encrypted provider keys (§7),
  export/import JSON+MD round-trip (§16), versioned DB migrations,
  optional toolcall audit (§2), hooks with a GUI editor (§7),
  skills (§6), themes + RU/EN (§16), server logs (§16).

Every handler resolves its ScopeContext from the authenticated request:
the 'main' scope for solo/team/setup, a per-user scope for accounts.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import re
import time

from aiohttp import web

from . import events as ev
from . import exporter
from . import providers as providers_mod
from .agent_loop import TurnRunner
from .attachments import MAX_ATTACHMENT_BYTES
from .audit import AuditLog
from .auth import SESSION_COOKIE, Auth, AuthError
from .checkpoints import CheckpointError
from .context import ScopeContext, ScopeRegistry, ToolGate, TurnGate
from .db import DATA_DIR, DB_PATH, SettingsStore
from .history import HistoryStore, SESSIONS_DIR
from .jail import JailError
from .mcp import McpError
from .planmode import approve_plan, reject_plan
from .providers import Provider, ProviderError, normalize_host
from .proxy import ProxyMatrix
from .router import ModelRouter
from .search import search_sessions
from .srvlog import LEVELS, LOGS
from .webtools import WebTools
from .exporter import import_json as importer_json
from .exporter import import_md as importer_md

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
DATA = DATA_DIR
THEMES_DIR = os.path.join(DATA, "themes")
SOUNDS_DIR = os.path.join(DATA, "sounds")
SOUND_EXTS = (".mp3", ".wav", ".ogg", ".m4a")
SOUND_MAX_BYTES = 2 * 1024 * 1024
THEME_MAX_BYTES = 512 * 1024
APP_VERSION = "1.3.1"
APP_NAME = "NAgent"

# ----------------------------------------------------------------- server-level singletons

settings_store = SettingsStore()                    # main.db — server settings
proxy_matrix = ProxyMatrix(settings_store)
webtools = WebTools(settings_store, proxy_matrix)  # §12 global cache lives here
auth = Auth(settings_store)
audit = AuditLog(settings_store)                   # §2 writes to main.db
turn_gate = TurnGate()                             # §2 team concurrency
tool_gate = ToolGate()

_isolation_cache = {"value": "off"}


def _current_isolation() -> str:
    return _isolation_cache["value"]


def publish_live(chat_id: str, frame: dict):
    """Transient frames (think/text/bg deltas/queue positions) straight to
    the SSE subscribers of whichever scope owns the chat."""
    frame["_live"] = True
    frame["chat_id"] = chat_id
    for ctx in scope_registry.all():
        ctx.history._publish(chat_id, frame)


scope_registry = ScopeRegistry(webtools=webtools, proxy_matrix=proxy_matrix,
                               publish_live=publish_live,
                               isolation_getter=_current_isolation,
                               main_settings=settings_store)


def main_scope() -> ScopeContext:
    return scope_registry.get("main")


def _bind_main_scope(ctx: ScopeContext) -> None:
    """Module-level aliases keep the Phase 1-4 import surface working
    (tests import tool_registry / history_store / jail / ... from here)."""
    g = globals()
    for alias, attr in (("tool_registry", "registry"),
                        ("history_store", "history"),
                        ("jail", "jail"),
                        ("permission_engine", "permissions"),
                        ("checkpoints", "checkpoints"),
                        ("attach_store", "attach_store"),
                        ("compactor", "compactor"),
                        ("summarizer", "summarizer"),
                        ("instructions_resolver", "instructions"),
                        ("mcp_manager", "mcp"),
                        ("browser_engine", "browser"),
                        ("bg_tasks", "bg"),
                        ("skills_store", "skills"),
                        ("hooks_engine", "hooks")):
        g[alias] = getattr(ctx, attr)


def _default_workspace() -> str:
    return os.environ.get("AGENT_WORKSPACE", os.path.join(DATA, "workspace"))


# eager main scope: constructed at import (sync), opened in init_app.
# Settings live in main.db (the global store instance — one object, one
# connection), chats in history.db — exactly the Phase 0-4 layout.
_main_ctx = ScopeContext(
    "main", db_path=DB_PATH, workspace_root=_default_workspace(),
    sessions_dir=SESSIONS_DIR, toolcache_dir=os.path.join(DATA, "toolcache"),
    publish_live=publish_live, webtools=webtools, proxy_matrix=proxy_matrix,
    isolation_getter=_current_isolation, settings_store=settings_store,
    history_db_path=os.path.join(DATA, "history.db"))
scope_registry.set_main(_main_ctx)
_bind_main_scope(_main_ctx)

# chat_id -> {"task": asyncio.Task, "user": str | None}
running: dict[str, dict] = {}


# --------------------------------------------------------------------------- helpers

def json_response(data, status=200):
    return web.json_response(data, status=status,
                             dumps=lambda d: json.dumps(d, ensure_ascii=False))


SERVER_SETTING_KEYS = {
    "host", "port", "cors_origins", "mode", "catalog_resync_hours",
    "search_backend", "search_searxng_url", "search_brave_key",
    "web_cache_ttl_hours", "web_cache_max_mb", "proxy_matrix",
    "audit_enabled", "concurrency_generations", "concurrency_tools",
    "attach_quota_mb", "workspace", "browser_engine",
}
USER_SETTING_KEYS = {
    "permission_mode", "tool_timeout_default", "tool_timeout_max", "isolation",
    "default_system_prompt", "loop_iterations_limit",
    "compact_auto", "compact_threshold", "compact_keep_head",
    "compact_keep_last", "compact_model_key", "summarizer_model_key",
    "two_step_summarize", "tool_failure_streak",
    "fallback_chain", "retry_max_attempts", "retry_backoff",
    "sounds_enabled", "sound_turn_done", "sound_task_done",
    "sound_file_turn", "sound_file_task", "ui_lang", "theme", "custom_css",
}


async def merged_settings(ctx: ScopeContext) -> dict:
    """Server-level keys come from main.db; user-level keys from the scope
    store (identical in solo/team; properly separated in accounts mode)."""
    scope_s = await ctx.settings.get_settings()
    server_s = await settings_store.get_settings()
    scope_s.update({k: v for k, v in server_s.items()
                    if k in SERVER_SETTING_KEYS})
    return scope_s


async def scope_for(request) -> ScopeContext:
    session = request.get("af_session")
    if session is None or session.get("implicit"):
        return main_scope()
    mode = await auth.mode()
    if mode == "accounts":
        ctx = scope_registry.get(session["user_id"])
        if ctx is not None:
            return ctx
    return main_scope()


def require_admin(request) -> bool:
    session = request.get("af_session")
    return bool(session and session.get("role") == "admin"
                and not session.get("implicit"))


@web.middleware
async def cors_middleware(request, handler):
    settings = await settings_store.get_settings()
    origins = settings.get("cors_origins", "*")
    headers = {}
    if origins == "*":
        headers = {"Access-Control-Allow-Origin": "*"}
    elif request.headers.get("Origin") in origins.split(","):
        headers = {"Access-Control-Allow-Origin": request.headers["Origin"],
                   "Vary": "Origin",
                   "Access-Control-Allow-Credentials": "true"}
    headers.update({"Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type,Authorization"})
    if request.method == "OPTIONS":
        return web.Response(headers=headers)
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    for k, v in headers.items():
        resp.headers.setdefault(k, v)
    return resp


@web.middleware
async def auth_middleware(request, handler):
    """§2 mode gate: solo/setup let everything through with an implicit
    user; team/accounts demand a session on /api/* (auth + onboarding +
    health stay public). /api/admin additionally needs a real admin."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        authz = request.headers.get("Authorization", "")
        if authz.lower().startswith("bearer "):
            token = authz[7:].strip()
    try:
        session = await auth.check_request(token, request.path)
    except AuthError as e:
        return json_response({"error": "unauthorized", "reason": str(e)},
                             status=e.status)
    request["af_session"] = session
    if request.path.startswith("/api/admin") and not require_admin(request):
        return json_response({"error": "forbidden",
                              "reason": "нужны права администратора"}, status=403)
    try:
        return await handler(request)
    except AuthError as e:
        return json_response({"error": "auth", "reason": str(e)},
                             status=e.status)


async def get_provider_from(ctx: ScopeContext, model_key: str):
    if not model_key or "::" not in model_key:
        return None
    provider_id, model_id = model_key.split("::", 1)
    p = await ctx.settings.get_provider(provider_id)
    if not p:
        return None
    if p.get("key_sealed"):
        raise ProviderError("ключ провайдера запечатан — войдите заново "
                            "(или разблокируйте мастер-пароль)")
    return Provider(p["id"], p["host"], p["api_key"], p.get("proxy_url")), model_id


# --------------------------------------------------------------------------- turn control

async def _run_turn(chat_id: str, text: str, attachments, plan_mode: bool,
                    ctx: ScopeContext, username: str | None,
                    continue_mode: str | None = None):
    chat = await ctx.history.get_chat(chat_id)
    router = await ModelRouter.create(ctx.settings)
    s = await merged_settings(ctx)

    def _num(key, default):
        try:
            return float(s.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    try:
        loop_limit = max(1, int(s.get("loop_iterations_limit", "24") or 24))
    except (TypeError, ValueError):
        loop_limit = 24

    # §2 team concurrency: a queued turn shows its position as live frames
    def _queued(position: int) -> None:
        publish_live(chat_id, {"live": "queue", "position": position})

    granted = False
    try:
        granted = await turn_gate.acquire(on_position=_queued)
        runner = TurnRunner(
            ctx.history, router, chat.get("model_key"), ctx.registry,
            permissions=ctx.permissions, checkpoints=ctx.checkpoints,
            timeout_default=_num("tool_timeout_default", 30),
            timeout_max=_num("tool_timeout_max", 600),
            settings_store=ctx.settings,
            instructions=ctx.instructions,
            attach_store=ctx.attach_store,
            compactor=ctx.compactor,
            summarizer=ctx.summarizer,
            loop_limit=loop_limit,
            hooks=ctx.hooks, audit=audit, tool_gate=tool_gate,
            user=username)
        if continue_mode:
            gen = runner.run_continue(chat, directive=continue_mode == "directive")
        else:
            gen = runner.run(chat, text, attachments=attachments,
                             plan_mode=plan_mode)
        async for event in gen:
            if "live" in event:
                publish_live(chat_id, event)
    finally:
        if granted:
            turn_gate.release()
    LOGS.log_event("turn",
                   f"ход завершён: {chat_id} ({username or 'solo'})",
                   "debug")


def start_turn(chat_id: str, text: str, attachments, plan_mode: bool,
               ctx: ScopeContext, username: str | None) -> bool:
    """Returns False if a turn is already running (state machine lock)."""
    if chat_id in running and not running[chat_id]["task"].done():
        return False
    task = asyncio.create_task(
        _run_turn(chat_id, text, attachments, plan_mode, ctx, username))
    running[chat_id] = {"task": task, "user": username}
    return True


def start_continue(chat_id: str, mode: str, ctx: ScopeContext,
                   username: str | None) -> bool:
    task = asyncio.create_task(
        _run_turn(chat_id, "", None, False, ctx, username, mode or "prefill"))
    running[chat_id] = {"task": task, "user": username}
    return True


def _busy(chat_id: str) -> bool:
    info = running.get(chat_id)
    return bool(info and not info["task"].done())


async def _lock_reason_async(chat_id: str) -> str:
    """§2 shared projects: the 409 says WHO holds the lock."""
    user = (running.get(chat_id) or {}).get("user")
    if user and user != "solo":
        return f"locked by user {user}"
    return "генерация уже идёт"


# --------------------------------------------------------------------------- background

async def _refresh_provider_catalog(ctx: ScopeContext, p: dict) -> None:
    if p.get("key_sealed"):
        await ctx.settings.set_provider_health(p["id"], "error", None,
                                               "ключ запечатан — unlock требуется")
        return
    provider = Provider(p["id"], p["host"], p["api_key"], p.get("proxy_url"))
    result = await provider.probe()
    await ctx.settings.set_provider_health(p["id"], result["status"],
                                           result["latency_ms"], result["error"])
    if result["status"] == "online":
        try:
            models = await provider.list_models()
            await ctx.settings.replace_models(p["id"], models)
        except ProviderError as e:
            await ctx.settings.set_provider_health(p["id"], "error", None, str(e))


async def resync_all() -> None:
    """Catalog + health resync for every provider and MCP server in every
    open scope (main + accounts users)."""
    for ctx in scope_registry.all():
        for p in await ctx.settings.list_providers():
            try:
                await _refresh_provider_catalog(ctx, p)
            except Exception:  # noqa: BLE001
                pass
        try:
            await ctx.mcp.probe_all()
        except Exception:  # noqa: BLE001
            pass


def _apply_gates(s: dict) -> None:
    try:
        turn_gate.set_limit(int(s.get("concurrency_generations", "0") or 0))
    except (TypeError, ValueError):
        turn_gate.set_limit(0)
    try:
        tool_gate.set_limit(int(s.get("concurrency_tools", "0") or 0))
    except (TypeError, ValueError):
        tool_gate.set_limit(0)


async def _resync_loop():
    await asyncio.sleep(15)
    while True:
        try:
            s = await settings_store.get_settings()
            hours = max(float(s.get("catalog_resync_hours", "6") or 6), 0.25)
        except (TypeError, ValueError):
            hours = 6.0
        try:
            await resync_all()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(hours * 3600)


async def _session_purge_loop():
    while True:
        await asyncio.sleep(300)
        auth.purge_expired()


# --------------------------------------------------------------------------- API: system

async def handle_health(request):
    return json_response({"ok": True, "version": APP_VERSION})


async def handle_get_settings(request):
    ctx = await scope_for(request)
    s = await merged_settings(ctx)
    s["workspace"] = ctx.jail.root
    s["mode"] = await auth.mode()
    s["turn_queue"] = turn_gate.queued()
    return json_response(s)


async def handle_update_settings(request):
    ctx = await scope_for(request)
    body = await request.json()
    clean_server: dict = {}
    clean_user: dict = {}
    for k, v in body.items():
        if k == "permission_mode" and v not in ("ask", "allow_all"):
            continue
        if k == "isolation" and v not in ("off", "docker", "bubblewrap"):
            continue
        if k == "ui_lang" and v not in ("ru", "en"):
            continue
        if k == "theme" and v not in ("auto", "light", "dark"):
            continue
        if k == "fallback_chain":
            if isinstance(v, list):
                (clean_user if k in USER_SETTING_KEYS else clean_server)[k] = \
                    json.dumps([str(x) for x in v if "::" in str(x)])
            continue
        if k == "search_backend":
            if v in ("off", "duckduckgo", "searxng", "brave"):
                clean_server[k] = v
            continue
        if k == "browser_engine":
            if v in ("", "chromium", "firefox", "webkit"):
                clean_server[k] = v
            continue
        if k == "proxy_matrix":
            data = v
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    data = None
            if isinstance(data, dict):
                clean_server[k] = json.dumps(
                    {r: val for r, val in data.items()
                     if r in ("global", "model_api", "web", "websearch",
                              "webfetch", "browser")
                     and isinstance(val, dict) and val.get("url")},
                    ensure_ascii=False)
            continue
        if k in SERVER_SETTING_KEYS:
            if k in ("mode", "workspace"):
                continue          # mode/workspace change via onboarding/admin
            clean_server[k] = v
        elif k in USER_SETTING_KEYS:
            clean_user[k] = v
    if not clean_server and not clean_user:
        return json_response({"error": "nothing to update"}, status=400)
    if clean_user:
        await ctx.settings.set_settings(clean_user)
    if clean_server:
        await settings_store.set_settings(clean_server)
        _apply_gates(await settings_store.get_settings())
        await proxy_matrix.refresh()
        providers_mod.set_model_proxy(proxy_matrix.effective("model_api"))
    s = await merged_settings(ctx)
    _isolation_cache["value"] = s.get("isolation", "off")
    s["workspace"] = ctx.jail.root
    s["mode"] = await auth.mode()
    return json_response(s)


# --------------------------------------------------------------------------- API: providers

def _mask(p: dict) -> dict:
    out = dict(p)
    if out.get("api_key"):
        out["api_key"] = out["api_key"][:4] + "…" if len(out["api_key"]) > 4 else "…"
    # proxy_url stays readable: the editor round-trips it (the proxy matrix
    # in settings is plain text too)
    out.pop("_key", None)
    return out


async def handle_add_provider(request):
    ctx = await scope_for(request)
    body = await request.json()
    raw_host = (body.get("host") or "").strip()
    if not raw_host:
        return json_response({"error": "host обязателен"}, status=400)
    host = raw_host if raw_host.startswith("mock://") else normalize_host(raw_host)
    provider = await ctx.settings.add_provider(
        host, body.get("api_key"), body.get("name"), body.get("proxy_url"))

    error = None
    try:
        models = await Provider(provider["id"], host, provider["api_key"],
                                provider.get("proxy_url")).list_models()
        await ctx.settings.replace_models(provider["id"], models)
        await ctx.settings.set_provider_health(provider["id"], "online", None, None)
    except ProviderError as e:
        models = []
        error = str(e)
        await ctx.settings.set_provider_health(provider["id"], "error", None, str(e))

    return json_response({"provider": _mask(provider), "models": models,
                          "error": error}, status=201)


async def handle_list_providers(request):
    ctx = await scope_for(request)
    providers = await ctx.settings.list_providers()
    return json_response({"providers": [_mask(p) for p in providers]})


async def handle_delete_provider(request):
    ctx = await scope_for(request)
    await ctx.settings.delete_provider(request.match_info["id"])
    return json_response({"ok": True})


async def handle_patch_provider(request):
    """§17: per-provider proxy (wins over the model_api route)."""
    ctx = await scope_for(request)
    provider_id = request.match_info["id"]
    p = await ctx.settings.get_provider(provider_id)
    if not p:
        return json_response({"error": "not found"}, status=404)
    body = await request.json()
    proxy_url = (body.get("proxy_url") or "").strip() or None
    if proxy_url:
        from .proxy import ProxyConfigError, parse_proxy_url
        try:
            parse_proxy_url(proxy_url)      # validation only
        except ProxyConfigError as e:
            return json_response({"error": str(e)}, status=400)
    await ctx.settings.update_provider_proxy(provider_id, proxy_url)
    p = await ctx.settings.get_provider(provider_id)
    return json_response({"provider": _mask(p)})


async def handle_list_models(request):
    ctx = await scope_for(request)
    provider_id = request.query.get("provider")
    models = await ctx.settings.list_models(provider_id)
    return json_response({"models": models})


async def handle_refresh_models(request):
    ctx = await scope_for(request)
    provider_id = request.match_info["id"]
    p = await ctx.settings.get_provider(provider_id)
    if not p:
        return json_response({"error": "not found"}, status=404)
    await _refresh_provider_catalog(ctx, p)
    models = await ctx.settings.list_models(provider_id)
    return json_response({"models": models})


# --------------------------------------------------------------------------- API: model overrides

async def handle_probe_model(request):
    """Token probe (§9): ~1 token + absurd max_tokens -> the API itself
    names the real context limit in the rejection. Result saved as override
    only when the API actually revealed a number."""
    ctx = await scope_for(request)
    body = await request.json()
    model_key = body.get("model_key") or ""
    if "::" not in model_key:
        return json_response({"error": "нужен model_key"}, status=400)
    m = await ctx.settings.get_model(model_key)
    if not m:
        return json_response({"error": "модель не найдена"}, status=404)
    p = await ctx.settings.get_provider(m["provider_id"])
    if not p:
        return json_response({"error": "провайдер не найден"}, status=404)
    if p.get("key_sealed"):
        return json_response({"error": "ключ запечатан — разопечатайте провайдера"},
                             status=400)
    result = await Provider(p["id"], p["host"], p["api_key"],
                             p.get("proxy_url")).probe_context(
        m["model_id"])
    if result.get("context_length"):
        override = dict(m.get("override") or {})
        override["context_length"] = result["context_length"]
        await ctx.settings.set_model_override(model_key, override)
    result["model"] = await ctx.settings.get_model(model_key)
    return json_response(result)


async def handle_set_model_override(request):
    ctx = await scope_for(request)
    body = await request.json()
    model_key = body.get("model_key") or ""
    override = body.get("override")
    if "::" not in model_key or not isinstance(override, dict):
        return json_response({"error": "нужны model_key и override"}, status=400)
    if not await ctx.settings.get_model(model_key):
        return json_response({"error": "модель не найдена"}, status=404)
    await ctx.settings.set_model_override(model_key, override)
    return json_response({"model": await ctx.settings.get_model(model_key)})


async def handle_delete_model_override(request):
    ctx = await scope_for(request)
    model_key = request.query.get("model_key") or ""
    if "::" not in model_key:
        return json_response({"error": "нужен model_key"}, status=400)
    await ctx.settings.delete_model_override(model_key)
    return json_response({"model": await ctx.settings.get_model(model_key)})


# --------------------------------------------------------------------------- API: diagnostics

async def handle_diagnostics(request):
    ctx = await scope_for(request)
    providers = await ctx.settings.list_providers()

    async def probe_one(p):
        if p.get("key_sealed"):
            await ctx.settings.set_provider_health(
                p["id"], "error", None, "ключ запечатан")
            return {"id": p["id"], "name": p["name"], "status": "error",
                    "latency_ms": None, "error": "ключ запечатан"}
        r = await Provider(p["id"], p["host"], p["api_key"],
                           p.get("proxy_url")).probe()
        await ctx.settings.set_provider_health(p["id"], r["status"],
                                               r["latency_ms"], r["error"])
        return {"id": p["id"], "name": p["name"], **r}

    results = await asyncio.gather(*[probe_one(p) for p in providers],
                                   return_exceptions=True)
    out = []
    for p, r in zip(providers, results):
        if isinstance(r, Exception):
            await ctx.settings.set_provider_health(p["id"], "error", None, str(r))
            out.append({"id": p["id"], "name": p["name"], "status": "error",
                        "latency_ms": None, "error": str(r)})
        else:
            out.append(r)
    mcp = await ctx.mcp.probe_all()
    return json_response({"providers": out, "mcp": mcp})


# --------------------------------------------------------------------------- API: MCP

async def handle_add_mcp(request):
    ctx = await scope_for(request)
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return json_response({"error": "url обязателен"}, status=400)
    try:
        result = await ctx.mcp.discover(url)
    except (McpError, asyncio.TimeoutError) as e:
        return json_response({"error": f"discovery не удался: {e}"}, status=400)
    return json_response(result, status=201)


async def handle_list_mcp(request):
    ctx = await scope_for(request)
    servers = await ctx.settings.list_mcp_servers()
    for s in servers:
        try:
            s["server_info"] = json.loads(s.get("server_info") or "{}")
        except (json.JSONDecodeError, TypeError):
            s["server_info"] = {}
    return json_response({"servers": servers})


async def handle_delete_mcp(request):
    ctx = await scope_for(request)
    await ctx.mcp.delete(request.match_info["id"])
    return json_response({"ok": True})


async def handle_refresh_mcp(request):
    ctx = await scope_for(request)
    try:
        result = await ctx.mcp.refresh(request.match_info["id"])
    except (McpError, asyncio.TimeoutError) as e:
        return json_response({"error": f"refresh не удался: {e}"}, status=400)
    return json_response(result)


# --------------------------------------------------------------------------- API: chats

async def handle_list_chats(request):
    ctx = await scope_for(request)
    session = request.get("af_session")
    mode = await auth.mode()
    if mode == "accounts" and session and not session.get("implicit"):
        chats = await ctx.history.list_chats(owner=session["user_id"])
        return json_response({"chats": chats, "mode": mode})
    if mode == "team" and session and not session.get("implicit"):
        users = {u["id"]: u["username"]
                 for u in await settings_store.list_users()}
        chats = await ctx.history.list_chats(owner=session["user_id"],
                                             include_shared=True)
        for c in chats:
            c["shared"] = bool(c.get("shared"))
            c["mine"] = (c.get("owner") in (None, session["user_id"]))
            if c["shared"] and not c["mine"]:
                c["shared_by"] = users.get(c.get("owner"), "?")
        return json_response({"chats": chats, "mode": mode})
    chats = await ctx.history.list_chats()
    return json_response({"chats": chats, "mode": mode})


async def handle_create_chat(request):
    ctx = await scope_for(request)
    session = request.get("af_session")
    body = await request.json()
    params = body.get("params") if isinstance(body.get("params"), dict) else None
    owner = None
    mode = await auth.mode()
    if mode in ("team", "accounts") and session and not session.get("implicit"):
        owner = session["user_id"]
    chat = await ctx.history.create_chat(
        title=body.get("title") or "Новый чат",
        model_key=body.get("model_key"),
        system_prompt=body.get("system_prompt"),
        params=params,
        compact_threshold=body.get("compact_threshold"),
        owner=owner)
    return json_response({"chat": chat}, status=201)


async def handle_get_chat(request):
    ctx = await scope_for(request)
    chat = await ctx.history.get_chat(request.match_info["id"])
    if not chat:
        return json_response({"error": "not found"}, status=404)
    return json_response({"chat": chat})


async def handle_update_chat(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    body = await request.json()
    fields = {k: v for k, v in body.items()
              if k in ("title", "model_key", "system_prompt", "send_think_blocks",
                       "params", "compact_threshold", "shared")}
    await ctx.history.update_chat(chat_id, **fields)
    return json_response({"chat": await ctx.history.get_chat(chat_id)})


async def handle_delete_chat(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    info = running.get(chat_id)
    if info and not info["task"].done():
        info["task"].cancel()
    await ctx.history.delete_chat(chat_id)
    return json_response({"ok": True})


async def handle_chat_history(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    events = await ctx.history.all_events(chat_id)
    return json_response({"events": events,
                          "last_seq": await ctx.history.last_seq(chat_id)})


async def handle_send_message(request):
    chat_id = request.match_info["id"]
    ctx = await scope_for(request)
    session = request.get("af_session")
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    body = await request.json()
    text = (body.get("text") or "").strip()
    plan_mode = bool(body.get("plan"))
    if not text and not body.get("attachments"):
        return json_response({"error": "text пуст"}, status=400)

    if chat_id in running and not running[chat_id]["task"].done():
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)

    try:
        if not await get_provider_from(ctx, chat.get("model_key")):
            return json_response({"error": "no_model",
                                  "reason": "выберите провайдера и модель "
                                            "(селектор снизу справа)"},
                                 status=409)
    except ProviderError as e:
        return json_response({"error": "no_model", "reason": str(e)}, status=409)

    attachments = None
    ids = body.get("attachments")
    if isinstance(ids, list) and ids:
        attachments = []
        for att_id in [str(x) for x in ids][:10]:
            att = await _attach_meta_get(ctx, att_id)
            if att:
                attachments.append(att)
        if not attachments:
            return json_response({"error": "вложения не найдены"}, status=400)

    username = session.get("username") if session else None
    start_turn(chat_id, text, attachments, plan_mode, ctx, username)
    return json_response({"ok": True, "chat_id": chat_id,
                          "plan": plan_mode,
                          "queued": turn_gate.queued()}, status=201)


async def handle_stop(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    info = running.get(chat_id)
    if info and not info["task"].done():
        info["task"].cancel()
        return json_response({"ok": True, "stopped": True})
    return json_response({"ok": True, "stopped": False})


async def handle_chat_status(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    info = running.get(chat_id)
    generating = bool(info and not info["task"].done())
    pending = await ctx.settings.list_pending_approvals(chat_id)
    return json_response({"chat_id": chat_id, "generating": generating,
                          "pending_approvals": pending})


# --------------------------------------------------------------------------- API: context control (§9)

def _attach_meta_path(ctx: ScopeContext, att_id: str) -> str:
    return os.path.join(ctx.attach_store.dir, f"{str(att_id)}.json")


async def _attach_meta_get(ctx: ScopeContext, att_id: str) -> dict | None:
    path = _attach_meta_path(ctx, att_id)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


async def _check_attach_quota(ctx: ScopeContext, incoming_bytes: int) -> str | None:
    """§10: attachment quota — accounts mode only, server setting."""
    mode = await auth.mode()
    if mode != "accounts":
        return None
    try:
        quota_mb = float((await settings_store.get_settings())
                         .get("attach_quota_mb", "0") or 0)
    except (TypeError, ValueError):
        quota_mb = 0
    if quota_mb <= 0:
        return None
    used = 0
    try:
        for fname in os.listdir(ctx.attach_store.dir):
            fp = os.path.join(ctx.attach_store.dir, fname)
            if os.path.isfile(fp):
                used += os.path.getsize(fp)
    except OSError:
        pass
    if (used + incoming_bytes) > quota_mb * 1024 * 1024:
        return (f"квота вложений превышена ({quota_mb:g} МБ; занято "
                f"{used / 1024 / 1024:.1f} МБ)")
    return None


async def handle_edit_event(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    event_id = request.match_info["eid"]
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    target = await ctx.history.get_event(chat_id, event_id)
    if not target:
        return json_response({"error": "not found"}, status=404)
    if target["type"] not in ev.CONTENT_TYPES:
        return json_response({"error": "этот тип события не редактируется"}, status=400)
    body = await request.json()
    data = body.get("data")
    if not isinstance(data, dict):
        return json_response({"error": "нужен data (новый payload)"}, status=400)
    edit_ev = await ctx.history.append(chat_id, ev.EDIT, data, target=event_id)
    return json_response({"edit": edit_ev})


async def handle_delete_event(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    event_id = request.match_info["eid"]
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    target = await ctx.history.get_event(chat_id, event_id)
    if not target:
        return json_response({"error": "not found"}, status=404)
    if target["type"] not in ev.CONTENT_TYPES | {ev.PLAN_APPROVED, ev.COMPACT}:
        return json_response({"error": "этот тип события не удаляется"}, status=400)
    checkpoint = await _turn_checkpoint_hint(ctx, chat_id, event_id)
    del_ev = await ctx.history.append(chat_id, ev.DELETE, {}, target=event_id)
    return json_response({"deleted": True, "event": del_ev,
                          "checkpoint": checkpoint})


async def _turn_checkpoint_hint(ctx: ScopeContext, chat_id: str,
                                event_id: str) -> dict | None:
    events = await ctx.history.all_events(chat_id)
    idx = next((i for i, e in enumerate(events) if e["id"] == event_id), None)
    if idx is None:
        return None
    start = idx
    while start >= 0 and events[start]["type"] != ev.USER_MESSAGE:
        start -= 1
    for j in range(max(start, 0), len(events)):
        if events[j]["type"] == ev.TURN_META:
            meta_id = events[j]["id"]
            for cp in await ctx.checkpoints.list(chat_id):
                if cp.get("turn_event_id") == meta_id:
                    return {"checkpoint_id": cp["id"],
                            "commit": cp["commit_sha"][:10],
                            "message": cp["message"], "files": cp["files"]}
            return None
    return None


async def handle_event_versions(request):
    ctx = await scope_for(request)
    versions = await ctx.history.event_versions(
        request.match_info["id"], request.match_info["eid"])
    if not versions:
        return json_response({"error": "not found"}, status=404)
    return json_response(versions)


async def handle_raw_events(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    if not await ctx.history.get_chat(chat_id):
        return json_response({"error": "not found"}, status=404)
    return json_response({"events": await ctx.history.raw_events(chat_id)})


async def handle_fork_chat(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    body = await request.json() if request.can_read_body else {}
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    until = body.get("until_event_id")
    new_chat = await ctx.history.fork_chat(
        chat_id, until, (chat.get("title") or "чат") + " (форк)")
    return json_response({"chat": new_chat}, status=201)


async def handle_continue(request):
    ctx = await scope_for(request)
    session = request.get("af_session")
    chat_id = request.match_info["id"]
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    body = await request.json() if request.can_read_body else {}
    mode = body.get("mode") or "prefill"
    if mode not in ("prefill", "directive"):
        return json_response({"error": "mode: prefill|directive"}, status=400)
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    try:
        if not await get_provider_from(ctx, chat.get("model_key")):
            return json_response({"error": "no_model",
                                  "reason": "модель не выбрана"}, status=409)
    except ProviderError as e:
        return json_response({"error": "no_model", "reason": str(e)}, status=409)
    username = session.get("username") if session else None
    ok = start_continue(chat_id, mode, ctx, username)
    return json_response({"ok": ok}, status=201 if ok else 409)


# --------------------------------------------------------------------------- API: compaction (§11)

async def handle_compact(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    event = await ctx.compactor.compact(chat, reason="manual")
    return json_response({"compact": event,
                          "note": None if event else "сжимать нечего — "
                                  "слишком мало событий между головой и хвостом"})


async def handle_clear(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    event = await ctx.compactor.clear(chat)
    return json_response({"cleared": True, "compact": event})


# --------------------------------------------------------------------------- API: plan mode (§5)

async def handle_plan_action(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    event_id = request.match_info["eid"]
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    plan_event = await ctx.history.get_event(chat_id, event_id)
    if not plan_event or plan_event["type"] != ev.PLAN_SUBMITTED:
        return json_response({"error": "не найден план с таким id"}, status=404)
    for e in await ctx.history.all_events(chat_id):
        if e["type"] in (ev.PLAN_APPROVED, ev.PLAN_REJECTED) \
                and (e.get("data") or {}).get("plan_event_id") == event_id:
            return json_response({"error": "план уже обработан"}, status=409)
    body = await request.json() if request.can_read_body else {}
    action = body.get("action")
    if action == "approve":
        event = await approve_plan(ctx.history, chat_id, plan_event,
                                   body.get("plan_text"))
    elif action == "reject":
        event = await reject_plan(ctx.history, chat_id, plan_event)
    else:
        return json_response({"error": "action: approve|reject"}, status=400)
    return json_response({"event": event}, status=201)


async def handle_plan_checklist(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    event_id = request.match_info["eid"]
    if _busy(chat_id):
        return json_response({"error": "locked",
                              "reason": await _lock_reason_async(chat_id)},
                             status=409)
    body = await request.json()
    try:
        index = int(body.get("index"))
        done = bool(body.get("done"))
    except (TypeError, ValueError):
        return json_response({"error": "нужны index и done"}, status=400)
    target = None
    for e in await ctx.history.all_events(chat_id):
        if e["id"] == event_id:
            target = e
            break
    if not target or target["type"] != ev.PLAN_APPROVED:
        return json_response({"error": "не найден одобренный план"}, status=404)
    checklist = list((target.get("data") or {}).get("checklist") or [])
    if not (0 <= index < len(checklist)):
        return json_response({"error": "index вне диапазона"}, status=400)
    checklist[index] = {**checklist[index], "done": done}
    new_data = {**target["data"], "checklist": checklist}
    edit_ev = await ctx.history.append(chat_id, ev.EDIT, new_data,
                                       target=event_id)
    return json_response({"edit": edit_ev, "checklist": checklist})


# --------------------------------------------------------------------------- API: attachments (§10)

async def handle_upload_attachments(request):
    """multipart upload -> workspace attachments/ + sidecar meta. The §10
    accounts-mode quota is enforced incrementally (per part, on the running
    total of the attachments dir)."""
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    if not await ctx.history.get_chat(chat_id):
        return json_response({"error": "not found"}, status=404)
    reader = await request.multipart()
    saved = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename is None:
            continue
        data = await part.read(decode=False)
        quota_err = await _check_attach_quota(ctx, len(data))
        if quota_err:
            return json_response({"error": quota_err}, status=400)
        try:
            att = ctx.attach_store.save(part.filename, data,
                                        part.headers.get("Content-Type"))
        except ValueError as e:
            return json_response({"error": str(e)}, status=400)
        try:
            with open(_attach_meta_path(ctx, att["id"]), "w", encoding="utf-8") as f:
                json.dump(att, f, ensure_ascii=False)
        except OSError:
            pass
        saved.append(att)
    if not saved:
        return json_response({"error": "файлы не переданы"}, status=400)
    return json_response({"attachments": saved}, status=201)


async def handle_list_attachments(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    if not await ctx.history.get_chat(chat_id):
        return json_response({"error": "not found"}, status=404)
    out = []
    try:
        for fname in sorted(os.listdir(ctx.attach_store.dir)):
            if not fname.endswith(".json"):
                continue
            att = await _attach_meta_get(ctx, fname[:-5])
            if att:
                out.append(att)
    except OSError:
        pass
    return json_response({"attachments": out})


# --------------------------------------------------------------------------- API: workspace (files panel)

async def handle_workspace_tree(request):
    ctx = await scope_for(request)
    path = request.query.get("path") or "."
    try:
        full = ctx.jail.resolve(path)
    except JailError as e:
        return json_response({"error": str(e)}, status=400)
    if not os.path.isdir(full):
        return json_response({"error": "не папка"}, status=400)
    entries = []
    try:
        with os.scandir(full) as it:
            for entry in it:
                if entry.name == ".agent":
                    continue
                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=False)
                    size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                entries.append({"name": entry.name,
                                "type": "link" if is_link else
                                        ("dir" if is_dir else "file"),
                                "size": size,
                                "path": os.path.relpath(
                                    os.path.join(full, entry.name), ctx.jail.root)})
    except OSError as e:
        return json_response({"error": str(e)}, status=400)
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return json_response({"path": ctx.jail.relative(path), "entries": entries})


async def handle_workspace_file(request):
    ctx = await scope_for(request)
    path = request.query.get("path") or ""
    try:
        full = ctx.jail.check_read(path)
    except JailError as e:
        return json_response({"error": str(e)}, status=403)
    if not os.path.isfile(full):
        return json_response({"error": "не файл"}, status=404)
    size = os.path.getsize(full)
    with open(full, "rb") as f:
        head = f.read(8192)
    if b"\x00" in head:
        return json_response({"path": path, "binary": True, "size": size})
    cap = 256 * 1024
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(cap)
    return json_response({"path": path, "size": size, "binary": False,
                          "truncated": size > cap, "content": content})


# --------------------------------------------------------------------------- API: permissions

async def handle_permission_rules(request):
    ctx = await scope_for(request)
    return json_response({"rules": await ctx.settings.list_rules(),
                          "mode": (await ctx.settings.get_settings())
                          .get("permission_mode", "ask")})


async def handle_add_rule(request):
    ctx = await scope_for(request)
    body = await request.json()
    effect = body.get("effect")
    if effect not in ("allow", "deny"):
        return json_response({"error": "effect: allow|deny"}, status=400)
    rule = await ctx.settings.add_rule(effect, str(body.get("tool") or "*"),
                                       str(body.get("pattern") or ""),
                                       body.get("note"))
    return json_response({"rule": rule}, status=201)


async def handle_delete_rule(request):
    ctx = await scope_for(request)
    await ctx.settings.delete_rule(request.match_info["id"])
    return json_response({"ok": True})


async def handle_list_approvals(request):
    ctx = await scope_for(request)
    chat_id = request.query.get("chat")
    if not chat_id:
        return json_response({"error": "нужен ?chat="}, status=400)
    return json_response(
        {"pending": await ctx.settings.list_pending_approvals(chat_id)})


async def handle_resolve_approval(request):
    ctx = await scope_for(request)
    approval_id = request.match_info["id"]
    body = await request.json()
    decision = body.get("decision")
    if decision not in ("once", "always", "deny"):
        return json_response({"error": "decision: once|always|deny"}, status=400)
    existing = await ctx.settings.get_approval(approval_id)
    if not existing:
        return json_response({"error": "not found"}, status=404)
    if existing["status"] != "pending":
        return json_response({"error": "уже решено", "approval": existing},
                             status=409)
    row = await ctx.permissions.resolve(approval_id, decision)
    return json_response({"approval": row})


# --------------------------------------------------------------------------- API: checkpoints

async def handle_list_checkpoints(request):
    ctx = await scope_for(request)
    chat_id = request.query.get("chat")
    return json_response({"checkpoints": await ctx.checkpoints.list(chat_id)})


async def handle_checkpoint_diff(request):
    ctx = await scope_for(request)
    try:
        result = await ctx.checkpoints.diff(request.match_info["id"])
    except CheckpointError as e:
        return json_response({"error": str(e)}, status=404)
    return json_response(result)


async def handle_revert_prepare(request):
    ctx = await scope_for(request)
    try:
        result = await ctx.checkpoints.prepare_revert(request.match_info["id"])
    except CheckpointError as e:
        return json_response({"error": str(e)}, status=400)
    return json_response(result)


async def handle_revert_apply(request):
    ctx = await scope_for(request)
    body = await request.json()
    force = bool(body.get("force"))
    try:
        result = await ctx.checkpoints.apply_revert(request.match_info["id"], force)
    except CheckpointError as e:
        return json_response({"error": str(e)}, status=409)
    return json_response(result)


async def handle_checkpoint_create(request):
    ctx = await scope_for(request)
    body = await request.json()
    try:
        cp = await ctx.checkpoints.snapshot(body.get("chat_id"), None,
                                            body.get("message") or "manual")
    except CheckpointError as e:
        return json_response({"error": str(e)}, status=400)
    if not cp:
        return json_response({"checkpoint": None,
                              "note": "нет изменений — коммитить нечего"})
    return json_response({"checkpoint": cp}, status=201)


async def handle_tools_catalog(request):
    ctx = await scope_for(request)
    return json_response({"tools": ctx.registry.describe()})


# --------------------------------------------------------------------------- API: context / usage

async def handle_chat_context(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    chat = await ctx.history.get_chat(chat_id)
    if not chat:
        return json_response({"error": "not found"}, status=404)
    last_usage = None
    for e in await ctx.history.all_events(chat_id):
        if e["type"] == ev.TURN_META and e["data"].get("usage"):
            last_usage = e["data"]["usage"]
    model = await ctx.settings.get_model(chat.get("model_key") or "")
    window = (model or {}).get("context_length")
    prompt_tok = (last_usage or {}).get("prompt_tokens")
    percent = round(prompt_tok / window * 100, 1) if (prompt_tok and window) else None
    return json_response({
        "usage": last_usage,
        "prompt_tokens": prompt_tok,
        "context_length": window,
        "percent": percent,
    })


async def handle_chat_usage(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    prompt = completion = 0
    turns = 0
    turns_with_usage = 0
    for e in await ctx.history.all_events(chat_id):
        if e["type"] != ev.TURN_META:
            continue
        turns += 1
        u = e["data"].get("usage") or {}
        if not u:
            continue
        turns_with_usage += 1
        prompt += u.get("prompt_tokens", 0) or 0
        completion += u.get("completion_tokens", 0) or 0
    return json_response({"turns": turns, "turns_with_usage": turns_with_usage,
                          "prompt_tokens": prompt,
                          "completion_tokens": completion,
                          "total_tokens": prompt + completion})


# --------------------------------------------------------------------------- API: Phase 4 web layer

async def handle_session_search(request):
    ctx = await scope_for(request)
    q = request.query.get("q") or ""
    results = await search_sessions(ctx.history, q)
    return json_response({"query": q, "results": results})


async def handle_webtools_status(request):
    ctx = await scope_for(request)
    return json_response({**await webtools.status(),
                          "proxy": proxy_matrix.describe(),
                          "browser": await ctx.browser.status()})


async def handle_webtools_cache_clear(request):
    await settings_store.web_cache_clear()
    return json_response({"ok": True,
                          "cache": await settings_store.web_cache_stats()})


# -- macros (§6) ---------------------------------------------------------------

async def handle_list_macros(request):
    ctx = await scope_for(request)
    return json_response({"macros": await ctx.settings.list_macros()})


async def handle_add_macro(request):
    ctx = await scope_for(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    prompt = body.get("prompt") or ""
    if not name or not prompt.strip():
        return json_response({"error": "нужны name и prompt"}, status=400)
    macro = await ctx.settings.add_macro(
        name, prompt, body.get("mode") or "fill", body.get("hotkey"))
    return json_response({"macro": macro}, status=201)


async def handle_update_macro(request):
    ctx = await scope_for(request)
    body = await request.json()
    macro = await ctx.settings.update_macro(request.match_info["id"], body)
    if not macro:
        return json_response({"error": "not found"}, status=404)
    return json_response({"macro": macro})


async def handle_delete_macro(request):
    ctx = await scope_for(request)
    await ctx.settings.delete_macro(request.match_info["id"])
    return json_response({"ok": True})


# -- background tasks (§13) ------------------------------------------------------

async def handle_background_list(request):
    ctx = await scope_for(request)
    chat_id = request.query.get("chat")
    return json_response({"tasks": ctx.bg.list(chat_id or None)})


async def handle_background_cancel(request):
    ctx = await scope_for(request)
    result = await ctx.bg.cancel(request.match_info["id"])
    if "error" in result:
        return json_response(result, status=404)
    return json_response(result)


# -- browser (§13) -----------------------------------------------------------------

async def handle_browser_status(request):
    ctx = await scope_for(request)
    return json_response(await ctx.browser.status())


async def handle_browser_engines(request):
    """§13 browser settings: Playwright's own engine list + install state."""
    ctx = await scope_for(request)
    return json_response(await ctx.browser.list_browsers())


async def handle_browser_select(request):
    ctx = await scope_for(request)
    body = await request.json()
    result = await ctx.browser.select_engine(body.get("engine") or "")
    status = 200 if "error" not in result else 400
    return json_response(result, status=status)


async def handle_browser_install(request):
    ctx = await scope_for(request)
    body = await request.json()
    result = await ctx.browser.install_engine(body.get("engine") or "")
    status = 202 if result.get("started") else 400
    return json_response(result, status=status)


async def handle_browser_close(request):
    ctx = await scope_for(request)
    body = await request.json() if request.can_read_body else {}
    chat_id = body.get("chat_id") or ""
    if not chat_id:
        return json_response({"error": "нужен chat_id"}, status=400)
    return json_response(await ctx.browser.close_session(chat_id))


async def handle_browser_ws(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    ws = web.WebSocketResponse(max_msg_size=1024 * 1024)
    await ws.prepare(request)
    try:
        await ctx.browser.screencast(chat_id, ws)
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    return ws


# -- sounds (§10) --------------------------------------------------------------------

async def handle_sounds_list(request):
    out = []
    try:
        for fname in sorted(os.listdir(SOUNDS_DIR)):
            if fname.lower().endswith(SOUND_EXTS):
                out.append({"name": fname,
                            "bytes": os.path.getsize(os.path.join(SOUNDS_DIR, fname))})
    except OSError:
        pass
    return json_response({"sounds": out})


async def handle_sounds_upload(request):
    reader = await request.multipart()
    saved = []
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename is None:
            continue
        data = await part.read(decode=False)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", part.filename)
        if not name.lower().endswith(SOUND_EXTS):
            return json_response(
                {"error": f"поддерживаются {', '.join(SOUND_EXTS)}"}, status=400)
        if len(data) > SOUND_MAX_BYTES:
            return json_response({"error": "файл больше 2 МБ"}, status=400)
        with open(os.path.join(SOUNDS_DIR, name), "wb") as f:
            f.write(data)
        saved.append({"name": name, "bytes": len(data)})
    if not saved:
        return json_response({"error": "файлы не переданы"}, status=400)
    return json_response({"sounds": saved}, status=201)


async def handle_sounds_file(request):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.match_info["name"])
    path = os.path.join(SOUNDS_DIR, name)
    if not name.lower().endswith(SOUND_EXTS) or not os.path.isfile(path):
        return json_response({"error": "not found"}, status=404)
    return web.FileResponse(path)


# --------------------------------------------------------------------------- API: Phase 5 — auth / onboarding

async def handle_auth_me(request):
    session = request.get("af_session")
    mode = await auth.mode()
    user = None
    if session and not session.get("implicit"):
        user = {"username": session["username"], "role": session["role"]}
    return json_response({
        "user": user,
        "mode": mode,
        "needs_onboarding": await auth.needs_onboarding(),
        "admin_exists": (await settings_store.count_users()) > 0,
        "master_configured": await auth.master_configured(),
        "master_locked": await auth.master_locked(),
    })


async def handle_auth_login(request):
    body = await request.json()
    session = await auth.verify_login(body.get("username") or "",
                                      body.get("password") or "")
    mode = await auth.mode()
    if mode == "accounts":
        # build the user scope right away (with the derived key cipher)
        user = await settings_store.get_user(session["user_id"])
        await scope_registry.get_or_create_user(user, session.get("_key"))
    resp = json_response({"user": {"username": session["username"],
                                   "role": session["role"]},
                          "mode": mode})
    resp.set_cookie(SESSION_COOKIE, session["token"], max_age=7 * 24 * 3600,
                    httponly=True, samesite="Lax")
    return resp


async def handle_auth_logout(request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        auth.logout(token)
    resp = json_response({"ok": True})
    resp.del_cookie(SESSION_COOKIE)
    return resp


async def handle_onboarding_status(request):
    mode = await auth.mode()
    return json_response({
        "mode": mode,
        "needs_onboarding": mode == "setup",
        "users": await settings_store.count_users(),
        "providers": len(await main_scope().settings.list_providers()),
        "workspace": main_scope().jail.root,
        "master_configured": await auth.master_configured(),
    })


async def handle_onboarding_register(request):
    """§16: the FIRST registered user becomes the admin (claim-once)."""
    if await settings_store.count_users() > 0:
        return json_response({"error": "админ уже есть — войдите (или попросите "
                                       "админа создать аккаунт)"}, status=409)
    body = await request.json()
    try:
        user = await auth.register(body.get("username") or "",
                                   body.get("password") or "")
    except AuthError as e:
        return json_response({"error": str(e)}, status=e.status)
    # auto-login the fresh admin
    session = await auth.verify_login(body.get("username") or "",
                                      body.get("password") or "")
    resp = json_response({"user": {"username": user["username"],
                                   "role": user["role"]}})
    resp.set_cookie(SESSION_COOKIE, session["token"], max_age=7 * 24 * 3600,
                    httponly=True, samesite="Lax")
    return resp


async def handle_onboarding_finish(request):
    """§16: mode chosen (provider + workspace set through the normal APIs
    during the wizard) — the server goes into its mode."""
    body = await request.json()
    mode = body.get("mode")
    if mode not in ("solo", "team", "accounts"):
        return json_response({"error": "mode: solo|team|accounts"}, status=400)
    current = await auth.mode()
    if current != "setup" and not require_admin(request):
        return json_response({"error": "онбординг уже пройден "
                                       "(нужны права админа для смены)"}, status=409)
    if mode in ("team", "accounts") and await settings_store.count_users() == 0:
        return json_response({"error": "сначала зарегистрируйте владельца "
                                       "(админа)"}, status=400)
    workspace = (body.get("workspace") or "").strip()
    if workspace:
        workspace = os.path.abspath(os.path.expanduser(workspace))
        try:
            os.makedirs(workspace, exist_ok=True)
        except OSError as e:
            return json_response({"error": f"не удалось создать папку: {e}"},
                                 status=400)
        await settings_store.set_settings({"workspace": workspace})
        ctx = await scope_registry.rebuild_main(workspace)
        _bind_main_scope(ctx)
        _isolation_cache["value"] = (await ctx.settings.get_settings()) \
            .get("isolation", "off")
    await auth.set_mode(mode)
    LOGS.log_event("onboarding", f"онбординг завершён: режим {mode}")
    return json_response({"ok": True, "mode": mode,
                          "workspace": main_scope().jail.root})


# --------------------------------------------------------------------------- API: Phase 5 — admin

async def handle_admin_overview(request):
    mode = await auth.mode()
    ctx = main_scope()
    return json_response({
        "mode": mode,
        "version": APP_VERSION,
        "users": await settings_store.list_users(),
        "providers": len(await ctx.settings.list_providers()),
        "workspace": ctx.jail.root,
        "audit_enabled": (await settings_store.get_settings())
        .get("audit_enabled", "0") == "1",
        "master_configured": await auth.master_configured(),
        "master_locked": await auth.master_locked(),
        "turn_queue": turn_gate.queued(),
        "active_turns": turn_gate.active,
        "scopes": sorted(scope_registry._scopes),
    })


async def handle_admin_users_create(request):
    body = await request.json()
    try:
        user = await auth.register(body.get("username") or "",
                                   body.get("password") or "",
                                   role=body.get("role") or None)
    except AuthError as e:
        return json_response({"error": str(e)}, status=e.status)
    return json_response({"user": {k: user[k] for k in
                                   ("id", "username", "role", "created_at")}},
                         status=201)


async def handle_admin_users_password(request):
    user_id = request.match_info["id"]
    user = await settings_store.get_user(user_id)
    if not user:
        return json_response({"error": "not found"}, status=404)
    body = await request.json()
    password = body.get("password") or ""
    if len(password) < 4:
        return json_response({"error": "пароль минимум 4 символа"}, status=400)
    import secrets as _secrets
    pw_salt = _secrets.token_bytes(16)
    key_salt = _secrets.token_bytes(16)
    from .auth import _scrypt, _hex
    await settings_store.set_user_password(
        user_id, _hex(pw_salt), _scrypt(password, pw_salt), _hex(key_salt))
    # the new key salt invalidates sealed provider keys of that user —
    # honest behaviour: they re-enter keys after a password reset
    LOGS.log_event("auth", f"пароль сброшен: {user['username']}")
    return json_response({"ok": True, "note": "пользователь должен перезайти; "
                                              "запечатанные ключи нужно "
                                              "ввести заново"})


async def handle_admin_users_patch(request):
    user_id = request.match_info["id"]
    user = await settings_store.get_user(user_id)
    if not user:
        return json_response({"error": "not found"}, status=404)
    body = await request.json()
    if "role" in body:
        if body["role"] not in ("admin", "member"):
            return json_response({"error": "role: admin|member"}, status=400)
        await settings_store.set_user_role(user_id, body["role"])
    if "disabled" in body:
        await settings_store.set_user_disabled(user_id, bool(body["disabled"]))
    return json_response({"user": await settings_store.get_user(user_id)})


async def handle_admin_users_delete(request):
    user_id = request.match_info["id"]
    session = request.get("af_session")
    if session and session.get("user_id") == user_id:
        return json_response({"error": "нельзя удалить себя"}, status=400)
    user = await settings_store.get_user(user_id)
    if not user:
        return json_response({"error": "not found"}, status=404)
    # drop the user's scope (accounts mode) + sessions
    ctx = scope_registry._scopes.pop(user_id, None)
    if ctx:
        await ctx.close()
    for token in [t for t, s in auth.sessions.items()
                  if s.get("user_id") == user_id]:
        auth.sessions.pop(token, None)
    await settings_store.delete_user(user_id)
    LOGS.log_event("auth", f"пользователь удалён: {user['username']}")
    return json_response({"ok": True})


async def handle_admin_mode(request):
    body = await request.json()
    mode = body.get("mode")
    if mode not in ("solo", "team", "accounts"):
        return json_response({"error": "mode: solo|team|accounts"}, status=400)
    if mode in ("team", "accounts") and await settings_store.count_users() == 0:
        return json_response({"error": "без пользователей режим невозможен — "
                                       "создайте аккаунты"}, status=400)
    old = await auth.mode()
    await auth.set_mode(mode)
    # mode switches invalidate every session — everyone re-logs in the new
    # reality (accounts needs fresh login to derive key ciphers anyway)
    auth.sessions.clear()
    note = None
    if (old, mode) in (("accounts", "solo"), ("accounts", "team")):
        note = ("чаты аккаунтов остались в их <user>.db — вернитесь в режим "
                "аккаунтов, чтобы снова их увидеть")
    if (old, mode) in (("solo", "accounts"), ("team", "accounts")):
        note = ("пользователи увидят пустые личные сессии; общие чаты остались "
                "в main.db (видны при возврате в соло/команду)")
    LOGS.log_event("admin", f"режим изменён: {old} -> {mode}")
    return json_response({"ok": True, "mode": mode, "note": note,
                          "relogin": True})


async def handle_admin_workspace(request):
    body = await request.json()
    path = (body.get("path") or "").strip()
    if not path:
        return json_response({"error": "нужен path"}, status=400)
    path = os.path.abspath(os.path.expanduser(path))
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return json_response({"error": f"не удалось создать папку: {e}"},
                             status=400)
    await settings_store.set_settings({"workspace": path})
    ctx = await scope_registry.rebuild_main(path)
    _bind_main_scope(ctx)
    _isolation_cache["value"] = (await ctx.settings.get_settings()) \
        .get("isolation", "off")
    LOGS.log_event("admin", f"рабочая папка: {path}")
    return json_response({"ok": True, "workspace": ctx.jail.root})


async def handle_admin_master_password(request):
    body = await request.json()
    password = body.get("password")
    try:
        result = await auth.set_master_password(password)
    except Exception as e:  # noqa: BLE001
        return json_response({"error": str(e)}, status=400)
    return json_response(result)


async def handle_admin_unlock(request):
    body = await request.json()
    try:
        ok = await auth.unlock_master(body.get("password") or "")
    except AuthError as e:
        return json_response({"error": str(e)}, status=e.status)
    if not ok:
        return json_response({"error": "неверный мастер-пароль"}, status=401)
    return json_response({"ok": True})


async def handle_admin_audit(request):
    limit = min(int(request.query.get("limit", "200") or 200), 1000)
    chat_id = request.query.get("chat")
    return json_response({"entries": await audit.list(limit, chat_id)})


# --------------------------------------------------------------------------- API: Phase 5 — skills / hooks / themes / logs

async def handle_skills_list(request):
    ctx = await scope_for(request)
    return json_response({"skills": await ctx.skills.list()})


async def handle_skills_upsert(request):
    ctx = await scope_for(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        return json_response({"error": "нужно name"}, status=400)
    skill = await ctx.skills.upsert(name, body.get("description") or "",
                                    body.get("body") or "")
    return json_response({"skill": skill}, status=201)


async def handle_skills_import(request):
    """SKILL.md round-trip: frontmatter (name/description) + markdown body."""
    ctx = await scope_for(request)
    if request.content_type and "multipart" in (request.content_type or ""):
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.filename is None:
            return json_response({"error": "файл не передан"}, status=400)
        text = (await part.read()).decode("utf-8", errors="replace")
    else:
        body = await request.json()
        text = body.get("text") or ""
    skill = await ctx.skills.import_md(text)
    if not skill:
        return json_response({"error": "не удалось разобрать SKILL.md "
                                       "(нужно name в frontmatter или '# заголовок')"},
                             status=400)
    return json_response({"skill": {k: skill[k] for k in
                                    ("name", "description")}}, status=201)


async def handle_skills_export(request):
    ctx = await scope_for(request)
    skill = await ctx.skills.get(request.match_info["name"])
    if not skill:
        return json_response({"error": "not found"}, status=404)
    from .skills import render_skill_md
    return web.Response(text=render_skill_md(skill),
                        content_type="text/markdown",
                        headers={"Content-Disposition":
                                 f'attachment; filename="{skill["name"]}.SKILL.md"'})


async def handle_skills_delete(request):
    ctx = await scope_for(request)
    await ctx.skills.delete(request.match_info["name"])
    return json_response({"ok": True})


async def handle_hooks_list(request):
    ctx = await scope_for(request)
    return json_response({"hooks": await ctx.settings.list_hooks(),
                          "events": list(ctx.settings.HOOK_EVENTS)})


async def handle_hooks_add(request):
    ctx = await scope_for(request)
    body = await request.json()
    event = body.get("event")
    command = (body.get("command") or "").strip()
    if event not in ctx.settings.HOOK_EVENTS or not command:
        return json_response({"error": f"event ∈ {ctx.settings.HOOK_EVENTS}, "
                                       "command обязателен"}, status=400)
    try:
        timeout = min(max(float(body.get("timeout", 10) or 10), 1), 60)
    except (TypeError, ValueError):
        timeout = 10
    hook = await ctx.settings.add_hook(event, command, timeout,
                                       bool(body.get("enabled", True)))
    return json_response({"hook": hook}, status=201)


async def handle_hooks_patch(request):
    ctx = await scope_for(request)
    body = await request.json()
    if "timeout" in body:
        try:
            body["timeout"] = min(max(float(body["timeout"] or 10), 1), 60)
        except (TypeError, ValueError):
            body["timeout"] = 10
    if "enabled" in body:
        body["enabled"] = int(bool(body["enabled"]))
    hook = await ctx.settings.update_hook(request.match_info["id"], body)
    if not hook:
        return json_response({"error": "not found или неверный event"}, status=404)
    return json_response({"hook": hook})


async def handle_hooks_delete(request):
    ctx = await scope_for(request)
    await ctx.settings.delete_hook(request.match_info["id"])
    return json_response({"ok": True})


async def handle_hooks_test(request):
    """Fire a command with a sample payload — the GUI editor's 'проверить'."""
    ctx = await scope_for(request)
    body = await request.json()
    command = (body.get("command") or "").strip()
    if not command:
        return json_response({"error": "command обязателен"}, status=400)
    try:
        timeout = min(max(float(body.get("timeout", 10) or 10), 1), 60)
    except (TypeError, ValueError):
        timeout = 10
    hook = {"event": body.get("event") or "Manual", "command": command,
            "timeout": timeout}
    out, code = await ctx.hooks._run_command(
        hook, {"event": hook["event"], "hook_id": "test", "chat_id": None,
               "user": "тест", "tool": "example", "args": {}})
    return json_response({"stdout": out[:4000], "exit_code": code,
                          "timeout": code is None})


# -- themes (§16) ---------------------------------------------------------------

def _theme_files() -> list[dict]:
    out = []
    try:
        for fname in sorted(os.listdir(THEMES_DIR)):
            if fname.endswith(".css"):
                out.append({"name": fname,
                            "bytes": os.path.getsize(os.path.join(THEMES_DIR, fname))})
    except OSError:
        pass
    return out


async def handle_themes_list(request):
    ctx = await scope_for(request)
    s = await ctx.settings.get_settings()
    return json_response({"theme": s.get("theme", "auto"),
                          "custom_css": s.get("custom_css", ""),
                          "files": _theme_files(),
                          "example_url": "/theme-example.css"})


async def handle_themes_upload(request):
    reader = await request.multipart()
    os.makedirs(THEMES_DIR, exist_ok=True)
    saved = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.filename is None:
            continue
        data = await part.read(decode=False)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", part.filename)
        if not name.endswith(".css"):
            return json_response({"error": "только .css"}, status=400)
        if len(data) > THEME_MAX_BYTES:
            return json_response({"error": "файл больше 512 КБ"}, status=400)
        with open(os.path.join(THEMES_DIR, name), "wb") as f:
            f.write(data)
        saved.append({"name": name, "bytes": len(data)})
    if not saved:
        return json_response({"error": "файлы не переданы"}, status=400)
    return json_response({"files": saved}, status=201)


async def handle_themes_select(request):
    ctx = await scope_for(request)
    body = await request.json()
    patch = {}
    if body.get("theme") in ("auto", "light", "dark"):
        patch["theme"] = body["theme"]
    if "custom_css" in body:
        patch["custom_css"] = str(body["custom_css"] or "")
        if patch["custom_css"] and not os.path.isfile(
                os.path.join(THEMES_DIR, patch["custom_css"])):
            return json_response({"error": "файл темы не найден"}, status=404)
    if not patch:
        return json_response({"error": "theme или custom_css"}, status=400)
    await ctx.settings.set_settings(patch)
    return json_response({"ok": True, **patch})


async def handle_themes_delete(request):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.match_info["name"])
    path = os.path.join(THEMES_DIR, name)
    if not name.endswith(".css") or not os.path.isfile(path):
        return json_response({"error": "not found"}, status=404)
    os.remove(path)
    ctx = await scope_for(request)
    s = await ctx.settings.get_settings()
    if s.get("custom_css") == name:
        await ctx.settings.set_settings({"custom_css": ""})
    return json_response({"ok": True})


async def handle_theme_file(request):
    name = re.sub(r"[^A-Za-z0-9._-]", "_", request.match_info["name"])
    path = os.path.join(THEMES_DIR, name)
    if not name.endswith(".css") or not os.path.isfile(path):
        return json_response({"error": "not found"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# -- logs (§16) --------------------------------------------------------------------

async def handle_logs_list(request):
    level = request.query.get("level")
    limit = min(int(request.query.get("limit", "200") or 200), 2000)
    return json_response({"entries": LOGS.entries(limit, level),
                          "level": LOGS.level, "levels": list(LEVELS)})


async def handle_logs_level(request):
    body = await request.json()
    level = body.get("level")
    if level not in LEVELS:
        return json_response({"error": f"level ∈ {LEVELS}"}, status=400)
    LOGS.set_level(level)
    return json_response({"ok": True, "level": LOGS.level})


async def handle_logs_export(request):
    return web.Response(text=LOGS.export_text(), content_type="text/plain",
                        headers={"Content-Disposition":
                                 'attachment; filename="nagent-logs.txt"'})


async def handle_logs_clear(request):
    LOGS.clear()
    return json_response({"ok": True})


# --------------------------------------------------------------------------- API: Phase 5 — export/import (§16)

async def handle_chat_export(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    fmt = (request.query.get("format") or "json").lower()
    try:
        if fmt == "md":
            text = await exporter.export_md(ctx.history, chat_id)
            chat = await ctx.history.get_chat(chat_id)
            fname = re.sub(r"[^\w\-. ]+", "_", chat.get("title") or "chat")[:60]
            return web.Response(text=text, content_type="text/markdown",
                                headers={"Content-Disposition":
                                         f'attachment; filename="{fname}.md"'})
        payload = await exporter.export_json(ctx.history, chat_id)
    except KeyError:
        return json_response({"error": "not found"}, status=404)
    chat = await ctx.history.get_chat(chat_id)
    fname = re.sub(r"[^\w\-. ]+", "_", chat.get("title") or "chat")[:60]
    return web.Response(
        text=json.dumps(payload, ensure_ascii=False, indent=1),
        content_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}.json"'})


async def handle_chat_import(request):
    ctx = await scope_for(request)
    body = await request.json()
    fmt = (body.get("format") or "json").lower()
    content = body.get("content") or ""
    if not content:
        return json_response({"error": "пустой content"}, status=400)
    owner = None
    mode = await auth.mode()
    session = request.get("af_session")
    if mode in ("team", "accounts") and session and not session.get("implicit"):
        owner = session["user_id"]
    try:
        if fmt == "json":
            payload = json.loads(content)
            chat = await importer_json(ctx.history, payload,
                                       body.get("title_prefix") or "")
        elif fmt == "md":
            chat = await importer_md(ctx.history, content,
                                     body.get("title_prefix") or "")
        else:
            return json_response({"error": "format: json|md"}, status=400)
    except (ValueError, json.JSONDecodeError) as e:
        return json_response({"error": f"импорт не удался: {e}"}, status=400)
    if owner:
        await ctx.history.update_chat(chat["id"], owner=owner)
    LOGS.log_event("export", f"импорт чата: {chat['title']} ({fmt})")
    return json_response({"chat": chat}, status=201)


# --------------------------------------------------------------------------- SSE stream

async def handle_chat_stream(request):
    ctx = await scope_for(request)
    chat_id = request.match_info["id"]
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    try:
        since = int(request.query.get("since", 0))
    except ValueError:
        since = 0

    queue = ctx.history.subscribe(chat_id)
    try:
        for event in await ctx.history.events_since(chat_id, since):
            await _sse_send(resp, event)

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
                continue
            if isinstance(event.get("seq"), int) and event["seq"] <= since:
                continue
            await _sse_send(resp, event)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        ctx.history.unsubscribe(chat_id, queue)
        try:
            await resp.write(b"event: bye\ndata: {}\n\n")
        except Exception:  # noqa: BLE001
            pass
    return resp


async def _sse_send(resp, event: dict):
    payload = json.dumps(event, ensure_ascii=False)
    etype = event.get("type", "event")
    if event.get("_live"):
        etype = "live"
    msg = f"event: {etype}\ndata: {payload}\n\n"
    await resp.write(msg.encode("utf-8"))


# --------------------------------------------------------------------------- app

def build_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware, auth_middleware])
    r = app.router

    r.add_get("/api/health", handle_health)
    r.add_get("/api/settings", handle_get_settings)
    r.add_patch("/api/settings", handle_update_settings)

    # auth / onboarding (public — the middleware lets these through)
    r.add_get("/api/auth/me", handle_auth_me)
    r.add_post("/api/auth/login", handle_auth_login)
    r.add_post("/api/auth/logout", handle_auth_logout)
    r.add_get("/api/onboarding/status", handle_onboarding_status)
    r.add_post("/api/onboarding/register", handle_onboarding_register)
    r.add_post("/api/onboarding/finish", handle_onboarding_finish)

    # admin (role=admin; enforced by the middleware)
    r.add_get("/api/admin/overview", handle_admin_overview)
    r.add_post("/api/admin/users", handle_admin_users_create)
    r.add_post("/api/admin/users/{id}/password", handle_admin_users_password)
    r.add_patch("/api/admin/users/{id}", handle_admin_users_patch)
    r.add_delete("/api/admin/users/{id}", handle_admin_users_delete)
    r.add_post("/api/admin/mode", handle_admin_mode)
    r.add_post("/api/admin/workspace", handle_admin_workspace)
    r.add_post("/api/admin/master-password", handle_admin_master_password)
    r.add_post("/api/admin/unlock", handle_admin_unlock)
    r.add_get("/api/admin/audit", handle_admin_audit)

    r.add_post("/api/providers", handle_add_provider)
    r.add_get("/api/providers", handle_list_providers)
    r.add_delete("/api/providers/{id}", handle_delete_provider)
    r.add_route("PATCH", r"/api/providers/{id}", handle_patch_provider)
    r.add_get("/api/providers/{id}/models", handle_refresh_models)
    r.add_get("/api/models", handle_list_models)
    r.add_post("/api/models/probe", handle_probe_model)
    r.add_patch("/api/models/override", handle_set_model_override)
    r.add_delete("/api/models/override", handle_delete_model_override)

    r.add_get("/api/diagnostics", handle_diagnostics)

    r.add_post("/api/mcp", handle_add_mcp)
    r.add_get("/api/mcp", handle_list_mcp)
    r.add_delete("/api/mcp/{id}", handle_delete_mcp)
    r.add_post("/api/mcp/{id}/refresh", handle_refresh_mcp)

    r.add_get("/api/chats", handle_list_chats)
    r.add_post("/api/chats", handle_create_chat)
    r.add_get("/api/chats/{id}", handle_get_chat)
    r.add_patch("/api/chats/{id}", handle_update_chat)
    r.add_delete("/api/chats/{id}", handle_delete_chat)
    r.add_get("/api/chats/{id}/history", handle_chat_history)
    r.add_get("/api/chats/{id}/context", handle_chat_context)
    r.add_get("/api/chats/{id}/usage", handle_chat_usage)
    r.add_post("/api/chats/{id}/messages", handle_send_message)
    r.add_post("/api/chats/{id}/stop", handle_stop)
    r.add_get("/api/chats/{id}/status", handle_chat_status)
    r.add_get("/api/chats/{id}/stream", handle_chat_stream)
    r.add_get("/api/chats/{id}/export", handle_chat_export)
    r.add_post("/api/chats/import", handle_chat_import)

    # workspace / permissions / checkpoints
    r.add_get("/api/workspace/tree", handle_workspace_tree)
    r.add_get("/api/workspace/file", handle_workspace_file)
    r.add_get("/api/permissions/rules", handle_permission_rules)
    r.add_post("/api/permissions/rules", handle_add_rule)
    r.add_delete("/api/permissions/rules/{id}", handle_delete_rule)
    r.add_get("/api/approvals", handle_list_approvals)
    r.add_post("/api/approvals/{id}/resolve", handle_resolve_approval)
    r.add_get("/api/checkpoints", handle_list_checkpoints)
    r.add_post("/api/checkpoints", handle_checkpoint_create)
    r.add_get("/api/checkpoints/{id}/diff", handle_checkpoint_diff)
    r.add_post("/api/checkpoints/{id}/revert", handle_revert_prepare)
    r.add_post("/api/checkpoints/{id}/revert/apply", handle_revert_apply)
    r.add_get("/api/tools", handle_tools_catalog)

    # context control / compaction / plan / attachments
    r.add_patch("/api/chats/{id}/events/{eid}", handle_edit_event)
    r.add_delete("/api/chats/{id}/events/{eid}", handle_delete_event)
    r.add_get("/api/chats/{id}/events/{eid}/versions", handle_event_versions)
    r.add_get("/api/chats/{id}/raw", handle_raw_events)
    r.add_post("/api/chats/{id}/fork", handle_fork_chat)
    r.add_post("/api/chats/{id}/continue", handle_continue)
    r.add_post("/api/chats/{id}/compact", handle_compact)
    r.add_post("/api/chats/{id}/clear", handle_clear)
    r.add_post("/api/chats/{id}/plan/{eid}", handle_plan_action)
    r.add_patch("/api/chats/{id}/plan/{eid}/checklist", handle_plan_checklist)
    r.add_post("/api/chats/{id}/attachments", handle_upload_attachments)
    r.add_get("/api/chats/{id}/attachments", handle_list_attachments)

    # web layer — search / macros / background / browser / sounds
    r.add_get("/api/search", handle_session_search)
    r.add_get("/api/webtools", handle_webtools_status)
    r.add_post("/api/webtools/cache/clear", handle_webtools_cache_clear)
    r.add_get("/api/macros", handle_list_macros)
    r.add_post("/api/macros", handle_add_macro)
    r.add_patch("/api/macros/{id}", handle_update_macro)
    r.add_delete("/api/macros/{id}", handle_delete_macro)
    r.add_get("/api/background", handle_background_list)
    r.add_post("/api/background/{id}/cancel", handle_background_cancel)
    r.add_get("/api/browser/status", handle_browser_status)
    r.add_get("/api/browser/engines", handle_browser_engines)
    r.add_post("/api/browser/select", handle_browser_select)
    r.add_post("/api/browser/install", handle_browser_install)
    r.add_post("/api/browser/close", handle_browser_close)
    r.add_get("/ws/browser/{id}", handle_browser_ws)
    r.add_get("/api/sounds", handle_sounds_list)
    r.add_post("/api/sounds", handle_sounds_upload)
    r.add_get("/api/sounds/{name}", handle_sounds_file)

    # Phase 5 — skills / hooks / themes / logs
    r.add_get("/api/skills", handle_skills_list)
    r.add_post("/api/skills", handle_skills_upsert)
    r.add_post("/api/skills/import", handle_skills_import)
    r.add_get("/api/skills/{name}/export", handle_skills_export)
    r.add_delete("/api/skills/{name}", handle_skills_delete)
    r.add_get("/api/hooks", handle_hooks_list)
    r.add_post("/api/hooks", handle_hooks_add)
    r.add_patch("/api/hooks/{id}", handle_hooks_patch)
    r.add_delete("/api/hooks/{id}", handle_hooks_delete)
    r.add_post("/api/hooks/test", handle_hooks_test)
    r.add_get("/api/themes", handle_themes_list)
    r.add_post("/api/themes/upload", handle_themes_upload)
    r.add_post("/api/themes/select", handle_themes_select)
    r.add_delete("/api/themes/{name}", handle_themes_delete)
    r.add_get("/themes/{name}", handle_theme_file)
    r.add_get("/api/logs", handle_logs_list)
    r.add_post("/api/logs/level", handle_logs_level)
    r.add_get("/api/logs/export", handle_logs_export)
    r.add_post("/api/logs/clear", handle_logs_clear)

    if os.path.isdir(STATIC):
        r.add_get("/admin", lambda req: web.FileResponse(
            os.path.join(STATIC, "admin.html")))
        r.add_get("/", lambda req: web.FileResponse(
            os.path.join(STATIC, "index.html")))
        r.add_static("/", STATIC, name="static")
    return app


async def init_app() -> web.Application:
    await settings_store.open()
    await main_scope().open()
    s = await settings_store.get_settings()
    _isolation_cache["value"] = (await main_scope().settings.get_settings()) \
        .get("isolation", "off")
    # §17: proxy matrix is live from the first request on
    await proxy_matrix.refresh()
    providers_mod.set_model_proxy(proxy_matrix.effective("model_api"))
    # §2 team concurrency gates
    _apply_gates(s)
    # §16: logging capture
    LOGS.attach_logging()
    LOGS.log_event("boot", f"{APP_NAME} {APP_VERSION} запущен "
                           f"(режим: {await auth.mode()})")
    # §7 team master password: unlock at boot from the environment
    master_pw = os.environ.get("AGENT_MASTER_PASSWORD")
    if master_pw and await auth.master_configured():
        try:
            await auth.unlock_master(master_pw)
        except Exception:  # noqa: BLE001
            LOGS.log_event("auth", "не удалось разблокировать мастер-пароль "
                                   "из AGENT_MASTER_PASSWORD", "warning")
    # §16 onboarding: apply the stored workspace on restart (env wins)
    if not os.environ.get("AGENT_WORKSPACE") and s.get("workspace"):
        try:
            if os.path.abspath(s["workspace"]) != main_scope().jail.root:
                ctx = await scope_registry.rebuild_main(s["workspace"])
                _bind_main_scope(ctx)
        except Exception as e:  # noqa: BLE001
            LOGS.log_event("boot", f"workspace из настроек не применился: {e}",
                           "warning")
    app = build_app()

    async def _background(app):
        tasks = [asyncio.create_task(_resync_loop()),
                 asyncio.create_task(_session_purge_loop())]
        yield
        for t in tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # full shutdown hygiene: every aiosqlite connection owns a worker
        # thread that keeps the process alive if left open
        await scope_registry.close_all()
        await settings_store.close()

    app.cleanup_ctx.append(_background)
    return app


def main():
    async def _serve():
        app = await init_app()
        settings = await settings_store.get_settings()
        host = os.environ.get("AGENT_HOST", settings.get("host", "127.0.0.1"))
        port = int(os.environ.get("AGENT_PORT", settings.get("port", "8765")))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        mode = await auth.mode()
        print(f"✓ {APP_NAME} server: http://{host}:{port}  "
              f"(режим: {mode})")
        if mode == "setup":
            print("  → откройте браузер: запустится визард первого запуска "
                  "(админ → режим → провайдер → рабочая папка)")
        try:
            await asyncio.Event().wait()
        finally:
            # graceful stop: closes every aiosqlite connection (worker
            # threads!), cancels background loops; capped so a stuck
            # subprocess can never wedge the shutdown
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass

    def _term(signum, frame):     # SIGTERM behaves like Ctrl+C (posix)
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError, AttributeError):  # windows / non-main thread
        pass
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        print("\n✓ NAgent остановлен")
        # After a hard interrupt some workers (aiosqlite threads, playwright,
        # terminal subprocesses) may never unblock and would hang the exit.
        # The DBs are WAL-journaled — terminate NOW, always.
        os._exit(0)


if __name__ == "__main__":
    main()
