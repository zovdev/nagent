"""Loop pathology detectors (§5 roadmap).

- RepeatDetector: sliding window over the assistant TEXT blocks of the
  CURRENT turn; a ≥90% match (difflib over whitespace-normalized text,
  texts shorter than `min_len` ignored) stops the turn and warns the user
  with a "продолжить" option.
- ToolFailureDetector: N consecutive failures of the SAME tool inside the
  turn warn the user and inject a hint into the model context (AGENT_NOTICE).
  A single success resets the streak.

Both are pure state machines — unit-testable without a server.
"""

from __future__ import annotations

import difflib
import re


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


class RepeatDetector:
    def __init__(self, similarity: float = 0.90, window: int = 6,
                 min_len: int = 80):
        self.similarity = similarity
        self.window = window
        self.min_len = min_len
        self._texts: list[str] = []

    def reset(self) -> None:
        self._texts.clear()

    def observe(self, text: str) -> dict | None:
        """Feed the next completed assistant text block of the turn.
        Returns detection info when it near-duplicates an earlier one."""
        norm = _normalize(text)
        if len(norm) < self.min_len:
            self._texts.append(norm)
            return None
        for prev in self._texts[-self.window:]:
            if not prev or len(prev) < self.min_len:
                continue
            ratio = difflib.SequenceMatcher(None, prev, norm).ratio()
            if ratio >= self.similarity:
                return {"kind": "repeat", "similarity": round(ratio, 3),
                        "against_chars": len(prev), "chars": len(norm)}
        self._texts.append(norm)
        return None


class ToolFailureDetector:
    def __init__(self, streak: int = 3):
        self.streak = max(2, int(streak or 3))
        self._streaks: dict[str, int] = {}

    def reset(self) -> None:
        self._streaks.clear()

    def observe(self, tool: str, ok: bool) -> dict | None:
        """Feed the outcome of one tool call. Returns detection info when the
        same tool failed `streak` times IN A ROW (re-warns at every multiple
        of the streak: 3, 6, 9...)."""
        if ok:
            self._streaks.pop(tool, None)
            return None
        n = self._streaks.get(tool, 0) + 1
        self._streaks[tool] = n
        if n >= self.streak and n % self.streak == 0:
            return {"kind": "tool_failures", "tool": tool, "count": n}
        return None

    def is_failing(self, tool: str) -> bool:
        return tool in self._streaks
