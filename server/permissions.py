"""Permission engine (§7 roadmap): deny-first rules + ask-mode approvals.

Evaluation order for a tool call:
  1. bash policy deny — destructive commands never run, regardless of rules;
  2. deny rules       — always win, the call never runs;
  3. allow rules      — run without asking («разрешить всегда для этого паттерна»);
  4. tool class       — read-class tools run silently; bash commands the
                        policy calls safe run silently too; write/exec class
                        depends on the mode (`ask` | `allow_all`).

An `ask` outcome creates a pending approval row + wakes the waiting agent
loop through an asyncio.Event when the user clicks one of the three buttons
(«разрешить один раз» / «разрешить всегда» / «отклонить»). `всегда` also
derives an allow-rule pattern from the call (directory for file tools,
command prefix for bash) so the next identical call does not ask again.

Pattern semantics:
  - file tools  : segment-aware glob against the jail-relative path
                  (`src/**`, `*.log` — `*` stays inside one path segment);
  - bash        : shell-style match against the command line (`git status*`,
                  `rm *` — `*` spans everything);
  - empty rule pattern matches the whole tool.
"""

from __future__ import annotations

import asyncio
import fnmatch
import re

from . import events as ev
from .bash_policy import classify as bash_classify


def glob_to_regex(pattern: str) -> re.Pattern | None:
    """`**` spans directories, `*`/`?` stay inside one segment."""
    if pattern is None:
        return None
    pat = str(pattern).strip()
    if not pat:
        return None                      # empty pattern = whole tool
    pat = pat.replace("\\", "/")
    out = ["^"]
    i = 0
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
                if pat[i:i + 3] == "/**":
                    out.append("(?:/.*)?")
                    i += 3
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    try:
        return re.compile("".join(out))
    except re.error:
        return re.compile(re.escape(pat))


def pattern_matches(pattern: str, subject: str, kind: str = "path") -> bool:
    """kind='path'    -> segment-aware glob (** spans, * stays in a segment)
       kind='command' -> shell-style match (* spans everything, spaces ok)
       empty pattern  -> matches the whole tool."""
    if not pattern:
        return True                      # empty pattern matches the whole tool
    subject = (subject or "").replace("\\", "/")
    if kind == "command":
        return fnmatch.fnmatch(subject.lower(), pattern.strip().lower())
    if pattern.endswith("/*") and not subject:
        return False
    rx = glob_to_regex(pattern)
    if rx:
        return bool(rx.match(subject))
    return fnmatch.fnmatch(subject, pattern)


def _is_agents_md(path) -> bool:
    """basename check — AGENTS.md anywhere in the workspace counts (§8)."""
    import os
    return isinstance(path, str) and os.path.basename(
        path.replace("\\", "/").rstrip("/")) == "AGENTS.md"


def derive_pattern(tool: str, args: dict) -> tuple[str, str, str]:
    """(rule-pattern-for-'always', subject, kind) for a call. The subject is
    what rules match against; the pattern is what 'always' stores."""
    if tool in ("bash", "background_run") and isinstance(args.get("command"), str):
        # subject: full command line; `always` rule prefix: first two tokens
        from .bash_policy import command_prefix
        return command_prefix(args["command"]), args["command"], "command"
    path = args.get("path") or args.get("file")
    if isinstance(path, str) and path:
        rel = (path or "").replace("\\", "/").lstrip("./")
        # `always` for a file tool -> its directory subtree
        dirname = rel.rsplit("/", 1)[0] if "/" in rel else ""
        return ((dirname + "/**") if dirname else "**"), rel, "path"
    return "", "", "path"


class PermissionDecision(dict):
    pass


