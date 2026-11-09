"""Scope contexts (§2 accounts mode): per-user isolation.

Every scope owns the full agent stack:
  settings store (main.db for solo/team; <user>.db for accounts) · history
  store + JSONL rollouts · workspace jail · tool registry (+ skills/meta
  tools) · MCP manager · permission engine · checkpoints · attachments ·
  summarizer · compactor · browser sessions · background tasks · hooks.

Server-GLOBAL things (§12 web cache, §17 proxy matrix, search backend
config, sounds) stay on the main settings store and the shared WebTools
instance — they are server infrastructure, not user data.

Scope ids: 'main' (solo / team / setup) and user ids (accounts). The
registry builds lazily; user scopes bind their key cipher at login (§7).
"""

from __future__ import annotations

import asyncio
import os

from . import providers as providers_mod
from .attachments import AttachmentStore
from .background import BackgroundTasks, register_background_tools
from .browser import BrowserEngine, register_browser_tools
from .checkpoints import Checkpoints
from .compaction import Compactor
from .core_tools import build_registry
from .db import DATA_DIR, SettingsStore
from .history import HistoryStore
from .hooks import HookEngine
from .instructions import InstructionsResolver
from .jail import WorkspaceJail
from .mcp import McpManager
from .permissions import PermissionEngine
from .router import ModelRouter
from .skills import SkillStore, register_skill_tools
from .subagents import register_task_tool
from .summarizer import TwoStepSummarizer
from .webtools import WebTools, register_web_tools


class ScopeContext:
    """One full agent stack (see module docstring). `publish_live` and the
    global webtools/proxy are injected by the caller — they are shared.

    Main scope keeps the Phase 0-4 layout: settings in main.db, chats in
    history.db. User scopes use a single <user>.db for everything (§2)."""

    def __init__(self, scope_id: str, *, db_path: str, workspace_root: str,
                 sessions_dir: str, toolcache_dir: str,
                 publish_live, webtools, proxy_matrix,
                 isolation_getter, settings_store: SettingsStore | None = None,
                 history_db_path: str | None = None):
        from .srvlog import LOGS
        self.scope_id = scope_id
        self.settings = settings_store or SettingsStore(db_path)
        self.history = HistoryStore(history_db_path or db_path, sessions_dir)
        self.jail = WorkspaceJail(workspace_root)
        self.registry = build_registry(self.jail, isolation_getter=isolation_getter)
        self.mcp = McpManager(self.settings, self.registry)
        self.permissions = PermissionEngine(self.settings)
        self.checkpoints = Checkpoints(self.jail, self.settings)
        self.instructions = InstructionsResolver(self.jail.root)
        self.attach_store = AttachmentStore(self.jail)
        self.summarizer = TwoStepSummarizer(self.history, self.settings,
                                            toolcache_dir)
        self.compactor = Compactor(self.history, self.settings,
                                   lambda: ModelRouter.create(self.settings))
        self.skills = SkillStore(self.settings)
        register_skill_tools(self.registry, self.skills)
        self.hooks = HookEngine(self.settings, lambda: self.jail.root)
        self.browser = BrowserEngine(proxy_matrix, self.jail, self.settings)
        self.bg = BackgroundTasks(self.jail, self.history, publish_live,
                                  isolation_getter=isolation_getter,
                                  notify_hook=lambda msg, chat_id:
                                      self.hooks.notification(msg, chat_id))
        register_task_tool(self.registry, self.history,
                           lambda: ModelRouter.create(self.settings),
                           permissions=self.permissions)
        register_web_tools(self.registry, webtools)
        register_browser_tools(self.registry, self.browser)
        register_background_tools(self.registry, self.bg)
        self.webtools = webtools
        self.proxy = proxy_matrix
        LOGS.log_event("scope",
                       f"контекст '{scope_id}' собран (workspace: {workspace_root})")

    async def open(self) -> None:
        await self.settings.open()
        await self.history.open()
        await self.permissions.open()
        await self.mcp.load()
        try:
            await self.checkpoints.ensure()
        except Exception as e:  # noqa: BLE001 — checkpoints are optional at boot
            from .srvlog import LOGS
            LOGS.log_event("scope", f"shadow-git недоступен: {e}", "warning")

    async def close(self) -> None:
        for coro, name in ((self.bg.shutdown(), "bg"),
                           (self.browser.shutdown(), "browser"),
                           (self.mcp.close(), "mcp"),
                           (self.history.close(), "history"),
                           (self.settings.close(), "settings")):
            try:
                await coro
            except Exception:  # noqa: BLE001
                pass


