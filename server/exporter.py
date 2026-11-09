"""Chat export/import (§16): round-trip in two formats.

JSON — the full-fidelity one: chat meta + EVERY stored event verbatim
(ids, seqs, mutation statuses). Import restores an identical rollout —
edits, deletions, masks and compactions replay exactly as before.

MD — the readable one: a transcript a human can post elsewhere; our own
marker headings make it parseable back (content events are rebuilt from
the readable text; service events ride along as compact JSON comments).
Import of MD never restores mutations — by design (§16 «читаемый,
для переноса в другой чат»).
"""

from __future__ import annotations

import json
import re
import time

from . import events as ev
from .history import HistoryStore

EXPORT_FORMAT = "agent-framework-chat"
EXPORT_VERSION = 1

# ------------------------------------------------------------------ JSON

def _chat_meta_public(chat: dict) -> dict:
    return {k: chat.get(k) for k in
            ("title", "model_key", "system_prompt", "send_think_blocks",
             "params", "compact_threshold")}


async def export_json(store: HistoryStore, chat_id: str) -> dict:
    chat = await store.get_chat(chat_id)
    if not chat:
        raise KeyError("chat not found")
    events = await store.raw_events(chat_id)
    return {"format": EXPORT_FORMAT, "version": EXPORT_VERSION,
            "exported_at": time.time(),
            "chat": _chat_meta_public(chat), "events": events}


async def import_json(store: HistoryStore, payload: dict,
                      title_prefix: str = "") -> dict:
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise ValueError("не похоже на экспорт agent-framework (format)")
    chat = payload.get("chat") or {}
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("нет списка events")
    # fresh ids (an import into the SAME database would collide with the
    # source rows); mutation references are remapped exactly like fork_chat
    id_map: dict[str, str] = {}
    cleaned = []
    for e in events:
        if not isinstance(e, dict) or not e.get("type"):
            continue
        old_id = str(e.get("id") or ev.new_id())
        id_map[old_id] = ev.new_id()
        cleaned.append({"_old_id": old_id, "seq": e.get("seq") or 0,
                        "ts": e.get("ts") or 0,
                        "type": str(e["type"]),
                        "data": e.get("data") if isinstance(e.get("data"), dict) else {},
                        "target": e.get("target"),
                        "status": e.get("status") or "active"})
    cleaned.sort(key=lambda e: e["seq"])
    for e in cleaned:
        old_id = e.pop("_old_id")
        e.pop("seq", None)                   # import_chat renumbers
        e["id"] = id_map[old_id]
        if e.get("target"):
            e["target"] = id_map.get(e["target"], e["target"])
        for key in ("targets", "forgotten_event_ids"):
            refs = (e.get("data") or {}).get(key)
            if isinstance(refs, list):
                e["data"][key] = [id_map.get(t, t) for t in refs]
    meta = dict(chat)
    if title_prefix:
        meta["title"] = title_prefix + (meta.get("title") or "Импорт")
    return await store.import_chat(meta, cleaned)


# ------------------------------------------------------------------ MD

_MD_HEAD = "agent-framework-chat v1"

async def export_md(store: HistoryStore, chat_id: str) -> str:
    chat = await store.get_chat(chat_id)
    if not chat:
        raise KeyError("chat not found")
    lines: list[str] = [f"# {chat.get('title') or 'Чат'}", ""]
    meta = _chat_meta_public(chat)
    meta.pop("title", None)
    lines.append(f"<!-- {_MD_HEAD} {json.dumps(meta, ensure_ascii=False)} -->")
    lines.append("")
    for e in await store.all_events(chat_id):
        st = e.get("status") or "active"
        if st == "deleted":
            continue
        d = e.get("data") or {}
        t = e["type"]
        if st in ("masked", "forgotten"):
            lines.append(f"<!-- status: {st} -->")
        if t == ev.USER_MESSAGE:
            lines.append("## 👤 Пользователь")
            lines.append("")
            lines.append(str(d.get("text") or ""))
            if d.get("attachments"):
                lines.append("")
                lines.append("<!-- attachments "
                             + json.dumps(d["attachments"], ensure_ascii=False) + " -->")
        elif t == ev.ASSISTANT_TEXT:
            lines.append("## 🤖 Ассистент")
            lines.append("")
            lines.append(str(d.get("text") or ""))
        elif t == ev.ASSISTANT_THINK:
            lines.append("## 💭 Размышления")
            lines.append("")
            lines.append(str(d.get("text") or ""))
        elif t == ev.TOOL_CALL:
            lines.append(f"## 🔧 Вызов тулзы: {d.get('name')}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(d.get("args") or {}, ensure_ascii=False, indent=1))
            lines.append("```")
        elif t == ev.TOOL_RESULT:
            lines.append(f"## 📤 Результат: {d.get('name')}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(d.get("result") or {}, ensure_ascii=False,
                                    indent=1, default=str))
            lines.append("```")
        else:
            # service events (turn_meta, compact, plan, notices, ...) —
            # full payload as a compact JSON comment keeps the round-trip
            lines.append(f"<!-- ev:{t} "
                         + json.dumps(d, ensure_ascii=False, default=str) + " -->")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_MD_SECTION = re.compile(r"^## (?:(👤|🤖|💭|🔧|📤)\s*)?(.+?)\s*$")


