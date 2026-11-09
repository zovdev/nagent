"""History Store — append-only event log backed by SQLite + JSONL.

- SQLite `events` table is the queryable index (effective history, SSE replay).
- `<chat>.jsonl` is the rollout file (Codex-style): every event is appended,
  nothing is ever rewritten there — full audit survives in the file.
- Edit/delete are themselves events (tombstone / replace), never mutations.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import aiosqlite

from . import events as ev

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    model_key TEXT,                -- "provider_id::model_id"
    system_prompt TEXT,
    send_think_blocks INTEGER DEFAULT 0,
    params TEXT,                   -- JSON: generation params (§3)
    compact_threshold TEXT,        -- '80%' or '15000' (§11; NULL = server default)
    owner TEXT,                    -- §2 team mode: user id (NULL = solo legacy)
    shared INTEGER DEFAULT 0      -- §2 team mode: shared project flag
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    target TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events(chat_id, seq);
"""

from .db import DATA_DIR

SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")


class HistoryStore:
    def __init__(self, db_path: str, sessions_dir: str | None = None):
        self.db_path = db_path
        self.sessions_dir = sessions_dir or SESSIONS_DIR
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()          # serialize seq allocation + append
        self._listeners: dict[str, set[asyncio.Queue]] = {}

    async def open(self) -> None:
        os.makedirs(self.sessions_dir, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = sqlite3.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        # migrations for older databases (params: Phase 0, compact_threshold:
        # Phase 3, owner/shared: Phase 5 §2 — team-mode shared projects)
        cur = await self._db.execute("PRAGMA table_info(chats)")
        cols = {r[1] for r in await cur.fetchall()}
        if "params" not in cols:
            await self._db.execute("ALTER TABLE chats ADD COLUMN params TEXT")
        if "compact_threshold" not in cols:
            await self._db.execute("ALTER TABLE chats ADD COLUMN compact_threshold TEXT")
        if "owner" not in cols:
            await self._db.execute("ALTER TABLE chats ADD COLUMN owner TEXT")
        if "shared" not in cols:
            await self._db.execute("ALTER TABLE chats ADD COLUMN shared INTEGER DEFAULT 0")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            db, self._db = self._db, None
            await db.close()

    # -- chats ---------------------------------------------------------------

    async def create_chat(self, title: str, model_key: str | None,
                          system_prompt: str | None,
                          send_think_blocks: bool = False,
                          params: dict | None = None,
                          compact_threshold: str | None = None,
                          owner: str | None = None,
                          shared: bool = False) -> dict:
        import time
        chat = {"id": ev.new_id(), "title": title, "created_at": time.time(),
                "model_key": model_key, "system_prompt": system_prompt,
                "send_think_blocks": int(send_think_blocks),
                "params": json.dumps(params or {}, ensure_ascii=False),
                "compact_threshold": compact_threshold,
                "owner": owner, "shared": int(bool(shared))}
        await self._db.execute(
            "INSERT INTO chats(id,title,created_at,model_key,system_prompt,"
            "send_think_blocks,params,compact_threshold,owner,shared) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (chat["id"], title, chat["created_at"], model_key, system_prompt,
             int(send_think_blocks), chat["params"], compact_threshold,
             owner, chat["shared"]))
        await self._db.commit()
        chat["params"] = params or {}
        return chat

    async def list_chats(self, owner: str | None = None,
                         include_shared: bool = False) -> list[dict]:
        """Solo/team/accounts listing. `owner=None` returns everything
        (admin view); a concrete owner sees own chats + shared ones when
        `include_shared` (team §2: shared projects carry the «общий» badge)."""
        if owner is None and not include_shared:
            cur = await self._db.execute("SELECT * FROM chats ORDER BY created_at DESC")
        elif owner is None:
            cur = await self._db.execute(
                "SELECT * FROM chats WHERE shared=1 ORDER BY created_at DESC")
        elif include_shared:
            cur = await self._db.execute(
                "SELECT * FROM chats WHERE owner=? OR shared=1 "
                "ORDER BY created_at DESC", (owner,))
        else:
            cur = await self._db.execute(
                "SELECT * FROM chats WHERE owner=? OR owner IS NULL "
                "ORDER BY created_at DESC", (owner,))
        return [self._parse_chat(dict(r)) for r in await cur.fetchall()]

    async def get_chat(self, chat_id: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM chats WHERE id=?", (chat_id,))
        row = await cur.fetchone()
        return self._parse_chat(dict(row)) if row else None

    @staticmethod
    def _parse_chat(chat: dict) -> dict:
        raw = chat.get("params")
        try:
            chat["params"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            chat["params"] = {}
        return chat

    async def update_chat(self, chat_id: str, **fields) -> None:
        if not fields:
            return
        if "shared" in fields:
            fields["shared"] = int(bool(fields["shared"]))
        if "params" in fields and isinstance(fields["params"], dict):
            fields["params"] = json.dumps(fields["params"], ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in fields)
        await self._db.execute(f"UPDATE chats SET {sets} WHERE id=?",
                               (*fields.values(), chat_id))
        await self._db.commit()

    async def delete_chat(self, chat_id: str) -> None:
        await self._db.execute("DELETE FROM events WHERE chat_id=?", (chat_id,))
        await self._db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        await self._db.commit()
        path = self._rollout_path(chat_id)
        if os.path.exists(path):
            os.remove(path)

    # -- append --------------------------------------------------------------

    async def append(self, chat_id: str, type_: str, data: dict | None = None,
                     target: str | None = None) -> dict:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE chat_id=?", (chat_id,))
            seq = (await cur.fetchone())[0] + 1
            event = ev.make_event(chat_id, seq, type_, data, target)

            # apply mutation semantics immediately for the index
            if type_ == ev.DELETE and target:
                await self._db.execute(
                    "UPDATE events SET status='deleted' WHERE id=? AND chat_id=?",
                    (target, chat_id))
            elif type_ == ev.EDIT and target:
                await self._db.execute(
                    "UPDATE events SET status='replaced' WHERE id=? AND chat_id=?",
                    (target, chat_id))
            elif type_ == ev.MASK and (data or {}).get("targets"):
                q = ",".join("?" * len(data["targets"]))
                await self._db.execute(
                    f"UPDATE events SET status='masked' WHERE chat_id=? AND id IN ({q})",
                    (chat_id, *data["targets"]))
            elif type_ == ev.COMPACT and (data or {}).get("forgotten_event_ids"):
                # §11 compaction: forgotten events leave the MODEL context but
                # stay fully visible to the user. Only ACTIVE events are
                # forgotten — deleted/masked ones keep their own status.
                q = ",".join("?" * len(data["forgotten_event_ids"]))
                await self._db.execute(
                    f"UPDATE events SET status='forgotten' "
                    f"WHERE chat_id=? AND status='active' AND id IN ({q})",
                    (chat_id, *data["forgotten_event_ids"]))

            await self._db.execute(
                "INSERT INTO events(id,chat_id,seq,ts,type,data,target,status) "
                "VALUES(?,?,?,?,?,?,?,'active')",
                (event["id"], chat_id, event["seq"], event["ts"], event["type"],
                 ev.dumps(event), event["target"]))
            await self._db.commit()

            # rollout JSONL: audit-grade append, never rewritten
            with open(self._rollout_path(chat_id), "a", encoding="utf-8") as f:
                f.write(ev.dumps(event) + "\n")

        self._publish(chat_id, event)
        return event

    # -- reads ---------------------------------------------------------------

    async def all_events(self, chat_id: str) -> list[dict]:
        """Replay: content events in seq order with tombstones/edits/masks applied.
        DELETE  -> target carries status='deleted' (filtered by effective()).
        EDIT    -> target's payload is replaced in-place (marked edited=True).
        MASK    -> targets carry status='masked' (user still sees them, model
                   does not — §15 timeout-retry / plan-mode cleanup).
        COMPACT -> data.forgotten_event_ids carry status='forgotten' (user
                   still sees them, model gets the summary instead — §11)."""
        cur = await self._db.execute(
            "SELECT * FROM events WHERE chat_id=? ORDER BY seq", (chat_id,))
        rows = await cur.fetchall()
        out = []
        by_id: dict[str, dict] = {}
        for r in rows:
            e = json.loads(r["data"])
            e["status"] = r["status"]
            if e["type"] == ev.DELETE:
                continue  # already reflected in target's status
            if e["type"] == ev.EDIT:
                target = by_id.get(e["target"])
                if target is not None:
                    target["data"] = e["data"]
                    target["edited"] = True
                continue
            if e["type"] == ev.MASK:
                for tid in (e["data"] or {}).get("targets", []):
                    t = by_id.get(tid)
                    if t is not None:
                        t["status"] = "masked"
                        t["masked_reason"] = (e["data"] or {}).get("reason")
                continue
            if e["type"] == ev.COMPACT:
                for tid in (e["data"] or {}).get("forgotten_event_ids", []):
                    t = by_id.get(tid)
                    if t is not None and t.get("status") == "active":
                        t["status"] = "forgotten"
                # the compact event itself stays: it IS the model-visible summary
                by_id[e["id"]] = e
                out.append(e)
                continue
            by_id[e["id"]] = e
            out.append(e)
        return out

    async def effective(self, chat_id: str) -> list[dict]:
        """Model-visible history: content events that survived tombstones,
        masks and compaction. `replaced` = EDITED (payload swapped at replay)
        and still visible to the model with its new payload — editing must
        never mean deleting (§9). forgotten = replaced by the compact
        summary, masked = hidden for §15/plan reasons — the user still
        sees both."""
        return [e for e in await self.all_events(chat_id)
                if e["status"] in ("active", "replaced")]

    async def events_since(self, chat_id: str, since_seq: int) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM events WHERE chat_id=? AND seq>? ORDER BY seq",
            (chat_id, since_seq))
        return [json.loads(r["data"]) | {"status": r["status"]}
                for r in await cur.fetchall()]

    async def last_seq(self, chat_id: str) -> int:
        cur = await self._db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE chat_id=?", (chat_id,))
        return (await cur.fetchone())[0]

    # -- per-event operations (§9 total context control) -------------------------

    async def get_event(self, chat_id: str, event_id: str) -> dict | None:
        """Raw stored event (original payload — EDITs never touch the row)."""
        cur = await self._db.execute(
            "SELECT * FROM events WHERE chat_id=? AND id=?", (chat_id, event_id))
        row = await cur.fetchone()
        if not row:
            return None
        e = json.loads(row["data"])
        e["status"] = row["status"]
        return e

    async def event_versions(self, chat_id: str, event_id: str) -> dict | None:
        """Edit history of one event: the original payload + every EDIT payload
        in order. The rollout file keeps all of them; this is the queryable view."""
        target = await self.get_event(chat_id, event_id)
        if target is None:
            return None
        cur = await self._db.execute(
            "SELECT * FROM events WHERE chat_id=? AND type=? AND target=? "
            "ORDER BY seq", (chat_id, ev.EDIT, event_id))
        versions = [{"ts": target.get("ts") or 0, "data": target["data"],
                     "original": True}]
        for r in await cur.fetchall():
            edit = json.loads(r["data"])
            versions.append({"ts": edit.get("ts") or r["ts"],
                             "data": edit.get("data") or {}, "original": False})
        return {"event_id": event_id, "type": target["type"],
                "versions": versions}

    async def raw_events(self, chat_id: str) -> list[dict]:
        """Every stored event verbatim (RAW view / fork source)."""
        cur = await self._db.execute(
            "SELECT * FROM events WHERE chat_id=? ORDER BY seq", (chat_id,))
        return [json.loads(r["data"]) | {"status": r["status"]}
                for r in await cur.fetchall()]

    async def fork_chat(self, chat_id: str, until_event_id: str | None,
                        title: str) -> dict:
        """Fork = new rollout file (Codex pattern). Copies every event up to
        and including `until_event_id` (None = whole chat) with FRESH event ids;
        mutation targets are remapped so the fork replays identically."""
        src = await self.get_chat(chat_id)
        new_chat = await self.create_chat(
            title, src.get("model_key"), src.get("system_prompt"),
            bool(src.get("send_think_blocks")), json.loads(src.get("params") or "{}"),
            src.get("compact_threshold"))
        rows = await self.raw_events(chat_id)
        id_map: dict[str, str] = {}
        picked = []
        for e in rows:
            picked.append(e)
            if until_event_id and e["id"] == until_event_id:
                break
        # pass 1: allocate fresh ids
        for e in picked:
            id_map[e["id"]] = ev.new_id()
        # pass 2: write with remapped references (status verbatim — the
        # mutation replay in all_events() relies on the persisted statuses)
        for e in picked:
            e = json.loads(json.dumps(e))     # deep copy
            e["id"] = id_map[e["id"]]
            e["chat_id"] = new_chat["id"]
            if e.get("target"):
                e["target"] = id_map.get(e["target"], e["target"])
            for key in ("targets", "forgotten_event_ids"):
                refs = (e.get("data") or {}).get(key)
                if isinstance(refs, list):
                    e["data"][key] = [id_map.get(t, t) for t in refs]
            async with self._lock:
                cur = await self._db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events WHERE chat_id=?",
                    (new_chat["id"],))
                seq = (await cur.fetchone())[0] + 1
                e["seq"] = seq
                await self._db.execute(
                    "INSERT INTO events(id,chat_id,seq,ts,type,data,target,status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (e["id"], new_chat["id"], seq, e["ts"], e["type"],
                     ev.dumps(e), e.get("target"), e.get("status") or "active"))
                await self._db.commit()
                with open(self._rollout_path(new_chat["id"]), "a", encoding="utf-8") as f:
                    f.write(ev.dumps(e) + "\n")
        return new_chat

    # -- live subscriptions ----------------------------------------------------

    def subscribe(self, chat_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._listeners.setdefault(chat_id, set()).add(q)
        return q

    def unsubscribe(self, chat_id: str, q: asyncio.Queue) -> None:
        s = self._listeners.get(chat_id)
        if s and q in s:
            s.discard(q)
            if not s:
                self._listeners.pop(chat_id, None)

    def _publish(self, chat_id: str, event: dict) -> None:
        """Synchronous fan-out to SSE subscriber queues (no awaits inside)."""
        for q in list(self._listeners.get(chat_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer drops; SSE client reconnects with ?since=

    def _rollout_path(self, chat_id: str) -> str:
        return os.path.join(self.sessions_dir, f"{chat_id}.jsonl")

    # -- import (§16 round-trip) ---------------------------------------------------

    async def import_chat(self, chat: dict, events: list[dict]) -> dict:
        """Create a chat and write exported events VERBATIM: ids, seqs, ts,
        targets and final statuses are preserved, so the imported rollout
        replays exactly like the original (round-trip §16). Events arrive in
        seq order; seqs are renumbered defensively in case of gaps."""
        new_chat = await self.create_chat(
            chat.get("title") or "Импорт", chat.get("model_key"),
            chat.get("system_prompt"), bool(chat.get("send_think_blocks")),
            chat.get("params") if isinstance(chat.get("params"), dict) else {},
            chat.get("compact_threshold"))
        n = 0
        for e in events:
            n += 1
            row = json.loads(json.dumps(e))      # deep copy
            row["chat_id"] = new_chat["id"]
            row["seq"] = n
            async with self._lock:
                await self._db.execute(
                    "INSERT INTO events(id,chat_id,seq,ts,type,data,target,status) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (row["id"], new_chat["id"], n, row.get("ts") or 0,
                     row["type"], ev.dumps(row), row.get("target"),
                     row.get("status") or "active"))
                await self._db.commit()
                with open(self._rollout_path(new_chat["id"]), "a",
                          encoding="utf-8") as f:
                    f.write(ev.dumps(row) + "\n")
        return new_chat
