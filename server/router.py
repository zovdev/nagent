"""Model router — the fallback cascade (§3 roadmap).

An ordered chain: the chat's own model first, then the user-configured
`fallback_chain`. Per candidate the retry policy applies:

- RateLimitError (429 RPM)  -> retry same model after backoff (Retry-After honoured);
- UnavailableError (5xx/net) -> retry same model, then move on;
- QuotaError (billing)       -> NO retry, next model immediately;
- FatalError (bad key/model) -> NO retry, next model immediately.

Every switch emits a `fallback` chunk so the loop can persist a user-visible
notice; the model that actually answered is exposed as `last_model_key`.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from .providers import (Provider, ProviderError, QuotaError, RateLimitError,
                        UnavailableError)

_RETRYABLE = (RateLimitError, UnavailableError)
MAX_BACKOFF = 30.0


def parse_backoff(spec: str) -> tuple[str, float]:
    """'fixed:2' | 'exp:1.5' -> (mode, base_seconds)."""
    mode, _, base = (spec or "exp:1.5").partition(":")
    try:
        base = float(base or 1.0)
    except ValueError:
        base = 1.0
    mode = mode if mode in ("fixed", "exp") else "exp"
    return mode, max(base, 0.0)


def backoff_seconds(mode: str, base: float, attempt: int,
                    retry_after: float | None = None) -> float:
    if retry_after is not None:
        return min(max(retry_after, 0.0), MAX_BACKOFF)
    if mode == "fixed":
        return min(base, MAX_BACKOFF)
    return min(base * (2 ** (attempt - 1)), MAX_BACKOFF)


class ModelRouter:
    def __init__(self, settings_store):
        self.settings_store = settings_store
        self.max_attempts = 2
        self.backoff_mode, self.backoff_base = "exp", 1.5
        self.chain: list[str] = []
        self.last_model_key: str | None = None
        self.current_key: str | None = None    # sticky within a turn (§3)

    @classmethod
    async def create(cls, settings_store) -> "ModelRouter":
        r = cls(settings_store)
        s = await settings_store.get_settings()
        try:
            r.max_attempts = max(1, int(s.get("retry_max_attempts", "2")))
        except (TypeError, ValueError):
            r.max_attempts = 2
        r.backoff_mode, r.backoff_base = parse_backoff(s.get("retry_backoff", "exp:1.5"))
        try:
            chain = json.loads(s.get("fallback_chain", "[]") or "[]")
            r.chain = [k for k in chain if isinstance(k, str) and "::" in k]
        except (json.JSONDecodeError, TypeError):
            r.chain = []
        return r

    # -- resolution -----------------------------------------------------------

    async def provider_for(self, model_key: str) -> Provider | None:
        if not model_key or "::" not in model_key:
            return None
        provider_id, _ = model_key.split("::", 1)
        p = await self.settings_store.get_provider(provider_id)
        if not p:
            return None
        return Provider(p["id"], p["host"], p["api_key"], p.get("proxy_url"))

    async def model_info(self, model_key: str) -> dict | None:
        """Effective model row (overrides merged) — context window for the meter."""
        if not model_key:
            return None
        return await self.settings_store.get_model(model_key)

    # -- the cascade ------------------------------------------------------------

    async def chat_stream(self, primary_key: str, messages: list[dict],
                          tools: list[dict] | None = None,
                          params: dict | None = None) -> AsyncIterator[dict]:
        chain = [primary_key] + [k for k in self.chain if k != primary_key]
        if not chain:
            raise ProviderError("модель не выбрана")

        # sticky: once a fallback answered in this turn, later requests of the
        # same turn continue from it (no flip-flopping between iterations)
        start = chain.index(self.current_key) if self.current_key in chain else 0

        last_err: Exception | None = None
        prev_failed: str | None = None
        for i in range(start, len(chain)):
            key = chain[i]
            if prev_failed is not None:
                yield {"type": "fallback", "from": prev_failed, "to": key,
                       "reason": _short_reason(last_err)}
                prev_failed = None

            provider = await self.provider_for(key)
            if provider is None:
                last_err = ProviderError(f"провайдер/модель не найдены: {key}")
                prev_failed = key
                continue

            _, model_id = key.split("::", 1)
            attempt = 0
            while True:
                attempt += 1
                try:
                    first_chunk = True
                    async for chunk in provider.chat_stream(model_id, messages,
                                                            tools, params):
                        if first_chunk:   # consumer may break early on 'done'
                            self.last_model_key = key
                            self.current_key = key
                            first_chunk = False
                        yield chunk
                    self.last_model_key = key
                    self.current_key = key
                    return
                except ProviderError as e:
                    last_err = e
                    if isinstance(e, _RETRYABLE) and attempt < self.max_attempts:
                        await asyncio.sleep(
                            backoff_seconds(self.backoff_mode, self.backoff_base,
                                            attempt, e.retry_after))
                        continue
                    prev_failed = key
                    break  # give up on this candidate -> next in chain

        if last_err is not None:
            raise ProviderError(f"каскад исчерпан ({len(chain) - start} мод.): {last_err}")


def _short_reason(err: Exception | None) -> str:
    if err is None:
        return "неизвестная ошибка"
    text = str(err).replace("\n", " ")
    return text[:160]
