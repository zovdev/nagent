"""Agent loop — a generator (queryLoop-style) driving one turn.

Events are appended to HistoryStore *and* yielded so the SSE layer can stream
them live. Buffering strategy: think/text blocks stream deltas to the UI as
transient "chunk" events but are persisted as completed blocks — RAW editing
works on blocks. On interrupt, unfinished blocks are flushed with
`interrupted: true`.

Phase 1: the loop talks to the ModelRouter (fallback cascade); turn meta
records the model that actually answered and the usage object EXACTLY as the
API returned it (cache fields included). No local token estimation.

Phase 2:
  - every tool call passes the permission engine first (deny-first rules;
    ask-mode suspends the loop until the user answers — the approval shows
    up in the chat as a permission_request event + card);
  - tool timeouts follow §15: on expiry the model sees the retry-with-
    timeout instruction; when it retries the SAME call with an explicit
    `timeout` argument, the failed call + intermediate think blocks are
    masked out of the model context (MASK event; the user still sees them);
  - oversized tool outputs are truncated for the context but the full text
    is cached server-side (`.agent/toolcache/<call_id>`);
  - after a turn that changed files, a shadow-git checkpoint is committed
    and bound to the turn's UUID (§14) + a checkpoint event lands in chat.

Phase 3:
  - system prompt hierarchy (§8): server default + chat prompt + AGENTS.md
    from the workspace, injected as separate stable system messages;
  - attachments (§10): user messages render as multimodal content parts
    driven by the EXPLICIT capability matrix (no name guessing);
  - plan mode (§5): read-only turn; the final text becomes a plan_submitted
    card; only the approved plan artifact ever enters the context;
  - loop detectors (§5): repeat detector stops the turn (user gets a
    «продолжить» card), tool-failure streak warns + hints the model;
  - two-step summarization of oversized outputs (§11): intermediate
    requests are user-visible `summarize` events, never model context;
  - auto-compaction (§11) after a turn that crossed the threshold, based on
    REAL usage only;
  - continue (§9): a turn without a new user message (assistant-prefill
    where the provider supports it, or a resend directive).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import AsyncIterator

from . import events as ev
from .checkpoints import CheckpointError, Checkpoints
from .detectors import RepeatDetector, ToolFailureDetector
from .history import HistoryStore
from .jail import MAX_OUTPUT_CHARS
from .permissions import PermissionEngine, derive_pattern
from .providers import ProviderError
from .router import ModelRouter
from .summarizer import TWO_STEP_THRESHOLD
from .tools import ToolRegistry

MAX_LOOP_ITERATIONS = 24          # default; configurable (§5, settings)

# tools whose success marks the turn as file-changing (checkpoint candidates)
_MUTATING_TOOLS = {"write_file", "edit_file", "bash", "background_run"}


def build_messages(chat: dict, history: list[dict], *,
                   system_messages: list[str] | None = None,
                   caps: dict | None = None,
                   attach_store=None) -> list[dict]:
    """Effective history -> OpenAI messages.

    - think blocks are dropped unless the chat enables 'send full context
      with think blocks'; tool calls are re-serialized as assistant
      tool_calls + tool role messages;
    - `system_messages` (§8 hierarchy) become separate leading system
      messages so the prefix stays prompt-cache friendly;
    - attachments (§10) render as multimodal content parts when the model's
      EXPLICIT capabilities allow; otherwise placeholders;
    - compact/plan_approved/agent_notice render as user-role messages
      (see events.model_role).
    """
    send_think = bool(chat.get("send_think_blocks", False))
    messages: list[dict] = []
    for sm in (system_messages or []):
        if sm and str(sm).strip():
            messages.append({"role": "system", "content": str(sm)})

    pending_tool_calls: list[dict] = []

    def _user_content(d: dict):
        atts = d.get("attachments")
        if atts and attach_store is not None:
            from .attachments import attachment_parts
            return attachment_parts(d.get("text", ""), atts, attach_store, caps)
        return d.get("text", "")

    for e in history:
        t = e["type"]
        d = e["data"]
        if t == ev.USER_MESSAGE:
            messages.append({"role": "user", "content": _user_content(d)})
        elif t == ev.ASSISTANT_TEXT:
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": d.get("text", ""),
                                 "tool_calls": pending_tool_calls})
                pending_tool_calls = []
            else:
                messages.append({"role": "assistant", "content": d.get("text", "")})
        elif t == ev.ASSISTANT_THINK:
            if send_think:
                messages.append({"role": "assistant",
                                 "content": f"[thinking] {d.get('text', '')}"})
        elif t == ev.TOOL_CALL:
            pending_tool_calls.append({
                "id": d.get("call_id") or d.get("id", "call"),
                "type": "function",
                "function": {"name": d.get("name"),
                             "arguments": json.dumps(d.get("args", {}),
                                                     ensure_ascii=False)},
            })
        elif t == ev.TOOL_RESULT:
            if pending_tool_calls:
                messages.append({"role": "assistant", "content": "",
                                 "tool_calls": pending_tool_calls})
                pending_tool_calls = []
            messages.append({"role": "tool", "tool_call_id": d.get("call_id"),
                             "content": json.dumps(d.get("result", {}), ensure_ascii=False)})
        elif t == ev.COMPACT:
            messages.append({"role": "user", "content":
                             f"[компакция] Предыдущая часть сессии суммаризована:\n"
                             f"{d.get('summary', '')}"})
        elif t == ev.PLAN_APPROVED:
            from .planmode import render_plan_for_model
            messages.append({"role": "user",
                             "content": render_plan_for_model(d)})
        elif t == ev.AGENT_NOTICE:
            messages.append({"role": "user", "content": d.get("text", "")})

    if pending_tool_calls:  # dangling tool calls (e.g. interrupt after call)
        messages.append({"role": "assistant", "content": "", "tool_calls": pending_tool_calls})
    return messages


def _gen_params(chat: dict) -> dict | None:
    p = chat.get("params") or {}
    params = {}
    for key in ("temperature", "top_p", "max_tokens"):
        v = p.get(key)
        if v is None or v == "":
            continue
        try:
            params[key] = float(v) if key != "max_tokens" else int(v)
        except (TypeError, ValueError):
            continue
    if p.get("reasoning_effort"):
        params["reasoning_effort"] = p["reasoning_effort"]
    return params or None


class TurnRunner:
    def __init__(self, store: HistoryStore, router: ModelRouter,
                 model_key: str | None, registry: ToolRegistry,
                 permissions: PermissionEngine | None = None,
                 checkpoints: Checkpoints | None = None,
                 timeout_default: float = 30.0, timeout_max: float = 600.0,
                 # Phase 3 wiring (all optional — Phase 0/2 callers keep working)
                 settings_store=None,
                 instructions=None,          # InstructionsResolver
                 attach_store=None,          # AttachmentStore
                 compactor=None,             # Compactor
                 summarizer=None,            # TwoStepSummarizer
                 loop_limit: int = MAX_LOOP_ITERATIONS,
                 # Phase 5 wiring (hooks §7, audit §2, team concurrency §2)
                 hooks=None,
                 audit=None,
                 tool_gate=None,
                 user: str | None = None):
        self.store = store
        self.router = router
        self.model_key = model_key
        self.registry = registry
        self.permissions = permissions
        self.checkpoints = checkpoints
        self.timeout_default = timeout_default
        self.timeout_max = timeout_max
        self.settings_store = settings_store
        self.instructions = instructions
        self.attach_store = attach_store
        self.compactor = compactor
        self.summarizer = summarizer
        self.hooks = hooks
        self.audit = audit
        self.tool_gate = tool_gate
        self.user = user
        self._last_tool_ms = 0.0              # for the §2 audit records
        self.loop_limit = max(1, int(loop_limit or MAX_LOOP_ITERATIONS))
        self._workspace_dirty = False
        self._turn_span: list[str] = []      # content-event ids of this turn
        self.repeat_detector = RepeatDetector()
        self.failure_detector = ToolFailureDetector()

    # kept for API compatibility with Phase 0 callers
    def build_messages(self, chat: dict, history: list[dict]) -> list[dict]:
        return build_messages(chat, history)

    # -- settings helpers ---------------------------------------------------

    async def _settings(self) -> dict:
        if self.settings_store is None:
            return {}
        try:
            return await self.settings_store.get_settings()
        except Exception:                    # noqa: BLE001 — settings optional
            return {}

    async def _system_messages(self, chat: dict) -> list[str]:
        """§8 hierarchy: server default -> chat -> AGENTS.md (workspace)."""
        s = await self._settings()
        out: list[str] = []
        default_sp = (s.get("default_system_prompt") or "").strip()
        if default_sp:
            out.append(default_sp)
        if self.instructions is not None:
            out.extend(self.instructions.system_messages(chat))
        else:
            chat_sp = (chat.get("system_prompt") or "").strip()
            if chat_sp:
                out.append(chat_sp)
        return out

    async def _failure_streak(self) -> int:
        s = await self._settings()
        try:
            return max(2, int(s.get("tool_failure_streak", "3")))
        except (TypeError, ValueError):
            return 3

    # -- the loop --------------------------------------------------------------

    async def run(self, chat: dict, user_text: str,
                  attachments: list[dict] | None = None,
                  plan_mode: bool = False) -> AsyncIterator[dict]:
        """Run a full turn. Yields every persisted event for live streaming."""
        chat_id = chat["id"]
        params = _gen_params(chat)
        self._workspace_dirty = False
        self._turn_span = []
        self.repeat_detector.reset()
        self.failure_detector.reset()

        # §7 UserPromptSubmit hooks: may block the prompt outright or add
        # extra context the model receives (user-visible as a notice).
        if self.hooks is not None:
            verdict = await self.hooks.user_prompt_submit(
                chat_id, user_text, user=self.user)
            if verdict is not None and verdict.blocked:
                err = await self.store.append(chat_id, ev.ERROR, {
                    "message": f"запрос отклонён хуком: {verdict.reason or 'без причины'}",
                    "stage": "hook"})
                yield err
                return
            if verdict is not None and verdict.context:
                notice = await self.store.append(chat_id, ev.AGENT_NOTICE, {
                    "text": f"[хук] дополнительный контекст:\n{verdict.context}"})
                yield notice

        user_data: dict = {"text": user_text}
        if attachments:
            user_data["attachments"] = attachments
        ev_user = await self.store.append(chat_id, ev.USER_MESSAGE, user_data)
        yield ev_user

        try:
            async for event in self._loop(chat, params, plan_mode=plan_mode,
                                          user_text=user_text):
                yield event
        except asyncio.CancelledError:
            if self.permissions:
                await self.permissions.cancel_chat(chat_id)
            raise

    async def run_continue(self, chat: dict,
                           directive: bool = False) -> AsyncIterator[dict]:
        """§9 Continue: resume generation without a new user message.

        Default mode = assistant-prefill: the history already ends with the
        assistant's own text, providers that support prefill continue from it.
        `directive` appends a short user nudge instead (strict providers).
        """
        chat_id = chat["id"]
        params = _gen_params(chat)
        self._workspace_dirty = False
        self._turn_span = []
        self.repeat_detector.reset()
        self.failure_detector.reset()

        history = await self.store.effective(chat_id)
        has_assistant = any(e["type"] in (ev.ASSISTANT_TEXT, ev.ASSISTANT_THINK)
                            for e in history)
        if not has_assistant:
            err = await self.store.append(chat_id, ev.ERROR, {
                "message": "нечего продолжять — в чате нет ответа модели",
                "stage": "continue"})
            yield err
            return
        if directive:
            ev_user = await self.store.append(chat_id, ev.USER_MESSAGE, {
                "text": "(продолжи ровно с того места, где остановился)"})
            yield ev_user

        try:
            async for event in self._loop(chat, params, plan_mode=False,
                                          user_text="(continue)"):
                yield event
        except asyncio.CancelledError:
            if self.permissions:
                await self.permissions.cancel_chat(chat_id)
            raise

    # -- shared turn pipeline -----------------------------------------------------

    async def _loop(self, chat: dict, params: dict | None,
                    plan_mode: bool = False,
                    user_text: str = "") -> AsyncIterator[dict]:
        chat_id = chat["id"]
        failure_streak = await self._failure_streak()
        self.failure_detector.streak = failure_streak
        plan_tools_list = None
        if plan_mode:
            from .planmode import PLAN_SYSTEM_PROMPT, plan_tools
            plan_tools_list = plan_tools(self.registry)

        for iteration in range(self.loop_limit):
            history = await self.store.effective(chat_id)
            system_messages = await self._system_messages(chat)
            if plan_mode:
                system_messages.append(PLAN_SYSTEM_PROMPT)
            effective_key = self.router.last_model_key or self.model_key
            caps = None
            if self.router is not None and effective_key:
                model_row = await self.router.model_info(effective_key)
                caps = (model_row or {}).get("capabilities") or {}
            messages = build_messages(chat, history,
                                      system_messages=system_messages,
                                      caps=caps, attach_store=self.attach_store)
            # an embedding model cannot take tool schemas at all — the API
            # declared it (no name guessing); a plain request lets the API
            # itself answer whether chat works
            tools = None if (caps or {}).get("embedding") else (
                plan_tools_list if plan_mode
                else (self.registry.openai_tools(chat_id) or None))

            think_buf: list[str] = []
            text_buf: list[str] = []
            tool_calls: list[dict] = []
            usage = None

            try:
                async for chunk in self.router.chat_stream(self.model_key, messages,
                                                           tools, params):
                    ctype = chunk["type"]
                    if ctype == "think":
                        think_buf.append(chunk["delta"])
                        yield {"live": "think", "delta": chunk["delta"]}
                    elif ctype == "text":
                        text_buf.append(chunk["delta"])
                        yield {"live": "text", "delta": chunk["delta"]}
                    elif ctype == "tool_call":
                        tool_calls.append(chunk)
                    elif ctype == "usage":
                        usage = chunk["usage"]
                    elif ctype == "fallback":
                        fb = await self.store.append(chat_id, ev.FALLBACK, {
                            "from": chunk.get("from"), "to": chunk.get("to"),
                            "reason": chunk.get("reason")})
                        yield fb
                    elif ctype == "done":
                        break
            except ProviderError as e:
                err = await self.store.append(chat_id, ev.ERROR,
                                              {"message": str(e), "stage": "provider"})
                yield err
                return
            except asyncio.CancelledError:
                # ESC: flush unfinished blocks as interrupted, stay consistent
                if think_buf:
                    e = await self.store.append(chat_id, ev.ASSISTANT_THINK,
                                                {"text": "".join(think_buf),
                                                 "interrupted": True})
                    yield e
                if text_buf:
                    e = await self.store.append(chat_id, ev.ASSISTANT_TEXT,
                                                {"text": "".join(text_buf),
                                                 "interrupted": True})
                    yield e
                ev_int = await self.store.append(chat_id, ev.INTERRUPT, {})
                yield ev_int
                raise

            # persist completed blocks in order: think -> text
            if think_buf:
                e = await self.store.append(chat_id, ev.ASSISTANT_THINK,
                                            {"text": "".join(think_buf)})
                self._turn_span.append(e["id"])
                yield e
            if text_buf:
                e = await self.store.append(chat_id, ev.ASSISTANT_TEXT,
                                            {"text": "".join(text_buf)})
                self._turn_span.append(e["id"])
                yield e

            # §5 repeat detector: near-duplicate assistant text -> stop + warn
            if text_buf:
                hit = self.repeat_detector.observe("".join(text_buf))
                if hit:
                    warn = await self.store.append(chat_id, ev.DETECTOR, {
                        **hit, "action": "stopped",
                        "note": "луп остановлен: похоже, модель повторяется. "
                                "Кнопка «продолжить» запустит новый ход."})
                    yield warn
                    turn_ev = await self._finish_turn(chat_id, iteration, usage)
                    yield turn_ev
                    cp_ev = await self._checkpoint(chat_id, user_text, turn_ev)
                    if cp_ev:
                        yield cp_ev
                    return

            if not tool_calls:
                # plan mode: the final text IS the plan (§5)
                if plan_mode and text_buf:
                    from .planmode import parse_checklist
                    submitted = await self.store.append(chat_id, ev.PLAN_SUBMITTED, {
                        "plan_text": "".join(text_buf),
                        "span": list(self._turn_span),
                        "checklist": parse_checklist("".join(text_buf))})
                    yield submitted
                turn_ev = await self._finish_turn(chat_id, iteration, usage)
                yield turn_ev
                cp_ev = await self._checkpoint(chat_id, user_text, turn_ev)
                if cp_ev:
                    yield cp_ev
                async for c_ev in self._auto_compact(chat):
                    yield c_ev
                return

            # persist tool calls, execute them (permission-gated)
            for tc in tool_calls:
                async for event in self._run_tool_call(chat_id, tc):
                    yield event

        # loop guard reached
        err = await self.store.append(chat_id, ev.ERROR, {
            "message": f"достигнут лимит итераций лупа ({self.loop_limit})",
            "stage": "loop"})
        yield err

    # -- tool call pipeline --------------------------------------------------------

    async def _run_tool_call(self, chat_id: str, tc: dict) -> AsyncIterator[dict]:
        name, args = tc["name"], tc.get("args") or {}

        # §15 transactional masking: same call retried with a timeout argument
        masked = await self._mask_timeout_retry(chat_id, name, args)

        e = await self.store.append(chat_id, ev.TOOL_CALL, {
            "call_id": tc["id"], "name": name, "args": args})
        self._turn_span.append(e["id"])
        yield e

        # §7 PreToolUse hooks — deny-first: a blocking verdict short-circuits
        # permissions and execution entirely.
        if self.hooks is not None:
            verdict = await self.hooks.pre_tool_use(chat_id, name, args,
                                                    user=self.user)
            if verdict is not None:
                result = {"error": f"заблокировано хуком: "
                                   f"{verdict.reason or 'без причины'}"}
            else:
                result = await self._execute_with_permissions(chat_id, name, args)
        else:
            result = await self._execute_with_permissions(chat_id, name, args)

        # §11 context hygiene for oversized outputs
        result = await self._process_big_output(chat_id, tc["id"], name, result)

        e = await self.store.append(chat_id, ev.TOOL_RESULT, {
            "call_id": tc["id"], "name": name, "result": result})
        if masked:
            e["data"]["masked_previous"] = masked
        self._turn_span.append(e["id"])
        yield e

        ok = not (result or {}).get("error") and not (result or {}).get("timeout")
        if ok and name in _MUTATING_TOOLS:
            self._workspace_dirty = True

        # §2 optional audit (fire-and-forget) + §7 PostToolUse observation
        if self.audit is not None:
            self.audit.record(self.user, chat_id, self.model_key, name, args,
                              ok, self._last_tool_ms)
        if self.hooks is not None:
            await self.hooks.post_tool_use(chat_id, name, args, result,
                                           user=self.user)

        # §5 tool-failure streak: warn the user + hint the model
        hit = self.failure_detector.observe(name, ok)
        if hit:
            warn = await self.store.append(chat_id, ev.DETECTOR, {
                **hit, "action": "hint",
                "note": f"тулза {name} падает {hit['count']} раз подряд — "
                        f"проверьте права/аргументы"})
            yield warn
            notice = await self.store.append(chat_id, ev.AGENT_NOTICE, {
                "text": f"[система] тулза {name} упала {hit['count']} раз "
                        f"подряд. Проверь аргументы и права доступа, либо "
                        f"смени подход — не повторяй тот же вызов вслепую."})
            self._turn_span.append(notice["id"])
            yield notice

    async def _execute_with_permissions(self, chat_id: str, name: str,
                                        args: dict) -> dict:
        """Permission gate (§7) + registry execution with timeouts (§15).
        The bash safe/ask/deny classification lives inside the engine."""
        entry = self.registry.entry(name)
        if entry is None:
            return {"error": f"unknown tool: {name}"}

        summary = self._summarize_call(name, args)

        if self.permissions is not None:
            _pattern, subject, kind = derive_pattern(name, args)
            decision = await self.permissions.check(name, args, entry.perm_class,
                                                    subject, kind)
            if decision["decision"] == "deny":
                return {"error": f"отклонено правами: {decision['reason']}"}
            if decision["decision"] == "ask":
                approval = await self.permissions.begin_approval(
                    chat_id, name, args, summary)
                # persisted + published by the store; the UI card listens for it
                await self.store.append(chat_id, ev.PERMISSION_REQUEST, {
                    "approval_id": approval["id"], "tool": name,
                    "summary": summary, "args": args})
                row = await self.permissions.wait(approval["id"])
                verdict = (row or {}).get("decision")
                await self.store.append(chat_id, ev.PERMISSION_RESULT, {
                    "approval_id": approval["id"], "tool": name,
                    "decision": verdict or "expired"})
                if verdict not in ("once", "always"):
                    reason = {"deny": "пользователь отклонил вызов",
                              "expired": "запрос истёк (генерация остановлена)"}
                    return {"error": reason.get(verdict, "разрешение не получено")}

        return await self._execute_timed(
            name, args, chat_id)

    async def _execute_timed(self, name: str, args: dict,
                             chat_id: str) -> dict:
        """registry.execute with the §2 team tool-concurrency gate."""
        import time as _time
        t0 = _time.monotonic()
        if self.tool_gate is not None:
            async with self.tool_gate:
                res = await self.registry.execute(
                    name, args, timeout_default=self.timeout_default,
                    timeout_max=self.timeout_max, chat_id=chat_id)
        else:
            res = await self.registry.execute(
                name, args, timeout_default=self.timeout_default,
                timeout_max=self.timeout_max, chat_id=chat_id)
        self._last_tool_ms = (_time.monotonic() - t0) * 1000
        return res

    def _summarize_call(self, name: str, args: dict) -> str:
        if name == "bash" and isinstance(args.get("command"), str):
            return f"bash: {args['command'][:160]}"
        if isinstance(args.get("path"), str):
            return f"{name}: {args['path'][:160]}"
        if isinstance(args.get("pattern"), str):
            return f"{name}: {args['pattern'][:160]}"
        return f"{name}: {json.dumps(args, ensure_ascii=False)[:160]}"

    # -- §15 clean history -----------------------------------------------------------

    async def _mask_timeout_retry(self, chat_id: str, name: str,
                                  args: dict) -> int | None:
        """The model retried the SAME call with an explicit `timeout` argument:
        mask (hide from model context) the failed TOOL_CALL + TOOL_RESULT and
        the think blocks in between. Returns the number of masked events."""
        if "timeout" not in args:
            return None
        stripped = {k: v for k, v in args.items() if k != "timeout"}
        events = await self.store.effective(chat_id)

        i = len(events) - 1
        think_ids: list[str] = []
        # walk back over think blocks produced after the failed call
        while i >= 0 and events[i]["type"] == ev.ASSISTANT_THINK:
            think_ids.append(events[i]["id"])
            i -= 1
        if i < 0 or events[i]["type"] != ev.TOOL_RESULT:
            return None
        result_ev = events[i]
        if result_ev["data"].get("name") != name:
            return None
        if not (result_ev["data"].get("result") or {}).get("timeout"):
            return None
        call_id = result_ev["data"].get("call_id")
        j = i - 1
        while j >= 0 and events[j]["type"] != ev.TOOL_CALL:
            j -= 1
        if j < 0:
            return None
        call_ev = events[j]
        if call_ev["data"].get("call_id") != call_id \
                or call_ev["data"].get("name") != name:
            return None
        if call_ev["data"].get("args") != stripped:
            return None

        targets = [call_ev["id"], result_ev["id"], *think_ids]
        await self.store.append(chat_id, ev.MASK, {
            "targets": targets, "reason": "timeout-retry",
            "tool": name, "call_id": call_id})
        return len(targets)

    # -- output cache + two-step summarization (§11) ---------------------------------

    async def _process_big_output(self, chat_id: str, call_id: str,
                                  name: str, result: dict) -> dict:
        """Oversized tool output: two-step summarization when enabled (§11),
        plain truncate+cache otherwise (Phase 2 behaviour)."""
        try:
            text = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            return result
        if len(text) <= TWO_STEP_THRESHOLD:
            return result
        if self.summarizer is not None and await self.summarizer.enabled():
            try:
                return await self.summarizer.summarize_tool_output(
                    chat_id, call_id, name, text,
                    self.router,
                    self.router.last_model_key or self.model_key)
            except Exception:                     # noqa: BLE001 — fall back below
                pass
        return self._truncate_cache(call_id, text)

    def _truncate_cache(self, call_id: str, text: str) -> dict:
        """Phase 2 fallback: truncate for the context, cache the full text."""
        if len(text) <= MAX_OUTPUT_CHARS:
            return json.loads(text)
        cache_dir = self._toolcache_dir()
        try:
            os.makedirs(cache_dir, exist_ok=True)
            path = os.path.join(cache_dir, f"{call_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            path = None
        return {
            "truncated": True,
            "note": (f"вывод сокращён до {MAX_OUTPUT_CHARS} символов; "
                     f"полный — в кеше сервера"),
            "cache": path,
            "preview": text[:MAX_OUTPUT_CHARS // 2],
        }

    def _toolcache_dir(self) -> str:
        return os.path.join(os.path.dirname(self.store.sessions_dir), "toolcache")

    # Phase 2 API compat: callers/tests using the old truncation entry point
    async def _maybe_cache_full_output(self, chat_id: str, call_id: str,
                                       result: dict) -> dict:
        return await self._process_big_output(chat_id, call_id, "", result)

    # -- auto-compaction (§11) --------------------------------------------------------

    async def _auto_compact(self, chat: dict) -> AsyncIterator[dict]:
        if self.compactor is None or self.router is None:
            return
        s = await self._settings()
        if str(s.get("compact_auto", "1")) in ("0", "false", "off"):
            return
        try:
            key = self.router.last_model_key or self.model_key
            model_row = await self.router.model_info(key) if key else None
            ctx = (model_row or {}).get("context_length")
            if not await self.compactor.over_threshold(chat, ctx):
                return
            event = await self.compactor.compact(chat, "auto", router=self.router)
            if event:
                yield event
        except Exception as e:                    # noqa: BLE001 — never kill the turn tail
            err = await self.store.append(chat["id"], ev.ERROR, {
                "message": f"компакция не удалась: {e}", "stage": "compact"})
            yield err

    # -- checkpoint + turn meta ---------------------------------------------------------

    async def _checkpoint(self, chat_id: str, user_text: str,
                          turn_event: dict) -> dict | None:
        """Shadow-git snapshot bound to the turn UUID (§14)."""
        if self.checkpoints is None:
            return None
        if not self._workspace_dirty:
            return None           # §14: a turn without file changes commits nothing
        try:
            message = (user_text or "turn")[:120]
            cp = await self.checkpoints.snapshot(chat_id, turn_event["id"], message)
        except CheckpointError as e:
            return await self.store.append(chat_id, ev.ERROR,
                                           {"message": f"чекпоинт: {e}",
                                            "stage": "checkpoint"})
        if not cp:
            return None
        return await self.store.append(chat_id, ev.CHECKPOINT, {
            "checkpoint_id": cp["id"], "commit": cp["commit_sha"][:10],
            "message": cp["message"], "files": cp["files"]})

    async def _finish_turn(self, chat_id: str, iteration: int,
                           usage: dict | None):
        # §7 Stop hooks — observation only (the turn is done either way)
        if self.hooks is not None:
            try:
                await self.hooks.stop(chat_id, user=self.user)
            except Exception:  # noqa: BLE001 — hooks never break the turn
                pass
        actual_key = self.router.last_model_key or self.model_key
        model_row = await self.router.model_info(actual_key)
        provider_name = None
        if model_row:
            provider = await self.router.settings_store.get_provider(model_row["provider_id"])
            provider_name = provider["name"] if provider else None

        meta = {"model": (model_row or {}).get("model_id") or actual_key,
                "provider": provider_name,
                "model_key": actual_key,
                "iterations": iteration + 1}
        if usage:
            # verbatim `usage` from the API response — prompt/completion/total
            # plus whatever cache fields the provider included. Never estimated.
            meta["usage"] = usage
        return await self.store.append(chat_id, ev.TURN_META, meta)
