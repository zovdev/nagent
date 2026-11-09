"""Versioned schema migrations (§2: 'auto-upgrade old DBs on a version bump').

A migration is `async def step(store: SettingsStore) -> None` keyed by an
integer version. Applied versions are recorded in `schema_migrations` inside
each database file (main.db AND every <user>.db), so reopening an old
Phase-4 database under Phase 5 upgrades it in place without data loss.
Steps must be idempotent-ish (IF NOT EXISTS / guarded ALTERs) — SQLite
saves us from most re-run hazards anyway since applied steps are skipped.
"""

from __future__ import annotations

import time


def _now() -> float:
    return time.time()


async def v2_team_shared_chats(store) -> None:
    """Phase 5: team mode shared projects — chats get owner + shared flags.
    Applied to main.db / shared history DBs; per-user DBs also get the
    columns (harmless, keeps schemas identical)."""
    cur = await store._db.execute("PRAGMA table_info(chats)")
    cols = {r[1] for r in await cur.fetchall()}
    if "chats" in cols or not cols:            # no chats table here — skip
        if not cols:
            return
    if "owner" not in cols:
        await store._db.execute("ALTER TABLE chats ADD COLUMN owner TEXT")
    if "shared" not in cols:
        await store._db.execute("ALTER TABLE chats ADD COLUMN shared INTEGER DEFAULT 0")
    await store._db.commit()


MIGRATIONS = {
    2: v2_team_shared_chats,
}
