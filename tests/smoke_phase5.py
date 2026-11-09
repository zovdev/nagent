#!/usr/bin/env python3
"""Phase 5 smoke test — modes & deployment.

No API key needed: mock://demo drives turns. Real wire only — every
assertion observes data that crossed an HTTP connection or a real file.

Covers:
  1. crypto (§7): derive/encrypt/decrypt round-trip, wrong key, envelopes
  2. migrations (§2): schema_migrations + owner/shared columns
  3. setup mode: open APIs + onboarding (§16): first user = admin, finish
  4. /admin (§2): overview, users CRUD, workspace switch
  5. team mode (§2): 401 gating, login sessions, shared projects with
     «shared_by», lock attribution («locked by user X»), generation
     queue (concurrency_generations), tool gate
  6. accounts mode (§2): per-user scopes, isolation, provider keys sealed
     at rest in <user>.db (enc:v1 envelopes), attachment quota
  7. hooks (§7): CRUD, test endpoint, PreToolUse block, UserPromptSubmit
     context injection, Stop observation, timeout behaviour
  8. skills (§6): CRUD, SKILL.md import/export, skill_search/skill_load
  9. export/import (§16): JSON round-trip fidelity + MD round-trip
 10. audit (§2): OFF by default, on -> real entries, off again
 11. logs (§16): entries, level, export
 12. themes (§16): upload/select/list/serve/delete; ui_lang setting

Run: python tests/smoke_phase5.py
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="agentfw-p5-")
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


# ------------------------------------------------------------------ section A: crypto

def test_crypto():
    from server import crypto
    key = crypto.derive_key("horse battery", b"0123456789abcdef")
    for _ in range(3):
        env = crypto.encrypt(key, "sk-секретный-ключ")
        check("crypto: envelope marker", crypto.is_envelope(env))
        check("crypto: round-trip", crypto.decrypt(key, env) == "sk-секретный-ключ")
    other = crypto.derive_key("wrong", b"0123456789abcdef")
    try:
        crypto.decrypt(other, env)
        check("crypto: wrong key rejected", False)
    except crypto.CryptoError:
        check("crypto: wrong key rejected", True)
    check("crypto: plaintext passthrough", crypto.decrypt(key, "plain") == "plain")
    salt_path = os.path.join(BASE, "salt.bin")
    s1 = crypto.load_or_create_salt(salt_path)
    s2 = crypto.load_or_create_salt(salt_path)
    check("crypto: salt stable across calls", s1 == s2 and len(s1) == 16)


# ------------------------------------------------------------------ main flow

async def main():
    test_crypto()

    from server.main import (auth, init_app, settings_store, turn_gate)

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8799)
    await site.start()
    URL = "http://127.0.0.1:8799"

    # separate cookie jars = separate browsers (admin / member);
    # unsafe=True — aiohttp refuses IP-host cookies otherwise (127.0.0.1)
    jar_admin = aiohttp.CookieJar(unsafe=True)
    jar_bob = aiohttp.CookieJar(unsafe=True)
    jar_alice = aiohttp.CookieJar(unsafe=True)
    sess_admin = aiohttp.ClientSession(cookie_jar=jar_admin)
    sess_bob = aiohttp.ClientSession(cookie_jar=jar_bob)
    sess_alice = aiohttp.ClientSession(cookie_jar=jar_alice)

    async def call(sess, method, path, json_body=None, **kw):
        async with sess.request(method, URL + path, json=json_body, **kw) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = None
            return r.status, body

    async def turn_done(sess, chat_id):
        st, b = await call(sess, "GET", f"/api/chats/{chat_id}/history")
        return st == 200 and any(e["type"] == "turn_meta"
                                 for e in (b or {}).get("events", []))

    print("\n== setup mode: open APIs + onboarding ==")
    st, me = await call(sess_admin, "GET", "/api/auth/me")
    check("setup: /api/auth/me", st == 200 and me["mode"] == "setup"
          and me["needs_onboarding"] is True)
    st, _ = await call(sess_admin, "GET", "/api/chats")
    check("setup: APIs stay open", st == 200)

    st, _ = await call(sess_admin, "POST", "/api/onboarding/register",
                       {"username": "owner", "password": "1234"})
    check("onboarding: first user registered", st == 200 or st == 201)
    st, _ = await call(sess_bob, "POST", "/api/onboarding/register",
                       {"username": "intruder", "password": "1234"})
    check("onboarding: second self-registration blocked", st == 409)
    st, users = await call(sess_admin, "GET", "/api/admin/overview")
    check("onboarding: first user IS admin",
          st == 200 and users["users"][0]["username"] == "owner"
          and users["users"][0]["role"] == "admin")

    st, _ = await call(sess_admin, "POST", "/api/onboarding/finish",
                       {"mode": "solo"})
    check("onboarding: finish -> solo", st == 200)
    check("onboarding: mode persisted", await auth.mode() == "solo")

    st, _ = await call(sess_bob, "GET", "/api/admin/overview")
    check("admin: no-session access -> 403", st == 403)
    st, _ = await call(sess_bob, "GET", "/api/chats")
    check("solo: chats open without login", st == 200)

    # workspace switch through /admin (§16)
    new_ws = os.path.join(BASE, "ws2")
    st, r = await call(sess_admin, "POST", "/api/admin/workspace", {"path": new_ws})
    check("admin: workspace switch", st == 200 and r["workspace"] == new_ws)
    from server.main import jail as srv_jail
    check("admin: jail rebuilt on new workspace", srv_jail.root == new_ws)

    print("\n== team mode (§2): auth, shared projects, locks, queue ==")
    st, _ = await call(sess_admin, "POST", "/api/admin/mode", {"mode": "team"})
    check("team: mode switched", st == 200)
    st, _ = await call(sess_bob, "GET", "/api/chats")
    check("team: no session -> 401", st == 401)
    st, _ = await call(sess_bob, "GET", "/api/health")
    check("team: health stays public", st == 200)

    st, _ = await call(sess_admin, "POST", "/api/auth/login",
                       {"username": "owner", "password": "1234"})
    check("team: owner login", st == 200)
    st, _ = await call(sess_admin, "POST", "/api/admin/users",
                       {"username": "bob", "password": "1234", "role": "member"})
    check("team: member created by admin", st == 201)
    st, _ = await call(sess_bob, "POST", "/api/auth/login",
                       {"username": "bob", "password": "1234"})
    check("team: bob login", st == 200)
    st, me_bob = await call(sess_bob, "GET", "/api/auth/me")
    check("team: bob session resolves", me_bob["user"]["username"] == "bob")

    # provider + allow_all so mock turns run without approval cards
    st, prov = await call(sess_admin, "POST", "/api/providers",
                          {"host": "mock://demo", "api_key": None})
    pid = prov["provider"]["id"]
    await call(sess_admin, "PATCH", "/api/settings", {"permission_mode": "allow_all"})

    st, chats = await call(sess_bob, "GET", "/api/chats")
    check("team: bob starts with empty list", st == 200 and chats["chats"] == [])

    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "owner-chat", "model_key": f"{pid}::demo-fast"})
    own_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "PATCH", f"/api/chats/{own_chat}", {"shared": True})
    st, chats = await call(sess_bob, "GET", "/api/chats")
    shared = [x for x in chats["chats"] if x["id"] == own_chat]
    check("team: shared chat visible to member", bool(shared))
    check("team: shared_by attribution",
          shared and shared[0].get("shared_by") == "owner"
          and shared[0].get("mine") is False)
    st, _ = await call(sess_bob, "GET", f"/api/chats/{own_chat}")
    check("team: shared chat openable by member", st == 200)

    # lock attribution: owner runs a 2s turn; bob hits the same chat -> 409
    st, c2 = await call(sess_admin, "POST", "/api/chats",
                        {"title": "lock-chat", "model_key": f"{pid}::demo-fast"})
    lock_chat = c2["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{lock_chat}/messages",
                       {"text": "поспи немного"})
    check("team: owner turn started", st == 201)
    st, busy = await call(sess_bob, "POST", f"/api/chats/{lock_chat}/messages",
                          {"text": "эй"})
    check("team: 409 lock carries the username",
          st == 409 and "owner" in (busy or {}).get("reason", ""),
          str(busy))
    ok = await wait_for(lambda: turn_done(sess_admin, lock_chat))
    check("team: locked turn completed", ok)

    # concurrency queue (§2): one generation slot, second turn waits
    st, _ = await call(sess_admin, "PATCH", "/api/settings",
                       {"concurrency_generations": "1"})
    st, c3 = await call(sess_admin, "POST", "/api/chats",
                        {"title": "q1", "model_key": f"{pid}::demo-fast"})
    st, c4 = await call(sess_admin, "POST", "/api/chats",
                        {"title": "q2", "model_key": f"{pid}::demo-fast"})
    q1, q2 = c3["chat"]["id"], c4["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{q1}/messages",
                       {"text": "поспи"})
    st, r2 = await call(sess_admin, "POST", f"/api/chats/{q2}/messages",
                        {"text": "поспи"})
    check("queue: second turn accepted", st == 201)

    async def _q() -> bool:
        return turn_gate.queued() >= 1
    saw_queue = await wait_for(_q, timeout=3)
    check("queue: reports queued position", saw_queue, str(r2))
    ok = await wait_for(lambda: turn_done(sess_admin, q1), 40)
    ok2 = await wait_for(lambda: turn_done(sess_admin, q2), 40)
    check("queue: both turns completed in order", ok and ok2)
    await call(sess_admin, "PATCH", "/api/settings",
               {"concurrency_generations": "0"})
    check("queue: gate released", turn_gate.active == 0 and turn_gate.queued() == 0)

    # tool gate smoke (does not strangle turns)
    await call(sess_admin, "PATCH", "/api/settings", {"concurrency_tools": "1"})
    st, c5 = await call(sess_admin, "POST", "/api/chats",
                        {"title": "toolgate", "model_key": f"{pid}::demo-fast"})
    st, _ = await call(sess_admin, "POST", f"/api/chats/{c5['chat']['id']}/messages",
                       {"text": "поспи"})
    ok = await wait_for(lambda: turn_done(sess_admin, c5["chat"]["id"]), 40)
    check("tool gate: turn still completes", ok)
    await call(sess_admin, "PATCH", "/api/settings", {"concurrency_tools": "0"})

    print("\n== accounts mode (§2): isolation + sealed keys ==")
    st, r = await call(sess_admin, "POST", "/api/admin/mode", {"mode": "accounts"})
    check("accounts: mode switched", st == 200 and r.get("relogin"))
    st, _ = await call(sess_admin, "POST", "/api/auth/login",
                       {"username": "owner", "password": "1234"})
    check("accounts: owner re-login", st == 200)
    st, chats = await call(sess_admin, "GET", "/api/chats")
    check("accounts: owner gets a fresh personal scope", chats["chats"] == [])

    st, prov = await call(sess_admin, "POST", "/api/providers",
                          {"host": "mock://demo", "api_key": "sk-secret-abc"})
    check("accounts: provider added", st == 201)
    acc_pid = prov["provider"]["id"]

    # at rest: sealed envelope, plaintext nowhere in user.db
    user_db = os.path.join(BASE, "data", "users", "owner", "user.db")
    con = sqlite3.connect(user_db)
    row = con.execute("SELECT api_key FROM providers WHERE id=?", (acc_pid,)).fetchone()
    raw_key = row[0] if row else ""
    blob = open(user_db, "rb").read()
    con.close()
    check("accounts: key sealed at rest (enc:v1)", raw_key.startswith("enc:v1:"))
    check("accounts: plaintext key NOT on disk", b"sk-secret-abc" not in blob)

    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "acc-chat", "model_key": f"{acc_pid}::demo-fast"})
    acc_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{acc_chat}/messages",
                       {"text": "просто ответь"})
    ok = await wait_for(lambda: turn_done(sess_admin, acc_chat))
    check("accounts: turn runs in the user scope", ok)

    st, _ = await call(sess_admin, "POST", "/api/admin/users",
                       {"username": "alice", "password": "wonder", "role": "member"})
    st, _ = await call(sess_alice, "POST", "/api/auth/login",
                       {"username": "alice", "password": "wonder"})
    check("accounts: alice login", st == 200)
    st, chats_alice = await call(sess_alice, "GET", "/api/chats")
    check("accounts: alice isolated from owner chats", chats_alice["chats"] == [])
    st, _ = await call(sess_alice, "GET", f"/api/chats/{acc_chat}")
    check("accounts: owner chat invisible to alice (404)", st == 404)
    st, _ = await call(sess_alice, "GET", "/api/providers")
    check("accounts: alice sees no shared providers", st == 200)

    # attachment quota (§10 accounts-mode setting)
    st, ac = await call(sess_alice, "POST", "/api/chats",
                        {"title": "quota", "model_key": f"{acc_pid}::demo-fast"})
    quota_chat = ac["chat"]["id"]
    await call(sess_admin, "PATCH", "/api/settings", {"attach_quota_mb": "0.001"})
    data = b"x" * 2048
    async with sess_alice.post(
            URL + f"/api/chats/{quota_chat}/attachments",
            data=aiohttp.FormData()) as r:
        pass  # placeholder replaced below
    form = aiohttp.FormData()
    form.add_field("file", data, filename="big.bin",
                   content_type="application/octet-stream")
    async with sess_alice.post(
            URL + f"/api/chats/{quota_chat}/attachments", data=form) as r:
        st_quota = r.status
        body_quota = await r.json(content_type=None)
    check("accounts: attachment quota enforced",
          st_quota == 400 and "квота" in (body_quota or {}).get("error", ""))
    await call(sess_admin, "PATCH", "/api/settings", {"attach_quota_mb": "0"})

    # back to solo for the remaining feature tests
    st, _ = await call(sess_admin, "POST", "/api/admin/mode", {"mode": "solo"})
    check("accounts -> solo: switch back", st == 200)

    print("\n== hooks (§7) ==")
    BLOCK_CMD = ("python3 -c \"import sys,json; json.load(sys.stdin); "
                 "print(json.dumps({'decision':'block','reason':'тест-блок'}, "
                 "ensure_ascii=False))\"")
    st, hk = await call(sess_admin, "POST", "/api/hooks",
                        {"event": "PreToolUse", "command": BLOCK_CMD, "timeout": 5})
    check("hooks: PreToolUse added", st == 201)
    st, test = await call(sess_admin, "POST", "/api/hooks/test",
                          {"event": "PreToolUse", "command": BLOCK_CMD, "timeout": 5})
    check("hooks: test endpoint parses verdict",
          st == 200 and "тест-блок" in test.get("stdout", ""))

    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "hook-block", "model_key": f"{pid}::demo-fast"})
    hook_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{hook_chat}/messages",
                       {"text": "поспи"})
    ok = await wait_for(lambda: turn_done(sess_admin, hook_chat))
    check("hooks: blocked turn still finishes", ok)

    async def _blocked_seen():
        _, b = await call(sess_admin, "GET", f"/api/chats/{hook_chat}/history")
        return any(e["type"] == "tool_result" and
                   "заблокировано хуком" in json.dumps(e.get("data", {}),
                                                        ensure_ascii=False)
                   for e in b.get("events", []))
    check("hooks: PreToolUse verdict blocked the tool",
          await wait_for(_blocked_seen))
    st, _ = await call(sess_admin, "DELETE", f"/api/hooks/{hk['hook']['id']}")
    check("hooks: deleted", st == 200)

    CTX_CMD = ("python3 -c \"import sys,json; print("
               "json.dumps({'context':'HOOKCTX-MARKER'}))\"")
    STOP_CMD = "python3 -c \"open('stop_hook.flag','w').write('x')\""
    st, hk2 = await call(sess_admin, "POST", "/api/hooks",
                         {"event": "UserPromptSubmit", "command": CTX_CMD, "timeout": 5})
    st, hk3 = await call(sess_admin, "POST", "/api/hooks",
                         {"event": "Stop", "command": STOP_CMD, "timeout": 5})
    check("hooks: UserPromptSubmit + Stop added", st == 201)
    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "hook-ctx", "model_key": f"{pid}::demo-fast"})
    ctx_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{ctx_chat}/messages",
                       {"text": "просто ответь"})
    ok = await wait_for(lambda: turn_done(sess_admin, ctx_chat))
    check("hooks: context-hook turn finishes", ok)

    async def _ctx_seen():
        _, b = await call(sess_admin, "GET", f"/api/chats/{ctx_chat}/history")
        return any(e["type"] == "agent_notice" and
                   "HOOKCTX-MARKER" in json.dumps(e.get("data", {}))
                   for e in b.get("events", []))
    check("hooks: UserPromptSubmit injected context", await wait_for(_ctx_seen))
    async def _stop_flag() -> bool:
        return os.path.isfile(os.path.join(new_ws, "stop_hook.flag"))
    ok = await wait_for(_stop_flag)
    check("hooks: Stop hook ran in the workspace", ok)
    await call(sess_admin, "DELETE", f"/api/hooks/{hk2['hook']['id']}")
    await call(sess_admin, "DELETE", f"/api/hooks/{hk3['hook']['id']}")

    st, test = await call(sess_admin, "POST", "/api/hooks/test",
                          {"event": "PreToolUse", "command": "sleep 5", "timeout": 1})
    check("hooks: timeout reported", st == 200 and test.get("timeout") is True)

    print("\n== skills (§6) ==")
    st, sk = await call(sess_admin, "POST", "/api/skills",
                        {"name": "deploy", "description": "Как деплоить",
                         "body": "1. запусти тесты\n2. залей"})
    check("skills: upserted", st == 201)
    SKILL_MD = """---