class ScopeRegistry:
    def __init__(self, *, webtools: WebTools, proxy_matrix, publish_live,
                 isolation_getter, main_settings: SettingsStore | None = None):
        self.webtools = webtools
        self.proxy = proxy_matrix
        self.publish_live = publish_live
        self.isolation_getter = isolation_getter
        self.main_settings = main_settings     # the global main.db instance
        self._scopes: dict[str, ScopeContext] = {}
        self._lock = asyncio.Lock()

    def get(self, scope_id: str) -> ScopeContext | None:
        return self._scopes.get(scope_id)

    def all(self) -> list[ScopeContext]:
        return list(self._scopes.values())

    def set_main(self, ctx: ScopeContext) -> None:
        self._scopes["main"] = ctx

    async def get_or_create_main(self, db_path: str, workspace_root: str,
                                 sessions_dir: str, toolcache_dir: str) -> ScopeContext:
        async with self._lock:
            existing = self._scopes.get("main")
            if existing:
                return existing
            ctx = ScopeContext("main", db_path=db_path,
                               workspace_root=workspace_root,
                               sessions_dir=sessions_dir,
                               toolcache_dir=toolcache_dir,
                               publish_live=self.publish_live,
                               webtools=self.webtools,
                               proxy_matrix=self.proxy,
                               isolation_getter=self.isolation_getter,
                               settings_store=self.main_settings,
                               history_db_path=os.path.join(
                                   os.path.dirname(db_path) or ".", "history.db"))
            await ctx.open()
            self._scopes["main"] = ctx
            return ctx

    async def get_or_create_user(self, user: dict, key: bytes | None) -> ScopeContext:
        """Accounts mode §2: <user>.db + isolated workspace under the user's
        data dir. `key` (derived from the login password) seals the keys."""
        from .db import KeyCipher
        async with self._lock:
            existing = self._scopes.get(user["id"])
            if existing:
                if key:
                    existing.settings.bind_cipher(KeyCipher(key))
                return existing
            base = os.path.join(DATA_DIR, "users", user["username"])
            os.makedirs(base, exist_ok=True)
            ctx = ScopeContext(user["id"],
                               db_path=os.path.join(base, "user.db"),
                               workspace_root=os.path.join(base, "workspace"),
                               sessions_dir=os.path.join(base, "sessions"),
                               toolcache_dir=os.path.join(base, "toolcache"),
                               publish_live=self.publish_live,
                               webtools=self.webtools,
                               proxy_matrix=self.proxy,
                               isolation_getter=self.isolation_getter)
            await ctx.open()
            if key:
                ctx.settings.bind_cipher(KeyCipher(key))
            self._scopes[user["id"]] = ctx
            return ctx

    async def rebuild_main(self, workspace_root: str) -> ScopeContext:
        """§16 onboarding / admin workspace change: tear the main scope down
        (it re-opens against the same main.db/history.db — data survives)
        and build it against the new workspace."""
        async with self._lock:
            old = self._scopes.pop("main", None)
            if old:
                await old.close()
            from .db import DB_PATH
            from .history import SESSIONS_DIR
            ctx = ScopeContext("main", db_path=DB_PATH,
                               workspace_root=workspace_root,
                               sessions_dir=SESSIONS_DIR,
                               toolcache_dir=os.path.join(DATA_DIR, "toolcache"),
                               publish_live=self.publish_live,
                               webtools=self.webtools,
                               proxy_matrix=self.proxy,
                               isolation_getter=self.isolation_getter,
                               settings_store=self.main_settings,
                               history_db_path=os.path.join(DATA_DIR, "history.db"))
            await ctx.open()
            self._scopes["main"] = ctx
            return ctx

    async def close_all(self) -> None:
        async with self._lock:
            for ctx in list(self._scopes.values()):
                await ctx.close()
            self._scopes.clear()


class TurnGate:
    """§2 team mode: concurrent generation limit + FIFO queue of turns.
    A queued turn emits live `queue` frames so the user sees the position."""

    def __init__(self):
        self.limit = 0                     # 0 = unlimited
        self.active = 0
        self._waiters: list[asyncio.Future] = []

    def set_limit(self, limit: int) -> None:
        self.limit = max(0, int(limit or 0))
        self._drain()

    def _drain(self) -> None:
        while self._waiters and (self.limit == 0 or self.active < self.limit):
            fut = self._waiters.pop(0)
            if not fut.done():
                self.active += 1
                fut.set_result(None)

    async def acquire(self, on_position=None) -> bool:
        """Returns True once a slot is GRANTED (the caller must release()).
        Cancellation while still queued releases nothing — the slot belongs
        to whoever actually runs."""
        if self.limit and self.active >= self.limit:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append(fut)
            if on_position:
                on_position(self._waiters.index(fut) + 1)
            try:
                await fut
            except asyncio.CancelledError:
                if fut in self._waiters:           # never granted
                    self._waiters.remove(fut)
                    raise
                return True                        # granted right before cancel
            return True
        self.active += 1
        return True

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self._drain()

    def queued(self) -> int:
        return len(self._waiters)


class ToolGate:
    """§2 team mode: concurrent tool execution limit (a plain semaphore that
    reads its limit fresh from the server settings when it changes)."""

    def __init__(self):
        self._sem: asyncio.Semaphore | None = None
        self._limit = 0

    def set_limit(self, limit: int) -> None:
        limit = max(0, int(limit or 0))
        if limit == self._limit:
            return
        self._limit = limit
        self._sem = asyncio.Semaphore(limit) if limit else None

    async def __aenter__(self) -> "ToolGate":
        if self._sem is not None:
            await self._sem.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._sem is not None:
            self._sem.release()
