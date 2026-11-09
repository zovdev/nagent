"""Server logs (§16): ring buffer + level filtering + support export.

Two sources feed the buffer:
  1. Python `logging` records (a handler is attached to the root logger);
  2. structured app events via `log_event()` — hook runs, fallback switches,
     scope builds, mode changes (things that are NOT chat events).

The tab in settings reads GET /api/logs; the export button fetches
GET /api/logs/export as text/plain. Nothing leaves the server otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

LEVELS = ("debug", "info", "warning", "error")
_LEVEL_NUM = {"debug": 10, "info": 20, "warning": 30, "error": 40}
MAX_ENTRIES = 2000


class LogBuffer:
    def __init__(self, maxlen: int = MAX_ENTRIES):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = asyncio.Lock()
        self._min_level = 20                       # 'info' by default
        self._handler: _RingHandler | None = None

    # -- configuration -----------------------------------------------------------

    def set_level(self, level: str) -> None:
        self._min_level = _LEVEL_NUM.get((level or "info").lower(), 20)

    @property
    def level(self) -> str:
        for name, num in _LEVEL_NUM.items():
            if num == self._min_level:
                return name
        return "info"

    # -- writing -----------------------------------------------------------------

    def append(self, level: str, source: str, message: str) -> None:
        num = _LEVEL_NUM.get(level, 20)
        if num < self._min_level:
            return
        self._buf.append({"ts": time.time(), "level": level, "source": source,
                          "message": str(message)[:2000]})

    def log_event(self, source: str, message: str, level: str = "info") -> None:
        """Structured app events (also mirrored to stdout for console runs)."""
        self.append(level, source, message)
        line = f"[{source}] {message}"
        print(line, flush=True)

    # -- reading -----------------------------------------------------------------

    def entries(self, limit: int = 200, level: str | None = None) -> list[dict]:
        min_num = _LEVEL_NUM.get((level or "").lower(), 0) if level else 0
        out = [e for e in self._buf
               if _LEVEL_NUM.get(e["level"], 20) >= max(min_num, 0)]
        # 'level' filter means "this level and more severe"
        return out[-limit:]

    def export_text(self) -> str:
        lines = []
        for e in self._buf:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
            lines.append(f"{ts} {e['level'].upper():7s} {e['source']}: {e['message']}")
        return "\n".join(lines) + ("\n" if lines else "")

    def attach_logging(self) -> None:
        """Capture stdlib logging (aiohttp, asyncio, our modules)."""
        if self._handler is not None:
            return
        self._handler = _RingHandler(self)
        root = logging.getLogger()
        self._handler.setLevel(logging.DEBUG)
        root.addHandler(self._handler)

    def clear(self) -> None:
        self._buf.clear()


class _RingHandler(logging.Handler):
    def __init__(self, buf: LogBuffer):
        super().__init__()
        self.buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname.lower()
            if level == "warn":
                level = "warning"
            if level not in LEVELS:
                level = "info" if record.levelno < 30 else "error"
            self.buf.append(level, record.name or "log", record.getMessage())
        except Exception:  # noqa: BLE001 — logging must never break the app
            pass


LOGS = LogBuffer()
