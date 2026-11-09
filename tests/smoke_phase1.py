#!/usr/bin/env python3
"""Phase 1 smoke test — full pipeline WITHOUT any API key.

Doctrine under test: «all data from the API, smart wrapper around it».

Covers:
  1. provider catalog passthrough for mock://demo and mock://flaky —
     capabilities only where declared, NO pricing fields anywhere
  2. fallback cascade: flaky fails (429 -> quota) -> cascade to demo;
     user-visible `fallback` event + turn_meta records the real model and
     the usage object VERBATIM from the API (no estimates, no cost)
  3. context meter fed by the last REAL usage object (no tokenizer)
  4. usage aggregation per chat — only real API numbers
  5. model overrides (survive catalog resync)
  6. MCP URL discovery against an in-process mock MCP server —
     tools registered under their NATIVE names, routing internal,
     name collisions get a `_2` suffix
  7. diagnostics endpoint

Run: python tests/smoke_phase1.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="agentfw-test-")
os.environ["AGENT_DATA"] = os.path.join(BASE, "data")
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


# ------------------------------------------------------------------ mock MCP server

MOCK_TOOLS = [
    {"name": "echo_time", "description": "Возвращает текущее время",
     "inputSchema": {"type": "object", "properties": {"fmt": {"type": "string"}}}},
    {"name": "add", "description": "Складывает два числа",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def build_mock_mcp():
    app = web.Application()

    async def rpc(req):
        body = await req.json()
        method = body.get("method")
        if method == "notifications/initialized":
            return web.Response(status=202)
        result = None
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "Demo MCP", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": MOCK_TOOLS}
        elif method == "prompts/list":
            result = {"prompts": [{"name": "greet"}]}
        elif method == "resources/list":
            result = {"resources": [{"uri": "file:///demo"}]}
        elif method == "tools/call":
            name = (body.get("params") or {}).get("name")
            result = ({"content": [{"type": "text", "text": "11:22:33"}]}
                      if name == "echo_time" else
                      {"content": [{"type": "text", "text": "42"}]})
        if result is None:
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"),
                                      "error": {"code": -32601, "message": "unknown"}})
        return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    app.router.add_post("/mcp", rpc)
    return app


# ------------------------------------------------------------------ main test

async def main() -> int:
    from server.main import init_app, tool_registry

    print("\n== phase 1 smoke test ==")

    # start the framework server
    app = await init_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    base = f"http://127.0.0.1:{port}"

    # start the mock MCP server
    mcp_runner = web.AppRunner(build_mock_mcp())
    await mcp_runner.setup()
    mcp_site = web.TCPSite(mcp_runner, "127.0.0.1", 0)
    await mcp_site.start()
    mcp_port = mcp_runner.addresses[0][1]
    mcp_url = f"http://127.0.0.1:{mcp_port}/mcp"

    async with aiohttp.ClientSession() as http:
        async def call(method, path, **kw):
            async with http.request(method, base + path, **kw) as r:
                return r.status, await r.json(content_type=None)

        # -- health
        st, body = await call("GET", "/api/health")
        check("health endpoint", st == 200 and bool(body.get("version")))

        # -- providers
        st, body = await call("POST", "/api/providers",
                              json={"host": "mock://demo", "name": "Demo"})
        p_demo = body["provider"]["id"]
        check("provider demo added", st == 201 and len(body["models"]) == 2)
        m0 = body["models"][0]
        check("catalog: capabilities from API only",
              m0.get("capabilities", {}).get("thinking") is True
              and m0.get("capabilities", {}).get("tools") is True,
              json.dumps(m0.get("capabilities")))
        check("catalog: NO pricing fields",
              "pricing_in" not in m0 and "pricing_out" not in m0
              and "cost_usd" not in m0)
        check("catalog: vision stays unknown (API didn't declare)",
              "vision" not in (m0.get("capabilities") or {}))

        st, body = await call("POST", "/api/providers",
                              json={"host": "mock://flaky", "name": "Flaky"})
        p_flaky = body["provider"]["id"]
        check("provider flaky added", st == 201 and len(body["models"]) == 1)

        # -- reliability settings
        st, body = await call("PATCH", "/api/settings", json={
            "fallback_chain": [f"{p_flaky}::flaky-1", f"{p_demo}::demo-reasoner"],
            "retry_max_attempts": 2, "retry_backoff": "exp:0.05"})
        check("fallback settings saved", st == 200 and
              json.loads(body["fallback_chain"])[0].endswith("flaky-1"))

        # -- chat with the flaky model as primary
        st, body = await call("POST", "/api/chats",
                              json={"title": "cascade", "model_key": f"{p_flaky}::flaky-1",
                                    "params": {"temperature": 0.7}})
        chat_id = body["chat"]["id"]
        check("chat created with params", st == 201 and
              body["chat"]["params"].get("temperature") == 0.7)

        st, body = await call("POST", f"/api/chats/{chat_id}/messages",
                              json={"text": "проверь время и ответь"})
        check("turn started (409-lock free)", st == 201)

        async def turn_done():
            _, b = await call("GET", f"/api/chats/{chat_id}/history")
            return any(e["type"] == "turn_meta" for e in b["events"])
        ok = await wait_for(turn_done)
        check("turn finished", ok)

        _, body = await call("GET", f"/api/chats/{chat_id}/history")
        types = [e["type"] for e in body["events"]]
        fallbacks = [e for e in body["events"] if e["type"] == "fallback"]
        meta = next((e for e in body["events"] if e["type"] == "turn_meta"), None)

        check("user-visible fallback event", len(fallbacks) == 1,
              json.dumps(types))
        if fallbacks:
            check("fallback target is demo",
                  fallbacks[0]["data"]["to"] == f"{p_demo}::demo-reasoner",
                  json.dumps(fallbacks[0]["data"], ensure_ascii=False))
        check("turn_meta has real model", bool(meta) and meta["data"]["model"] == "demo-reasoner",
              json.dumps(meta and meta["data"], ensure_ascii=False))
        check("turn_meta has verbatim API usage",
              bool(meta and meta["data"].get("usage", {}).get("prompt_tokens") == 128
              and meta["data"]["usage"].get("completion_tokens") == 96),
              json.dumps(meta and meta["data"].get("usage")))
        check("turn_meta: usage stays verbatim (no invented total)",
              bool(meta) and "total_tokens" not in meta["data"].get("usage", {}))
        check("turn_meta: no invented usage/cost",
              bool(meta) and "usage_estimated" not in meta["data"]
              and "cost_usd" not in meta["data"])
        check("tool call in history", "tool_call" in types and "tool_result" in types)

        # -- context meter: fed by the last REAL usage from the API
        st, ctx = await call("GET", f"/api/chats/{chat_id}/context")
        check("context: real prompt_tokens from usage",
              st == 200 and ctx["prompt_tokens"] > 0
              and ctx["usage"].get("prompt_tokens") == ctx["prompt_tokens"],
              json.dumps(ctx))
        check("context: window + percent from catalog",
              ctx["context_length"] == 128000 and ctx["percent"] is not None
              and 0 < ctx["percent"] < 50)
        check("context: no estimator fields",
              "estimated_tokens" not in ctx and "exact" not in ctx)

        # -- usage aggregate (real numbers only)
        st, usage = await call("GET", f"/api/chats/{chat_id}/usage")
        check("usage aggregate", st == 200 and usage["turns"] == 1
              and usage["turns_with_usage"] == 1 and usage["total_tokens"] > 0
              and "cost_usd" not in usage,
              json.dumps(usage))

        # -- model overrides survive resync (applied to the chat's own model
        #    so the context meter reflects it)
        key = f"{p_flaky}::flaky-1"
        st, body = await call("PATCH", "/api/models/override",
                              json={"model_key": key,
                                    "override": {"context_length": 64000}})
        check("override saved", st == 200 and body["model"]["context_length"] == 64000)
        st, _ = await call("GET", f"/api/providers/{p_demo}/models")  # resync
        st, body = await call("GET", f"/api/models?provider={p_flaky}")
        m = next(x for x in body["models"] if x["model_id"] == "flaky-1")
        check("override survives resync", m["context_length"] == 64000
              and m["override"] is not None)
        st, ctx2 = await call("GET", f"/api/chats/{chat_id}/context")
        check("context meter uses override", ctx2["context_length"] == 64000)
        st, body = await call("DELETE", f"/api/models/override?model_key={key}")
        check("override cleared", body["model"]["context_length"] == 128000)

        # -- MCP discovery: NATIVE tool names, internal routing
        st, body = await call("POST", "/api/mcp", json={"url": mcp_url})
        check("mcp discovered", st == 201 and len(body["tools"]) == 2,
              json.dumps(body, ensure_ascii=False)[:200])
        tool_ids = sorted(t["id"] for t in body["tools"])
        check("mcp tools keep native names (no mcp__ prefixes)",
              tool_ids == ["add", "echo_time"], str(tool_ids))

        st, body = await call("GET", "/api/mcp")
        check("mcp card data", st == 200 and body["servers"][0]["prompts_count"] == 1
              and body["servers"][0]["resources_count"] == 1)

        # direct tool call through the shared registry — name as the model sees it
        res = await tool_registry.execute("echo_time", {"fmt": "iso"})
        check("mcp tool call by native name", res.get("text") == "11:22:33", json.dumps(res))
        check("mcp tool in registry for the model", "add" in tool_registry.names())

        # -- name collision: same server added twice -> _2 suffix, routing intact
        st, body = await call("POST", "/api/mcp", json={"url": mcp_url})
        dup_ids = sorted(t["id"] for t in body["tools"])
        check("collision gets _2 suffix", dup_ids == ["add_2", "echo_time_2"], str(dup_ids))
        res = await tool_registry.execute("add_2", {"a": 40, "b": 2})
        check("routed tool still callable", res.get("text") == "42", json.dumps(res))
        dup_server_id = body["server"]["id"]
        st, _ = await call("DELETE", f"/api/mcp/{dup_server_id}")
        check("duplicate server deleted", st == 200)
        check("suffixed names unregistered",
              "add_2" not in tool_registry.names()
              and "echo_time_2" not in tool_registry.names())

        # -- diagnostics
        st, body = await call("GET", "/api/diagnostics")
        ok_prov = all(p["status"] == "online" for p in body["providers"])
        ok_mcp = all(mm["status"] == "online" for mm in body["mcp"])
        check("diagnostics: providers online", st == 200 and ok_prov,
              json.dumps(body.get("providers")))
        check("diagnostics: mcp online", ok_mcp, json.dumps(body.get("mcp")))

    await mcp_runner.cleanup()
    await runner.cleanup()   # cleanup_ctx closes mcp session + both aiosqlite stores

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    for name, detail in FAIL:
        print(f"  FAILED: {name} {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
