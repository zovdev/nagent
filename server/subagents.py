"""Subagents (§5 roadmap): the `task` tool spawns a ZERO-CONTEXT agent.

The main agent supplies: `prompt`, optional `system_prompt`, and an optional
`allowlist` of tool names. The subagent starts with a blank slate — none of
the main chat's history leaks in, and only the FINAL RESULT returns to the
main context. Progress is visible in the UI (subagent_start / progress /
end events) but never enters the model context.

Tool calls inside the subagent go through the same permission engine —
ask-cards surface in the main chat and suspend the subagent until resolved.

Default allowlist: every read-class core tool (the subagent can investigate
but not mutate unless the main agent explicitly grants write/exec tools).
`task` itself is never in the default allowlist — no runaway nesting.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from . import events as ev
from .providers import ProviderError, ToolSpec
from .tools import ToolRegistry

DEFAULT_MAX_ITERATIONS = 8


def summarize_call(name: str, args: dict) -> str:
    if name == "bash" and isinstance(args.get("command"), str):
        return f"bash: {args['command'][:160]}"
    if isinstance(args.get("path"), str):
        return f"{name}: {args['path'][:160]}"
    if isinstance(args.get("pattern"), str):
        return f"{name}: {args['pattern'][:160]}"
    return f"{name}: {json.dumps(args, ensure_ascii=False)[:160]}"


class SubagentRunner:
    """Zero-context agent loop. Events -> UI only; result -> tool_result."""

    def __init__(self, store, router, registry: ToolRegistry,
                 model_key: str | None, permissions=None,
                 timeout_default: float = 30.0, timeout_max: float = 600.0):
        self.store = store
        self.router = router
        self.registry = registry
        self.model_key = model_key
        self.permissions = permissions
        self.timeout_default = timeout_default
        self.timeout_max = timeout_max
        self.result: dict = {"error": "сабагент не выполнился"}

    def _allowlist_tools(self, allowlist: list[str] | None) -> list[dict]:
        if allowlist:
            tools = []
            for name in allowlist:
                entry = self.registry.entry(str(name))
                if entry is not None:
                    tools.append(entry.spec.to_openai())
            return tools
        tools = []
        for name in self.registry.names():
            entry = self.registry.entry(name)
            if (entry is not None and not entry.deferred
                    and entry.perm_class == "read" and name != "task"):
                tools.append(entry.spec.to_openai())
        return tools

    async def run(self, chat_id: str, prompt: str,
                  system_prompt: str | None = None,
                  allowlist: list[str] | None = None,
                  max_iterations: int = DEFAULT_MAX_ITERATIONS
                  ) -> AsyncIterator[dict]:
        task_id = ev.new_id()[:8]
        tools = self._allowlist_tools(allowlist)
        await self.store.append(chat_id, ev.SUBAGENT_START, {
            "task_id": task_id, "prompt": prompt[:400],
            "system_prompt": (system_prompt or "")[:200],
            "allowlist": allowlist or "read-only default",
            "tools": [t["function"]["name"] for t in tools]})

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        result_text = ""
        error: str | None = None
        iterations = 0
        try:
            for iteration in range(max_iterations):
                iterations = iteration + 1
                text_parts: list[str] = []
                tool_calls: list[dict] = []
                async for chunk in self.router.chat_stream(
                        self.model_key, messages, tools or None, None):
                    ctype = chunk.get("type")
                    if ctype == "text":
                        text_parts.append(chunk.get("delta") or "")
                    elif ctype == "tool_call":
                        tool_calls.append(chunk)

                assistant_msg: dict = {"role": "assistant",
                                       "content": "".join(text_parts)}
                if tool_calls:
                    assistant_msg["content"] = assistant_msg["content"] or ""
                    assistant_msg["tool_calls"] = [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": _dumps(tc.get("args"))}}
                        for tc in tool_calls]
                messages.append(assistant_msg)

                if not tool_calls:
                    result_text = "".join(text_parts).strip()
                    break

                await self.store.append(chat_id, ev.SUBAGENT_PROGRESS, {
                    "task_id": task_id, "iteration": iterations,
                    "tools": [tc["name"] for tc in tool_calls]})

                for tc in tool_calls:
                    result = await self._execute(chat_id, tc["name"],
                                                 tc.get("args") or {})
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "content": _dumps(result)})
            else:
                error = (f"сабагент не уложился в {max_iterations} итераций; "
                         f"последний текст: {result_text[:400] or '—'}")
        except asyncio.CancelledError as e:
            error = f"сабагент прерван: {e}"
            raise
        except ProviderError as e:
            error = f"сабагент упал по ошибке провайдера: {e}"

        if error is None:
            self.result = {"task_id": task_id, "ok": True,
                           "iterations": iterations, "result": result_text}
        else:
            self.result = {"task_id": task_id, "ok": False,
                           "iterations": iterations, "error": error}

        yield await self.store.append(chat_id, ev.SUBAGENT_END, {
            "task_id": task_id, "ok": error is None,
            "iterations": iterations,
            "result": (result_text or error or "")[:1000]})

    async def _execute(self, chat_id: str, name: str, args: dict) -> dict:
        entry = self.registry.entry(name)
        if entry is None:
            return {"error": f"unknown tool: {name}"}
        if self.permissions is not None:
            from .permissions import derive_pattern
            _pattern, subject, kind = derive_pattern(name, args)
            decision = await self.permissions.check(name, args, entry.perm_class,
                                                    subject, kind)
            if decision["decision"] == "deny":
                return {"error": f"отклонено правами: {decision['reason']}"}
            if decision["decision"] == "ask":
                approval = await self.permissions.begin_approval(
                    chat_id, name, args, summarize_call(name, args))
                await self.store.append(chat_id, ev.PERMISSION_REQUEST, {
                    "approval_id": approval["id"], "tool": name,
                    "summary": f"[сабагент] {summarize_call(name, args)}",
                    "args": args})
                row = await self.permissions.wait(approval["id"])
                verdict = (row or {}).get("decision")
                await self.store.append(chat_id, ev.PERMISSION_RESULT, {
                    "approval_id": approval["id"], "tool": name,
                    "decision": verdict or "expired"})
                if verdict not in ("once", "always"):
                    return {"error": "разрешение на тулзу сабагента не получено"}
        return await self.registry.execute(
            name, args, timeout_default=self.timeout_default,
            timeout_max=self.timeout_max, chat_id=chat_id)


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def register_task_tool(registry: ToolRegistry, store, router_factory,
                       permissions=None, timeout_default: float = 30.0,
                       timeout_max: float = 600.0) -> None:
    """Wire the `task` tool: zero-context subagent (§5).
    `router_factory()` -> awaitable ModelRouter; the subagent runs on the
    chat's own model (fallback cascade applies inside the router)."""

    async def _task(prompt: str, system_prompt: str = "",
                    allowlist: list | None = None, chat_id: str = "",
                    timeout: float | None = None) -> dict:
        router = await router_factory()
        chat = await store.get_chat(chat_id) if chat_id else None
        model_key = (chat or {}).get("model_key")
        runner = SubagentRunner(store, router, registry, model_key,
                                permissions, timeout_default, timeout_max)
        async for _ in runner.run(chat_id, prompt, system_prompt or None,
                                  [str(n) for n in (allowlist or [])] or None):
            pass  # lifecycle events already persisted + published by the store
        return runner.result

    registry.register(ToolSpec(
        "task", "Запустить суб-агента с НУЛЁВЫМ контекстом (не видит историю "
                "чата). Дай ему prompt, при желании system_prompt и allowlist "
                "тулзов (по умолчанию read-only). Вернётся только итог.",
        {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "полная самодостаточная задача сабагенту"},
            "system_prompt": {"type": "string",
                              "description": "роль/правила сабагента (опционально)"},
            "allowlist": {"type": "array", "items": {"type": "string"},
                          "description": "тулзы сабагента (опционально; "
                                         "default read-only)"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 1800}},
         "required": ["prompt"], "additionalProperties": False},
        accepts_timeout=True),
        _task, perm_class="read", default_timeout=300.0, needs_chat=True)
