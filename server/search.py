"""Keyword search over own sessions (§12 roadmap).

Full-text (keyword) search across chat titles and message texts. Semantic
search via embeddings stays a later optional step — the roadmap is honest
about that. The search hits the events table JSON payloads; a match inside
the JSON string is re-verified against the decoded text so snippets are
always real decoded content, never raw JSON fragments.
"""

from __future__ import annotations

import json

from . import events as ev

SEARCHABLE = (ev.USER_MESSAGE, ev.ASSISTANT_TEXT)
SNIPPET_RADIUS = 60


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_sessions(history_store, query: str, limit: int = 50) -> list[dict]:
    query = (query or "").strip()
    if len(query) < 2:
        return []
    like = f"%{_like_escape(query)}%"
    cur = await history_store._db.execute(
        "SELECT e.chat_id, e.id, e.seq, e.ts, e.type, e.data "
        "FROM events e WHERE e.type IN (?,?) AND e.data LIKE ? ESCAPE '\\' "
        "ORDER BY e.ts DESC LIMIT ?",
        (*SEARCHABLE, like, limit * 3))
    rows = await cur.fetchall()

    chats: dict[str, dict] = {}
    for c in await history_store.list_chats():
        chats[c["id"]] = c

    out = []
    for r in rows:
        try:
            event = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        # the data column stores the WHOLE event JSON; the text lives inside
        text = (event.get("data") or {}).get("text")
        if not isinstance(text, str):
            continue
        low = text.lower()
        pos = low.find(query.lower())
        if pos < 0:
            continue                 # LIKE matched escaped JSON, not the text
        start = max(0, pos - SNIPPET_RADIUS)
        snippet = ("…" if start > 0 else "") + \
            text[start:pos + len(query) + SNIPPET_RADIUS] + \
            ("…" if pos + len(query) + SNIPPET_RADIUS < len(text) else "")
        out.append({
            "chat_id": r["chat_id"],
            "title": (chats.get(r["chat_id"]) or {}).get("title", "чат"),
            "event_id": r["id"], "seq": r["seq"], "ts": r["ts"],
            "type": r["type"],
            "snippet": snippet,
        })
        if len(out) >= limit:
            break
    return out
