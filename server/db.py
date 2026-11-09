"""Server settings store (main.db): providers, models, overrides, MCP, config.

Schema migrations are auto-applied on open (light ALTER-based upgrades so
older Phase 0 databases keep working).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

import aiosqlite

from . import events as ev

DATA_DIR = os.environ.get(
    "AGENT_DATA",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "main.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS server_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,            -- normalized base host (no /v1 suffix)
    api_key TEXT,
    proxy_url TEXT,                -- §17 per-provider proxy (wins over model_api)
    created_at REAL NOT NULL,
    health_status TEXT,            -- 'online' | 'error' | NULL (unknown)
    health_latency REAL,           -- ms
    health_ts REAL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,           -- "provider_id::model_id"
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    title TEXT,
    context_length INTEGER,        -- ONLY if the API declared it (else NULL)
    capabilities TEXT,             -- JSON: explicit API declarations only (else NULL)
    raw TEXT                       -- the /v1/models item, verbatim
);
CREATE TABLE IF NOT EXISTS model_overrides (
    model_key TEXT PRIMARY KEY,    -- manual UI overrides survive catalog resync
    override TEXT NOT NULL         -- JSON
);
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at REAL NOT NULL,
    health_status TEXT,
    health_latency REAL,
    health_ts REAL,
    last_error TEXT,
    server_info TEXT,              -- JSON: initialize result
    prompts_count INTEGER DEFAULT 0,
    resources_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mcp_tools (
    id TEXT PRIMARY KEY,           -- public tool name the MODEL sees (routing to
                                   -- the owning server is internal; collisions get _2)
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,       -- native name on the MCP server
    description TEXT,
    schema TEXT
);
CREATE TABLE IF NOT EXISTS permission_rules (
    id TEXT PRIMARY KEY,
    effect TEXT NOT NULL,          -- 'allow' | 'deny'
    tool TEXT NOT NULL,            -- tool name or '*'
    pattern TEXT NOT NULL DEFAULT '',  -- '' matches everything for the tool
    note TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT,                     -- JSON
    summary TEXT,                  -- human-readable preview of what happens
    created_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|denied|expired
    decision TEXT,                 -- once|always|deny
    rule_id TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints (
    id TEXT PRIMARY KEY,
    chat_id TEXT,
    turn_event_id TEXT,            -- UUID <-> commit binding (§14)
    commit_sha TEXT NOT NULL,
    message TEXT,
    files TEXT,                    -- JSON list of changed files
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS web_cache (        -- §12: GLOBAL websearch/webfetch cache
    kind TEXT NOT NULL,            -- 'search' | 'fetch'
    key TEXT NOT NULL,             -- backend::query | normalized URL
    payload TEXT NOT NULL,         -- JSON
    size INTEGER NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    last_used REAL NOT NULL,
    PRIMARY KEY(kind, key)
);
CREATE TABLE IF NOT EXISTS macros (           -- §6: user macros (name + prompt + mode + hotkey)
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'fill',   -- 'fill' (into input) | 'run' (send at once)
    hotkey TEXT,                          -- e.g. 'ctrl+shift+1'
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (            -- §2/§16: team/accounts users; the
    id TEXT PRIMARY KEY,                      -- FIRST registered user becomes admin
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'member',      -- 'admin' | 'member'
    pw_salt TEXT NOT NULL,                    -- scrypt salt (hex) — password verifier
    pw_hash TEXT NOT NULL,                    -- scrypt hash (hex)
    key_salt TEXT,                            -- PBKDF2 salt (hex) -> provider-key cipher
    created_at REAL NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hooks (            -- §7: event hooks, command + timeout
    id TEXT PRIMARY KEY,
    event TEXT NOT NULL,                      -- PreToolUse | PostToolUse |
                                              -- UserPromptSubmit | Stop | Notification
    command TEXT NOT NULL,
    timeout REAL NOT NULL DEFAULT 10,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skills (           -- §6: SkillSearch pattern; SKILL.md-compatible
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (        -- §2: optional toolcall audit, OFF by default
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    user TEXT,
    chat_id TEXT,
    model_key TEXT,
    tool TEXT NOT NULL,
    args TEXT,
    ok INTEGER NOT NULL,
    duration_ms REAL
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "host": "127.0.0.1",
    "port": "8765",
    "cors_origins": "*",           # or comma-separated list
    "mode": "setup",               # §16: fresh install -> onboarding wizard;
                                   # older DBs keep whatever they had
    "fallback_chain": "[]",        # JSON list of model_keys, tried in order
    "retry_max_attempts": "2",     # attempts per model before moving on
    "retry_backoff": "exp:1.5",    # 'fixed:N' | 'exp:N' seconds
    "catalog_resync_hours": "6",   # background catalog + health resync
    "permission_mode": "ask",      # 'ask' | 'allow_all' (§7)
    "tool_timeout_default": "30",  # seconds, per-call cap for tools
    "tool_timeout_max": "600",     # hard ceiling for explicit timeout=N
    "isolation": "off",            # 'off' | 'docker' | 'bubblewrap' (§7 layer 2)
    # -- Phase 3 (§5/§8/§11) ------------------------------------------------
    "default_system_prompt": "",   # server-level system prompt (hierarchy §8)
    "loop_iterations_limit": "24", # loop guard per turn (§5, configurable)
    "compact_auto": "1",           # auto-compact after a turn crosses the threshold
    "compact_threshold": "80%",    # server default; per chat column overrides (§11)
    "compact_keep_head": "2",      # first N content events kept verbatim
    "compact_keep_last": "8",      # last N content events kept verbatim
    "compact_model_key": "",       # routing to a cheap model (§11); '' = chat model
    "summarizer_model_key": "",    # two-step tool-output summarizer; '' = chat model
    "two_step_summarize": "1",     # 0 = truncate+cache only (Phase 2 behaviour)
    "tool_failure_streak": "3",    # consecutive tool failures before the warning (§5)
    # -- Phase 4 (§12/§17 web layer) ---------------------------------------------
    "search_backend": "off",       # 'off' | 'duckduckgo' | 'searxng' | 'brave'
    "search_searxng_url": "",      # self-hosted SearXNG base URL
    "search_brave_key": "",        # Brave Search API key
    "web_cache_ttl_hours": "24",   # §12 eviction: TTL
    "web_cache_max_mb": "50",      # §12 eviction: total size cap (LRU by last_used)
    "proxy_matrix": "{}",          # §17: {route: {url}} JSON, routes: global/model_api/web/browser
    "sounds_enabled": "1",         # §10: notification sounds
    "sound_turn_done": "1",        # a turn finished generating
    "sound_task_done": "1",        # background task finished
    "sound_file_turn": "",         # custom sound file name (uploaded via /api/sounds); '' = built-in blip
    "sound_file_task": "",
    # -- Phase 5 (§2/§7/§16: modes, deployment, appearance) -----------------------
    "ui_lang": "ru",               # 'ru' | 'en' — interface language
    "theme": "auto",               # 'auto' (system) | 'light' | 'dark'
    "custom_css": "",              # uploaded theme file name; '' = none
    "audit_enabled": "0",          # §2 toolcall audit — OFF by default
    "concurrency_generations": "0",  # team mode: max simultaneous turns (0 = off)
    "concurrency_tools": "0",        # team mode: max simultaneous tool executions
    "attach_quota_mb": "0",        # accounts mode: attachment quota per user (0 = off)
    "workspace": "",               # solo/team workspace path (set by onboarding)
    "browser_engine": "",          # §13: '' = first installed; else chromium/firefox/webkit
}

_OVERRIDE_KEYS = {"title", "context_length", "capabilities"}


class KeyCipher:
    """Encrypts provider api_keys at rest (§7). Bound to a SettingsStore
    AFTER the unlock (user login / master password) — until then keys stay
    whatever they are on disk (plaintext for a not-yet-encrypted db, or
    sealed envelopes that read back as-is with a `_sealed` marker)."""

    def __init__(self, key: bytes):
        self.key = key

    def encrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        from . import crypto
        if crypto.is_envelope(value):
            return value                    # already sealed
        return crypto.encrypt(self.key, value)

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return value
        from . import crypto
        if not crypto.is_envelope(value):
            return value                    # legacy plaintext
        return crypto.decrypt(self.key, value)


class SettingsStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self.cipher: KeyCipher | None = None   # bound after unlock (§7)

    def bind_cipher(self, cipher: KeyCipher | None) -> None:
        self.cipher = cipher

    async def open(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._migrate_versions()
        for k, v in DEFAULT_SETTINGS.items():
            await self._db.execute(
                "INSERT OR IGNORE INTO server_settings(key,value) VALUES(?,?)", (k, v))
        await self._db.commit()

    async def _migrate(self) -> None:
        async def add_column(table: str, column: str, ddl_type: str) -> None:
            cur = await self._db.execute(f"PRAGMA table_info({table})")
            cols = {r[1] for r in await cur.fetchall()}
            if column not in cols:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        await add_column("providers", "proxy_url", "TEXT")
        await add_column("providers", "proxy_url", "TEXT")
        await add_column("providers", "health_status", "TEXT")
        await add_column("providers", "health_latency", "REAL")
        await add_column("providers", "health_ts", "REAL")
        await add_column("providers", "last_error", "TEXT")
        await add_column("models", "capabilities", "TEXT")
        # pre-v0.3 catalogs carried guessed capability flags and pricing —
        # drop them; the catalog re-syncs from the providers automatically
        cur = await self._db.execute("PRAGMA table_info(models)")
        cols = {r[1] for r in await cur.fetchall()}
        if "pricing_in" in cols:
            await self._db.execute("DROP TABLE models")
            await self._db.execute(
                "CREATE TABLE models ("
                "id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_id TEXT NOT NULL,"
                "title TEXT, context_length INTEGER, capabilities TEXT, raw TEXT)")

    async def _migrate_versions(self) -> None:
        """Versioned migrations (§2 'auto-upgrade of old DBs on version bump').
        Each step is a plain async callable; applied steps are recorded in
        schema_migrations and never re-run. Ad-hoc ALTERs above are the v1
        baseline; Phase 5 steps start at v2."""
        from .migrations import MIGRATIONS
        cur = await self._db.execute(
            "SELECT version FROM schema_migrations")
        done = {r[0] for r in await cur.fetchall()}
        for version, step in sorted(MIGRATIONS.items()):
            if version in done:
                continue
            await step(self)
            await self._db.execute(
                "INSERT OR REPLACE INTO schema_migrations(version,applied_at) "
                "VALUES(?,?)", (version, time.time()))
            await self._db.commit()

    async def close(self) -> None:
        if self._db:
            db, self._db = self._db, None
            await db.close()

    # -- settings --------------------------------------------------------------

    async def get_settings(self) -> dict:
        cur = await self._db.execute("SELECT key,value FROM server_settings")
        return {r["key"]: r["value"] for r in await cur.fetchall()}

    async def set_settings(self, patch: dict) -> dict:
        for k, v in patch.items():
            await self._db.execute(
                "INSERT INTO server_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        await self._db.commit()
        return await self.get_settings()

    # -- providers ---------------------------------------------------------------

    async def add_provider(self, host: str, api_key: str | None,
                            name: str | None, proxy_url: str | None = None) -> dict:
        stored_key = self.cipher.encrypt(api_key) if self.cipher else api_key
        provider = {"id": ev.new_id(), "name": name or host, "host": host,
                    "api_key": api_key, "proxy_url": (proxy_url or "").strip() or None,
                    "created_at": time.time(),
                    "health_status": None, "health_latency": None,
                    "health_ts": None, "last_error": None}
        await self._db.execute(
            "INSERT INTO providers(id,name,host,api_key,proxy_url,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (provider["id"], provider["name"], provider["host"],
             stored_key, provider["proxy_url"], provider["created_at"]))
        await self._db.commit()
        return provider

    async def list_providers(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM providers ORDER BY created_at")
        return [self._provider_row(dict(r)) for r in await cur.fetchall()]

    async def get_provider(self, provider_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM providers WHERE id=?", (provider_id,))
        row = await cur.fetchone()
        return self._provider_row(dict(row)) if row else None

    def _provider_row(self, row: dict) -> dict:
        """api_key read back decrypted (in-memory plaintext for runtime use);
        a still-sealed envelope (no cipher bound) keeps its masked marker so
        the UI shows WHY the provider is unusable until unlock."""
        from . import crypto
        raw = row.get("api_key")
        if crypto.is_envelope(raw):
            if self.cipher is not None:
                try:
                    row["api_key"] = self.cipher.decrypt(raw)
                    row["key_sealed"] = False
                except Exception as e:               # wrong password / corrupted
                    row["api_key"] = None
                    row["key_sealed"] = True
                    row["key_error"] = str(e)
            else:
                row["api_key"] = None
                row["key_sealed"] = True
        else:
            row["key_sealed"] = False
        return row

    async def update_provider_proxy(self, provider_id: str,
                                     proxy_url: str | None) -> None:
        """§17: per-provider proxy override (empty string clears it)."""
        await self._db.execute(
            "UPDATE providers SET proxy_url=? WHERE id=?",
            ((proxy_url or "").strip() or None, provider_id))
        await self._db.commit()

    async def delete_provider(self, provider_id: str) -> None:
        await self._db.execute("DELETE FROM models WHERE provider_id=?", (provider_id,))
        await self._db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
        await self._db.commit()

    async def set_provider_health(self, provider_id: str, status: str,
                                  latency_ms: float | None, error: str | None) -> None:
        await self._db.execute(
            "UPDATE providers SET health_status=?, health_latency=?, health_ts=?, "
            "last_error=? WHERE id=?",
            (status, latency_ms, time.time(), error, provider_id))
        await self._db.commit()

    # -- models ----------------------------------------------------------------

    async def replace_models(self, provider_id: str, models: list[dict]) -> None:
        await self._db.execute("DELETE FROM models WHERE provider_id=?", (provider_id,))
        for m in models:
            await self._db.execute(
                "INSERT OR REPLACE INTO models(id,provider_id,model_id,title,"
                "context_length,capabilities,raw) VALUES(?,?,?,?,?,?,?)",
                (f"{provider_id}::{m['model_id']}", provider_id, m["model_id"],
                 m.get("title"), m.get("context_length"),
                 json.dumps(m["capabilities"], ensure_ascii=False)
                 if m.get("capabilities") else None,
                 m.get("raw")))
        await self._db.commit()

    async def list_models(self, provider_id: str | None = None) -> list[dict]:
        if provider_id:
            cur = await self._db.execute(
                "SELECT * FROM models WHERE provider_id=? ORDER BY model_id", (provider_id,))
        else:
            cur = await self._db.execute("SELECT * FROM models ORDER BY model_id")
        return [await self._merge_override(dict(r)) for r in await cur.fetchall()]

    async def get_model(self, model_key: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM models WHERE id=?", (model_key,))
        row = await cur.fetchone()
        return await self._merge_override(dict(row)) if row else None

    async def _merge_override(self, row: dict) -> dict:
        cur = await self._db.execute(
            "SELECT override FROM model_overrides WHERE model_key=?", (row["id"],))
        r = await cur.fetchone()
        row["override"] = None
        try:
            row["capabilities"] = json.loads(row["capabilities"]) if row.get("capabilities") else None
        except (json.JSONDecodeError, TypeError):
            row["capabilities"] = None
        if r:
            try:
                ov = json.loads(r["override"])
                row["override"] = ov
                for k in _OVERRIDE_KEYS:
                    if k in ov and ov[k] is not None:
                        row[k] = ov[k]
                # capabilities: dict override REPLACES API-declared caps,
                # so a manual answer in the §9 modal wins over stale data
            except (json.JSONDecodeError, TypeError):
                pass
        return row

    async def set_model_override(self, model_key: str, override: dict) -> None:
        clean = {k: v for k, v in override.items() if k in _OVERRIDE_KEYS}
        await self._db.execute(
            "INSERT OR REPLACE INTO model_overrides(model_key,override) VALUES(?,?)",
            (model_key, json.dumps(clean, ensure_ascii=False)))
        await self._db.commit()

    async def delete_model_override(self, model_key: str) -> None:
        await self._db.execute("DELETE FROM model_overrides WHERE model_key=?", (model_key,))
        await self._db.commit()

    # -- MCP ---------------------------------------------------------------------

    async def add_mcp_server(self, server_id: str, name: str, url: str,
                             server_info: dict, prompts: int, resources: int) -> dict:
        row = {"id": server_id, "name": name, "url": url, "created_at": time.time(),
               "health_status": None, "health_latency": None, "health_ts": None,
               "last_error": None,
               "server_info": json.dumps(server_info, ensure_ascii=False),
               "prompts_count": prompts, "resources_count": resources}
        await self._db.execute(
            "INSERT INTO mcp_servers(id,name,url,created_at,server_info,"
            "prompts_count,resources_count) VALUES(?,?,?,?,?,?,?)",
            (server_id, name, url, row["created_at"], row["server_info"],
             prompts, resources))
        await self._db.commit()
        return row

    async def list_mcp_servers(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM mcp_servers ORDER BY created_at")
        servers = [dict(r) for r in await cur.fetchall()]
        for s in servers:
            s["tools"] = await self.list_mcp_tools(s["id"])
        return servers

    async def get_mcp_server(self, server_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_mcp_server(self, server_id: str) -> None:
        await self._db.execute("DELETE FROM mcp_tools WHERE server_id=?", (server_id,))
        await self._db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        await self._db.commit()

    async def replace_mcp_tools(self, server_id: str, tools: list[dict]) -> None:
        await self._db.execute("DELETE FROM mcp_tools WHERE server_id=?", (server_id,))
        for t in tools:
            await self._db.execute(
                "INSERT OR REPLACE INTO mcp_tools(id,server_id,tool_name,description,"
                "schema) VALUES(?,?,?,?,?)",
                (t["id"], server_id, t["tool_name"], t.get("description"),
                 json.dumps(t.get("schema") or {}, ensure_ascii=False)))
        await self._db.commit()

    async def list_mcp_tools(self, server_id: str | None = None) -> list[dict]:
        if server_id:
            cur = await self._db.execute(
                "SELECT * FROM mcp_tools WHERE server_id=? ORDER BY tool_name", (server_id,))
        else:
            cur = await self._db.execute("SELECT * FROM mcp_tools ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]

    async def set_mcp_health(self, server_id: str, status: str,
                             latency_ms: float | None, error: str | None) -> None:
        await self._db.execute(
            "UPDATE mcp_servers SET health_status=?, health_latency=?, health_ts=?, "
            "last_error=? WHERE id=?",
            (status, latency_ms, time.time(), error, server_id))
        await self._db.commit()

    # -- permission rules (§7) ---------------------------------------------------

    async def add_rule(self, effect: str, tool: str, pattern: str,
                       note: str | None) -> dict:
        rule = {"id": ev.new_id(), "effect": effect, "tool": tool,
                "pattern": pattern or "", "note": note, "created_at": time.time()}
        await self._db.execute(
            "INSERT INTO permission_rules(id,effect,tool,pattern,note,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (rule["id"], effect, tool, rule["pattern"], note, rule["created_at"]))
        await self._db.commit()
        return rule

    async def list_rules(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM permission_rules ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]

    async def delete_rule(self, rule_id: str) -> None:
        await self._db.execute("DELETE FROM permission_rules WHERE id=?", (rule_id,))
        await self._db.commit()

    # -- approvals (ask-mode pending decisions) -----------------------------------

    async def create_approval(self, approval_id: str, chat_id: str, tool: str,
                              args: dict, summary: str) -> dict:
        row = {"id": approval_id, "chat_id": chat_id, "tool": tool,
               "args": json.dumps(args, ensure_ascii=False), "summary": summary,
               "created_at": time.time(), "status": "pending",
               "decision": None, "rule_id": None}
        await self._db.execute(
            "INSERT INTO approvals(id,chat_id,tool,args,summary,created_at,status) "
            "VALUES(?,?,?,?,?,?,'pending')",
            (row["id"], chat_id, tool, row["args"], summary, row["created_at"]))
        await self._db.commit()
        return row

    async def get_approval(self, approval_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))
        row = await cur.fetchone()
        if not row:
            return None
        out = dict(row)
        try:
            out["args"] = json.loads(out.get("args") or "{}")
        except (json.JSONDecodeError, TypeError):
            out["args"] = {}
        return out

    async def resolve_approval(self, approval_id: str, status: str,
                               decision: str | None, rule_id: str | None) -> None:
        await self._db.execute(
            "UPDATE approvals SET status=?, decision=?, rule_id=? WHERE id=?",
            (status, decision, rule_id, approval_id))
        await self._db.commit()

    async def expire_orphan_approvals(self) -> int:
        """Server restart: nothing is awaiting these anymore."""
        cur = await self._db.execute(
            "UPDATE approvals SET status='expired' WHERE status='pending'")
        await self._db.commit()
        return cur.rowcount

    async def list_pending_approvals(self, chat_id: str) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM approvals WHERE chat_id=? AND status='pending' "
            "ORDER BY created_at", (chat_id,))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["args"] = json.loads(d.get("args") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["args"] = {}
            out.append(d)
        return out

    # -- checkpoints (§14) ---------------------------------------------------------

    async def add_checkpoint(self, checkpoint_id: str, chat_id: str | None,
                             turn_event_id: str | None, commit_sha: str,
                             message: str, files: list[str]) -> dict:
        row = {"id": checkpoint_id, "chat_id": chat_id,
               "turn_event_id": turn_event_id, "commit_sha": commit_sha,
               "message": message, "files": json.dumps(files, ensure_ascii=False),
               "ts": time.time()}
        await self._db.execute(
            "INSERT INTO checkpoints(id,chat_id,turn_event_id,commit_sha,message,"
            "files,ts) VALUES(?,?,?,?,?,?,?)",
            (row["id"], chat_id, turn_event_id, commit_sha, message,
             row["files"], row["ts"]))
        await self._db.commit()
        row["files"] = files
        return row

    async def list_checkpoints(self, chat_id: str | None = None) -> list[dict]:
        if chat_id:
            cur = await self._db.execute(
                "SELECT * FROM checkpoints WHERE chat_id=? ORDER BY ts", (chat_id,))
        else:
            cur = await self._db.execute("SELECT * FROM checkpoints ORDER BY ts")
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["files"] = json.loads(d.get("files") or "[]")
            except (json.JSONDecodeError, TypeError):
                d["files"] = []
            out.append(d)
        return out

    async def get_checkpoint(self, checkpoint_id: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["files"] = json.loads(d.get("files") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["files"] = []
        return d

    # -- web cache (§12: global for the whole server) -----------------------------

    async def web_cache_get(self, kind: str, key: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT payload FROM web_cache WHERE kind=? AND key=?",
            (kind, key))
        row = await cur.fetchone()
        if not row:
            return None
        await self._db.execute(
            "UPDATE web_cache SET hits=hits+1, last_used=? WHERE kind=? AND key=?",
            (time.time(), kind, key))
        await self._db.commit()
        return {"payload": row["payload"]}

    async def web_cache_put(self, kind: str, key: str, payload: str) -> None:
        # UPSERT: a refresh (fresh=true) replaces the payload but keeps the
        # hit counter and the original creation time — cache statistics
        # must survive revalidations
        await self._db.execute(
            "INSERT INTO web_cache(kind,key,payload,size,hits,created,last_used) "
            "VALUES(?,?,?,?,0,?,?) "
            "ON CONFLICT(kind,key) DO UPDATE SET payload=excluded.payload, "
            "size=excluded.size, last_used=excluded.last_used",
            (kind, key, payload, len(payload.encode("utf-8")), time.time(), time.time()))
        await self._db.commit()

    async def web_cache_evict(self, ttl_seconds: float, max_bytes: int) -> dict:
        """TTL + size-cap LRU eviction (§12). Returns what it removed."""
        cur = await self._db.execute(
            "DELETE FROM web_cache WHERE last_used < ?", (time.time() - ttl_seconds,))
        expired = cur.rowcount or 0
        removed_lru = 0
        cur = await self._db.execute(
            "SELECT COALESCE(SUM(size),0) FROM web_cache")
        total = (await cur.fetchone())[0]
        if total > max_bytes:
            cur = await self._db.execute(
                "SELECT kind,key,size FROM web_cache ORDER BY last_used")
            for r in await cur.fetchall():
                if total <= max_bytes:
                    break
                await self._db.execute(
                    "DELETE FROM web_cache WHERE kind=? AND key=?", (r[0], r[1]))
                total -= r[2]
                removed_lru += 1
        await self._db.commit()
        return {"expired": expired, "lru_removed": removed_lru}

    async def web_cache_stats(self) -> dict:
        cur = await self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(SUM(hits),0) "
            "FROM web_cache")
        r = await cur.fetchone()
        return {"entries": r[0], "bytes": r[1], "hits": r[2],
                "mb": round(r[1] / 1024 / 1024, 2)}

    async def web_cache_clear(self) -> None:
        await self._db.execute("DELETE FROM web_cache")
        await self._db.commit()

    # -- macros (§6) ----------------------------------------------------------------

    async def add_macro(self, name: str, prompt: str, mode: str,
                        hotkey: str | None) -> dict:
        from . import events as _ev
        macro = {"id": _ev.new_id(), "name": name, "prompt": prompt,
                 "mode": mode if mode in ("fill", "run") else "fill",
                 "hotkey": hotkey or "", "created_at": time.time()}
        await self._db.execute(
            "INSERT INTO macros(id,name,prompt,mode,hotkey,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (macro["id"], macro["name"], macro["prompt"], macro["mode"],
             macro["hotkey"], macro["created_at"]))
        await self._db.commit()
        return macro

    async def list_macros(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM macros ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]

    async def update_macro(self, macro_id: str, fields: dict) -> dict | None:
        allowed = {k: v for k, v in fields.items()
                   if k in ("name", "prompt", "mode", "hotkey") and v is not None}
        if allowed.get("mode") not in ("fill", "run", None):
            allowed.pop("mode")
        if not allowed:
            return await self.get_macro(macro_id)
        sets = ", ".join(f"{k}=?" for k in allowed)
        await self._db.execute(f"UPDATE macros SET {sets} WHERE id=?",
                               (*allowed.values(), macro_id))
        await self._db.commit()
        return await self.get_macro(macro_id)

    async def get_macro(self, macro_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM macros WHERE id=?", (macro_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_macro(self, macro_id: str) -> None:
        await self._db.execute("DELETE FROM macros WHERE id=?", (macro_id,))
        await self._db.commit()

    # -- users (§2: team/accounts; first registered user becomes admin) -------------

    async def count_users(self) -> int:
        cur = await self._db.execute("SELECT COUNT(*) FROM users")
        return (await cur.fetchone())[0]

    async def add_user(self, username: str, pw_salt: str, pw_hash: str,
                       key_salt: str, role: str | None = None) -> dict:
        from . import events as _ev
        if role is None:                       # the first user ever -> admin
            role = "admin" if await self.count_users() == 0 else "member"
        user = {"id": _ev.new_id(), "username": username, "role": role,
                "pw_salt": pw_salt, "pw_hash": pw_hash, "key_salt": key_salt,
                "created_at": time.time(), "disabled": 0}
        await self._db.execute(
            "INSERT INTO users(id,username,role,pw_salt,pw_hash,key_salt,"
            "created_at,disabled) VALUES(?,?,?,?,?,?,?,0)",
            (user["id"], username, role, pw_salt, pw_hash, key_salt,
             user["created_at"]))
        await self._db.commit()
        return user

    async def get_user(self, user_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_user_by_name(self, username: str) -> dict | None:
        cur = await self._db.execute(
            "SELECT * FROM users WHERE username=?", (username,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_users(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT id,username,role,created_at,disabled FROM users "
            "ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]

    async def set_user_password(self, user_id: str, pw_salt: str,
                                pw_hash: str, key_salt: str) -> None:
        await self._db.execute(
            "UPDATE users SET pw_salt=?, pw_hash=?, key_salt=? WHERE id=?",
            (pw_salt, pw_hash, key_salt, user_id))
        await self._db.commit()

    async def set_user_role(self, user_id: str, role: str) -> None:
        await self._db.execute(
            "UPDATE users SET role=? WHERE id=?", (role, user_id))
        await self._db.commit()

    async def set_user_disabled(self, user_id: str, disabled: bool) -> None:
        await self._db.execute(
            "UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
        await self._db.commit()

    async def delete_user(self, user_id: str) -> None:
        await self._db.execute("DELETE FROM users WHERE id=?", (user_id,))
        await self._db.commit()

    # -- hooks (§7) ------------------------------------------------------------------

    HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit",
                   "Stop", "Notification")

    async def add_hook(self, event: str, command: str, timeout: float,
                       enabled: bool = True) -> dict:
        from . import events as _ev
        hook = {"id": _ev.new_id(), "event": event, "command": command,
                "timeout": float(timeout or 10), "enabled": int(bool(enabled)),
                "created_at": time.time()}
        await self._db.execute(
            "INSERT INTO hooks(id,event,command,timeout,enabled,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (hook["id"], event, command, hook["timeout"], hook["enabled"],
             hook["created_at"]))
        await self._db.commit()
        return hook

    async def list_hooks(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM hooks ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]

    async def update_hook(self, hook_id: str, fields: dict) -> dict | None:
        allowed = {k: v for k, v in fields.items()
                   if k in ("event", "command", "timeout", "enabled")
                   and v is not None}
        if not allowed:
            return await self.get_hook(hook_id)
        if "event" in allowed and allowed["event"] not in self.HOOK_EVENTS:
            return None
        sets = ", ".join(f"{k}=?" for k in allowed)
        await self._db.execute(f"UPDATE hooks SET {sets} WHERE id=?",
                               (*allowed.values(), hook_id))
        await self._db.commit()
        return await self.get_hook(hook_id)

    async def get_hook(self, hook_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM hooks WHERE id=?", (hook_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_hook(self, hook_id: str) -> None:
        await self._db.execute("DELETE FROM hooks WHERE id=?", (hook_id,))
        await self._db.commit()

    # -- skills (§6) -----------------------------------------------------------------

    async def upsert_skill(self, name: str, description: str, body: str) -> dict:
        from . import events as _ev
        now = time.time()
        cur = await self._db.execute("SELECT id,created_at FROM skills WHERE name=?",
                                     (name,))
        row = await cur.fetchone()
        if row:
            await self._db.execute(
                "UPDATE skills SET description=?, body=?, updated_at=? WHERE id=?",
                (description, body, now, row["id"]))
            await self._db.commit()
            return {"id": row["id"], "name": name, "description": description,
                    "body": body, "created_at": row["created_at"], "updated_at": now}
        skill = {"id": _ev.new_id(), "name": name, "description": description,
                 "body": body, "created_at": now, "updated_at": now}
        await self._db.execute(
            "INSERT INTO skills(id,name,description,body,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (skill["id"], name, description, body, now, now))
        await self._db.commit()
        return skill

    async def list_skills(self) -> list[dict]:
        cur = await self._db.execute(
            "SELECT id,name,description,created_at,updated_at FROM skills "
            "ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]

    async def get_skill(self, name: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM skills WHERE name=?", (name,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def delete_skill(self, name: str) -> None:
        await self._db.execute("DELETE FROM skills WHERE name=?", (name,))
        await self._db.commit()

    # -- audit log (§2: optional, OFF by default) ------------------------------------

    async def audit_write(self, user: str | None, chat_id: str | None,
                          model_key: str | None, tool: str, args: dict,
                          ok: bool, duration_ms: float) -> None:
        await self._db.execute(
            "INSERT INTO audit_log(ts,user,chat_id,model_key,tool,args,ok,"
            "duration_ms) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), user, chat_id, model_key, tool,
             json.dumps(args, ensure_ascii=False)[:4000], int(bool(ok)),
             duration_ms))
        await self._db.commit()

    async def audit_list(self, limit: int = 200, chat_id: str | None = None) -> list[dict]:
        if chat_id:
            cur = await self._db.execute(
                "SELECT * FROM audit_log WHERE chat_id=? ORDER BY ts DESC LIMIT ?",
                (chat_id, limit))
        else:
            cur = await self._db.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            try:
                d["args"] = json.loads(d.get("args") or "null")
            except (json.JSONDecodeError, TypeError):
                d["args"] = None
            out.append(d)
        return out
