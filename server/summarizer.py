"""Two-step summarization of oversized tool outputs (§11 roadmap).

Step 1 — the MAIN model (the one driving the turn) writes a prompt for
         summarization: WHAT matters in this output for the task at hand.
Step 2 — a CHEAP model (settings.summarizer_model_key, '' = the chat model)
         summarizes the full output following that prompt.

Both intermediate requests are appended to the chat as `summarize` events:
the USER sees them, the MODEL context never does (lifecycle events don't map
to roles). The tool_result the model receives carries only the summary; the
full output is cached server-side (`.agent/toolcache/<call_id>`).

`complete_text` is the shared non-streaming helper (also used by compaction
and subagents): collects text chunks from the router's stream.
"""

from __future__ import annotations

import os

from . import events as ev
from .providers import ProviderError

TWO_STEP_THRESHOLD = 30_000          # serialized-result chars before it kicks in

_STEP1_SYSTEM = (
    "Ты — помощник агентской обвязки. Тулза `{tool}` вернула огромный вывод. "
    "Напиши КОРОТКУЮ инструкцию (до 10 строк) для модели-суммаризатора: что "
    "в этом выводе важно для исходной задачи, что сохранить обязательно "
    "(числа, пути, ошибки, статусы), что можно выбросить. Только инструкция."
)
_STEP2_SYSTEM = (
    "Ты — модель-суммаризатор. Следуй инструкции пользователя и выдай "
    "сжатую выжимку большого вывода тулзы. Сохраняй все ключевые факты "
    "(числа, пути, ошибки); формат — плотный текст до 2000 символов."
)


class SummarizeError(RuntimeError):
    pass


async def complete_text(router, model_key: str, messages: list[dict],
                        max_chars: int = 8000,
                        params: dict | None = None) -> str:
    """One non-streaming-style completion: collect text deltas, drop think."""
    chunks: list[str] = []
    total = 0
    async for chunk in router.chat_stream(model_key, messages, None, params):
        ctype = chunk.get("type")
        if ctype == "text":
            piece = chunk.get("delta") or ""
            chunks.append(piece)
            total += len(piece)
            if total >= max_chars:
                break
        elif ctype == "usage":
            continue
        elif ctype == "done":
            break
    text = "".join(chunks).strip()
    if not text:
        raise SummarizeError("модель вернула пустой текст")
    return text[:max_chars]


class TwoStepSummarizer:
    def __init__(self, store, settings_store, toolcache_dir: str):
        self.store = store                    # HistoryStore (events)
        self.settings = settings_store
        self.toolcache_dir = toolcache_dir

    async def enabled(self) -> bool:
        s = await self.settings.get_settings()
        return str(s.get("two_step_summarize", "1")) not in ("0", "false", "off")

    async def cheap_model_key(self, chat_model_key: str) -> str:
        s = await self.settings.get_settings()
        key = (s.get("summarizer_model_key") or "").strip()
        return key or chat_model_key

    async def summarize_tool_output(self, chat_id: str, call_id: str,
                                    tool: str, result_text: str,
                                    router, main_model_key: str) -> dict:
        """Run both steps; append `summarize` events; return the model-visible
        result payload (summary + cache pointer). Raises SummarizeError on
        model failure — the caller falls back to plain truncation."""
        # step 1: the MAIN model writes the summarization prompt
        step1 = await complete_text(
            router, main_model_key,
            [{"role": "system", "content": _STEP1_SYSTEM.format(tool=tool)},
             {"role": "user",
              "content": result_text[:4000] + ("\n…[обрезано]" if len(result_text) > 4000 else "")}])
        await self.store.append(chat_id, ev.SUMMARIZE, {
            "step": 1, "tool": tool, "call_id": call_id,
            "note": "основная модель написала промпт для суммаризации",
            "text": step1})

        # step 2: the CHEAP model summarizes the FULL output
        cheap_key = await self.cheap_model_key(main_model_key)
        summary = await complete_text(
            router, cheap_key,
            [{"role": "system", "content": _STEP2_SYSTEM},
             {"role": "user",
              "content": f"Инструкция:\n{step1}\n\n--- Вывод тулзы {tool} ---\n"
                         f"{result_text}"}], max_chars=3000)
        await self.store.append(chat_id, ev.SUMMARIZE, {
            "step": 2, "tool": tool, "call_id": call_id,
            "model": cheap_key, "chars": len(result_text),
            "note": "дешёвая модель суммаризировала полный вывод",
            "text": summary})

        cache_path = self._cache(call_id, result_text)
        return {"summarized": True,
                "summary": summary,
                "original_chars": len(result_text),
                "note": "вывод сокращён двухшаговой суммаризацией (§11); "
                        "полный текст в кеше сервера",
                "cache": cache_path}

    def _cache(self, call_id: str, text: str) -> str | None:
        try:
            os.makedirs(self.toolcache_dir, exist_ok=True)
            path = os.path.join(self.toolcache_dir, f"{call_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            return path
        except OSError:
            return None
