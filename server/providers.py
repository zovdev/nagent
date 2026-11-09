"""Provider layer: OpenAI-compatible hosts with model catalog passthrough.

- Host normalization: user may paste `host`, `host/v1`, `host/v1/` — we always
  store the bare host and append endpoints ourselves.
- Provider = host + API key. Nothing else. We are an agent wrapper, not a
  reseller: NO local price tables, NO capability guessing from model names.
  The catalog is a passthrough of `/v1/models`: context window / capabilities
  are stored ONLY when the API explicitly declares them, the full raw item is
  kept verbatim and shown in the UI. Absent field = honest "unknown".
- Token/usage data comes ONLY from the `usage` field the API returns at the
  end of generation (cache fields included). No tiktoken, no estimators.
- Streaming chat with reasoning normalization: `reasoning_content` (DeepSeek)
  and inline thinking tags become `think` chunks, so the system sees one shape.
- Generation params (temperature / top_p / max_tokens / reasoning_effort)
  are passed through per chat (§3 roadmap).
- Error taxonomy for the fallback cascade (§3): RateLimit (429 RPM -> retry
  with backoff, honouring Retry-After), Quota (billing/limit -> next model
  immediately), Fatal (bad key/model -> next model, no retry), Unavailable
  (network/5xx -> retry, then next model).
- `mock://demo` and `mock://flaky` providers let the whole pipeline —
  including the fallback cascade — be tested without any API key.
"""

from __future__ import annotations

import json
import re
import time
from typing import AsyncIterator

import aiohttp

# §17 proxy matrix: model_api route. Set by main.py on startup/settings
# change; None = direct connection. http/https use aiohttp's request-level
# proxy; socks requires the optional aiohttp-socks package.
_MODEL_PROXY: dict | None = None


def set_model_proxy(cfg: dict | None) -> None:
    global _MODEL_PROXY
    _MODEL_PROXY = cfg


def normalize_host(raw: str) -> str:
    h = raw.strip().rstrip("/")
    h = re.sub(r"/v1$", "", h)
    return h


# ----------------------------------------------------------------- error taxonomy

class ProviderError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(ProviderError):
    """429 RPM-style: retry the same model after a backoff."""


class QuotaError(ProviderError):
    """Quota/billing exhausted: skip retries, go to the next model now."""


class FatalError(ProviderError):
    """Bad key / unknown model / bad request: no retry, next model."""


class UnavailableError(ProviderError):
    """Network error / 5xx: retry, then next model."""


_QUOTA_MARKERS = ("quota", "billing", "credit", "insufficient",
                  "exceeded", "limit reached", "payment", "balance",
                  "превышен", "недостаточно")


def classify_error(status: int | None, text: str,
                   retry_after: float | None = None) -> ProviderError:
    low = (text or "").lower()
    quotaish = any(m in low for m in _QUOTA_MARKERS)
    if status == 429:
        if quotaish:
            return QuotaError(f"HTTP 429: {text[:300]}")
        return RateLimitError(f"HTTP 429: {text[:300]}", status=status,
                              retry_after=retry_after)
    if status == 402 or (status == 403 and quotaish):
        return QuotaError(f"HTTP {status}: {text[:300]}")
    if status in (400, 401, 403, 404, 422):
        return FatalError(f"HTTP {status}: {text[:300]}")
    return UnavailableError(f"HTTP {status}: {text[:300]}" if status
                            else f"network: {text[:300]}")


def _models_url(host: str) -> str:
    return f"{host}/v1/models"


_CONTEXT_KEYS = ("context_length", "context_window", "max_context_length_tokens",
                 "max_model_len", "top_provider_context_length")


def _is_embedding(item: dict) -> bool:
    """Embedding/reranker models are declared as such by the API (LM Studio
    `type: "embedding"`, vLLM/other `object` fields). Explicit declarations
    only — no name guessing."""
    for key in ("type", "object", "family", "category"):
        val = str(item.get(key) or "").lower()
        if "embed" in val or val == "reranker":
            return True
    return False


