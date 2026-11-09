"""Git Checkpoint Engine (§14 roadmap) — shadow-git in <workspace>/.agent/git.

- The user's own `.git` is NEVER touched; the shadow repo lives in the
  server-owned `.agent` area (excluded from listings/tools via the jail).
- After each finished turn, if the worktree is dirty, everything is
  committed and bound to the turn's UUID (the TURN_META event id):
  message ↔ commit, so "удалить сообщение с файловыми изменениями ->
  revert + merge-request" has an exact anchor.
- A turn without file changes produces no commit.
- Revert = merge-request flow: prepare (reverse-patch preview + conflict
  check against later edits) -> user approves -> apply (clean) or force
  (file-level rollback of the files that commit touched).
"""

from __future__ import annotations

import asyncio
import os

from . import events as ev
from .jail import WorkspaceJail

GIT_ENV_EXTRA = {
    "GIT_AUTHOR_NAME": "agent-framework",
    "GIT_AUTHOR_EMAIL": "bot@agent-framework.local",
    "GIT_COMMITTER_NAME": "agent-framework",
    "GIT_COMMITTER_EMAIL": "bot@agent-framework.local",
}


class CheckpointError(RuntimeError):
    pass


class Checkpoints:
    def __init__(self, jail: WorkspaceJail, store):
        self.jail = jail
        self.store = store                      # SettingsStore
        self.git_dir = os.path.join(jail.root, ".agent", "git")

    # -- plumbing ----------------------------------------------------------------

    async def _git(self, *args: str, stdin: str | None = None,
                   raw: bool = False) -> tuple[int, str]:
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "HOME": self.jail.root, "GIT_DIR": self.git_dir,
               "GIT_WORK_TREE": self.jail.root, **GIT_ENV_EXTRA}
        argv = ["git", *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.jail.root, env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except (OSError, FileNotFoundError) as e:
            raise CheckpointError(f"git не запускается: {e}") from e
        out, _ = await proc.communicate(
            stdin.encode("utf-8") if stdin is not None else None)
        text = out.decode("utf-8", "replace")
        # raw output preserves patch bytes exactly (context lines end with
        # significant whitespace); everything else is fine trimmed
        return proc.returncode, text if raw else text.strip()

    async def ensure(self) -> None:
        """Init the shadow repo + baseline commit (idempotent)."""
        if not os.path.isdir(os.path.join(self.git_dir, "refs")):
            os.makedirs(self.git_dir, exist_ok=True)
            rc, out = await self._git("init", "-q")
            if rc != 0:
                raise CheckpointError(f"git init: {out}")
            info = os.path.join(self.git_dir, "info", "exclude")
            os.makedirs(os.path.dirname(info), exist_ok=True)
            with open(info, "a", encoding="utf-8") as f:
                f.write("\n.agent/\n")            # never track our own area
        rc, _ = await self._git("rev-parse", "--verify", "HEAD")
        if rc != 0:
            # empty repo: make the baseline commit so diffs/reverts have a base
            rc, out = await self._git("add", "-A")
            rc2, out2 = await self._git("commit", "-q", "-m", "baseline",
                                        "--allow-empty")
            if rc != 0 or rc2 != 0:
                # race with a concurrent ensure() — HEAD existing is enough
                rc3, _ = await self._git("rev-parse", "--verify", "HEAD")
                if rc3 != 0:
                    raise CheckpointError(f"baseline commit: {out2 or out}")

    # -- snapshot -------------------------------------------------------------

    async def dirty(self) -> bool:
        await self.ensure()
        rc, out = await self._git("status", "--porcelain")
        return rc == 0 and bool(out)

    async def snapshot(self, chat_id: str | None, turn_event_id: str | None,
                       message: str) -> dict | None:
        """Commit the current worktree state; returns the checkpoint row
        (None when nothing changed — §14: a turn without changes commits
        nothing)."""
        if not await self.dirty():
            return None
        rc, out = await self._git("add", "-A")
        if rc != 0:
            raise CheckpointError(f"git add: {out}")
        safe_msg = (message or "turn").replace("\n", " ")[:200]
        rc, out = await self._git("commit", "-q", "-m", safe_msg)
        if rc != 0:
            # a concurrent snapshot may have committed the same state
            if not await self.dirty():
                return None
            raise CheckpointError(f"git commit: {out}")
        rc, sha = await self._git("rev-parse", "HEAD")
        if rc != 0:
            raise CheckpointError("rev-parse HEAD failed")
        rc, files_out = await self._git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")
        files = [f for f in (files_out or "").splitlines() if f]
        return await self.store.add_checkpoint(ev.new_id(), chat_id,
                                               turn_event_id, sha.strip(),
                                               safe_msg, files)

    # -- reads ----------------------------------------------------------------

    async def list(self, chat_id: str | None = None) -> list[dict]:
        return await self.store.list_checkpoints(chat_id)

    async def diff(self, checkpoint_id: str, cap: int = 40000) -> dict:
        cp = await self.store.get_checkpoint(checkpoint_id)
        if not cp:
            raise CheckpointError("чекпоинт не найден")
        sha = cp["commit_sha"]
        await self.ensure()
        rc, out = await self._git("show", "--format=", "--no-color", sha)
        if rc != 0:
            raise CheckpointError(f"diff {sha[:8]}: {out}")
        truncated = len(out) > cap
        return {"commit": sha, "diff": out[:cap], "truncated": truncated,
                "files": cp["files"], "message": cp["message"]}

    # -- revert (merge-request flow) -----------------------------------------------

    async def prepare_revert(self, checkpoint_id: str) -> dict:
        """Dry-run the revert of one checkpoint's commit.

        Returns {checkpoint, revert_patch, since_diff, conflicts, files}:
        - revert_patch — the reverse of the commit's own changes (what
          approving would remove);
        - since_diff   — current tree vs that commit (context for the user);
        - conflicts    — files the commit touched that later commits also
          touched (checked by dry-running `git apply --reverse`).
        """
        cp = await self.store.get_checkpoint(checkpoint_id)
        if not cp:
            raise CheckpointError("чекпоинт не найден")
        sha = cp["commit_sha"]
        await self.ensure()
        # the commit's own patch (raw: trailing whitespace is significant)
        rc, patch = await self._git("diff-tree", "-p", "-r", "--no-color", sha,
                                    raw=True)
        if rc != 0 or not patch.strip():
            return {"checkpoint": cp, "revert_patch": "", "since_diff": "",
                    "conflicts": [], "files": [],
                    "note": "пустой коммит — откатывать нечего"}
        # would the reverse apply cleanly onto the current tree?
        rc, check_out = await self._git("apply", "--check", "--reverse", "-",
                                        stdin=patch)
        conflicts: list[str] = []
        if rc != 0:
            conflicts = await self._revert_conflict_files(sha)
        # context: what changed since that checkpoint
        _, since_diff = await self._git("diff", "--no-color", sha)
        return {"checkpoint": cp,
                "revert_patch": patch[:40000],
                "since_diff": (since_diff or "")[:40000],
                "conflicts": conflicts, "files": cp["files"]}

    async def _revert_conflict_files(self, sha: str) -> list[str]:
        """Files the commit touched that also changed later: intersect the
        commit's files with files changed between the commit and HEAD."""
        rc, files_out = await self._git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
        commit_files = set(f for f in (files_out or "").splitlines() if f)
        rc, later_out = await self._git(
            "diff", "--name-only", f"{sha}..HEAD")
        later_files = set(f for f in (later_out or "").splitlines() if f)
        return sorted(commit_files & later_files)

    async def apply_revert(self, checkpoint_id: str,
                           force: bool = False) -> dict:
        """Apply the prepared revert. force=True rolls the commit's files
        back wholesale (overwriting later edits — the user was warned)."""
        cp = await self.store.get_checkpoint(checkpoint_id)
        if not cp:
            raise CheckpointError("чекпоинт не найден")
        sha = cp["commit_sha"]
        await self.ensure()

        if force:
            parent = f"{sha}^"
            for f in cp["files"]:
                # did the file exist before the checkpoint?
                rc, _ = await self._git("cat-file", "-e", f"{parent}:{f}")
                if rc == 0:
                    rc, out = await self._git("checkout", parent, "--", f)
                    if rc != 0:
                        raise CheckpointError(f"откат {f}: {out}")
                else:
                    # the checkpoint CREATED the file -> removing it is the revert
                    full = os.path.join(self.jail.root, f)
                    if os.path.exists(full):
                        os.remove(full)
        else:
            rc, patch = await self._git("diff-tree", "-p", "-r", "--no-color", sha,
                                        raw=True)
            rc, out = await self._git("apply", "--reverse", "-", stdin=patch)
            if rc != 0:
                raise CheckpointError(
                    f"реверс-патч не применился (конфликт с поздними правками): "
                    f"{out}. Попробуйте force=true или отмените.")
        rc, out = await self._git("add", "-A")
        if rc != 0:
            raise CheckpointError(f"git add: {out}")
        msg = f"revert: {cp['message'][:150]}"
        rc, out = await self._git("commit", "-q", "-m", msg)
        if rc != 0 and await self.dirty():
            raise CheckpointError(f"git commit: {out}")
        rc, new_sha = await self._git("rev-parse", "HEAD")
        return {"reverted": checkpoint_id, "commit": cp["commit_sha"],
                "revert_commit": new_sha.strip(), "force": force}
