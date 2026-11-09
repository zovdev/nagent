"""Skills (§6): the SkillSearch pattern — the model sees names + short
descriptions only; the body is loaded on demand, exactly like ToolSearch.

SKILL.md-compatible format (import/export):
    ---
    name: my-skill
    description: One line about what it does.
    ---
    Markdown body — instructions the model reads after skill_load.

Storage: the scope's settings DB (solo/team: main.db; accounts: <user>.db —
«SKILL.md-совместимый формат, юзер-БД» §6). Two meta-tools registered in the
core tool registry:
  skill_search(query)  -> [{name, description}]
  skill_load(name)     -> {name, description, body}
"""

from __future__ import annotations

import re

from .providers import ToolSpec
from .srvlog import LOGS


def parse_skill_md(text: str) -> dict | None:
    """YAML frontmatter (--- delimited) + markdown body. Lenient: a bare
    `# name` heading + following paragraph works as description fallback."""
    text = text.strip()
    name = description = None
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            fm = parts[1].strip()
            body = parts[2].strip()
            for line in fm.splitlines():
                m = re.match(r"^(\w[\w-]*)\s*:\s*(.+?)\s*$", line)
                if not m:
                    continue
                key, value = m.group(1).lower(), m.group(2).strip().strip('"\'')
                if key == "name":
                    name = value
                elif key == "description":
                    description = value
    if not name:
        m = re.match(r"^#\s+(.+?)\s*$", body.splitlines()[0]) \
            if body.splitlines() else None
        if m:
            name = m.group(1).strip()
    if not name:
        return None
    if description is None:
        for line in body.splitlines()[1:]:
            if line.strip():
                description = line.strip().lstrip("#> ").strip()
                break
    return {"name": name, "description": description or "", "body": body}


def render_skill_md(skill: dict) -> str:
    return (f"---\nname: {skill['name']}\ndescription: {skill.get('description') or ''}\n"
            f"---\n\n{skill.get('body') or ''}\n")


class SkillStore:
    def __init__(self, settings_store):
        self.store = settings_store

    async def list(self) -> list[dict]:
        return await self.store.list_skills()

    async def get(self, name: str) -> dict | None:
        return await self.store.get_skill(name)

    async def search(self, query: str) -> list[dict]:
        q = (query or "").strip().lower()
        tokens = [t for t in q.split() if len(t) >= 2]
        out = []
        for s in await self.list():
            hay = f"{s['name']} {s.get('description') or ''}".lower()
            if not tokens or any(t in hay for t in tokens):
                out.append({"name": s["name"],
                            "description": s.get("description") or ""})
        out.sort(key=lambda s: s["name"])
        return out[:25]

    async def upsert(self, name: str, description: str, body: str) -> dict:
        LOGS.log_event("skills", f"скилл сохранён: {name}")
        return await self.store.upsert_skill(name, description or "", body or "")

    async def delete(self, name: str) -> None:
        await self.store.delete_skill(name)
        LOGS.log_event("skills", f"скилл удалён: {name}")

    async def import_md(self, text: str) -> dict | None:
        parsed = parse_skill_md(text)
        if not parsed:
            return None
        return await self.upsert(parsed["name"], parsed["description"],
                                 parsed["body"])


def register_skill_tools(registry, skills: SkillStore) -> None:
    """`skill_search` / `skill_load` — the §6 SkillSearch meta-tools."""

    async def _skill_search(query: str) -> dict:
        found = await skills.search(query)
        if not found:
            return {"skills": [], "note": "ничего не найдено — попробуйте "
                                          "другие ключевые слова"}
        return {"skills": found,
                "note": "skill_load(name) вернёт полное тело скила"}

    async def _skill_load(name: str) -> dict:
        skill = await skills.get(name)
        if not skill:
            return {"error": f"скилл не найден: {name}"}
        return {"name": skill["name"], "description": skill["description"],
                "body": skill["body"]}

    registry.register(
        ToolSpec("skill_search",
                 "Поиск по скилам (SKILL.md-библиотека): возвращает названия и "
                 "краткие описания. Тело скила подгружается через skill_load.",
                 {"type": "object",
                  "properties": {"query": {"type": "string",
                                           "description": "ключевые слова"}},
                  "required": ["query"]}),
        _skill_search, perm_class="read", default_timeout=15)

    registry.register(
        ToolSpec("skill_load",
                 "Загружает полное тело скила по имени (инструкции к выполнению).",
                 {"type": "object",
                  "properties": {"name": {"type": "string",
                                          "description": "имя скила из skill_search"}},
                  "required": ["name"]}),
        _skill_load, perm_class="read", default_timeout=15)
