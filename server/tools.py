"""Tool registry.

Tools carry:
  - a ToolSpec (OpenAI function schema) — what the MODEL sees;
  - a permission class — 'read' | 'write' | 'exec' (§7), used by the
    permission engine;
  - a `deferred` flag — deferred tools are NOT sent to the model until it
    loads them through `tool_search` / `tool_load` (§6 ToolSearch /
    defer_loading, the anti-saturation pattern). MCP tools register as
    deferred by default (§4): their schemas never bloat the prompt.

Execution wraps handlers with a timeout (§15): every tool has a default
(seconds), any tool schema may expose an explicit `timeout` argument the
model can raise — capped by `tool_timeout_max`. On expiry the model gets
the timeout-protocol payload (retry the SAME call with timeout=N).

Registry.execute returns {"timeout": True, ...} markers the agent loop
uses for the transactional clean-history masking (§15).
"""

from __future__ import annotations

import asyncio
import json

from . import events as ev
from .providers import ToolSpec

# tools that accept an explicit `timeout` argument (§15 protocol) declare it
# in their schema; the registry pops it before calling the handler
TOOL_TIMEOUT_ARG = "timeout"


class ToolEntry:
    __slots__ = ("spec", "handler", "perm_class", "deferred", "default_timeout",
                 "needs_chat")

    def __init__(self, spec: ToolSpec, handler, perm_class: str = "read",
                 deferred: bool = False, default_timeout: float = 60.0,
                 needs_chat: bool = False):
        self.spec = spec
        self.handler = handler
        self.perm_class = perm_class
        self.deferred = deferred
        self.default_timeout = default_timeout
        self.needs_chat = needs_chat   # meta-tools: receive chat_id kwarg


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._exposure: dict[str, set[str]] = {}   # chat_id -> loaded names

    # -- registration ------------------------------------------------------------

    def register(self, spec: ToolSpec, handler, perm_class: str = "read",
                 deferred: bool = False, default_timeout: float = 60.0,
                 needs_chat: bool = False) -> None:
        self._tools[spec.name] = ToolEntry(spec, handler, perm_class, deferred,
                                           default_timeout, needs_chat)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        for loaded in self._exposure.values():
            loaded.discard(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def entry(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def perm_class(self, name: str) -> str:
        return self._tools[name].perm_class if name in self._tools else "exec"

    # -- model-facing schemas -------------------------------------------------------

    def exposed_names(self, chat_id: str) -> set[str]:
        """Core (non-deferred) tools + everything this chat has loaded."""
        base = {n for n, t in self._tools.items() if not t.deferred}
        return base | self._exposure.get(chat_id, set())

    def openai_tools(self, chat_id: str | None = None) -> list[dict]:
        if chat_id is None:
            items = [t for t in self._tools.values() if not t.deferred]
        else:
            exposed = self.exposed_names(chat_id)
            items = [t for n, t in self._tools.items() if n in exposed]
        return [t.spec.to_openai() for t in items]

    # -- ToolSearch / ToolLoad (§6) ----------------------------------------------------

    def search(self, query: str, chat_id: str) -> list[dict]:
        """Fuzzy search over name+description; loaded flags per chat."""
        q = (query or "").strip().lower()
        tokens = [t for t in q.split() if len(t) >= 2]
        exposed = self.exposed_names(chat_id)
        scored: list[tuple[int, dict]] = []
        for name, t in sorted(self._tools.items()):
            hay = f"{name} {t.spec.description}".lower()
            if not tokens:
                score = 1
            else:
                score = sum(1 for tok in tokens if tok in hay)
                if q and q in name.lower():
                    score += 2
            if score <= 0:
                continue
            scored.append((score, {
                "name": name,
                "description": t.spec.description,
                "loaded": name in exposed,
            }))
        scored.sort(key=lambda p: (-p[0], p[1]["name"]))
        return [item for _s, item in scored[:40]]

    def load(self, chat_id: str, names: list[str]) -> dict:
        loaded, failed = [], []
        for n in names or []:
            if n not in self._tools:
                failed.append(n)
                continue
            self._exposure.setdefault(chat_id, set()).add(n)
            loaded.append(n)
        return {"loaded": loaded, "failed": failed}

    def reset_exposure(self, chat_id: str) -> None:
        self._exposure.pop(chat_id, None)

    # -- execution ------------------------------------------------------------------

    async def execute(self, name: str, args: dict,
                      timeout_default: float = 30.0,
                      timeout_max: float = 600.0,
                      chat_id: str | None = None) -> dict:
        """Run a tool with the §15 timeout protocol.

        The handler raises/fails -> {"error": ...} (model-visible).
        Timeout expiry -> the timeout-protocol payload with timeout=True;
        the agent loop masks the failed call when the model retries with
        an explicit `timeout` argument.
        """
        entry = self._tools.get(name)
        if entry is None:
            return {"error": f"unknown tool: {name}"}
        call_args = dict(args or {})
        if entry.needs_chat:
            call_args["chat_id"] = chat_id if chat_id else ""

        # explicit timeout=N from the model (§15) — honored up to the cap
        explicit = None
        if TOOL_TIMEOUT_ARG in call_args and entry.spec.accepts_timeout:
            try:
                explicit = max(1.0, float(call_args.pop(TOOL_TIMEOUT_ARG)))
            except (TypeError, ValueError):
                call_args.pop(TOOL_TIMEOUT_ARG, None)
        elif TOOL_TIMEOUT_ARG in call_args:
            call_args.pop(TOOL_TIMEOUT_ARG, None)   # tool doesn't support it

        seconds = explicit if explicit is not None else min(
            entry.default_timeout, timeout_default)
        seconds = min(seconds, timeout_max)

        try:
            result = entry.handler(**call_args)
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=seconds)
        except asyncio.TimeoutError:
            return ev.timeout_error_payload(name, seconds, timeout_max)
        except Exception as e:  # noqa: BLE001 — tool errors are model-visible
            return {"error": f"{type(e).__name__}: {e}"}

        if not isinstance(result, dict):
            result = {"output": result}
        return result

    def describe(self) -> list[dict]:
        return [{"name": n, "description": t.spec.description,
                 "deferred": t.deferred, "perm_class": t.perm_class}
                for n, t in sorted(self._tools.items())]