async def import_md(store: HistoryStore, text: str,
                    title_prefix: str = "") -> dict:
    head = None
    cur_type = None            # section being filled
    buf: list[str] = []
    json_buf: list[str] = []   # fenced json block inside a section
    in_fence = False
    chat_meta: dict = {}
    events: list[dict] = []

    def _user_event(type_, data):
        events.append({"id": ev.new_id(), "ts": time.time(),
                       "type": type_, "data": data, "target": None,
                       "status": "active"})

    def _flush():
        nonlocal cur_type, buf, json_buf
        if cur_type is None:
            buf, json_buf = [], []
            return
        body = "\n".join(buf).strip()
        raw_json = "\n".join(json_buf).strip()
        buf, json_buf = [], []
        ctype, cur = cur_type, None
        cur_type = None
        if ctype == "user":
            data = {"text": body}
            m = re.search(r"<!-- attachments (.+?) -->", body, re.S)
            if m:
                try:
                    data["attachments"] = json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass
                data["text"] = re.sub(r"\s*<!-- attachments .+? -->", "", body,
                                      flags=re.S).strip()
            _user_event(ev.USER_MESSAGE, data)
        elif ctype == "assistant":
            _user_event(ev.ASSISTANT_TEXT, {"text": body})
        elif ctype == "think":
            _user_event(ev.ASSISTANT_THINK, {"text": body})
        elif ctype == "tool":
            args = {}
            if raw_json:
                try:
                    args = json.loads(raw_json)
                except json.JSONDecodeError:
                    args = {"_raw": raw_json}
            _user_event(ev.TOOL_CALL, {"call_id": ev.new_id(),
                                       "name": ctype_name, "args": args})
        elif ctype == "result":
            result = {}
            if raw_json:
                try:
                    result = json.loads(raw_json)
                except json.JSONDecodeError:
                    result = {"output": raw_json[:4000]}
            _user_event(ev.TOOL_RESULT, {"call_id": "", "name": ctype_name,
                                         "result": result})

    ctype_name = ""
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            json_buf.append(line)
            continue
        if line.startswith(f"<!-- {_MD_HEAD} ") and head is None:
            head = line[len(f"<!-- {_MD_HEAD} "):-4].strip()
            try:
                chat_meta = json.loads(head)
            except json.JSONDecodeError:
                chat_meta = {}
            continue
        if line.startswith("<!-- ev:"):
            _flush()
            rest = line[8:]
            if rest.endswith("-->"):
                rest = rest[:-3].strip()
            parts = rest.split(" ", 1)
            etype = parts[0]
            data = {}
            if len(parts) > 1:
                try:
                    data = json.loads(parts[1])
                except json.JSONDecodeError:
                    data = {}
            events.append({"id": ev.new_id(), "ts": time.time(), "type": etype,
                           "data": data, "target": None, "status": "active"})
            continue
        m = _MD_SECTION.match(line)
        if m:
            _flush()
            icon, rest = m.group(1), m.group(2)
            ctype = None
            if icon == "👤" or rest.startswith("Пользователь"):
                ctype = "user"
            elif icon == "🤖" or rest.startswith("Ассистент"):
                ctype = "assistant"
            elif icon == "💭" or rest.startswith("Размышления"):
                ctype = "think"
            elif icon == "🔧" or rest.startswith("Вызов тулзы"):
                ctype = "tool"
                ctype_name = rest.replace("Вызов тулзы:", "").strip()
            elif icon == "📤" or rest.startswith("Результат"):
                ctype = "result"
                ctype_name = rest.replace("Результат:", "").strip()
            if ctype:
                cur_type = ctype
            continue
        if line.startswith("# ") and head is None and not chat_meta.get("title"):
            chat_meta["title"] = line[2:].strip()
            continue
        if cur_type is not None:
            buf.append(line)
    _flush()

    if not events:
        raise ValueError("в MD не найдено ни одного события нашего формата")
    meta = dict(chat_meta)
    meta["title"] = title_prefix + (meta.get("title") or "Импорт из MD")
    return await store.import_chat(meta, events)
