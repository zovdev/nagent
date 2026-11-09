#!/usr/bin/env python3
"""Phase 2 smoke test — tools, rights, jail, timeouts, checkpoints.

No API key needed: mock://demo drives write_file and bash-timeout flows
end-to-end, exactly like a real provider would.

Covers:
  1. workspace jail: `..`/absolute/symlink/`~` escapes rejected, secret
     reads denied, .agent writes denied
  2. core tools through the registry (read/write/edit/list/glob/grep/bash)
  3. permission engine: ask-mode approval round-trip (once / always /
     deny-rule), allow_all mode, bash safe-class runs silently
  4. §15 timeout protocol: default timeout fires -> model-visible
     instruction -> retry with timeout=N -> MASK (clean history: model
     context drops the failed call, user still sees it)
  5. checkpoints: UUID<->commit binding, diff API, revert MR (clean +
     conflict + force)
  6. ToolSearch: deferred MCP tools loadable per chat
  7. files panel API + oversized-output cache

Run: python tests/smoke_phase2.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="agentfw-p2-")
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
    # unit-ish imports before the server starts (fresh module state)
    from server.jail import JailError, WorkspaceJail

    ws_root = os.path.join(BASE, "ws")
    os.makedirs(ws_root, exist_ok=True)
    jail = WorkspaceJail(ws_root)

    print("\n== phase 2 smoke test ==")

    # ---------------------------------------------------------------- 1. jail
    print("-- jail")
    check("jail: relative resolves inside", jail.resolve("a/b.txt")
          == os.path.join(ws_root, "a/b.txt"))
    try:
        jail.resolve("../../etc/passwd"); ok = False
    except JailError:
        ok = True
    check("jail: .. escape rejected", ok)
    try:
        jail.resolve("/etc/passwd"); ok = False
    except JailError:
        ok = True
    check("jail: absolute outside rejected", ok)
    try:
        jail.resolve("~/secret"); ok = False
    except JailError:
        ok = True
    check("jail: ~ rejected", ok)
    # symlink escape
    os.makedirs(os.path.join(BASE, "outside"), exist_ok=True)
    with open(os.path.join(BASE, "outside", "target.txt"), "w") as f:
        f.write("x")
    os.symlink(os.path.join(BASE, "outside"), os.path.join(ws_root, "leak"))
    try:
        jail.resolve("leak/target.txt"); ok = False
    except JailError:
        ok = True
    check("jail: symlink escape rejected", ok)
    # secret reads
    with open(os.path.join(ws_root, ".env"), "w") as f:
        f.write("KEY=1")
    try:
        jail.check_read(".env"); ok = False
    except JailError:
        ok = True
    check("jail: .env read denied", ok)
    try:
        jail.check_write(".agent/git/config"); ok = False
    except JailError:
        ok = True
    check("jail: .agent write denied", ok)
    check("jail: deep inside allowed", jail.resolve("x/y/z").startswith(ws_root))

    # ---------------------------------------------------------------- server up
    from server.main import (checkpoints, init_app, jail as srv_jail,
                             permission_engine, settings_store, tool_registry)

    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"

    # mock MCP for ToolSearch deferred loading
    mcp_app = web.Application()

    async def rpc(req):
        body = await req.json()
        m = body.get("method")
        result = None
        if m == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "P2 MCP", "version": "1.0"}}
        elif m == "tools/list":
            result = {"tools": [
                {"name": "fetch_weather", "description": "Прогноз погоды по городу",
                 "inputSchema": {"type": "object",
                                 "properties": {"city": {"type": "string"}},
                                 "required": ["city"]}}]}
        elif m == "tools/call":
            result = {"content": [{"type": "text", "text": "ясно, +21"}]}
        if result is None:
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"),
                                      "error": {"code": -32601, "message": "?"}})
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": result})
    mcp_app.router.add_post("/mcp", rpc)
    mcp_runner = web.AppRunner(mcp_app)
    await mcp_runner.setup()
    mcp_site = web.TCPSite(mcp_runner, "127.0.0.1", 0)
    await mcp_site.start()
    mcp_port = mcp_runner.addresses[0][1]

    async with aiohttp.ClientSession() as http:
        async def call(method, path, **kw):
            async with http.request(method, base + path, **kw) as r:
                return r.status, await r.json(content_type=None)

        # ------------------------------------------------------------ 2. providers + chat
        st, body = await call("POST", "/api/providers",
                              json={"host": "mock://demo", "name": "Demo"})
        p_demo = body["provider"]["id"]
        model_key = f"{p_demo}::demo-reasoner"
        check("provider added", st == 201)

        # tight tool timeout so the §15 flow actually times out on `sleep 2`
        st, _ = await call("PATCH", "/api/settings", json={
            "tool_timeout_default": 1, "tool_timeout_max": 8})
        check("tool timeouts configured", st == 200)

        st, body = await call("POST", "/api/chats",
                              json={"title": "p2", "model_key": model_key})
        chat_id = body["chat"]["id"]
        check("chat created", st == 201)

        # ------------------------------------------------------------ 3. ask-mode approval
        print("-- permissions: ask flow")
        st, _ = await call("POST", f"/api/chats/{chat_id}/messages",
                           json={"text": "запиши файл notes/hello.txt"})
        check("turn started", st == 201)

        async def has_pending():
            _, b = await call("GET", f"/api/chats/{chat_id}/status")
            return bool(b.get("pending_approvals"))
        ok = await wait_for(has_pending)
        check("pending approval appears on status", ok)

        _, status = await call("GET", f"/api/chats/{chat_id}/status")
        approval = status["pending_approvals"][0]
        check("approval carries summary", "write_file" in approval["summary"],
              json.dumps(approval.get("summary", ""), ensure_ascii=False))
        st, body = await call("POST", f"/api/approvals/{approval['id']}/resolve",
                              json={"decision": "once"})
        check("resolve once accepted", st == 200
              and body["approval"]["status"] == "approved")

        async def turn_done():
            _, b = await call("GET", f"/api/chats/{chat_id}/history")
            return any(e["type"] == "turn_meta" for e in b["events"])
        ok = await wait_for(turn_done)
        check("turn finished after approval", ok)

        async def checkpoint_ready():
            _, cps = await call("GET", f"/api/checkpoints?chat={chat_id}")
            return len(cps["checkpoints"]) >= 1
        ok = await wait_for(checkpoint_ready)
        _, hist = await call("GET", f"/api/chats/{chat_id}/history")
        types = [e["type"] for e in hist["events"]]
        check("permission_request event persisted", "permission_request" in types)
        check("permission_result event persisted", "permission_result" in types)
        full_path = os.path.join(ws_root, "notes", "hello.txt")
        check("file actually written", os.path.isfile(full_path))

        # ------------------------------------------------------------ 4. checkpoint bound to the turn
        print("-- checkpoints")
        _, cps = await call("GET", f"/api/checkpoints?chat={chat_id}")
        cp_rows = cps["checkpoints"]
        check("checkpoint created for the turn", len(cp_rows) == 1
              and "notes/hello.txt" in cp_rows[0]["files"],
              json.dumps(cp_rows))
        check("checkpoint has commit sha", len(cp_rows[0]["commit_sha"]) >= 7)
        check("checkpoint event in chat", "checkpoint" in types)
        cp1 = cp_rows[0]
        _, diff = await call("GET", f"/api/checkpoints/{cp1['id']}/diff")
        check("checkpoint diff returns patch", "hello.txt" in diff.get("diff", ""))

        # ------------------------------------------------------------ 5. §15 timeout protocol
        print("-- §15 timeout protocol")
        st, _ = await call("POST", f"/api/chats/{chat_id}/messages",
                           json={"text": "поспи пару секунд"})
        check("sleep turn started", st == 201)

        # bash 'sleep 2' is exec-class: ask-mode gates it — approve with
        # 'always' so the §15 retry (same command) passes without re-asking
        ok = await wait_for(has_pending)
        check("bash asks for permission", ok)
        _, status = await call("GET", f"/api/chats/{chat_id}/status")
        sleep_appr = status["pending_approvals"][0]
        st, body = await call("POST", f"/api/approvals/{sleep_appr['id']}/resolve",
                              json={"decision": "always"})
        check("bash approved (always)", st == 200)

        async def sleep_turn_done():
            _, b = await call("GET", f"/api/chats/{chat_id}/history")
            metas = [e for e in b["events"] if e["type"] == "turn_meta"]
            return len(metas) >= 2
        ok = await wait_for(sleep_turn_done, timeout=40)
        check("sleep turn finished (retry succeeded)", ok)

        _, hist = await call("GET", f"/api/chats/{chat_id}/history")
        events = hist["events"]
        results = [e for e in events if e["type"] == "tool_result"
                   and e["data"].get("name") == "bash"]
        check("first bash result is the timeout payload",
              bool(results) and results[0]["data"]["result"].get("timeout") is True
              and "timeout=N" in results[0]["data"]["result"].get("message", ""),
              json.dumps(results[0]["data"]["result"] if results else {},
                         ensure_ascii=False)[:200])
        check("retry call carries timeout arg",
              any(e["type"] == "tool_call" and e["data"].get("name") == "bash"
                  and e["data"]["args"].get("timeout") == 5 for e in events))
        check("retry succeeded (exit_code 0)",
              any(e["type"] == "tool_result" and e["data"].get("name") == "bash"
                  and e["data"]["result"].get("exit_code") == 0 for e in events))
        masked = [e for e in events if e.get("status") == "masked"]
        check("failed call+result+think masked for the user view",
              len(masked) == 3 and {e["type"] for e in masked}
              == {"tool_call", "tool_result", "assistant_think"},
              json.dumps([(e["type"], e.get("status")) for e in events]))
        # model context: effective history has ONE bash call (the retry)
        from server.main import history_store
        eff_events = await history_store.effective(chat_id)
        bash_calls = [e for e in eff_events if e["type"] == "tool_call"
                      and e["data"].get("name") == "bash"]
        check("model sees exactly ONE clean bash call",
              len(bash_calls) == 1 and bash_calls[0]["data"]["args"].get("timeout") == 5,
              json.dumps([e["data"] for e in bash_calls], ensure_ascii=False))

        # ------------------------------------------------------------ 6. always + deny rules
        print("-- permissions: rules")
        st, _ = await call("POST", f"/api/chats/{chat_id}/messages",
                           json={"text": "запиши файл notes/hello.txt"})
        ok = await wait_for(has_pending)
        check("second write asks again (once only lasted once)", ok)
        _, status = await call("GET", f"/api/chats/{chat_id}/status")
        approval2 = status["pending_approvals"][0]
        st, body = await call("POST", f"/api/approvals/{approval2['id']}/resolve",
                              json={"decision": "always"})
        check("resolve always accepted", st == 200)
        _, rules = await call("GET", "/api/permissions/rules")
        allow_rules = [r for r in rules["rules"] if r["effect"] == "allow"
                       and r["tool"] == "write_file"]
        check("always created a scoped allow-rule",
              bool(allow_rules) and allow_rules[0]["pattern"] == "notes/**",
              json.dumps(allow_rules))

        async def second_write_done():
            _, b = await call("GET", f"/api/chats/{chat_id}/history")
            metas = [e for e in b["events"] if e["type"] == "turn_meta"]
            return len(metas) >= 3
        ok = await wait_for(second_write_done)
        check("always-rule turn finished without asking", ok)
        check("file rewritten with v2 content",
              "v2" in open(os.path.join(ws_root, "notes", "hello.txt"),
                           encoding="utf-8").read())

        # deny rule for bash — checked through the engine gate (the agent loop
        # refuses BEFORE execution; the registry itself stays permission-free)
        st, body = await call("POST", "/api/permissions/rules", json={
            "effect": "deny", "tool": "bash", "pattern": "rm*",
            "note": "никаких rm"})
        check("deny rule added", st == 201)
        from server.permissions import derive_pattern
        _pat, subject, kind = derive_pattern("bash", {"command": "rm notes/hello.txt"})
        dec = await permission_engine.check("bash", {"command": "rm notes/hello.txt"},
                                            "exec", subject, kind)
        check("deny rule blocks rm", dec["decision"] == "deny",
              json.dumps(dec, ensure_ascii=False))
        st, _ = await call("DELETE",
                           f"/api/permissions/rules/{body['rule']['id']}")
        check("deny rule deleted", st == 200)

        # bash policy: destructive commands refused even without any rule
        dec = await permission_engine.check("bash", {"command": "sudo reboot"},
                                            "exec", "sudo reboot", "command")
        check("bash policy denies sudo reboot (no rule needed)",
              dec["decision"] == "deny", json.dumps(dec, ensure_ascii=False))

        # bash safe-class: ls runs silently even in ask mode
        dec = await permission_engine.check("bash", {"command": "ls -la"}, "exec",
                                            "ls -la", "command")
        check("safe bash (ls) not gated in ask mode", dec["decision"] == "allow",
              json.dumps(dec, ensure_ascii=False))

        # ------------------------------------------------------------ 7. ToolSearch
        print("-- toolsearch")
        st, body = await call("POST", "/api/mcp",
                              json={"url": f"http://127.0.0.1:{mcp_port}/mcp"})
        check("mcp discovered", st == 201 and len(body["tools"]) == 1)

        exposed = tool_registry.openai_tools(chat_id)
        names = [t["function"]["name"] for t in exposed]
        check("core tools exposed by default",
              all(n in names for n in ("read_file", "write_file", "bash",
                                       "tool_search", "tool_load")))
        check("MCP tool deferred by default (schema not sent)",
              "fetch_weather" not in names, json.dumps(names))
        res = await tool_registry.execute("tool_search", {"query": "погод"},
                                          chat_id=chat_id)
        found = res.get("results", [])
        check("tool_search finds the deferred tool",
              any(r["name"] == "fetch_weather" for r in found),
              json.dumps(res, ensure_ascii=False)[:200])
        res = await tool_registry.execute("tool_load", {"names": ["fetch_weather"]},
                                          chat_id=chat_id)
        check("tool_load loads it", res.get("loaded") == ["fetch_weather"])
        names_after = [t["function"]["name"]
                       for t in tool_registry.openai_tools(chat_id)]
        check("loaded schema now visible to the model",
              "fetch_weather" in names_after)
        res = await tool_registry.execute("fetch_weather", {"city": "msk"},
                                          chat_id=chat_id)
        check("deferred tool callable by native name",
              res.get("text") == "ясно, +21", json.dumps(res, ensure_ascii=False))

        # ------------------------------------------------------------ 8. files API
        print("-- files panel api")
        st, tree = await call("GET", "/api/workspace/tree")
        entries = tree.get("entries", [])
        check("tree lists notes dir", any(e["name"] == "notes"
                                          and e["type"] == "dir" for e in entries))
        st, tree = await call("GET", "/api/workspace/tree?path=notes")
        check("tree: notes lists hello.txt",
              "hello.txt" in {e["name"] for e in tree["entries"]})
        st, fbody = await call("GET", "/api/workspace/file?path=notes/hello.txt")
        check("file content readable", st == 200
              and "создано агентом" in fbody.get("content", ""))
        st, fbody = await call("GET", "/api/workspace/file?path=.env")
        check("panel refuses to serve .env", st == 403)
        st, fbody = await call("GET", "/api/workspace/tree?path=..")
        check("panel refuses to leave the jail", st == 400)

        # ------------------------------------------------------------ 9. allow_all mode
        print("-- allow_all mode")
        st, _ = await call("PATCH", "/api/settings", json={"permission_mode": "allow_all"})
        st, _ = await call("POST", f"/api/chats/{chat_id}/messages",
                           json={"text": "запиши файл notes/third.txt"})
        async def third_done():
            _, b = await call("GET", f"/api/chats/{chat_id}/history")
            metas = [e for e in b["events"] if e["type"] == "turn_meta"]
            return len(metas) >= 4
        ok = await wait_for(third_done)
        check("allow_all writes without approval", ok and
              os.path.isfile(os.path.join(ws_root, "notes", "third.txt")))
        st, _ = await call("PATCH", "/api/settings", json={"permission_mode": "ask"})

        # ------------------------------------------------------------ 10. revert MR
        print("-- revert (merge-request)")
        async def checkpoints_settled():
            _, cpsx = await call("GET", f"/api/checkpoints?chat={chat_id}")
            return len(cpsx["checkpoints"]) >= 3
        ok = await wait_for(checkpoints_settled)
        _, cps = await call("GET", f"/api/checkpoints?chat={chat_id}")
        cp_rows = cps["checkpoints"]
        check("checkpoints accumulated per write-turn", len(cp_rows) >= 3,
              str(len(cp_rows)))
        # latest checkpoint = notes/third.txt creation -> revert cleanly
        cp3 = cp_rows[-1]
        st, mr = await call("POST", f"/api/checkpoints/{cp3['id']}/revert")
        check("revert prepare: no conflicts for the latest checkpoint",
              st == 200 and mr.get("conflicts") == []
              and "third.txt" in mr.get("revert_patch", ""),
              json.dumps(mr, ensure_ascii=False)[:200])
        st, applied = await call("POST", f"/api/checkpoints/{cp3['id']}/revert/apply",
                                 json={"force": False})
        check("revert applied cleanly", st == 200 and applied.get("revert_commit"),
              json.dumps(applied, ensure_ascii=False))
        check("reverted file removed",
              not os.path.exists(os.path.join(ws_root, "notes", "third.txt")))

        # conflict path: cp1 CREATED hello.txt and cp2 later MODIFIED it —
        # prepare must flag the conflict while the modification is still in
        # place; the clean revert of cp2 then restores v1; force-reverting
        # cp1 (a creation) finally removes the file
        cp2 = next(c for c in cp_rows if "notes/hello.txt" in c["files"]
                   and c["id"] != cp1["id"])
        st, mr = await call("POST", f"/api/checkpoints/{cp1['id']}/revert")
        check("revert prepare detects conflicts with later edits",
              st == 200 and "notes/hello.txt" in mr.get("conflicts", []),
              json.dumps(mr.get("conflicts", [])))

        st, mr = await call("POST", f"/api/checkpoints/{cp2['id']}/revert")
        check("revert prepare: modification checkpoint has no conflicts",
              st == 200 and mr.get("conflicts") == [],
              json.dumps(mr.get("conflicts", [])))
        st, applied = await call("POST", f"/api/checkpoints/{cp2['id']}/revert/apply",
                                 json={"force": False})
        check("clean revert restores v1 content", st == 200
              and "v1" in open(os.path.join(ws_root, "notes", "hello.txt"),
                               encoding="utf-8").read(),
              json.dumps(applied, ensure_ascii=False))

        st, applied = await call("POST", f"/api/checkpoints/{cp1['id']}/revert/apply",
                                 json={"force": True})
        check("force revert applies despite conflicts", st == 200,
              json.dumps(applied, ensure_ascii=False))
        check("force revert of a creation removes the file",
              not os.path.exists(os.path.join(ws_root, "notes", "hello.txt")))

        # ------------------------------------------------------------ 11. tool catalog + truncation cache
        print("-- misc")
        st, cat = await call("GET", "/api/tools")
        tool_names = [t["name"] for t in cat["tools"]]
        check("tool catalog endpoint", st == 200 and "bash" in tool_names
              and any(t["deferred"] for t in cat["tools"]))

        from server.agent_loop import TurnRunner
        tr = TurnRunner(history_store, None, None, tool_registry)
        big = {"stdout": "x" * 200_000}
        small = await tr._maybe_cache_full_output("c", "callbig", big)
        check("oversized output truncated + cached",
              small.get("truncated") is True and small.get("cache")
              and os.path.isfile(small["cache"])
              and os.path.getsize(small["cache"]) > 190_000)

    await mcp_runner.cleanup()
    await runner.cleanup()

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    for name, detail in FAIL:
        print(f"  FAILED: {name} {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
