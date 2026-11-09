"""Core tools (Phase 2, §6/§7/§15 roadmap).

Everything a tool touches goes through the WorkspaceJail first (path
canonicalization + escape checks + secret read deny-list). Mutating tools
carry permission classes ('write' / 'exec') so the permission engine can
ask the user; read-class tools run silently unless a deny rule matches.

bash: subprocess with a minimal environment, cwd = workspace root, the
§15 timeout argument, and the optional OS-isolation wrapper (docker /
bubblewrap — layer 2 of §7; 'off' by default).

The two meta-tools implement ToolSearch (§6): the model gets core tool
schemas up front, MCP/deferred tools only after `tool_search` +
`tool_load` — schemas never bloat the prompt.
"""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import os
import re
import time

from .jail import JailError, MAX_FILE_BYTES, WorkspaceJail
from .providers import ToolSpec
from .tools import ToolRegistry

GREP_MAX_MATCHES = 200
GREP_MAX_FILE_BYTES = 1024 * 1024
LIST_MAX_ENTRIES = 500
GLOB_MAX_MATCHES = 500

# OS isolation (§7 layer 2) — argv builders, unit-testable without docker
DOCKER_IMAGE = "alpine:3.20"


def isolation_argv(mode: str, root: str, command: str) -> list[str] | None:
    """Wrap a shell command in the OS-isolation layer. None = run directly.

    docker : only the workspace is mounted (read-write), no network —
             there is literally nothing else to touch.
    bubblewrap: root of the FS read-only, /tmp private, workspace writable.
    """
    mode = (mode or "off").strip().lower()
    if mode == "docker":
        return ["docker", "run", "--rm", "--network", "none",
                "-v", f"{root}:{root}", "-w", root, DOCKER_IMAGE,
                "sh", "-c", command]
    if mode in ("bubblewrap", "bwrap"):
        return ["bwrap",
                "--ro-bind", "/", "/",
                "--bind", root, root,
                "--dev", "/dev",
                "--proc", "/proc",
                "--tmpfs", "/tmp",
                "sh", "-c", command]
    return None


