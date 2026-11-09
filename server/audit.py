"""Toolcall audit (§2): optional, OFF by default.

This is NOT the internal append-only event log (that one is always on and
is the source of edit history) — this is the opt-in compliance record:
who / which session / which model / which tool / when. Toggle: the
`audit_enabled` setting (admin tab). Writes are fire-and-forget; failures
only reach the log buffer, never the turn.

In team/accounts mode the record lands in main.db (the admin-visible one)
regardless of which scope the call came from — the audit is a server-level
view. In solo it also goes to main.db, which IS the scope store.
"""

from __future__ import annotations

import asyncio

from .srvlog import LOGS


class AuditLog:
    def __init__(self, settings_store):
        self.store = settings_store          # main.db (server-level view)

    async def enabled(self) -> bool:
        try:
            s = await self.store.get_settings()
            return str(s.get("audit_enabled", "0")) == "1"
        except Exception:  # noqa: BLE001 — audit must never break a turn
            return False

    def record(self, user: str | None, chat_id: str | None, model_key: str | None,
               tool: str, args: dict, ok: bool, duration_ms: float) -> None:
        """Fire-and-forget write (a dropped record is logged, not raised)."""

        async def _write():
            try:
                if not await self.enabled():
                    return
                await self.store.audit_write(user, chat_id, model_key, tool,
                                             args, ok, duration_ms)
            except Exception as e:  # noqa: BLE001
                LOGS.log_event("audit", f"запись аудита не удалась: {e}", "error")

        try:
            asyncio.get_running_loop().create_task(_write())
        except RuntimeError:
            pass

    async def list(self, limit: int = 200, chat_id: str | None = None) -> list[dict]:
        return await self.store.audit_list(limit, chat_id)