def _capabilities(item: dict) -> dict | None:
    """Explicit capability declarations ONLY — field-name mapping over real API
    data (OpenRouter-style `supported_parameters` / `architecture.input_modalities`).
    NO guessing from the model id: what the API didn't say stays "unknown"
    (key absent). Returns None when nothing was declared.
    Embedding models get a single `embedding` flag: tool/video/audio chatter
    capabilities are meaningless for them, so they are never emitted."""
    if _is_embedding(item):
        return {"embedding": True}
    caps: dict = {}
    sp = item.get("supported_parameters")
    if isinstance(sp, list):
        low = {str(x).lower() for x in sp}
        if any(k in low for k in ("tools", "tool_choice")):
            caps["tools"] = True
        if "reasoning" in low:
            caps["thinking"] = True
    mods = ((item.get("architecture") or {}).get("input_modalities")
            or item.get("input_modalities") or item.get("modalities"))
    if isinstance(mods, list):
        mlow = {str(x).lower() for x in mods}
        if "image" in mlow:
            caps["vision"] = True
        if "video" in mlow:
            caps["video"] = True
        if "audio" in mlow:
            caps["audio"] = True
    return caps or None


class Provider:
    def __init__(self, provider_id: str, host: str, api_key: str | None,
                 proxy_url: str | None = None):
        self.id = provider_id
        self.host = host
        self.api_key = api_key
        self.proxy_url = (proxy_url or "").strip() or None

    def _proxy_cfg(self) -> dict | None:
        """§17: a per-provider proxy wins over the model_api route."""
        if self.proxy_url:
            from .proxy import ProxyConfigError, parse_proxy_url
            try:
                return parse_proxy_url(self.proxy_url)
            except ProxyConfigError:
                return None        # broken entry: fall through to the route
        return _MODEL_PROXY

    def _request_proxy(self) -> str | None:
        cfg = self._proxy_cfg()
        if cfg is None:
            return None
        if cfg.get("scheme") in ("socks4", "socks5"):
            return None            # handled by the socks connector in _session()
        return cfg.get("url")

    def _session(self):
        """ClientSession honoring the proxy (per-provider or model_api §17)."""
        cfg = self._proxy_cfg()
        if cfg is not None and cfg.get("scheme") in ("socks4", "socks5"):
            try:
                from aiohttp_socks import ProxyConnector
            except ImportError as e:
                raise ProviderError(
                    "socks-прокси для API-запросов требует 'aiohttp-socks' "
                    "(pip install aiohttp-socks)") from e
            return aiohttp.ClientSession(
                connector=ProxyConnector.from_url(cfg["url"]))
        return aiohttp.ClientSession()

    @property
    def is_mock(self) -> bool:
        return self.host.startswith("mock://")

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # -- catalog -----------------------------------------------------------

    async def list_models(self) -> list[dict]:
        if self.is_mock:
            return self._mock_catalog()
        timeout = aiohttp.ClientTimeout(total=15)
        async with self._session() as s:
            async with s.get(_models_url(self.host), headers=self._headers(),
                             timeout=timeout, proxy=self._request_proxy()) as r:
                if r.status != 200:
                    raise classify_error(r.status, await r.text())
                body = await r.json(content_type=None)
        out = []
        for item in body.get("data", []):
            mid = item.get("id") or item.get("name")
            if not mid:
                continue
            ctx = next((item.get(k) for k in _CONTEXT_KEYS if item.get(k)), None)
            out.append({
                "model_id": mid, "title": item.get("name") or mid,
                "context_length": int(ctx) if ctx else None,
                "capabilities": _capabilities(item),
                "raw": json.dumps(item, ensure_ascii=False),
            })
        return out

    def _mock_catalog(self) -> list[dict]:
        # The mock IS the provider API: it declares everything explicitly —
        # no pricing (an agent wrapper doesn't need it), capabilities stated.
        if "flaky" in self.host:
            return [{"model_id": "flaky-1", "title": "Flaky Model (mock)",
                     "context_length": 128000,
                     "capabilities": {"tools": True, "thinking": True},
                     "raw": None}]
        return [
            {"model_id": "demo-reasoner", "title": "Demo Reasoner (mock)",
             "context_length": 128000,
             "capabilities": {"tools": True, "thinking": True}, "raw": None},
            {"model_id": "demo-fast", "title": "Demo Fast (mock)",
             "context_length": 32000, "capabilities": {"tools": True},
             "raw": None},
        ]

    # -- health -------------------------------------------------------------

    async def probe(self) -> dict:
        """Timed /v1/models request -> {'status', 'latency_ms', 'error'}."""
        if self.is_mock:
            return {"status": "online", "latency_ms": 1, "error": None}
        t0 = time.monotonic()
        try:
            await self.list_models()
            return {"status": "online",
                    "latency_ms": round((time.monotonic() - t0) * 1000),
                    "error": None}
        except ProviderError as e:
            return {"status": "error",
                    "latency_ms": round((time.monotonic() - t0) * 1000),
                    "error": str(e)}

    # -- context probe (§9 GUI): 1 token + absurd max_tokens ------------------

    _PROBE_MAX_TOKENS = 100_000_000
    _PROBE_PATTERNS = (
        # OpenAI/vLLM/LM Studio: "This model's maximum context length is 131072
        # tokens. However, you requested ..."
        re.compile(r"maximum context length is\D{0,40}(\d{4,12})", re.I),
        # vLLM/others: "This model's context length is X"
        re.compile(r"context length (?:is|of)\D{0,40}(\d{4,12})", re.I),
        # "max_tokens must be at most X" / "cannot exceed X"
        re.compile(r"max_tokens\D{0,60}?(?:at most|atmost|exceed[s]?)\D{0,20}(\d{4,12})", re.I),
        # llama.cpp: "n_ctx = 8192" / "--ctx-size 8192"
        re.compile(r"n_ctx\D{0,10}(\d{4,12})", re.I),
        re.compile(r"ctx[_ -]?size\D{0,10}(\d{4,12})", re.I),
        # generic: "... limited to 8192 tokens"
        re.compile(r"limit(?:ed)? to\D{0,20}(\d{4,12})\s*tokens", re.I),
    )

    async def probe_context(self, model_id: str) -> dict:
        """Token probe: send ~1 token with an absurd max_tokens and let the
        API itself name the real limit in the rejection message. The ONLY
        source of truth is what the API answers — no local tables.

        Returns {'context_length': int|None, 'reply': str} — `reply` is the
        raw provider answer for the UI (honesty: show what we parsed)."""
        if self.is_mock:
            return {"context_length": 4096,
                    "reply": "mock: контекст известен из каталога"}
        payload = {"model": model_id,
                   "messages": [{"role": "user", "content": "1"}],
                   "max_tokens": self._PROBE_MAX_TOKENS,
                   "stream": False}
        url = f"{self.host}/v1/chat/completions"
        try:
            async with self._session() as s:
                async with s.post(url, headers=self._headers(), json=payload,
                                  timeout=aiohttp.ClientTimeout(total=30),
                                  proxy=self._request_proxy()) as resp:
                    text = await resp.text()
        except aiohttp.ClientError as e:
            return {"context_length": None,
                    "reply": f"сеть: {str(e)[:200]}"}
        # some servers ACCEPT the request and answer normally: then the only
        # honest result is "unknown" — we do not trust a lucky 200
        found = None
        if resp.status != 200:
            for pat in self._PROBE_PATTERNS:
                m = pat.search(text)
                if m:
                    found = int(m.group(1))
                    break
        return {"context_length": found, "reply": text[:400]}

    # -- streaming chat -------------------------------------------------------

    async def chat_stream(self, model_id: str, messages: list[dict],
                          tools: list[dict] | None = None,
                          params: dict | None = None) -> AsyncIterator[dict]:
        """Yields chunks: {type: think|text|tool_call|usage|fallback|done, ...}"""
        if self.is_mock:
            async for chunk in self._mock_stream(model_id, messages):
                yield chunk
            return

        payload = {"model": model_id, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        if params:
            for key in ("temperature", "top_p", "max_tokens", "reasoning_effort"):
                if params.get(key) is not None:
                    payload[key] = params[key]

        url = f"{self.host}/v1/chat/completions"
        async with self._session() as s:
            try:
                async with s.post(url, headers=self._headers(), json=payload,
                                  timeout=aiohttp.ClientTimeout(total=300,
                                                                sock_read=300),
                                  proxy=self._request_proxy()) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        retry_after = None
                        ra = resp.headers.get("Retry-After")
                        if ra:
                            try:
                                retry_after = float(ra)
                            except ValueError:
                                pass
                        raise classify_error(resp.status, text, retry_after)
                    async for chunk in self._parse_sse(resp):
                        yield chunk
            except aiohttp.ClientError as e:
                raise classify_error(None, str(e)) from e
        yield {"type": "done"}

    async def _parse_sse(self, resp) -> AsyncIterator[dict]:
        tool_acc: dict[int, dict] = {}
        think_open = False

        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue

            if obj.get("usage"):
                yield {"type": "usage", "usage": obj["usage"]}

            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    yield {"type": "think", "delta": reasoning}

                content = delta.get("content")
                if content:
                    # inline thinking-tag normalization
                    if think_open:
                        if "</think>" in content:
                            pre, _, post = content.partition("</think>")
                            if pre:
                                yield {"type": "think", "delta": pre}
                            think_open = False
                            if post:
                                yield {"type": "text", "delta": post}
                        else:
                            yield {"type": "think", "delta": content}
                    elif "<think>" in content:
                        pre, _, post = content.partition("<think>")
                        if pre:
                            yield {"type": "text", "delta": pre}
                        think_open = True
                        if post:
                            yield {"type": "think", "delta": post}
                    else:
                        yield {"type": "text", "delta": content}

                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        acc["name"] = fn["name"]
                    if fn.get("arguments"):
                        acc["args"] += fn["arguments"]

        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            if acc.get("name"):
                try:
                    args = json.loads(acc["args"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": acc["args"]}
                yield {"type": "tool_call", "id": acc["id"] or f"call_{idx}",
                       "name": acc["name"], "args": args}

    # -- mock -------------------------------------------------------------------

    _flaky_attempts: dict[tuple[str, str], int] = {}

    async def _mock_stream(self, model_id: str, messages: list[dict]) -> AsyncIterator[dict]:
        import asyncio

        if "flaky" in self.host:
            key = (self.host, model_id)
            n = self._flaky_attempts.get(key, 0) + 1
            self._flaky_attempts[key] = n
            if n == 1:
                raise RateLimitError("429 mock: rate limit (RPM) — подождите",
                                     status=429, retry_after=0.05)
            if n == 2:
                raise QuotaError("402 mock: insufficient quota — исчерпана квота")
            # n >= 3 — ведёт себя как нормальная модель

        def _texts(role: str) -> list[str]:
            return [m.get("content") or "" for m in messages if m.get("role") == role]

        systems = _texts("system")
        last = messages[-1] if messages else None
        last_tool = last if last and last.get("role") == "tool" else None

        # -- Phase 3: assistant-prefill continue (§9) ------------------------------
        if last and last.get("role") == "assistant" and not last.get("tool_calls"):
            text = ("Продолжаю с места остановки: конвейер работает, "
                    "хвост ответа дописан. Кнопка «продолжить» вызвала "
                    "prefill-режим — история не разорвана.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "usage", "usage": {"prompt_tokens": 150,
                                              "completion_tokens": 40}}
            yield {"type": "done"}
            return

        # -- Phase 3: two-step summarizer / compactor service calls (§11) ----------
        if systems and any(k in s.lower() for s in systems for k in (
                "суммаризатор", "компрессор контекста",
                "промпт для суммаризации", "инструкцию для модели-суммаризатора")):
            text = ("Сжатая выжимка: в выводе — монотонная последовательность "
                    "чисел 1..9000 без ошибок; ключевой факт: объём ~44k символов; "
                    "аномалий нет.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.005)
            yield {"type": "usage", "usage": {"prompt_tokens": 900,
                                              "completion_tokens": 60}}
            yield {"type": "done"}
            return

        # -- Phase 3: subagent conversation (zero-context Task tool, §5) ------------
        is_subagent = bool(systems) and any("сабагент" in s.lower() for s in systems)
        if is_subagent and not last_tool:
            yield {"type": "tool_call", "id": "call_sub_read", "name": "read_file",
                   "args": {"path": "notes/hello.txt"}}
            yield {"type": "done"}
            return
        if is_subagent and last_tool is not None:
            text = ("Подзадача выполнена: файл notes/hello.txt прочитан, "
                    "в нём записка агента. Готово к передаче результата "
                    "главному агенту.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "usage", "usage": {"prompt_tokens": 80,
                                              "completion_tokens": 30}}
            yield {"type": "done"}
            return

        # -- Phase 3: plan mode (read-only research -> numbered plan, §5) ----------
        if systems and any("PLAN MODE" in s for s in systems):
            if last_tool is not None:
                plan = ("Цель: записать заметку по инструкции юзера.\n"
                        "Что уже есть: в рабочей папке найдена папка notes.\n"
                        "Шаги:\n"
                        "1. Прочитать существующие файлы в notes.\n"
                        "2. Записать notes/plan.txt с итоговым планом.\n"
                        "3. Проверить запись через чтение файла.\n"
                        "Проверка: файл существует и содержит текст плана.\n"
                        "Открытые вопросы: нужна ли нумерация версий?")
                # stream by LINE: newlines must survive into the persisted
                # text — the checklist parser needs numbered lines
                for line in plan.splitlines():
                    yield {"type": "text", "delta": line + "\n"}
                    await asyncio.sleep(0.005)
                yield {"type": "usage", "usage": {"prompt_tokens": 300,
                                                  "completion_tokens": 120}}
                yield {"type": "done"}
                return
            think = "План-режим: только чтение. Сначала посмотрю структуру папки."
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.005)
            yield {"type": "tool_call", "id": "call_plan_ls", "name": "list_dir",
                   "args": {"path": "."}}
            yield {"type": "done"}
            return

        # -- Phase 2 flows: drive write_file / bash-timeout e2e without a key
        if last_tool is not None:
            content = last_tool.get("content") or ""
            if '"timeout": true' in content or "не успел завершиться" in content:
                # §15: retry the SAME call, now with an explicit timeout arg
                prev_call = next((m for m in reversed(messages)
                                  if m.get("role") == "assistant" and m.get("tool_calls")), None)
                name, args = "bash", {"command": "sleep 2"}
                if prev_call:
                    tc = prev_call["tool_calls"][-1]["function"]
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"])
                    except json.JSONDecodeError:
                        args = {"command": "sleep 2"}
                args = dict(args)
                args["timeout"] = 5
                think = "Вызов не уложился в таймаут. Повторю тот же вызов с параметром timeout=5."
                for w in think.split():
                    yield {"type": "think", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "tool_call", "id": "call_mock_retry", "name": name,
                       "args": args}
                return
            # -- Phase 4 flows: web / browser / background results --------------
            if '"bg": true' in content and '"exit_code"' in content:
                # background_wait returned the finished task — wrap up
                text = ("Фоновая задача завершена: exit-код и хвост вывода "
                        "получены через background_wait. Юзер видел живой "
                        "вывод в терминале, мне в контекст попал только итог.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 180,
                                                  "completion_tokens": 45}}
                yield {"type": "done"}
                return
            if '"bg": true' in content and '"task_id"' in content:
                # background_run started — park on the wait tool
                import json as _json
                try:
                    tid = _json.loads(content).get("task_id")
                except (json.JSONDecodeError, TypeError):
                    tid = ""
                yield {"type": "tool_call", "id": "call_mock_bgwait",
                       "name": "background_wait",
                       "args": {"task_id": tid, "seconds": 30}}
                yield {"type": "done"}
                return
            if '"results"' in content and '"backend"' in content:
                # websearch results arrived — answer with what was found
                text = ("Поиск выполнен реальным бэкендом: вот выдержка из "
                        "результатов — заголовки, URL и сниппеты получил "
                        "тулом websearch, кеш глобальный.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 210,
                                                  "completion_tokens": 40}}
                yield {"type": "done"}
                return
            if '"title"' in content and '"url"' in content:
                # browser_navigate/browser_snapshot result — final answer
                text = ("Страница открыта во встроенном серверном браузере: "
                        "заголовок и текст получены. Скринкаст доступен юзеру "
                        "во вкладке «Браузер» — сессия живёт на сервере.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 230,
                                                  "completion_tokens": 42}}
                yield {"type": "done"}
                return
            if '"mode"' in content and '"content"' in content:
                # webfetch result — the page body arrived (sliced)
                text = ("Страница загружена через webfetch: сырец и Markdown "
                        "кешатся глобально, длинные страницы читаются кусками "
                        "через offset/length.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 240,
                                                  "completion_tokens": 40}}
                yield {"type": "done"}
                return
            if '"summarized"' in content:
                # §11: the two-step summary came back — wrap up the turn
                text = ("Готово: большой вывод тулзы был суммаризирован "
                        "двухшагово — в контексте только выжимка, полный "
                        "текст лежит в кеше сервера и виден юзеру.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 260,
                                                  "completion_tokens": 50}}
                yield {"type": "done"}
                return
            if '"task_id"' in content:
                # the subagent's result returned to the main agent (§5)
                text = ("Делегировал сабагенту: он прочитал notes/hello.txt "
                        "с нулевым контекстом и вернул выжимку. В мой контекст "
                        "попал только итог — прогресс юзер видел в карточке.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 220,
                                                  "completion_tokens": 70}}
                yield {"type": "done"}
                return
            if '"path"' in content or "write_file" in content \
                    or '"exit_code"' in content:
                # write_file / bash flow finished — final answer
                text = ("Готово: файл записан, чекпоинт снят. "
                        "Серверный агентный цикл с тулзами работает от и до.")
                for w in text.split():
                    yield {"type": "text", "delta": w + " "}
                    await asyncio.sleep(0.01)
                yield {"type": "usage", "usage": {"prompt_tokens": 200,
                                                  "completion_tokens": 80}}
                yield {"type": "done"}
                return
            think = ("Юзер спросил время. Я вызвал get_time, получил ответ. "
                     "Осталось аккуратно сформулировать финальный ответ.")
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.02)
            text = ("Готово: время я получил через инструмент get_time — "
                    "серверный агентный цикл работает от и до. "
                    "Это mock-провайдер: подключите реальный OpenAI-compat host в настройках, "
                    "и этот же пайплайн пойдёт к живой модели.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.02)
            yield {"type": "usage", "usage": {"prompt_tokens": 128,
                                              "completion_tokens": 96}}
            yield {"type": "done"}
            return

        user_raw = last.get("content", "") if last and last.get("role") == "user" else ""
        # multimodal messages arrive as OpenAI content PARTS — flatten them
        if isinstance(user_raw, list):
            user_text = " ".join(str(p.get("text", "")) for p in user_raw
                                 if isinstance(p, dict) and p.get("type") == "text")
        else:
            user_text = user_raw

        # -- Phase 4: websearch / browser / background e2e without a key -------
        if "поищи" in user_text or "найди в интернете" in user_text:
            m = re.search(r"(?:поищи|найди в интернете)[:\s]+(.+)", user_text)
            query = (m.group(1).strip() if m else "agent framework")[:120]
            think = "Поищу в интернете тулом websearch — результаты реальные, " \
                    "с глобальным кешом сервера."
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "tool_call", "id": "call_mock_search", "name": "websearch",
                   "args": {"query": query}}
            yield {"type": "done"}
            return
        if "зайди на" in user_text or "браузер" in user_text.lower():
            m = re.search(r"https?://\S+", user_text)
            url = m.group(0) if m else "https://example.com"
            think = "Открою страницу во встроенном серверном браузере " \
                    "и сниму текстовый снапшот."
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "tool_call", "id": "call_mock_nav", "name": "browser_navigate",
                   "args": {"url": url}}
            yield {"type": "done"}
            return
        if "фонов" in user_text.lower():
            m = re.search(r"(?:команду|запусти)[:\s]+`?([^`]+)`?", user_text)
            command = (m.group(1).strip() if m else
                       "seq 1 100000 | tail -5")[:200]
            think = "Долгая команда — уведу в фон тулом background_run, " \
                    "потом подожду через background_wait."
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "tool_call", "id": "call_mock_bg", "name": "background_run",
                   "args": {"command": command, "name": "demo"}}
            yield {"type": "done"}
            return

        if "поспи" in user_text or "sleep" in user_text.lower():
            # bash flow: first call has no timeout -> server-side default applies
            yield {"type": "tool_call", "id": "call_mock_sleep", "name": "bash",
                   "args": {"command": "sleep 2"}}
            yield {"type": "done"}
            return
        if "запиши" in user_text or "write" in user_text.lower():
            # path comes from the user text; content is versioned so the same
            # path written twice produces a real modification diff
            import re as _re
            m = _re.search(r"(?:запиши файл|write file)\s+(\S+)", user_text)
            path = m.group(1) if m else "notes/hello.txt"
            writes = 0
            for msg in messages:
                if msg.get("role") != "assistant" or not msg.get("tool_calls"):
                    continue
                for tc in msg["tool_calls"]:
                    if tc.get("function", {}).get("name") == "write_file":
                        writes += 1
            think = "Юзер просит записать файл. Вызову write_file — обвязка спросит разрешение."
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "tool_call", "id": f"call_mock_write_{writes + 1}",
                   "name": "write_file",
                   "args": {"path": path,
                            "content": f"создано агентом v{writes + 1}\n"}}
            yield {"type": "done"}
            return
        if "делегируй" in user_text or "субагент" in user_text.lower() \
                or "сабагент" in user_text.lower():
            # §5: spawn a zero-context subagent through the Task tool
            yield {"type": "tool_call", "id": "call_mock_task", "name": "task",
                   "args": {"prompt": "подзадача: прочитай notes/hello.txt "
                                      "и верни первые строки",
                            "system_prompt": "Ты — сабагент-исследователь.",
                            "allowlist": ["read_file", "list_dir"]}}
            yield {"type": "done"}
            return
        if "дамп" in user_text or "dump" in user_text.lower():
            # §11: a huge bash output triggers the two-step summarization
            yield {"type": "tool_call", "id": "call_mock_dump", "name": "bash",
                   "args": {"command": "seq 1 9000"}}
            yield {"type": "done"}
            return

        user_msgs = [m.get("content") for m in messages if m.get("role") == "user"]
        if ("выполняй" in user_text or "приступай" in user_text) and any(
                isinstance(t, str) and "Одобренный юзером план" in t
                for t in user_msgs):
            # §5: an approved plan artifact is in the context and the user
            # explicitly told the agent to execute it
            text = ("Приступаю к одобренному плану: шаг 1 выполнен — "
                    "структура изучена. Дальше по чеклисту; статусы "
                    "отмечает юзер в карточке плана.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "usage", "usage": {"prompt_tokens": 340,
                                              "completion_tokens": 60}}
            yield {"type": "done"}
            return

        if model_id == "demo-fast" and not last_tool:
            text = ("Короткий и быстрый ответ от demo-fast: пайплайн жив, "
                    "тулзы в этом ходу не нужны.")
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.01)
            yield {"type": "usage", "usage": {"prompt_tokens": 64, "completion_tokens": 32}}
        else:
            think = ("Юзер прислал сообщение. По правилам системы мне нужно ответить. "
                     "Сначала проверю текущее время инструментом get_time — "
                     "заодно покажу юзеру, как работают тулкаллы в стриме.")
            for w in think.split():
                yield {"type": "think", "delta": w + " "}
                await asyncio.sleep(0.02)
            text = "Одну секунду, проверю время."
            for w in text.split():
                yield {"type": "text", "delta": w + " "}
                await asyncio.sleep(0.02)
            yield {"type": "tool_call", "id": "call_mock_1", "name": "get_time",
                   "args": {"tz": "local"}}
        yield {"type": "done"}


class ToolSpec:
    """OpenAI function-tool schema helper."""

    def __init__(self, name: str, description: str, params: dict,
                 accepts_timeout: bool = False):
        self.name = name
        self.description = description
        self.params = params
        # §15: tool supports the explicit `timeout=N` retry argument
        self.accepts_timeout = accepts_timeout

    def to_openai(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.params}}