class PermissionEngine:
    def __init__(self, store):
        self.store = store
        self._waiters: dict[str, asyncio.Event] = {}

    async def open(self) -> None:
        await self.store.expire_orphan_approvals()

    # -- evaluation ---------------------------------------------------------------

    async def check(self, tool: str, args: dict, tool_class: str,
                    subject: str, pattern_kind: str = "path") -> PermissionDecision:
        """Pure decision — no waiting. Returns one of:
        {decision: allow|deny, reason, mode}  or  {decision: ask, ...}."""
        settings = await self.store.get_settings()
        mode = settings.get("permission_mode", "ask")

        # 1. bash policy: destructive commands never run — nothing overrides this
        #    (background_run executes shell too, so the same policy applies)
        if tool in ("bash", "background_run") and isinstance(args.get("command"), str):
            verdict = bash_classify(args["command"])
            if verdict["class"] == "deny":
                return PermissionDecision(
                    decision="deny",
                    reason="; ".join(verdict["reasons"]) or "запрещено политикой bash",
                    mode=mode)
            if verdict["class"] == "safe":
                tool_class = "read"     # safe bash reads — no ask

        rules = await self.store.list_rules()
        for r in rules:                                   # deny first, always
            if r["effect"] != "deny":
                continue
            if (r["tool"] in ("*", tool)
                    and pattern_matches(r.get("pattern") or "", subject, pattern_kind)):
                return PermissionDecision(
                    decision="deny",
                    reason=f"запрещено правилом deny ({r['tool']}"
                           f"{' / ' + r['pattern'] if r['pattern'] else ''})",
                    mode=mode)
        for r in rules:
            if r["effect"] != "allow":
                continue
            if (r["tool"] in ("*", tool)
                    and pattern_matches(r.get("pattern") or "", subject, pattern_kind)):
                return PermissionDecision(decision="allow", reason="allow-правило",
                                          mode=mode)

        # §8: AGENTS.md is the USER's instruction file — the agent may not
        # rewrite it silently. An explicit allow-rule (user-granted) still
        # wins (checked above); everything else asks, allow_all included.
        if tool in ("write_file", "edit_file") and _is_agents_md(args.get("path")):
            return PermissionDecision(
                decision="ask",
                reason="изменение AGENTS.md — только с явного разрешения юзера",
                mode=mode)

        if tool_class == "read":
            return PermissionDecision(decision="allow", reason="чтение", mode=mode)
        if mode == "allow_all":
            return PermissionDecision(decision="allow", reason="allow_all", mode=mode)
        return PermissionDecision(decision="ask",
                                  reason=f"{tool}: {tool_class}", mode=mode)

    # -- ask flow -------------------------------------------------------------------

    async def begin_approval(self, chat_id: str, tool: str, args: dict,
                             summary: str) -> dict:
        approval_id = ev.new_id()
        row = await self.store.create_approval(approval_id, chat_id, tool,
                                               args, summary)
        self._waiters[approval_id] = asyncio.Event()
        return row

    async def wait(self, approval_id: str) -> dict | None:
        """Block until the user resolves the approval (None = expired/cancelled)."""
        waiter = self._waiters.get(approval_id)
        if waiter is None:
            return None
        try:
            await waiter.wait()
        finally:
            self._waiters.pop(approval_id, None)
        row = await self.store.get_approval(approval_id)
        return row

    async def resolve(self, approval_id: str, decision: str) -> dict | None:
        """decision: 'once' | 'always' | 'deny'. Returns the updated row."""
        row = await self.store.get_approval(approval_id)
        if not row or row["status"] != "pending":
            return row
        if decision not in ("once", "always", "deny"):
            return row

        rule_id = None
        if decision == "always":
            pattern, _subject, _kind = derive_pattern(row["tool"], row["args"] or {})
            rule = await self.store.add_rule(
                "allow", row["tool"], pattern,
                note=f"всегда разрешить ({row['summary'][:80]})")
            rule_id = rule["id"]

        status = "denied" if decision == "deny" else "approved"
        await self.store.resolve_approval(approval_id, status, decision, rule_id)
        waiter = self._waiters.get(approval_id)
        if waiter:
            waiter.set()
        return await self.store.get_approval(approval_id)

    async def cancel_chat(self, chat_id: str) -> None:
        """Turn stopped/replaced: every pending approval of this chat expires."""
        for row in await self.store.list_pending_approvals(chat_id):
            await self.store.resolve_approval(row["id"], "expired", None, None)
            waiter = self._waiters.pop(row["id"], None)
            if waiter:
                waiter.set()
