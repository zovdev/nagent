"""System prompt hierarchy + AGENTS.md auto-loading (§8 roadmap).

Hierarchy: server default (settings) -> chat system_prompt -> AGENTS.md from
the workspace root. Each level is a SEPARATE system message so the stable
prefix stays cache-friendly (prompt caching §11).

AGENTS.md:
- auto-loaded from the workspace root on every turn build (≤ MAX_BYTES);
- when the file changes between turns, the injected copy carries a notice
  (delta-injection, OpenCode pattern) — the model is told the instructions
  changed, the full new text is re-injected (a true diff would be ambiguous);
- the AGENT may not write AGENTS.md silently — the permission engine gates
  it with an explicit ask even in allow_all mode (see permissions.py).
"""

from __future__ import annotations

import hashlib
import os

AGENTS_MD_NAME = "AGENTS.md"
MAX_AGENTS_MD_BYTES = 128 * 1024


def read_agents_md(workspace_root: str) -> str | None:
    """Current AGENTS.md content or None (absent / too large / unreadable)."""
    path = os.path.join(workspace_root, AGENTS_MD_NAME)
    try:
        if not os.path.isfile(path):
            return None
        if os.path.getsize(path) > MAX_AGENTS_MD_BYTES:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
        return text or None
    except OSError:
        return None


class InstructionsResolver:
    """Assembles the system messages for a turn + tracks AGENTS.md changes."""

    def __init__(self, workspace_root: str):
        self.root = workspace_root
        self._agents_md_hash: str | None = None
        self._agents_md_text: str | None = None
        self._agents_md_changed = False

    def refresh_agents_md(self) -> None:
        """Re-read the file; flags a change vs the previously seen version."""
        text = read_agents_md(self.root)
        digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest() \
            if text else None
        if self._agents_md_hash is not None and digest != self._agents_md_hash:
            self._agents_md_changed = True      # seen at least once before
        self._agents_md_hash = digest
        self._agents_md_text = text

    def agents_md_block(self) -> str | None:
        """The AGENTS.md system-message body (with a change notice if any)."""
        if not self._agents_md_text:
            return None
        body = (f"=== {AGENTS_MD_NAME} (инструкции рабочей папки) ===\n"
                f"{self._agents_md_text}")
        if self._agents_md_changed:
            body = (f"({AGENTS_MD_NAME} изменился с прошлого хода — "
                    f"приведённая ниже версия актуальна)\n" + body)
            self._agents_md_changed = False      # notice is one-shot
        return body

    def system_messages(self, chat: dict) -> list[str]:
        """Hierarchy §8: server default -> chat -> AGENTS.md (workspace)."""
        self.refresh_agents_md()
        out: list[str] = []
        # server default + chat prompt live in the caller-provided settings;
        # this resolver only owns the AGENTS.md layer and the ordering.
        chat_prompt = (chat.get("system_prompt") or "").strip()
        if chat_prompt:
            out.append(chat_prompt)
        agents = self.agents_md_block()
        if agents:
            out.append(agents)
        return out
