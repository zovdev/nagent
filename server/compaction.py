"""Compaction (§11 roadmap): threshold from REAL usage, summary by a cheap
model, `forgotten_event_ids` (OpenHands) — the model sees the compact, the
user keeps the full history.

Threshold is ONE field (per chat, server default as fallback):
  '80%'    — percentage of the model's context window (from the catalog /
             override — never guessed);
  '15000'  — absolute prompt tokens.

The current context size is the LAST turn's real `usage.prompt_tokens`
(the API's own accounting — no local tokenizer). Before the first turn
there is nothing to compact.

Kept verbatim: the first `keep_head` content events (the original task) and
the last `keep_last` content events. Everything between is summarized and
forgotten. `clear()` forgets everything (the "Clear" button, §6).
"""

from __future__ import annotations

from . import events as ev
from .summarizer import complete_text

SUMMARY_SYSTEM = (
    "Ты — компрессор контекста агентской сессии. Ниже — транскрипт части "
    "переписки юзера и агента (включая тулкаллы и их результаты). Сожми его "
    "в выжимку для дальнейшей работы агента: цель задачи, ключевые решения, "
    "созданные/изменённые файлы, важные числа и факты, незакрытые хвосты. "
    "Плотный текст до 1500 символов, без воды."
)

CLEAR_SUMMARY = ("(контекст очищен пользователем — история в UI сохранилась, "
                 "модель начинает с чистого листа)")


def parse_threshold(spec: str | None) -> tuple[str, float] | None:
    """'80%' -> ('percent', 80.0); '15000' -> ('tokens', 15000.0)."""
    raw = (spec or "").strip()
    if not raw:
        return None
    if raw.endswith("%"):
        try:
            pct = float(raw[:-1].strip())
        except ValueError:
            return None
        if 0 < pct <= 100:
            return ("percent", pct)
        return None
    try:
        tokens = float(raw)
    except ValueError:
        return None
    if tokens > 0:
        return ("tokens", tokens)
    return None


def render_transcript(events: list[dict], cap: int = 100_000) -> str:
    """Forgotten events -> compact text transcript for the summarizer."""
    lines: list[str] = []
    total = 0
    for e in events:
        t, d = e["type"], e.get("data") or {}
        if t == ev.USER_MESSAGE:
            line = f"ЮЗЕР: {d.get('text', '')}"
        elif t == ev.ASSISTANT_TEXT:
            line = f"АГЕНТ: {d.get('text', '')}"
        elif t == ev.ASSISTANT_THINK:
            line = f"(размышление: {d.get('text', '')[:400]})"
        elif t == ev.TOOL_CALL:
            line = f"ТУЛКАЛЛ {d.get('name')}: {str(d.get('args'))[:300]}"
        elif t == ev.TOOL_RESULT:
            line = f"  ↳ {str(d.get('result'))[:600]}"
        elif t == ev.AGENT_NOTICE:
            line = f"[система] {d.get('text', '')}"
        elif t == ev.PLAN_APPROVED:
            line = f"ОДОБРЕННЫЙ ПЛАН: {d.get('plan_text', '')[:800]}"
        else:
            continue
        lines.append(line)
        total += len(line)
        if total > cap:
            lines.append("… [транскрипт обрезан]")
            break
    return "\n".join(lines)


class Compactor:
    def __init__(self, store, settings_store, router_factory):
        self.store = store                    # HistoryStore
        self.settings = settings_store
        self.router_factory = router_factory  # () -> awaitable ModelRouter
        self._busy: set[str] = set()

    # -- helpers -------------------------------------------------------------

    async def _config(self) -> dict:
        s = await self.settings.get_settings()
        return s

    async def threshold_for(self, chat: dict) -> tuple[str, float] | None:
        s = await self._config()
        spec = chat.get("compact_threshold") or s.get("compact_threshold")
        return parse_threshold(spec)

    @staticmethod
    async def _last_usage(store, chat_id: str) -> dict | None:
        last = None
        for e in await store.all_events(chat_id):
            if e["type"] == ev.TURN_META and (e.get("data") or {}).get("usage"):
                last = e["data"]["usage"]
        return last

    async def over_threshold(self, chat: dict,
                             context_length: int | None) -> bool:
        """REAL usage vs the threshold — no estimates anywhere."""
        thr = await self.threshold_for(chat)
        if thr is None:
            return False
        usage = await self._last_usage(self.store, chat["id"])
        if not usage:
            return False
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            return False
        mode, value = thr
        if mode == "tokens":
            return prompt >= value
        if not context_length:
            return False            # window unknown → % is meaningless
        return prompt / context_length * 100 >= value

    # -- compaction -------------------------------------------------------------

    async def compact(self, chat: dict, reason: str = "manual",
                      router=None) -> dict | None:
        """Build the summary, forget the middle. Returns the COMPACT event."""
        chat_id = chat["id"]
        if chat_id in self._busy:
            return None
        self._busy.add(chat_id)
        try:
            s = await self._config()
            keep_head = _int(s.get("compact_keep_head"), 2)
            keep_last = _int(s.get("compact_keep_last"), 8)

            content = [e for e in await self.store.all_events(chat_id)
                       if e["type"] in ev.CONTENT_TYPES]
            if len(content) <= keep_head + keep_last:
                return None          # nothing in the middle to forget

            head, middle, tail = (content[:keep_head],
                                  content[keep_head:-keep_last] if keep_last else content[keep_head:],
                                  content[-keep_last:] if keep_last else [])
            forgotten_ids = [e["id"] for e in middle]
            if not forgotten_ids:
                return None

            router = router or await self.router_factory()
            model_key = (s.get("compact_model_key") or "").strip() \
                or chat.get("model_key")
            summary = await self._summarize(chat_id, middle, router, model_key)

            return await self.store.append(chat_id, ev.COMPACT, {
                "summary": summary,
                "forgotten_event_ids": forgotten_ids,
                "kept_head": len(head), "kept_tail": len(tail),
                "model": model_key, "reason": reason,
                "threshold": (await self.threshold_for(chat) or (None, None))[0]})
        finally:
            self._busy.discard(chat_id)

    async def _summarize(self, chat_id: str, middle: list[dict],
                         router, model_key: str | None) -> str:
        transcript = render_transcript(middle)
        try:
            return await complete_text(
                router, model_key,
                [{"role": "system", "content": SUMMARY_SYSTEM},
                 {"role": "user", "content": transcript}], max_chars=4000)
        except Exception as e:                       # noqa: BLE001
            # the compaction must never kill the chat: fall back to a stub
            return (f"(авто-выжимка недоступна: {e}; сжато механически)\n"
                    f"{transcript[:1500]}…")

    async def clear(self, chat: dict) -> dict:
        """The Clear button (§6): forget ALL content, keep the rollout."""
        chat_id = chat["id"]
        content = [e for e in await self.store.all_events(chat_id)
                   if e["type"] in ev.CONTENT_TYPES]
        return await self.store.append(chat_id, ev.COMPACT, {
            "summary": CLEAR_SUMMARY,
            "forgotten_event_ids": [e["id"] for e in content],
            "kept_head": 0, "kept_tail": 0, "model": None,
            "reason": "clear"})


def _int(raw, default: int) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default
