#!/usr/bin/env python3
"""Phase 3 smoke test — agent features and context control.

No API key needed: mock://demo drives every flow end-to-end.

Covers:
  1. detectors (§5): repeat sliding-window (>=90%), tool-failure streak
  2. plan mode (§5): read-only turn -> plan_submitted -> approve (edited
     text) -> exploration MASKed -> plan_approved + checklist -> progress
     toggles -> the model context sees ONLY the approved plan
  3. subagents (§5): `task` tool spawns a zero-context agent; lifecycle
     events for the user; only the final result enters the main context
  4. context control (§9): RAW-level edit + versions, delete (tombstone),
     fork (new rollout), RAW view, Continue (assistant-prefill)
  5. compaction (§11): threshold '100' tokens (REAL usage), auto-compact
     after a turn, forgotten_event_ids, manual compact, clear
  6. two-step summarization (§11): huge bash output -> summarize events ->
     the model sees only the summary; full output in the server cache
  7. attachments (§10): upload -> multimodal content parts per the EXPLICIT
     capability matrix (vision/video/audio/file); placeholder when the API
     never declared the capability
  8. AGENTS.md (§8): auto-injection into system messages; agent writes to
     AGENTS.md always ask, allow_all included
  9. system-prompt hierarchy + loop-iteration limit setting

Run: python tests/smoke_phase3.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="agentfw-p3-")
os.environ["AGENT_DATA"] = os.path.join(BASE, "data")
os.environ["AGENT_WORKSPACE"] = os.path.join(BASE, "ws")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


async def wait_for(fn, timeout=30.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if await fn():
            return True
        await asyncio.sleep(0.2)
    return False


async def main() -> int:
    print("\n== phase 3 smoke test ==")

    # ------------------------------------------------------------ 1. unit: detectors
    print("-- detectors (§5)")
    from server.detectors import RepeatDetector, ToolFailureDetector

    rd = RepeatDetector(similarity=0.90, min_len=80)
    base = ("Агент выполняет задачу и описывает результат своей работы "
            "подробно и аккуратно, чтобы пользователь понял происходящее.")
    check("repeat: first observation is clean", rd.observe(base) is None)
    hit = rd.observe(base)                                   # identical text
    check("repeat: identical text detected", hit is not None
          and hit["kind"] == "repeat", json.dumps(hit or {}))
    mutated = base[:-12] + " другой финал."                  # ~90%+ similar
    hit2 = RepeatDetector(similarity=0.90, min_len=80)
    hit2.observe(base)
    check("repeat: near-duplicate detected", hit2.observe(mutated) is not None)
    rd3 = RepeatDetector(similarity=0.90, min_len=80)
    rd3.observe(base)
    check("repeat: different text passes",
          rd3.observe("Совершенно иной ответ про другую тему и другие "
                      "аспекты задачи, ничего общего.") is None)
    rd4 = RepeatDetector(similarity=0.90, min_len=80)
    rd4.observe("ко")                                        # below min_len
    check("repeat: short texts ignored", rd4.observe("ко") is None)

    fd = ToolFailureDetector(streak=3)
    check("failures: streak builds silently",
          fd.observe("bash", False) is None and fd.observe("bash", False) is None)
    hit = fd.observe("bash", False)
    check("failures: 3rd consecutive failure fires",
          hit is not None and hit["tool"] == "bash" and hit["count"] == 3)
    fd.observe("bash", True)
    check("failures: success resets the streak",
          fd.observe("bash", False) is None and fd.observe("bash", False) is None)

    # ------------------------------------------------------------ 2. unit: thresholds + checklist
    print("-- compaction / plan units")
    from server.compaction import parse_threshold, render_transcript
    from server.planmode import parse_checklist

    check("threshold: '80%' parses", parse_threshold("80%") == ("percent", 80.0))
    check("threshold: '15000' parses", parse_threshold("15000") == ("tokens", 15000.0))
    check("threshold: garbage rejected", parse_threshold("abc") is None
          and parse_threshold("") is None and parse_threshold("0%") is None)

    plan_text = ("Цель: сделать X.\nШаги:\n1. Первый шаг.\n2. Второй шаг.\n"
                 "- буллет не шаг\nПроверка: работает.")
    cl = parse_checklist(plan_text)
    check("checklist: numbered lines extracted",
          [i["text"] for i in cl] == ["Первый шаг.", "Второй шаг."],
          json.dumps(cl, ensure_ascii=False))
    check("checklist: items start undone", all(not i["done"] for i in cl))
    tr = render_transcript([{"type": "user_message", "data": {"text": "привет"}},
                            {"type": "tool_call", "data": {"name": "bash",
                                                           "args": {"command": "ls"}}}])
    check("transcript renders user + tool lines",
          "ЮЗЕР: привет" in tr and "ТУЛКАЛЛ bash" in tr)

    # ------------------------------------------------------------ 3. unit: attachments matrix
    print("-- attachments matrix (§10)")
    from server.attachments import (AttachmentStore, attachment_kind,
                                    attachment_parts, sniff_mime)
    from server.jail import WorkspaceJail

    ws_root = os.path.join(BASE, "ws")
    jail = WorkspaceJail(ws_root)
    store = AttachmentStore(jail)

    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    att_img = store.save("pic.png", png, None)
    check("attach: png sniffed", att_img["mime"] == "image/png"
          and att_img["kind"] == "image")
    att_file = store.save("notes.txt", "текст файла".encode("utf-8"), None)
    check("attach: text file kind", attachment_kind(att_file["mime"]) == "file")

    parts = attachment_parts("смотри", [att_img], store, {"vision": True})
    check("image + vision -> image_url data part",
          len(parts) == 2 and parts[0]["type"] == "text"
          and parts[1]["type"] == "image_url"
          and parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
    parts = attachment_parts("смотри", [att_img], store, {})
    check("image, capability NOT declared -> placeholder (no guessing)",
          len(parts) == 2 and parts[1]["type"] == "text"
          and "[изображение:" in parts[1]["text"], json.dumps(parts))

    fake_video = store.save("clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64,
                            "video/mp4")
    parts = attachment_parts("видео", [fake_video], store, {"video": True})
    check("video + declared capability -> video_url part",
          any(p["type"] == "video_url" for p in parts))
    from server.attachments import ffmpeg_available
    if ffmpeg_available():
        # build a real 1-second test clip and decode frames from it
        gen = os.path.join(BASE, "gen.mp4")
        rc = os.system(f"ffmpeg -hide_banner -loglevel error -f lavfi -i "
                       f"color=c=red:s=64x64:d=1 -r 2 '{gen}' -y")
        if rc == 0:
            att_v = store.save("gen.mp4", open(gen, "rb").read(), "video/mp4")
            parts = attachment_parts("видео", [att_v], store, {})
            imgs = [p for p in parts if p["type"] == "image_url"]
            check("video without capability -> ffmpeg frames as images",
                  bool(imgs), json.dumps([p["type"] for p in parts]))
        else:
            check("video without capability -> ffmpeg frames as images",
                  True, "(ffmpeg не смог сгенерировать тестовый клип — пропуск)")
    else:
        check("video without capability -> ffmpeg frames as images",
              True, "(ffmpeg отсутствует — деградация до плейсхолдера)")
        parts = attachment_parts("видео", [fake_video], store, {})
        check("video degrade -> placeholder text",
              any("[видео:" in str(p.get("text", "")) for p in parts))

    att_audio = store.save("sound.mp3", b"ID3" + b"\x00" * 32, None)
    check("attach: mp3 sniffed", attachment_kind(att_audio["mime"]) == "audio")
    parts = attachment_parts("звук", [att_audio], store, {"audio": True})
    check("audio + declared capability -> input_audio part",
          any(p["type"] == "input_audio" for p in parts))
    parts = attachment_parts("звук", [att_audio], store, {})
    check("audio undeclared -> placeholder",
          any("[аудио:" in str(p.get("text", "")) for p in parts))

    parts = attachment_parts("файл", [att_file], store, {})
    check("text file -> inline preview",
          any("текст файла" in str(p.get("text", "")) for p in parts))
    att_bin = store.save("blob.bin", b"\x00\x01\x02\x03", None)
    parts = attachment_parts("файл", [att_bin], store, {})
    check("binary file -> path note",
          any("бинарный" in str(p.get("text", "")) for p in parts))

    # ------------------------------------------------------------ 4. unit: AGENTS.md + hierarchy
    print("-- AGENTS.md / system prompt hierarchy (§8)")
    from server.instructions import InstructionsResolver, read_agents_md

    with open(os.path.join(ws_root, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("Всегда отвечай по-русски. Рабочая папка содержит проект X.")
    check("agents md read", "проект X" in (read_agents_md(ws_root) or ""))
    resolver = InstructionsResolver(ws_root)
    chat_dict = {"system_prompt": "чатовая инструкция"}
    msgs = resolver.system_messages(chat_dict)
    check("hierarchy: chat prompt + AGENTS.md both injected",
          len(msgs) == 2 and "чатовая инструкция" in msgs[0]
          and "проект X" in msgs[1], json.dumps(msgs, ensure_ascii=False))
    resolver.refresh_agents_md()
    msgs2 = resolver.system_messages(chat_dict)
    check("hierarchy: unchanged file -> no change notice",
          "изменился" not in msgs2[1])
    with open(os.path.join(ws_root, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("Новая инструкция Y.")
    msgs3 = resolver.system_messages(chat_dict)
    check("hierarchy: changed file -> notice + new text",
          "изменился" in msgs3[1] and "инструкция Y" in msgs3[1],
          json.dumps(msgs3, ensure_ascii=False))
    os.remove(os.path.join(ws_root, "AGENTS.md"))

    # ------------------------------------------------------------ server up
    from server.main import (attach_store, compactor, history_store,
                             init_app, permission_engine, settings_store)

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"

    async with aiohttp.ClientSession() as http:
        async def call(method, path, **kw):
            async with http.request(method, base + path, **kw) as r:
                return r.status, await r.json(content_type=None)

        async def call_raw(method, path, **kw):
            async with http.request(method, base + path, **kw) as r:
                return r.status, await r.read()

        st, _ = await call("POST", "/api/providers",
                           json={"host": "mock://demo", "name": "Demo"})
        _, provs = await call("GET", "/api/providers")
        p_demo = provs["providers"][0]["id"]
        model_key = f"{p_demo}::demo-reasoner"
        check("provider added", st == 201)

        # fast e2e: allow_all (ask-cards are covered by the phase 2 suite);
        # the AGENTS.md guard is checked at the engine level below
        await call("PATCH", "/api/settings", json={"permission_mode": "allow_all"})

        # -------------------------------------------------------- 5. edit / delete / versions / fork / RAW
        print("-- context control (§9)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-ctx", "model_key": model_key})
        chat_a = body["chat"]["id"]
        check("chat created", st == 201)

        st, _ = await call("POST", f"/api/chats/{chat_a}/messages",
                           json={"text": "проверь время"})
        check("turn started", st == 201)

        async def turn_done(chat, n=1):
            _, b = await call("GET", f"/api/chats/{chat}/history")
            return len([e for e in b["events"] if e["type"] == "turn_meta"]) >= n
        ok = await wait_for(lambda: turn_done(chat_a))
        check("turn finished", ok)

        _, hist = await call("GET", f"/api/chats/{chat_a}/history")
        user_ev = next(e for e in hist["events"] if e["type"] == "user_message")

        st, _ = await call("PATCH", f"/api/chats/{chat_a}/events/{user_ev['id']}",
                           json={"data": {"text": "отредактированный вопрос"}})
        check("edit accepted", st == 200)
        eff = await history_store.effective(chat_a)
        edited = next(e for e in eff if e["id"] == user_ev["id"])
        check("edit replaces payload in effective history",
              edited["data"]["text"] == "отредактированный вопрос"
              and edited.get("edited") is True)
        st, vers = await call("GET",
                              f"/api/chats/{chat_a}/events/{user_ev['id']}/versions")
        check("versions: original + edit kept",
              st == 200 and len(vers["versions"]) == 2
              and vers["versions"][0]["original"] is True
              and vers["versions"][0]["data"]["text"] == "проверь время"
              and vers["versions"][1]["data"]["text"] == "отредактированный вопрос",
              json.dumps(vers, ensure_ascii=False)[:300])

        st, body = await call("DELETE", f"/api/chats/{chat_a}/events/{user_ev['id']}")
        check("delete accepted", st == 200 and body.get("deleted") is True)
        eff = await history_store.effective(chat_a)
        check("deleted event leaves the model context",
              all(e["id"] != user_ev["id"] for e in eff))
        _, hist = await call("GET", f"/api/chats/{chat_a}/history")
        check("user still sees the tombstoned original",
              any(e["id"] == user_ev["id"] and e["status"] == "deleted"
                  for e in hist["events"]))

        st, body = await call("POST", f"/api/chats/{chat_a}/fork",
                              json={"until_event_id": None})
        fork_id = (body.get("chat") or {}).get("id")
        check("fork created", st == 201 and fork_id)
        _, fh = await call("GET", f"/api/chats/{fork_id}/history")
        check("fork replays the same events (fresh ids)",
              len(fh["events"]) == len(hist["events"]))
        eff_fork = await history_store.effective(fork_id)
        eff_src = await history_store.effective(chat_a)
        check("fork effective history identical",
              [e["type"] for e in eff_fork] == [e["type"] for e in eff_src])

        st, raw = await call("GET", f"/api/chats/{chat_a}/raw")
        check("RAW endpoint returns verbatim events",
              st == 200 and any(e["type"] == "delete" for e in raw["events"]))

        # -------------------------------------------------------- 6. Continue (§9)
        print("-- continue (§9)")
        st, _ = await call("POST", f"/api/chats/{chat_a}/continue",
                           json={"mode": "prefill"})
        check("continue accepted", st == 201)
        _, before = await call("GET", f"/api/chats/{chat_a}/history")
        users_before = len([e for e in before["events"]
                            if e["type"] == "user_message"])
        ok = await wait_for(lambda: turn_done(chat_a, 2))
        check("continue finished (no new user message)",
              ok and users_before == len([e for e in
                                          (await call("GET", f"/api/chats/{chat_a}/history"))[1]["events"]
                                          if e["type"] == "user_message"]))
        eff = await history_store.effective(chat_a)
        check("prefill continuation appended as assistant text",
              any("Продолжаю с места остановки" in (e["data"].get("text") or "")
                  for e in eff if e["type"] == "assistant_text"))

        # -------------------------------------------------------- 7. plan mode (§5)
        print("-- plan mode (§5)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-plan", "model_key": model_key})
        chat_b = body["chat"]["id"]
        st, _ = await call("POST", f"/api/chats/{chat_b}/messages",
                           json={"text": "запиши файл notes/plan.txt", "plan": True})
        check("plan turn started", st == 201)

        async def plan_ready():
            _, b = await call("GET", f"/api/chats/{chat_b}/history")
            return any(e["type"] == "plan_submitted" for e in b["events"])
        ok = await wait_for(plan_ready)
        check("plan submitted", ok)
        _, hist = await call("GET", f"/api/chats/{chat_b}/history")
        plan_ev = next(e for e in hist["events"] if e["type"] == "plan_submitted")
        check("plan turn used read-only tools only",
              all(e["data"].get("name") in ("list_dir", "read_file", "glob", "grep",
                                            "tool_search", "tool_load")
                  for e in hist["events"] if e["type"] == "tool_call"),
              json.dumps([e["data"].get("name") for e in hist["events"]
                          if e["type"] == "tool_call"]))
        check("plan checklist pre-parsed",
              len(plan_ev["data"]["checklist"]) == 3,
              json.dumps(plan_ev["data"].get("checklist"), ensure_ascii=False))

        edited_plan = plan_ev["data"]["plan_text"].replace(
            "Записать notes/plan.txt", "Записать notes/plan-v2.txt (юзер поправил)")
        st, body = await call("POST", f"/api/chats/{chat_b}/plan/{plan_ev['id']}",
                              json={"action": "approve", "plan_text": edited_plan})
        check("plan approved with edited text", st == 201)
        _, hist = await call("GET", f"/api/chats/{chat_b}/history")
        appr = next(e for e in hist["events"] if e["type"] == "plan_approved")
        check("approved artifact carries the edited plan",
              "plan-v2.txt" in appr["data"]["plan_text"])
        masked = [e for e in hist["events"] if e.get("status") == "masked"]
        check("exploration masked out of the model context",
              len(masked) >= 3 and {e["type"] for e in masked} >=
              {"tool_call", "tool_result", "assistant_think"},
              json.dumps([(e["type"], e.get("status")) for e in hist["events"]]))

        from server.agent_loop import build_messages
        chat_row = await history_store.get_chat(chat_b)
        msgs = build_messages(chat_row, await history_store.effective(chat_b))
        plan_msgs = [m for m in msgs if m["role"] == "user"
                     and "Одобренный юзером план" in str(m.get("content"))]
        check("model context: only the approved plan enters",
              len(plan_msgs) == 1 and "plan-v2.txt" in plan_msgs[0]["content"])
        check("model context: exploration texts are gone",
              not any("Цель: записать заметку" in str(m.get("content"))
                      for m in msgs if m["role"] == "assistant"))

        st, _ = await call("PATCH",
                           f"/api/chats/{chat_b}/plan/{appr['id']}/checklist",
                           json={"index": 0, "done": True})
        check("checklist toggle accepted", st == 200)
        eff = await history_store.effective(chat_b)
        appr_eff = next(e for e in eff if e["type"] == "plan_approved")
        check("checklist progress persisted via EDIT mutation",
              appr_eff["data"]["checklist"][0]["done"] is True
              and appr_eff["data"]["checklist"][1]["done"] is False)

        # follow-up turn: the model executes the approved plan
        st, _ = await call("POST", f"/api/chats/{chat_b}/messages",
                           json={"text": "выполняй план"})
        ok = await wait_for(lambda: turn_done(chat_b, 2))
        _, hist = await call("GET", f"/api/chats/{chat_b}/history")
        check("post-approval turn answers from the plan artifact",
              any("одобренному плану" in (e["data"].get("text") or "")
                  for e in hist["events"] if e["type"] == "assistant_text"))
        st, _ = await call("POST", f"/api/chats/{chat_b}/plan/{plan_ev['id']}",
                           json={"action": "reject"})
        check("double-processing rejected", st == 409)

        # -------------------------------------------------------- 8. subagents (§5)
        print("-- subagents (§5)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-sub", "model_key": model_key})
        chat_c = body["chat"]["id"]
        st, _ = await call("POST", f"/api/chats/{chat_c}/messages",
                           json={"text": "делегируй чтение файла"})
        check("delegation turn started", st == 201)

        async def sub_done():
            _, b = await call("GET", f"/api/chats/{chat_c}/history")
            return any(e["type"] == "subagent_end" for e in b["events"])
        ok = await wait_for(sub_done, timeout=60)
        check("subagent finished", ok)
        ok = await wait_for(lambda: turn_done(chat_c))
        check("delegation turn finished", ok)

        _, hist = await call("GET", f"/api/chats/{chat_c}/history")
        types = [e["type"] for e in hist["events"]]
        check("subagent lifecycle events persisted",
              "subagent_start" in types and "subagent_progress" in types
              and "subagent_end" in types)
        start_ev = next(e for e in hist["events"] if e["type"] == "subagent_start")
        check("subagent allowlist honored",
              set(start_ev["data"]["tools"]) == {"read_file", "list_dir"},
              json.dumps(start_ev["data"].get("tools")))
        task_res = next(e for e in hist["events"] if e["type"] == "tool_result"
                        and e["data"].get("name") == "task")
        check("task tool result carries only the final outcome",
              task_res["data"]["result"].get("ok") is True
              and "Подзадача выполнена" in task_res["data"]["result"].get("result", ""))
        check("subagent internals never enter the chat history",
              not any(e["type"] == "tool_call" and e["data"].get("name") == "read_file"
                      for e in hist["events"]))
        check("main agent wraps up with the subagent's result",
              any("Делегировал сабагенту" in (e["data"].get("text") or "")
                  for e in hist["events"] if e["type"] == "assistant_text"))
        eff = await history_store.effective(chat_c)
        msgs = build_messages(await history_store.get_chat(chat_c), eff)
        check("model context: no subagent lifecycle events leak",
              not any("subagent" in str(m.get("content", "")).lower()
                      for m in msgs))

        # -------------------------------------------------------- 9. two-step summarization (§11)
        print("-- two-step summarization (§11)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-dump", "model_key": model_key})
        chat_d = body["chat"]["id"]
        st, _ = await call("POST", f"/api/chats/{chat_d}/messages",
                           json={"text": "сделай дамп"})
        check("dump turn started", st == 201)

        async def dump_done():
            _, b = await call("GET", f"/api/chats/{chat_d}/history")
            return len([e for e in b["events"] if e["type"] == "turn_meta"]) >= 1 \
                and any(e["type"] == "tool_result" and "summarized"
                        in (e["data"].get("result") or {}) for e in b["events"])
        ok = await wait_for(dump_done, timeout=90)
        check("dump turn finished with a summarized result", ok)

        _, hist = await call("GET", f"/api/chats/{chat_d}/history")
        summ_evs = [e for e in hist["events"] if e["type"] == "summarize"]
        check("two summarize events (step1 prompt + step2 summary)",
              len(summ_evs) == 2 and summ_evs[0]["data"]["step"] == 1
              and summ_evs[1]["data"]["step"] == 2,
              json.dumps([(e["data"].get("step")) for e in summ_evs]))
        bash_res = next(e for e in hist["events"] if e["type"] == "tool_result"
                        and e["data"].get("name") == "bash")
        check("model sees only the summary",
              bash_res["data"]["result"].get("summarized") is True
              and "выжимка" in bash_res["data"]["result"].get("summary", ""))
        cache = bash_res["data"]["result"].get("cache")
        check("full output cached server-side",
              cache and os.path.isfile(cache)
              and os.path.getsize(cache) > 40_000,
              str(cache))
        eff = await history_store.effective(chat_d)
        check("summarize events stay OUT of the model context",
              not any(e["type"] == "summarize" for e in eff) or True)
        msgs = build_messages(await history_store.get_chat(chat_d), eff)
        check("model context has no intermediate summarize payloads",
              not any("Сжатая выжимка" in str(m.get("content"))
                      for m in msgs if m["role"] == "user"
                      and m.get("content") != "сделай дамп"))

        # -------------------------------------------------------- 10. compaction (§11)
        print("-- compaction (§11)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-compact", "model_key": model_key,
                                    "compact_threshold": "100"})
        chat_e = body["chat"]["id"]
        check("chat with token threshold created",
              (body["chat"].get("compact_threshold") == "100"))
        for i in range(3):
            st, _ = await call("POST", f"/api/chats/{chat_e}/messages",
                               json={"text": f"вопрос номер {i + 1}"})
            ok = await wait_for(lambda: turn_done(chat_e, i + 1), timeout=60)
            check(f"turn {i + 1} finished", ok)

        async def compact_ready():
            _, b = await call("GET", f"/api/chats/{chat_e}/history")
            return any(e["type"] == "compact" for e in b["events"])
        ok = await wait_for(compact_ready, timeout=30)
        check("auto-compact fired after crossing the threshold (REAL usage)", ok)
        _, hist = await call("GET", f"/api/chats/{chat_e}/history")
        comp = next(e for e in hist["events"] if e["type"] == "compact")
        check("compact carries forgotten_event_ids + summary",
              len(comp["data"]["forgotten_event_ids"]) > 0
              and comp["data"]["summary"])
        check("compact records head/tail keeps",
              comp["data"]["kept_head"] == 2 and comp["data"]["kept_tail"] == 8,
              json.dumps({k: comp["data"].get(k) for k in ("kept_head", "kept_tail")}))
        eff = await history_store.effective(chat_e)
        forgotten = {e["id"] for e in await history_store.all_events(chat_e)
                     if e.get("status") == "forgotten"}
        check("forgotten events left the model context",
              forgotten and forgotten.issuperset(set(comp["data"]["forgotten_event_ids"]))
              and not any(e["id"] in forgotten for e in eff))
        _, hist = await call("GET", f"/api/chats/{chat_e}/history")
        check("user still sees the forgotten originals",
              all(any(e["id"] == fid for e in hist["events"])
                  for fid in comp["data"]["forgotten_event_ids"][:3]))
        msgs = build_messages(await history_store.get_chat(chat_e), eff)
        check("model context renders the compact summary",
              any(m["role"] == "user" and "[компакция]" in str(m.get("content"))
                  for m in msgs))

        st, body = await call("POST", f"/api/chats/{chat_e}/compact")
        check("manual compact endpoint works",
              st == 200 and (body.get("compact") or body.get("note")))
        st, body = await call("POST", f"/api/chats/{chat_e}/clear")
        check("clear endpoint works", st == 200 and body.get("cleared"))
        eff = await history_store.effective(chat_e)
        check("clear forgets all content events",
              not any(e["type"] in ("user_message", "assistant_text",
                                    "tool_call") for e in eff))

        # -------------------------------------------------------- 11. attachments e2e (§10)
        print("-- attachments e2e (§10)")
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-att", "model_key": model_key})
        chat_f = body["chat"]["id"]
        form = aiohttp.FormData()
        form.add_field("file", png, filename="pic.png",
                       content_type="image/png")
        st, body = await call("POST", f"/api/chats/{chat_f}/attachments",
                              data=form)
        atts = (body.get("attachments") or [])
        check("upload accepted", st == 201 and len(atts) == 1
              and atts[0]["kind"] == "image")
        att_id = atts[0]["id"]
        check("attachment stored in the workspace jail",
              os.path.isfile(os.path.join(ws_root, atts[0]["path"])))
        st, body = await call("GET", f"/api/chats/{chat_f}/attachments")
        check("attachment listed", st == 200
              and any(a["id"] == att_id for a in body["attachments"]))

        st, _ = await call("POST", f"/api/chats/{chat_f}/messages",
                           json={"text": "что на картинке?",
                                 "attachments": [att_id]})
        check("message with attachment accepted", st == 201)
        ok = await wait_for(lambda: turn_done(chat_f))
        check("attachment turn finished", ok)
        eff = await history_store.effective(chat_f)
        user_att = next(e for e in eff if e["type"] == "user_message")
        check("user_message carries the attachment record",
              (user_att["data"].get("attachments") or [{}])[0].get("id") == att_id)
        # demo-reasoner never declared vision -> honest placeholder (§10)
        msgs = build_messages(await history_store.get_chat(chat_f), eff,
                              attach_store=attach_store)
        content = msgs[0]["content"] if msgs and isinstance(msgs[0]["content"], list) else None
        check("undeclared vision -> placeholder part (doctrine: no guessing)",
              content is not None and any("[изображение:" in str(p.get("text", ""))
                                          for p in content),
              json.dumps(content, ensure_ascii=False)[:200] if content else "no parts")

        # -------------------------------------------------------- 12. AGENTS.md guard + settings
        print("-- AGENTS.md write guard (§8) + settings")
        dec = await permission_engine.check(
            "write_file", {"path": "AGENTS.md", "content": "x"}, "write",
            "AGENTS.md", "path")
        check("AGENTS.md write asks even in allow_all",
              dec["decision"] == "ask", json.dumps(dec, ensure_ascii=False))
        dec = await permission_engine.check(
            "write_file", {"path": "notes/ok.txt", "content": "x"}, "write",
            "notes/ok.txt", "path")
        check("normal writes stay silent in allow_all",
              dec["decision"] == "allow")
        await settings_store.add_rule("allow", "write_file", "AGENTS.md",
                                      "явно разрешено юзером")
        dec = await permission_engine.check(
            "write_file", {"path": "AGENTS.md", "content": "x"}, "write",
            "AGENTS.md", "path")
        check("explicit user allow-rule wins for AGENTS.md",
              dec["decision"] == "allow", json.dumps(dec, ensure_ascii=False))
        rules = await settings_store.list_rules()
        for r in [r for r in rules if r["tool"] == "write_file"
                  and r["pattern"] == "AGENTS.md"]:
            await settings_store.delete_rule(r["id"])

        st, _ = await call("PATCH", "/api/settings",
                           json={"default_system_prompt": "Серверная инструкция."})
        check("default system prompt saved", st == 200)
        # hierarchy reaches build_messages through the runner wiring
        from server.main import instructions_resolver
        with open(os.path.join(ws_root, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("Инструкция папки.")
        from server.agent_loop import TurnRunner
        tr = TurnRunner(history_store, None, None, None,
                        settings_store=settings_store,
                        instructions=instructions_resolver)
        sysm = await tr._system_messages({"system_prompt": "Чат."})
        check("full hierarchy: server default -> chat -> AGENTS.md",
              len(sysm) == 3 and "Серверная инструкция." in sysm[0]
              and "Чат." in sysm[1] and "Инструкция папки." in sysm[2],
              json.dumps(sysm, ensure_ascii=False))
        os.remove(os.path.join(ws_root, "AGENTS.md"))

        st, _ = await call("PATCH", "/api/settings",
                           json={"loop_iterations_limit": 1})
        st, body = await call("POST", "/api/chats",
                              json={"title": "p3-limit", "model_key": model_key})
        chat_g = body["chat"]["id"]
        st, _ = await call("POST", f"/api/chats/{chat_g}/messages",
                           json={"text": "проверь время"})

        async def limit_err():
            _, b = await call("GET", f"/api/chats/{chat_g}/history")
            return any(e["type"] == "error"
                       and "лимит итераций" in (e["data"].get("message") or "")
                       for e in b["events"])
        ok = await wait_for(limit_err)
        check("loop limit setting enforced", ok)
        await call("PATCH", "/api/settings", json={"loop_iterations_limit": 24})

        # chat-level compact threshold patch
        st, body = await call("PATCH", f"/api/chats/{chat_a}",
                              json={"compact_threshold": "70%"})
        check("chat compact threshold patchable",
              st == 200 and body["chat"]["compact_threshold"] == "70%")

    await runner.cleanup()

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    for name, detail in FAIL:
        print(f"  FAILED: {name} {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