def _minimal_env(root: str) -> dict:
    """No secrets ever leak into the subprocess environment."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": root,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
    }


# ----------------------------------------------------------------- file tools

async def read_file(jail: WorkspaceJail, path: str, offset: int = 1,
                    limit: int = 2000) -> dict:
    try:
        full = jail.check_read(path)
    except JailError as e:
        return {"error": str(e)}
    if not os.path.isfile(full):
        return {"error": f"не файл: {path}"}
    size = os.path.getsize(full)
    if size > MAX_FILE_BYTES:
        return {"error": f"файл слишком большой ({size} байт > {MAX_FILE_BYTES})"}
    with open(full, "rb") as f:
        raw = f.read(8192)
    if b"\x00" in raw:
        return {"error": "бинарный файл — read_file не читает бинарники"}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return {"error": f"не читается: {e}"}
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    total = len(lines)
    chunk = lines[offset - 1:offset - 1 + limit]
    rel = os.path.relpath(full, jail.root)
    return {"path": rel, "content": "".join(chunk), "total_lines": total,
            "offset": offset, "lines_shown": len(chunk)}


async def write_file(jail: WorkspaceJail, path: str, content: str) -> dict:
    try:
        full = jail.check_write(path)
    except JailError as e:
        return {"error": str(e)}
    jail.ensure_parent(full)
    existed = os.path.exists(full)
    data = (content or "").encode("utf-8")
    with open(full, "wb") as f:
        f.write(data)
    return {"path": os.path.relpath(full, jail.root), "bytes": len(data),
            "created": not existed}


async def edit_file(jail: WorkspaceJail, path: str, old_string: str,
                    new_string: str, replace_all: bool = False) -> dict:
    try:
        full = jail.check_write(path)
    except JailError as e:
        return {"error": str(e)}
    if not os.path.isfile(full):
        return {"error": f"файл не найден: {path}"}
    try:
        with open(full, "r", encoding="utf-8", errors="strict") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError) as e:
        return {"error": f"не читается как текст: {e}"}
    count = text.count(old_string) if old_string else 0
    if count == 0:
        return {"error": "old_string не найден в файле", "path": path}
    if count > 1 and not replace_all:
        return {"error": f"old_string встречается {count} раз — уточни фрагмент "
                         f"или передай replace_all=true", "occurrences": count}
    new_text = text.replace(old_string, new_string) if replace_all else \
        text.replace(old_string, new_string, 1)
    with open(full, "w", encoding="utf-8") as f:
        f.write(new_text)
    return {"path": os.path.relpath(full, jail.root), "replacements": count}


async def list_dir(jail: WorkspaceJail, path: str = ".") -> dict:
    try:
        full = jail.resolve(path)
    except JailError as e:
        return {"error": str(e)}
    if not os.path.isdir(full):
        return {"error": f"не папка: {path}"}
    entries = []
    with os.scandir(full) as it:
        for entry in it:
            if entry.name == ".agent":
                continue               # server-owned area (shadow git, caches)
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                size = 0 if is_dir else entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            entries.append({"name": entry.name, "type": "dir" if is_dir else "file",
                            "size": size})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    truncated = len(entries) > LIST_MAX_ENTRIES
    return {"path": os.path.relpath(full, jail.root),
            "entries": entries[:LIST_MAX_ENTRIES], "count": len(entries),
            "truncated": truncated}


async def glob_files(jail: WorkspaceJail, pattern: str, path: str = ".") -> dict:
    if ".." in (pattern or ""):
        return {"error": "шаблон с '..' запрещён"}
    try:
        base = jail.resolve(path or ".")
    except JailError as e:
        return {"error": str(e)}
    if not os.path.isdir(base):
        return {"error": f"не папка: {path}"}
    import glob as _glob
    matches = []
    for m in _glob.glob(os.path.join(base, pattern), recursive=True):
        if os.path.basename(m) == ".agent" or "/.agent/" in m:
            continue
        matches.append(os.path.relpath(m, jail.root))
    matches.sort()
    return {"matches": matches[:GLOB_MAX_MATCHES], "count": len(matches),
            "truncated": len(matches) > GLOB_MAX_MATCHES}


async def grep_files(jail: WorkspaceJail, pattern: str, path: str = ".",
                     glob: str = "*", ignore_case: bool = False) -> dict:
    try:
        rx = re.compile(pattern or "",
                        re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return {"error": f"битое регулярное выражение: {e}"}
    try:
        base = jail.resolve(path or ".")
    except JailError as e:
        return {"error": str(e)}
    if not os.path.isdir(base):
        return {"error": f"не папка: {path}"}
    matches = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != ".agent"]
        for fname in filenames:
            if not fnmatch.fnmatch(fname, glob or "*"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                if os.path.getsize(fpath) > GREP_MAX_FILE_BYTES:
                    continue
                with open(fpath, "rb") as f:
                    if b"\x00" in f.read(1024):
                        continue                     # binary
                scanned += 1
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for no, line in enumerate(f, 1):
                        if rx.search(line):
                            matches.append({
                                "file": os.path.relpath(fpath, jail.root),
                                "line_no": no,
                                "line": line.rstrip("\n")[:300]})
                            if len(matches) >= GREP_MAX_MATCHES:
                                return {"matches": matches, "count": len(matches),
                                        "files_scanned": scanned, "truncated": True}
            except OSError:
                continue
    return {"matches": matches, "count": len(matches),
            "files_scanned": scanned, "truncated": False}


# ----------------------------------------------------------------- bash

async def run_bash(jail: WorkspaceJail, isolation: str, command: str,
                   timeout: float | None = None) -> dict:
    """`timeout` is consumed by the registry (§15); accepted here so the
    schema exposes it to the model."""
    argv = isolation_argv(isolation, jail.root, command)
    if argv is None:
        argv = ["sh", "-c", command]
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=jail.root, env=_minimal_env(jail.root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, FileNotFoundError) as e:
        return {"error": f"не удалось запустить: {e}",
                "hint": "проверьте команду/изоляцию"}
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    duration = round((time.monotonic() - t0) * 1000)
    stdout = jail.truncate_output(out.decode("utf-8", "replace"))
    stderr = jail.truncate_output(err.decode("utf-8", "replace"))
    return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
            "duration_ms": duration, "isolation": isolation}


# ----------------------------------------------------------------- demo tools

def _get_time(tz: str = "local") -> dict:
    now = datetime.datetime.now(datetime.timezone.utc)
    return {"iso": now.isoformat(), "tz": tz}


def _echo(text: str = "") -> dict:
    return {"echo": text, "len": len(text)}


# ----------------------------------------------------------------- registry

def build_registry(jail: WorkspaceJail, isolation_getter=None) -> ToolRegistry:
    """Core tools + meta-tools. `isolation_getter` -> current isolation mode
    (async, reads settings) so the bash tool follows live settings."""
    reg = ToolRegistry()

    def isolation_mode() -> str:
        if isolation_getter is None:
            return "off"
        try:
            return isolation_getter() or "off"
        except Exception:
            return "off"

    reg.register(ToolSpec(
        "read_file", "Прочитать текстовый файл рабочей папки. offset/limit — строки.",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1}},
         "required": ["path"], "additionalProperties": False}),
        lambda **kw: read_file(jail, **kw), perm_class="read", default_timeout=15)

    reg.register(ToolSpec(
        "write_file", "Создать/перезаписать файл в рабочей папке.",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"], "additionalProperties": False}),
        lambda **kw: write_file(jail, **kw), perm_class="write", default_timeout=15)

    reg.register(ToolSpec(
        "edit_file", "Заменить фрагмент текста в файле (exact string replace).",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"}},
         "required": ["path", "old_string", "new_string"],
         "additionalProperties": False}),
        lambda **kw: edit_file(jail, **kw), perm_class="write", default_timeout=15)

    reg.register(ToolSpec(
        "list_dir", "Список файлов и папок (файлы рабочей области).",
        {"type": "object", "properties": {"path": {"type": "string"}},
         "additionalProperties": False}),
        lambda **kw: list_dir(jail, **kw), perm_class="read", default_timeout=10)

    reg.register(ToolSpec(
        "glob", "Найти файлы по шаблону (например '**/*.py').",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}},
         "required": ["pattern"], "additionalProperties": False}),
        lambda **kw: glob_files(jail, **kw), perm_class="read", default_timeout=20)

    reg.register(ToolSpec(
        "grep", "Поиск по регулярному выражению в файлах рабочей папки.",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
            "glob": {"type": "string"}, "ignore_case": {"type": "boolean"}},
         "required": ["pattern"], "additionalProperties": False}),
        lambda **kw: grep_files(jail, **kw), perm_class="read", default_timeout=30)

    reg.register(ToolSpec(
        "bash", "Выполнить shell-команду в рабочей папке. Если вызов не успел "
                "завершиться — повтори ТОТ ЖЕ вызов с параметром timeout=N (сек).",
        {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 600}},
         "required": ["command"], "additionalProperties": False},
        accepts_timeout=True),
        lambda **kw: run_bash(jail, isolation_mode(), **kw),
        perm_class="exec", default_timeout=30)

    reg.register(ToolSpec(
        "get_time", "Текущее время сервера (ISO 8601, UTC).",
        {"type": "object", "properties": {"tz": {"type": "string"}},
         "additionalProperties": False}),
        _get_time, perm_class="read")

    reg.register(ToolSpec(
        "echo", "Echo the given text back. Demo tool.",
        {"type": "object", "properties": {"text": {"type": "string"}},
         "additionalProperties": False}),
        _echo, perm_class="read")

    # -- ToolSearch meta-tools (§6) --------------------------------------------

    async def _tool_search(query: str = "", chat_id: str = "") -> dict:
        results = reg.search(query, chat_id)
        return {"results": results, "hint":
                "tool_load(names=[...]) подключает схемы выбранных тулзов"}

    async def _tool_load(names: list | None = None, chat_id: str = "") -> dict:
        res = reg.load(chat_id, [str(n) for n in (names or [])])
        if res["loaded"]:
            res["note"] = "схемы подключены и видны с следующей итерации"
        return res

    reg.register(ToolSpec(
        "tool_search", "Поиск по каталогу тулзов (имена + описания). Полный "
                       "каталог не раздувает промпт — догружай схемы по требованию.",
        {"type": "object", "properties": {"query": {"type": "string"}},
         "additionalProperties": False}),
        _tool_search, perm_class="read", needs_chat=True)

    reg.register(ToolSpec(
        "tool_load", "Подключить схемы тулзов (из tool_search) для этого чата.",
        {"type": "object", "properties": {"names": {"type": "array",
                                                    "items": {"type": "string"}}},
         "required": ["names"], "additionalProperties": False}),
        _tool_load, perm_class="read", needs_chat=True)

    return reg


def default_registry(jail: WorkspaceJail | None = None) -> ToolRegistry:
    """Back-compat: build with an implicit jail (tests / phase-0 callers)."""
    if jail is None:
        import tempfile
        jail = WorkspaceJail(tempfile.mkdtemp(prefix="agentfw-jail-"))
    return build_registry(jail)
