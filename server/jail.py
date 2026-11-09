"""Workspace Jail — layer 1 of the two-layer isolation (§7 roadmap).

Path jail: every path a tool (or the model) touches is canonicalized and
checked against the resolved workspace root — `..` traversals, absolute
paths pointing outside and symlinks escaping the root are all rejected
BEFORE any filesystem access. Layer 2 (OS isolation: Docker / bubblewrap /
Seatbelt) is a setting that wraps command execution; see bash_policy.py.

Read deny-list: files that must never be read regardless of location
(credentials, keys) — checked on the basename pattern.

The jail is policy, not data: patterns here are security rules, which is
exactly the "smart wrapper" part of the doctrine.
"""

from __future__ import annotations

import fnmatch
import os
import re

# basenames (glob patterns) that are denied for READ everywhere in the jail
DENY_READ_BASENAMES = (
    ".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*",
    "credentials*.json", "*.p12", "*.pfx", "secrets.*",
)
# path fragments denied for read (resolved path containment)
DENY_READ_FRAGMENTS = ("/.ssh/", "/.gnupg/", "/.aws/", "/.kube/")

MAX_FILE_BYTES = 2 * 1024 * 1024          # read cap per file call
MAX_OUTPUT_CHARS = 60_000                 # tool-output cap (context hygiene §11)


class JailError(RuntimeError):
    """Path rejected by the jail — the message goes to the model verbatim."""


class WorkspaceJail:
    def __init__(self, root: str):
        self.root = os.path.realpath(os.path.abspath(root))
        os.makedirs(self.root, exist_ok=True)

    # -- path checks ------------------------------------------------------------

    def resolve(self, raw: str) -> str:
        """Resolve a model-supplied path to an absolute INSIDE-jail path.

        Accepts relative paths (workspace-rooted, `~/`-free) and absolute
        paths that land inside the jail after symlink resolution; rejects
        everything escaping it.
        """
        if raw is None:
            raise JailError("путь пуст")
        p = str(raw).strip()
        if not p:
            raise JailError("путь пуст")
        # expand user-style home references are NOT honored: there is no home
        # inside the jail
        if p.startswith("~"):
            raise JailError("путь ~ вне рабочей папки запрещён")
        if os.path.isabs(p):
            candidate = os.path.normpath(p)
        else:
            candidate = os.path.normpath(os.path.join(self.root, p))
        # resolve symlinks — the final target is what must stay inside;
        # realpath() handles non-existent tails lexically after the deepest
        # existing component, so a symlink chain cannot smuggle an escape
        try:
            final = os.path.realpath(candidate)
        except OSError as e:
            raise JailError(f"путь не читается: {e}")

        if final != self.root and not final.startswith(self.root + os.sep):
            raise JailError(
                f"путь вне рабочей папки запрещён: {raw} (резолвится в {final})")
        return final

    def relative(self, raw: str) -> str:
        """Jail-rooted relative path (for UI / diffs / git)."""
        full = self.resolve(raw)
        return os.path.relpath(full, self.root)

    def check_read(self, raw: str) -> str:
        full = self.resolve(raw)
        base = os.path.basename(full)
        if any(fnmatch.fnmatch(base, pat) for pat in DENY_READ_BASENAMES):
            raise JailError(
                f"чтение запрещено (секретный файл): {base}")
        norm = "/" + full.replace(os.sep, "/")
        if any(frag in norm for frag in DENY_READ_FRAGMENTS):
            raise JailError("чтение запрещено (служебная директория)")
        return full

    def check_write(self, raw: str) -> str:
        full = self.resolve(raw)
        # the .agent area is server-owned (shadow git, tool cache)
        rel = os.path.relpath(full, self.root)
        if rel == ".agent" or rel.startswith(f".agent{os.sep}"):
            raise JailError("запись в служебную папку .agent запрещена")
        return full

    def exists_in_jail(self, raw: str) -> bool:
        try:
            return os.path.exists(self.resolve(raw))
        except JailError:
            return False

    # -- helpers -----------------------------------------------------------------

    def ensure_parent(self, full_path: str) -> None:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)

    def truncate_output(self, text: str, cap: int = MAX_OUTPUT_CHARS) -> str:
        """Context hygiene: cap tool output; the full version is kept in the
        server-side tool cache (see core_tools / agent_loop)."""
        if text is None:
            return ""
        if len(text) <= cap:
            return text
        return (text[:cap] +
                f"\n… [вывод обрезан: {len(text)} символов, показано {cap}. "
                f"Полный вывод сохранён в кеше сервера]")
