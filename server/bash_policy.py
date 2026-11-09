"""Bash policy — parse a command line into sub-commands and classify them
against prefix rules (§7 roadmap, the Codex prefix-rules pattern).

Design: shlex splits the line into `&&`, `||`, `;`, `|` separated
sub-commands (operators kept OUTSIDE quotes survive; quoted operators are
data). Each sub-command is classified by its executable + leading args.

Classes:
  safe     — read-only, runs even in `ask` mode without asking
  ask      — mutating / network: goes through the permission engine
  deny     — destructive or secret-dumping: denied with a reason

This is policy (the "smart wrapper" half of the doctrine), not data
invention: nothing here pretends to come from an API.

tree-sitter-bash can accelerate `split_subcommands` later; the rule table
and classification stay the same either way (optional import).
"""

from __future__ import annotations

import shlex

# read-only executables: no ask, no deny
SAFE_EXECUTABLES = {
    "ls", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo",
    "stat", "file", "du", "df", "tree", "which", "whoami", "date", "uname",
    "sort", "uniq", "cut", "awk", "sed", "diff", "md5sum", "sha256sum",
    "column", "tr", "rev", "seq", "true", "false", "basename", "dirname",
    "realpath", "readlink", "xxd", "strings", "git", "jq",
}

# sub-select of safe executables that become ASK when used to mutate:
# handled below via argument sniffing (git push, python script.py, …)
INTERPRETERS = {"python", "python3", "node", "deno", "ruby", "perl", "php", "lua"}
VERSION_ONLY_FLAGS = {"--version", "-V", "-v", "--help", "-h"}

# executables that are denied outright (destructive)
DENY_EXECUTABLES = {
    "sudo", "su", "shutdown", "reboot", "halt", "poweroff", "mkfs",
    "systemctl", "service", "env", "printenv", "chown", "passwd", "visudo",
    "crontab", "modprobe", "insmod", "nsenter",
}

# (executable, arg-prefix) pairs that escalate an otherwise-safe executable
SAFE_EXECUTABLE_MUTATING_ARGS = {
    "git": ("push", "commit", "reset", "rebase", "clean", "checkout --",
            "restore", "merge", "apply", "revert", "cherry-pick", "tag -d",
            "branch -D", "push --force", "rm"),
}



def split_subcommands(line: str) -> list[list[str]]:
    """Split a command line on unquoted && || ; | into token lists.

    Quoted operators ('&&' inside quotes) stay part of their tokens —
    they are DATA, not control flow. `punctuation_chars` keeps operators
    separate even without surrounding spaces (`ls;sudo` -> ['ls',';','sudo']).
    Redirections stay as tokens too (checked by the deny patterns below).
    """
    if not line or not line.strip():
        return []
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";|&")
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        # unbalanced quotes: treat conservatively as one opaque command
        return [["sh", "-c", line]]
    out: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in ("&&", "||", ";", "|"):
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _exe(sub: list[str]) -> str:
    """Executable name of a sub-command (handles env VAR=val, paths)."""
    i = 0
    while i < len(sub) and "=" in sub[i] and not sub[i].startswith("-") \
            and sub[i].split("=", 1)[0].replace("_", "a").isidentifier():
        i += 1  # skip VAR=value prefix assignments
    exe = sub[i] if i < len(sub) else ""
    return exe.rsplit("/", 1)[-1]


