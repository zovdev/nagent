"""Hooks (§7): events -> arbitrary commands, JSON on stdin, timeout, verdicts.

Supported events:
  PreToolUse       can BLOCK a tool call  ({"decision": "block", "reason": ...})
  PostToolUse      observation (tool result included in the payload)
  UserPromptSubmit can BLOCK the prompt or ADD context ({"context": "..."})
  Stop             observation (turn finished)
  Notification     observation (toasts / background tasks)

A hook is `{event, command, timeout, enabled}` stored in the scope settings
DB (§7 GUI editor lives in the settings modal). Execution: the command runs
through the shell with cwd = the scope's workspace, receives the full event
payload as JSON on stdin, and its stdout (first 64 KB) is parsed as a JSON
verdict when one is expected. Non-zero exit codes and timeouts are logged
and reported in the turn — they never crash the loop.

Classic use cases (§7): auto-format on PostToolUse, secret scan /
pattern block on PreToolUse, notifications.
"""

from __future__ import annotations

import asyncio
import json

from .srvlog import LOGS

HOOK_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit",
               "Stop", "Notification")
MAX_STDOUT = 64 * 1024


class HookVerdict(dict):
    """Parsed stdout of a hook. `.blocked` / `.context` helpers."""

    @property
    def blocked(self) -> bool:
        return self.get("decision") == "block"

    @property
    def reason(self) -> str | None:
        r = self.get("reason")
        return str(r)[:500] if r else None

    @property
    def context(self) -> str | None:
        c = self.get("context")
        return str(c)[:8000] if c else None


class HookEngine:
    def __init__(self, settings_store, workspace_getter=None):
        """:param settings_store: the scope's store (hooks table + settings).
        :param workspace_getter: callable -> workspace path (hook cwd)."""
        self.store = settings_store
        self.workspace_getter = workspace_getter or (lambda: None)

    # -- execution -----------------------------------------------------------------

    async def _run_command(self, hook: dict, payload: dict) -> tuple[str, int | None]:
        """Returns (stdout, exit_code|None). None code = timeout."""
        try:
            proc = await asyncio.create_subprocess_shell(
                hook["command"],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace_getter() or None)
        except (OSError, ValueError) as e:
            LOGS.log_event("hooks",
                           f"не удалось запустить: {e} — {hook['command'][:120]}",
                           "error")
            return "", -1
        stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin_data), timeout=float(hook["timeout"] or 10))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            LOGS.log_event("hooks",
                           f"таймаут {hook['timeout']:g}с — {hook['command'][:120]}",
                           "warning")
            return "", None
        if proc.returncode not in (0, None):
            LOGS.log_event("hooks",
                           f"exit {proc.returncode}: "
                           f"{(err or b'').decode('utf-8', 'replace')[:300]}",
                           "warning")
        return out[:MAX_STDOUT].decode("utf-8", "replace"), proc.returncode

    @staticmethod
    def _parse_verdict(stdout: str) -> HookVerdict:
        stdout = (stdout or "").strip()
        if not stdout:
            return HookVerdict()
        # tolerate surrounding junk: find the first {...} JSON object
        start = stdout.find("{")
        if start < 0:
            return HookVerdict()
        try:
            data = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return HookVerdict()
        if isinstance(data, dict):
            return HookVerdict(data)
        return HookVerdict()

    # -- public API ----------------------------------------------------------------

    async def fire(self, event: str, payload: dict,
                   expect_verdict: bool = False) -> list[dict]:
        """Run every enabled hook bound to `event`. Returns verdict dicts
        (empty when none). Only verdict-bearing events parse stdout."""
        if event not in HOOK_EVENTS:
            return []
        hooks = [h for h in await self.store.list_hooks()
                 if h["event"] == event and h["enabled"]]
        verdicts: list[dict] = []
        for hook in hooks:
            out, _code = await self._run_command(hook, {
                "event": event, "hook_id": hook["id"], **payload})
            if expect_verdict:
                verdicts.append(self._parse_verdict(out))
            else:
                if out.strip():
                    LOGS.log_event("hooks", f"[{event}] {out.strip()[:300]}")
        return verdicts

    async def pre_tool_use(self, chat_id: str, tool: str, args: dict,
                           user: str | None = None) -> dict | None:
        """First blocking verdict wins (§7 deny-first spirit)."""
        verdicts = await self.fire("PreToolUse", {
            "chat_id": chat_id, "tool": tool, "args": args, "user": user},
            expect_verdict=True)
        for v in verdicts:
            if v.blocked:
                return v
        return None

    async def post_tool_use(self, chat_id: str, tool: str, args: dict,
                            result: dict, user: str | None = None) -> None:
        await self.fire("PostToolUse", {
            "chat_id": chat_id, "tool": tool, "args": args,
            "result": _short(result), "user": user})

    async def user_prompt_submit(self, chat_id: str, prompt: str,
                                 user: str | None = None) -> HookVerdict | None:
        """Blocking verdict or extra context (HookVerdict either way)."""
        verdicts = await self.fire("UserPromptSubmit", {
            "chat_id": chat_id, "prompt": prompt, "user": user},
            expect_verdict=True)
        merged = HookVerdict()
        for v in verdicts:
            if v.blocked:
                return v
            if v.context:
                merged["context"] = (merged.get("context") or "") + v.context + "\n"
        return merged or None

    async def stop(self, chat_id: str, user: str | None = None) -> None:
        await self.fire("Stop", {"chat_id": chat_id, "user": user})

    async def notification(self, message: str, chat_id: str | None = None,
                           kind: str = "info") -> None:
        await self.fire("Notification", {
            "chat_id": chat_id, "message": message, "kind": kind})


def _short(result: dict) -> dict:
    """Hook payloads carry a size-capped tool result copy."""
    try:
        text = json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"error": "unserializable result"}
    if len(text) > 4000:
        return {"truncated": True, "preview": text[:4000]}
    return result
