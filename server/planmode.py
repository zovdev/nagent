"""Plan mode (§5 roadmap): a SEPARATE read-only process.

The flow:
1. The user sends a message with plan mode ON. The turn runs read-only
   (read-class tools only) with the PLAN MODE system prompt below.
2. The final text IS the plan -> `plan_submitted` event (user-visible card).
3. The user can EDIT the plan text before approving (it is just text).
4. Approve -> the exploration events (everything between the user's task
   message and the plan) are MASKed out of the model context, and a
   `plan_approved` artifact (plan + checklist) lands IN the context. Only
   the final plan ever reaches the main context — that is the point.
5. The checklist tracks progress (EDIT events on the artifact — append-only).

Reject -> `plan_rejected`; the exploration stays masked too (it was read-only
noise either way), nothing enters the context.
"""

from __future__ import annotations

import re

from . import events as ev

PLAN_SYSTEM_PROMPT = """Ты в режиме планирования (PLAN MODE). Задача — исследовать и составить исполняемый план, НЕ внося изменений.
Правила: только чтение (read/glob/grep/websearch); любые изменения запрещены. Сначала собери факты — найди релевантные файлы, функции, зависимости, ограничения; не предполагай — проверяй. Если данных не хватает — собери конкретные вопросы юзеру в конец плана.
Структура ответа: 1) Цель — одним абзацем. 2) Что уже есть — найденные файлы/модули с путями. 3) Шаги — нумерованный список; для каждого: что делаем, какие файлы затрагиваем, риски. 4) Проверка — как убедиться, что работает (тесты/критерии). 5) Открытые вопросы.
Каждый шаг атомарный: один шаг = один осмысленный коммит. Не начинай выполнение — финал вывода и есть план."""

_STEP_LINE = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")


def parse_checklist(plan_text: str, limit: int = 30) -> list[dict]:
    """Numbered lines of the plan -> checklist items. Bullets are NOT steps:
    they may belong to «Открытые вопросы» or other sections."""
    items: list[dict] = []
    for raw in (plan_text or "").splitlines():
        m = _STEP_LINE.match(raw)
        if not m:
            continue
        text = m.group(1).strip()
        if len(items) >= limit or not text:
            continue
        items.append({"text": text[:400], "done": False})
    return items


def plan_tools(registry) -> list[dict]:
    """Read-only toolset for a plan turn (§5: read/glob/grep only)."""
    tools = []
    for name in registry.names():
        entry = registry.entry(name)
        if (entry is not None and not entry.deferred
                and entry.perm_class == "read"):
            tools.append(entry.spec.to_openai())
    return tools


async def approve_plan(store, chat_id: str, plan_event: dict,
                       plan_text: str | None = None) -> dict:
    """Mask the exploration, persist the approved plan artifact.
    Returns the plan_approved event."""
    text = (plan_text if plan_text is not None
            else (plan_event.get("data") or {}).get("plan_text", ""))
    span = (plan_event.get("data") or {}).get("span") or []

    # the exploration (tool calls, results, think blocks, the raw plan text
    # itself) leaves the model context — the artifact replaces it wholesale
    if span:
        await store.append(chat_id, ev.MASK, {
            "targets": span, "reason": "plan-mode",
            "note": "исследование план-режима скрыто из контекста модели; "
                    "в контексте — только одобренный план"})
    return await store.append(chat_id, ev.PLAN_APPROVED, {
        "plan_text": text,
        "checklist": parse_checklist(text),
        "plan_event_id": plan_event.get("id")})


async def reject_plan(store, chat_id: str, plan_event: dict) -> dict:
    span = (plan_event.get("data") or {}).get("span") or []
    if span:
        await store.append(chat_id, ev.MASK, {
            "targets": span, "reason": "plan-mode",
            "note": "план отклонён; исследование скрыто из контекста модели"})
    return await store.append(chat_id, ev.PLAN_REJECTED, {
        "plan_event_id": plan_event.get("id")})


def render_plan_for_model(data: dict) -> str:
    """plan_approved event -> the user-role message the model sees."""
    checklist = data.get("checklist") or []
    lines = ["Одобренный юзером план (выполняй по шагам; статусы отмечает юзер):",
             data.get("plan_text", "")]
    if checklist:
        lines.append("")
        lines.append("Чеклист:")
        for i, item in enumerate(checklist, 1):
            mark = "x" if item.get("done") else " "
            lines.append(f"  [{mark}] {i}. {item.get('text', '')}")
    return "\n".join(lines)
