"""Background tasks subsystem (§13 roadmap).

The agent starts a long command as a background task (tool), checks its
status (tool) and can park itself waiting for completion (tool wait) —
when the task finishes, the RESULT is delivered to the agent and the loop
continues. The user gets: live output in the terminal tab + a notification
toast/sound on completion.

Model context stays clean: only what the tools RETURN enters it. The live
output stream is user-visible transient frames (never persisted as content
events); BG_START/BG_END are lightweight lifecycle events.

Commands run through the same jail + isolation layer as the bash tool and
the same permission engine (background_run is an exec-class tool whose
subject is the command line — bash policy applies, destructive commands
are denied).
"""

from __future__ import annotations

import asyncio
import time

from .core_tools import _minimal_env, isolation_argv
from .providers import ToolSpec

TAIL_BYTES = 16 * 1024          # ring buffer per task (what status/wait return)
MAX_TASKS_PER_CHAT = 8


class BackgroundTasks:
    def __init__(self, jail, history_store, publish_live,
                 isolation_getter=None, notify_hook=None):
        """:param publish_live: callable(chat_id, frame) for transient SSE
        frames (bg_output deltas). :param history_store: persisted events.
        :param notify_hook: callable(message, chat_id) — §7 Notification
        hooks fire when a toast-worthy event lands."""
        self.jail = jail
        self.history = history_store
        self.publish = publish_live
        self.isolation_getter = isolation_getter or (lambda: "off")
        self.notify_hook = notify_hook
        self._tasks: dict[str, dict] = {}

    # -- helpers ------------------------------------------------------------------

    def _isolation(self) -> str:
        try:
            return self.isolation_getter() or "off"
        except Exception:  # noqa: BLE001
            return "off"

    def list(self, chat_id: str | None = None) -> list[dict]:
        out = []
        for t in self._tasks.values():
            if chat_id and t["chat_id"] != chat_id:
                continue
            out.append(self._brief(t))
        out.sort(key=lambda t: t["started_at"])
        return out

    def get(self, task_id: str) -> dict | None:
        t = self._tasks.get(task_id)
        return self._brief(t) if t else None

    @staticmethod
    def _brief(t: dict) -> dict:
        return {"task_id": t["task_id"], "chat_id": t["chat_id"],
                "name": t["name"], "command": t["command"],
                "status": t["status"], "exit_code": t["exit_code"],
                "started_at": t["started_at"],
                "duration_ms": t["duration_ms"], "tail": t["tail"][-2000:],
                "bg": True}                       # mock-provider e2e marker

    # -- lifecycle ------------------------------------------------------------------

    async def start(self, chat_id: str, command: str, name: str = "") -> dict:
        command = (command or "").strip()
        if not command:
            return {"error": "пустая команда"}
        running = [t for t in self._tasks.values()
                   if t["chat_id"] == chat_id and t["status"] == "running"]
        if len(running) >= MAX_TASKS_PER_CHAT:
            return {"error": f"слишком много фоновых задач у чата "
                             f"({MAX_TASKS_PER_CHAT}) — дождитесь завершения"}
        isolation = self._isolation()
        argv = isolation_argv(isolation, self.jail.root, command)
        if argv is None:
            argv = ["sh", "-c", command]
        import uuid
        task_id = f"bg-{uuid.uuid4().hex[:8]}"
        label = (name or "").strip()
        if not label:
            parts = command.split()
            label = parts[0] if parts else "task"
        t = {"task_id": task_id, "chat_id": chat_id,
             "name": label[:60],
             "command": command, "status": "running", "exit_code": None,
             "started_at": time.time(), "duration_ms": None,
             "tail": "", "proc": None, "task": None}
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=self.jail.root, env=_minimal_env(self.jail.root),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, FileNotFoundError) as e:
            return {"error": f"не удалось запустить: {e}"}
        t["proc"] = proc
        self._tasks[task_id] = t

        await self.history.append(chat_id, "bg_start", {
            "task_id": task_id, "command": command, "name": t["name"]})
        t["task"] = asyncio.create_task(self._pump(t))
        return {"task_id": task_id, "status": "running", "command": command,
                "note": "фон: статус — background_status, ожидание — "
                        "background_wait", "bg": True}

    async def _pump(self, t: dict) -> None:
        proc: asyncio.subprocess.Process = t["proc"]

        async def _drain(stream, label: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace")
                t["tail"] = (t["tail"] + text)[-TAIL_BYTES:]
                if self.publish is not None:
                    self.publish(t["chat_id"], {
                        "live": "bg_output", "task_id": t["task_id"],
                        "delta": text, "status": t["status"]})

        try:
            await asyncio.gather(_drain(proc.stdout, "out"),
                                 _drain(proc.stderr, "err"))
            await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            t["status"] = "killed"
            raise
        t["duration_ms"] = round((time.time() - t["started_at"]) * 1000)
        t["exit_code"] = proc.returncode
        t["status"] = "done"
        await self.history.append(t["chat_id"], "bg_end", {
            "task_id": t["task_id"], "exit_code": proc.returncode,
            "duration_ms": t["duration_ms"], "command": t["command"],
            "tail": t["tail"][-800:]})
        await self.history.append(t["chat_id"], "notification", {
            "kind": "bg_done",
            "text": f"Фоновая задача {t['task_id']} завершена: "
                    f"{t['command'][:80]} — exit {proc.returncode}",
            "sound": "task_done"})
        if self.notify_hook is not None:
            try:
                await self.notify_hook(
                    f"Фоновая задача {t['task_id']} завершена "
                    f"(exit {proc.returncode})", t["chat_id"])
            except Exception:  # noqa: BLE001 — hooks never break the task
                pass

    async def wait(self, task_id: str, seconds: float = 30.0) -> dict:
        t = self._tasks.get(task_id)
        if not t:
            return {"error": f"задача не найдена: {task_id}"}
        try:
            seconds = max(0.1, min(float(seconds or 30.0), 3600.0))
        except (TypeError, ValueError):
            seconds = 30.0
        if t["status"] == "running" and t["task"] is not None:
            try:
                await asyncio.wait_for(asyncio.shield(t["task"]), timeout=seconds)
            except asyncio.TimeoutError:
                return {"task_id": task_id, "status": "running",
                        "note": f"ещё выполняется (ждал {seconds:g} сек) — "
                                f"продолжай опрос или работай дальше",
                        "tail": t["tail"][-1500:], "bg": True}
        brief = self.get(task_id)
        brief["result"] = t["tail"][-4000:]
        return brief

    async def cancel(self, task_id: str) -> dict:
        t = self._tasks.get(task_id)
        if not t:
            return {"error": f"задача не найдена: {task_id}"}
        if t["task"] is not None and not t["task"].done():
            t["task"].cancel()
            try:
                await t["task"]
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            t["status"] = "killed"
            await self.history.append(t["chat_id"], "bg_end", {
                "task_id": task_id, "exit_code": None,
                "duration_ms": round((time.time() - t["started_at"]) * 1000),
                "command": t["command"], "tail": t["tail"][-800:],
                "killed": True})
        return self.get(task_id)

    async def shutdown(self) -> None:
        for t in list(self._tasks.values()):
            if t["task"] is not None and not t["task"].done():
                t["task"].cancel()


# ----------------------------------------------------------------- registration

def register_background_tools(registry, tasks: BackgroundTasks) -> None:

    async def _run(chat_id: str = "", command: str = "", name: str = "") -> dict:
        return await tasks.start(chat_id, command, name)

    async def _status(chat_id: str = "", task_id: str = "") -> dict:
        t = tasks.get(task_id)
        if not t:
            return {"error": f"задача не найдена: {task_id}",
                    "running": tasks.list(chat_id)}
        return t

    async def _wait(chat_id: str = "", task_id: str = "",
                    seconds: float = 30.0) -> dict:
        return await tasks.wait(task_id, seconds)

    registry.register(ToolSpec(
        "background_run", "Запустить ДОЛГУЮ shell-команду в фоне (сборка, "
                          "тесты, watch-процессы). Вернёт task_id сразу; "
                          "живой вывод видит юзер в терминале, тебе — через "
                          "background_status/background_wait.",
        {"type": "object", "properties": {
            "command": {"type": "string"}, "name": {"type": "string"}},
         "required": ["command"], "additionalProperties": False}),
        _run, perm_class="exec", default_timeout=15, needs_chat=True)

    registry.register(ToolSpec(
        "background_status", "Статус фоновой задачи (running/done + хвост "
                             "вывода). Без task_id — список задач этого чата.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"}},
         "additionalProperties": False}),
        _status, perm_class="read", default_timeout=10, needs_chat=True)

    registry.register(ToolSpec(
        "background_wait", "Ждать завершения фоновой задачи до N секунд. "
                           "По завершении вернёт exit-код и хвост вывода; "
                           "не успела — статус running, продолжай опрос.",
        {"type": "object", "properties": {
            "task_id": {"type": "string"},
            "seconds": {"type": "number", "minimum": 1, "maximum": 3600}},
         "required": ["task_id"], "additionalProperties": False},
        accepts_timeout=True),
        _wait, perm_class="read", default_timeout=45, needs_chat=True)