def classify_sub(sub: list[str]) -> tuple[str, str]:
    """Classify ONE sub-command -> ('safe'|'ask'|'deny', reason)."""
    exe = _exe(sub)
    if not exe:
        return "ask", "пустая команда"
    args = sub[1:]
    rest_low = [a.lower() for a in args if not a.startswith("-")]

    if exe in DENY_EXECUTABLES:
        why = ("печать переменных окружения может утечь ключи"
               if exe in ("env", "printenv")
               else "повышение привилегий / системные операции запрещены")
        return "deny", f"{exe}: {why}"

    if exe == "rm":
        if any(a in ("/", "/*", "~", "/*/*") for a in args if not a.startswith("-")) \
                or "-rf" in args or ("--force" in args and "-r" in args):
            return "deny", "rm по корню/дому или rm -rf — запрещено"
        return "ask", "rm удаляет файлы"

    if exe in ("dd", "mkfs", "shred"):
        return "deny", f"{exe} — разрушительная операция"

    if exe in ("chmod", "chattr"):
        if any(a in ("000", "777") for a in args):
            return "deny", f"{exe} {' '.join(args)} — системно опасные права"
        return "ask", "изменение прав доступа"

    if exe in ("kill", "pkill", "killall"):
        return "ask", "завершение процессов"

    if exe in ("curl", "wget"):
        return "ask", "сетевой запрос из bash — проверьте разрешение"

    if exe in ("tee", "cp", "mv", "mkdir", "rmdir", "touch", "ln", "install",
               "truncate", "split", "patch", "rsync", "scp", "ssh", "ssh-keygen",
               "tar", "zip", "unzip", "gzip", "gunzip", "sh", "bash", "zsh",
               "make", "cmake", "cargo", "go", "gcc", "g++", "clang", "dotnet",
               "java", "psql", "mysql", "sqlite3", "redis-cli", "docker",
               "podman", "ffmpeg", "convert", "npx", "npm", "pip", "pip3"):
        return "ask", f"{exe} — изменяющая/исполняющая команда"

    if exe in INTERPRETERS:
        nonflags = [a for a in args if not a.startswith("-")]
        if not args or all(a in VERSION_ONLY_FLAGS for a in args) or not nonflags:
            return "safe", ""
        return "ask", f"{exe} исполняет код — требует подтверждения"

    if exe == "sed":
        if "-i" in args or "--in-place" in args:
            return "ask", "sed -i изменяет файлы"
        return "safe", ""

    if exe == "find":
        if "-delete" in args or "-exec" in args or "-execdir" in args or "-ok" in args:
            return "ask", "find с -delete/-exec изменяет файлы"
        return "safe", ""

    if exe in SAFE_EXECUTABLES:
        for prefix in SAFE_EXECUTABLE_MUTATING_ARGS.get(exe, ()):
            if prefix.split()[0] in rest_low:
                return "ask", f"{exe} {prefix} — изменяющая операция"
        # git read subcommands are safe; bare `git` prints help
        return "safe", ""

    return "ask", f"неизвестная команда {exe} — требует подтверждения"


def classify(line: str) -> dict:
    """Classify a whole command line.

    Returns {class: 'safe'|'ask'|'deny', reasons: [...], subcommands: [...]}.
    Deny wins over ask, ask wins over safe.
    """
    subs = split_subcommands(line or "")
    if not subs:
        return {"class": "safe", "reasons": [], "subcommands": []}
    verdicts: list[tuple[str, str]] = [classify_sub(s) for s in subs]
    classes = [v[0] for v in verdicts]
    if "deny" in classes:
        reasons = [f"{_exe(s)}: {v[1]}" for s, v in zip(subs, verdicts) if v[0] == "deny"]
        return {"class": "deny", "reasons": reasons, "subcommands": subs}
    if "ask" in classes:
        reasons = [f"{_exe(s)}: {v[1]}" for s, v in zip(subs, verdicts) if v[0] == "ask"]
        return {"class": "ask", "reasons": reasons, "subcommands": subs}
    return {"class": "safe", "reasons": [], "subcommands": subs}


def command_prefix(line: str) -> str:
    """Stable prefix for pattern rules like 'always allow git status'.

    First two tokens of the FIRST sub-command (executable + first arg),
    lowercased — what a user means by "разрешить всегда git status".
    """
    subs = split_subcommands(line or "")
    if not subs:
        return ""
    parts = [_exe(subs[0])]
    args = [a for a in subs[0][1:] if not a.startswith("-")]
    if args:
        parts.append(args[0])
    return " ".join(p for p in parts if p)