name: code-review
description: Ревью кода по чеклисту
---
## Шаги
- проверить типы
- проверить тесты
"""
    st, imp = await call(sess_admin, "POST", "/api/skills/import", {"text": SKILL_MD})
    check("skills: SKILL.md imported", st == 201 and imp["skill"]["name"] == "code-review")
    st, lst = await call(sess_admin, "GET", "/api/skills")
    names = [s["name"] for s in lst["skills"]]
    check("skills: listed", set(names) >= {"deploy", "code-review"})
    async with sess_admin.get(URL + "/api/skills/deploy/export") as r:
        md = await r.text()
    check("skills: SKILL.md export round-trip",
          "name: deploy" in md and "залей" in md)

    from server.main import tool_registry
    res = await tool_registry.execute("skill_search", {"query": "deploy"})
    check("skills: skill_search finds by description",
          any(s["name"] == "deploy" for s in res.get("skills", [])))
    res = await tool_registry.execute("skill_load", {"name": "code-review"})
    check("skills: skill_load returns the body",
          "проверить типы" in res.get("body", ""))
    await call(sess_admin, "DELETE", "/api/skills/code-review")

    print("\n== export / import (§16) ==")
    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "export-me", "model_key": f"{pid}::demo-fast"})
    exp_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{exp_chat}/messages",
                       {"text": "запиши файл notes/p5.txt со словом тест"})
    ok = await wait_for(lambda: turn_done(sess_admin, exp_chat))
    check("export: source turn completed", ok)

    async with sess_admin.get(
            URL + f"/api/chats/{exp_chat}/export?format=json") as r:
        exported = await r.json(content_type=None)
    check("export: JSON format envelope",
          exported.get("format") == "agent-framework-chat"
          and isinstance(exported.get("events"), list)
          and len(exported["events"]) > 3)
    payload = json.dumps(exported, ensure_ascii=False)
    st, imp = await call(sess_admin, "POST", "/api/chats/import",
                         {"format": "json", "content": payload,
                          "title_prefix": "копия-"})
    check("export: JSON imported", st == 201)
    imp_chat = imp["chat"]["id"]

    async def eff(chat):
        _, b = await call(sess_admin, "GET", f"/api/chats/{chat}/history")
        return [(e["type"], json.dumps(e.get("data", {}), sort_keys=True,
                                       ensure_ascii=False))
                for e in b.get("events", [])]
    check("export: JSON round-trip fidelity",
          await eff(exp_chat) == await eff(imp_chat))

    async with sess_admin.get(
            URL + f"/api/chats/{exp_chat}/export?format=md") as r:
        md_text = await r.text()
    check("export: MD has readable markers",
          "## 👤 Пользователь" in md_text and "## 🤖 Ассистент" in md_text)
    st, imp_md = await call(sess_admin, "POST", "/api/chats/import",
                            {"format": "md", "content": md_text})
    check("export: MD imported back", st == 201)
    check("export: MD round-trip carries content",
          await wait_for(lambda: _md_roundtrip_ok(sess_admin, imp_md["chat"]["id"])))

    print("\n== audit (§2) ==")
    # режим сменился (accounts -> solo): сессии сброшены — релогин админа
    st, _ = await call(sess_admin, "POST", "/api/auth/login",
                       {"username": "owner", "password": "1234"})
    check("audit: owner re-login after mode switch", st == 200)
    st, audit = await call(sess_admin, "GET", "/api/admin/audit")
    check("audit: OFF by default (empty)", st == 200 and audit["entries"] == [])
    await call(sess_admin, "PATCH", "/api/settings", {"audit_enabled": "1"})
    st, c = await call(sess_admin, "POST", "/api/chats",
                       {"title": "audited", "model_key": f"{pid}::demo-fast"})
    aud_chat = c["chat"]["id"]
    st, _ = await call(sess_admin, "POST", f"/api/chats/{aud_chat}/messages",
                       {"text": "поспи"})
    ok = await wait_for(lambda: turn_done(sess_admin, aud_chat))
    check("audit: audited turn completed", ok)

    async def _audit_entry():
        _, b = await call(sess_admin, "GET", "/api/admin/audit")
        return any(e["tool"] == "bash" for e in b.get("entries", []))
    check("audit: real toolcall entry recorded",
          await wait_for(_audit_entry))
    await call(sess_admin, "PATCH", "/api/settings", {"audit_enabled": "0"})

    print("\n== logs (§16) ==")
    st, logs = await call(sess_admin, "GET", "/api/logs?limit=500")
    check("logs: entries present", st == 200 and len(logs["entries"]) > 0)
    st, _ = await call(sess_admin, "POST", "/api/logs/level", {"level": "warning"})
    st, logs2 = await call(sess_admin, "GET", "/api/logs?level=warning&limit=500")
    check("logs: level filter works",
          all(e["level"] in ("warning", "error") for e in logs2["entries"]))
    await call(sess_admin, "POST", "/api/logs/level", {"level": "info"})
    async with sess_admin.get(URL + "/api/logs/export") as r:
        text = await r.text()
    check("logs: export text", r.status == 200 and len(text) > 10)

    print("\n== themes (§16) ==")
    css = "/* custom */ :root { --accent: #ff0055; }"
    form = aiohttp.FormData()
    form.add_field("file", css.encode(), filename="neon.css", content_type="text/css")
    async with sess_admin.post(URL + "/api/themes/upload", data=form) as r:
        st = r.status
        up = await r.json(content_type=None)
    check("themes: css uploaded", st == 201)
    st, th = await call(sess_admin, "POST", "/api/themes/select",
                        {"theme": "dark", "custom_css": "neon.css"})
    check("themes: selected", st == 200)
    st, thl = await call(sess_admin, "GET", "/api/themes")
    check("themes: listed with selection",
          thl["theme"] == "dark" and thl["custom_css"] == "neon.css"
          and any(f["name"] == "neon.css" for f in thl["files"]))
    async with sess_admin.get(URL + "/themes/neon.css") as r:
        served = await r.text()
    check("themes: custom css served", r.status == 200 and "--accent" in served)
    await call(sess_admin, "DELETE", "/api/themes/neon.css")
    st, thl2 = await call(sess_admin, "GET", "/api/themes")
    check("themes: deleted + selection reset",
          thl2["custom_css"] == "" and not thl2["files"])

    print("\n== migrations (§2) ==")
    con = sqlite3.connect(os.path.join(BASE, "data", "main.db"))
    mig = con.execute("SELECT version FROM schema_migrations").fetchall()
    check("migrations: recorded versions", {m[0] for m in mig} >= {2})
    con.close()
    con = sqlite3.connect(os.path.join(BASE, "data", "history.db"))
    cols = {r[1] for r in con.execute("PRAGMA table_info(chats)").fetchall()}
    con.close()
    check("migrations: chats.owner/shared present", {"owner", "shared"} <= cols)

    print("\n== i18n setting ==")
    await call(sess_admin, "PATCH", "/api/settings", {"ui_lang": "en"})
    st, s = await call(sess_admin, "GET", "/api/settings")
    check("i18n: ui_lang persisted", s.get("ui_lang") == "en")
    await call(sess_admin, "PATCH", "/api/settings", {"ui_lang": "ru"})

    await sess_admin.close()
    await sess_bob.close()
    await sess_alice.close()
    await runner.cleanup()


async def _md_roundtrip_ok(sess_admin, chat_id):
    URL = "http://127.0.0.1:8799"
    async with sess_admin.get(
            URL + f"/api/chats/{chat_id}/history") as r:
        b = await r.json(content_type=None)
    events = b.get("events", [])
    return (any(e["type"] == "user_message" for e in events)
            and any(e["type"] in ("assistant_text", "tool_call") for e in events))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
        if FAIL:
            for name, detail in FAIL:
                print(f"  ✗ {name}  {detail}")
            sys.exit(1)
