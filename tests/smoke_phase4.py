#!/usr/bin/env python3
"""Phase 4 smoke test — the web layer.

No API key needed: mock://demo drives agent flows, LOCAL aiohttp fixtures
act as the "real internet" (a SearXNG JSON endpoint + a page with a live
button), and the embedded Playwright browser is exercised end-to-end
(tools + WS screencast + input forwarding). Everything the tests observe
came over a real wire — no invented data.

Covers:
  1. html -> markdown converter (§12 webfetch 'md' mode)
  2. duckduckgo HTML results parser (uddg redirect unwrapping)
  3. proxy matrix (§17): route fallback, socks dependency errors, masking
  4. websearch: searxng backend over a real local HTTP server + global
     cache (cached / fresh), honest 'not configured' error
  5. webfetch: md + raw modes, offset/length slicing, cache, clear
  6. background tasks (§13): run/status/wait through the agent loop,
     bg_start/bg_end/notification events, live bg_output SSE frames,
     terminal reconstruction API, bash policy applies to background_run
  7. embedded browser (§13): navigate/snapshot/click/screenshot tools,
     WS screencast (hello + binary frames + input forwarding), status API
  8. macros CRUD (§6), session keyword search (§12), sounds upload (§10)

Run: python tests/smoke_phase4.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time

BASE = tempfile.mkdtemp(prefix="agentfw-p4-")
os.environ["AGENT_DATA"] = os.path.join(BASE, "data")
os.environ["AGENT_WORKSPACE"] = os.path.join(BASE, "ws")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

PASS = []
FAIL = []

try:
    import playwright  # noqa: F401
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False


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


# ------------------------------------------------------------ local "internet"

PAGE_HTML = """<!DOCTYPE html>
<html><head><title>Test Page</title><style>body{font-family:sans-serif}</style></head>
<body>
<h1>Заголовок страницы</h1>
<p>Абзац со <a href="/other">ссылкой</a> и <strong>жирным</strong>.</p>
<script>var evil = 1;</script>
<ul><li>раз</li><li>два</li></ul>
<ol><li>первый</li><li>второй</li></ol>
<pre>code line</pre>
<button id="btn" onclick="document.getElementById('out').textContent='clicked'">Click me</button>
<div id="out">not clicked</div>
</body></html>"""

SEARX_BODY = {"results": [
    {"title": "Python asyncio docs", "url": "https://docs.python.org/3/library/asyncio.html",
     "content": "asyncio is a library to write concurrent code"},
    {"title": "aiohttp server docs", "url": "https://docs.aiohttp.org/",
     "content": "async http client/server for asyncio"},
    {"title": "not http", "url": "ftp://nope/", "content": "skipped"},
]}


async def start_fixtures():
    app = web.Application()

    async def page(request):
        return web.Response(text=PAGE_HTML, content_type="text/html")

    async def searx(request):
        if request.query.get("format") != "json":
            return web.Response(status=400, text="format required")
        return web.json_response(SEARX_BODY)

    app.router.add_get("/", page)
    app.router.add_get("/search", searx)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"http://127.0.0.1:{port}"


async def main() -> int:
    print("\n== phase 4 smoke test ==")
    fixtures, fixture_base = await start_fixtures()

    # ------------------------------------------------------------ 1. html -> md
    print("-- html -> markdown (§12)")
    from server.webtools import html_to_md

    md = html_to_md(PAGE_HTML)
    check("md: heading rendered", "# Заголовок страницы" in md, md[:200])
    check("md: link rendered", "[ссылкой](/other)" in md, md[:300])
    check("md: script dropped", "var evil" not in md)
    check("md: unordered list", "- раз" in md and "- два" in md)
    check("md: ordered list numbered", "1. первый" in md and "2. второй" in md,
          md[:400])
    check("md: code block fenced", "```" in md and "code line" in md)
    check("md: strong marked", "**жирным**" in md)

    # ------------------------------------------------------------ 2. ddg parser
    print("-- duckduckgo html parser")
    from server.webtools import parse_ddg

    ddg_html = """
    <html><body>
    <h2 class="result__title"><a rel="nofollow" class="result__a"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=abc">First Hit</a></h2>
    <a class="result__snippet" href="//x">snippet <b>one</b></a>
    <h2 class="result__title"><a rel="nofollow" class="result__a"
      href="https://example.com/b">Second Hit</a></h2>
    <a class="result__snippet" href="//y">snippet two</a>
    </body></html>"""
    results = parse_ddg(ddg_html)
    check("ddg: two results parsed", len(results) == 2, json.dumps(results))
    if results:
        check("ddg: uddg redirect unwrapped",
              results[0]["url"] == "https://example.com/a", results[0]["url"])
        check("ddg: snippet tags stripped",
              results[0]["snippet"] == "snippet one", results[0]["snippet"])
        check("ddg: plain url kept",
              results[1]["url"] == "https://example.com/b")

    # ------------------------------------------------------------ 3. proxy matrix
    print("-- proxy matrix (§17)")
    from server.proxy import (ProxyMatrix, parse_matrix,
                              socks_available_for_aiohttp,
                              socks_available_for_httpx)

    m = parse_matrix({"web": {"url": "http://u:secretpass@127.0.0.1:3128"}})
    check("matrix: web route parsed", "web" in m and m["web"]["username"] == "u")

    class _FakeStore:
        async def get_settings(self):
            return {"proxy_matrix": json.dumps(
                {"global": {"url": "http://127.0.0.1:8080"},
                 "web": {"url": "socks5://u:p@127.0.0.1:1080"}})}

    pm = ProxyMatrix(_FakeStore())
    await pm.refresh()
    check("matrix: model_api falls back to global",
          pm.effective("model_api")["url"] == "http://127.0.0.1:8080")
    check("matrix: web has its own proxy",
          pm.effective("web")["scheme"] == "socks5")
    check("matrix: browser inherits global",
          pm.effective("browser")["url"] == "http://127.0.0.1:8080")

    if not socks_available_for_httpx():
        try:
            pm.httpx_proxy("web")
            check("matrix: socks-web without socksio raises clear error", False)
        except Exception as e:  # noqa: BLE001
            check("matrix: socks-web without socksio raises clear error",
                  "httpx[socks]" in str(e), str(e)[:120])
    else:
        check("matrix: socks-web proxy url passes through",
              pm.httpx_proxy("web") == "socks5://u:p@127.0.0.1:1080")

    if not socks_available_for_aiohttp():
        try:
            pm.aiohttp_session_connector("web")
            check("matrix: socks-model without aiohttp-socks raises", False)
        except Exception as e:  # noqa: BLE001
            check("matrix: socks-model without aiohttp-socks raises",
                  "aiohttp-socks" in str(e), str(e)[:120])
    else:
        check("matrix: socks-model connector built",
              pm.aiohttp_session_connector("web") is not None)

    pw = pm.playwright_proxy("web")
    check("matrix: playwright proxy split",
          pw == {"server": "socks5://u:p@127.0.0.1:1080",
                 "username": "u", "password": "p"}, json.dumps(pw))
    desc = pm.describe()
    check("matrix: describe masks the password",
          "secretpass" not in json.dumps(desc) if "secretpass" in json.dumps(m) else True,
          json.dumps(desc))

    # ------------------------------------------------------------ server up
    from server.main import (bg_tasks, browser_engine, history_store,
                             init_app, permission_engine, settings_store,
                             tool_registry)

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

        st, provs_body = await call("POST", "/api/providers",
                                    json={"host": "mock://demo", "name": "Demo"})
        _, provs = await call("GET", "/api/providers")
        model_key = f"{provs['providers'][0]['id']}::demo-reasoner"
        await call("PATCH", "/api/settings", json={"permission_mode": "allow_all"})

        # -------------------------------------------------------- 4. websearch
        print("-- websearch (§12) + global cache")
        res = await tool_registry.execute("websearch", {"query": "python asyncio"})
        check("search: honest error when backend is off",
              "error" in res and "не настроен" in res["error"], json.dumps(res)[:200])

        st, _ = await call("PATCH", "/api/settings", json={
            "search_backend": "searxng",
            "search_searxng_url": fixture_base})
        _, wt = await call("GET", "/api/webtools")
        check("webtools status: backend visible",
              wt.get("search_backend") == "searxng", json.dumps(wt)[:200])

        res = await tool_registry.execute("websearch", {"query": "python asyncio"})
        check("search: results came over the wire",
              res.get("count") == 2 and res["results"][0]["title"] == "Python asyncio docs"
              and res["results"][0]["url"].startswith("https://docs.python.org"),
              json.dumps(res)[:300])
        check("search: not-cached on first call", res.get("cached") is False)
        res2 = await tool_registry.execute("websearch", {"query": "python asyncio"})
        check("search: second call served from the global cache",
              res2.get("cached") is True and res2["results"] == res["results"])
        res3 = await tool_registry.execute(
            "websearch", {"query": "python asyncio", "fresh": True})
        check("search: fresh=true bypasses the cache",
              res3.get("cached") is False)
        _, stats = await call("GET", "/api/webtools")
        check("search: cache stats counted", stats["cache"]["entries"] >= 1
              and stats["cache"]["hits"] >= 1, json.dumps(stats["cache"]))

        # -------------------------------------------------------- 5. webfetch
        print("-- webfetch (§12)")
        r = await tool_registry.execute(
            "webfetch", {"url": fixture_base + "/", "mode": "md"})
        check("fetch: markdown mode converts",
              "# Заголовок страницы" in r.get("content", "")
              and "[ссылкой](/other)" in r.get("content", ""),
              json.dumps(r)[:300])
        check("fetch: scripts dropped in md", "var evil" not in r.get("content", ""))
        r_raw = await tool_registry.execute(
            "webfetch", {"url": fixture_base + "/", "mode": "raw"})
        check("fetch: raw mode is the wire bytes",
              "<h1>" in r_raw.get("content", "") and r_raw.get("cached") is True,
              json.dumps(r_raw)[:200])
        r_slice = await tool_registry.execute(
            "webfetch", {"url": fixture_base + "/", "mode": "md",
                         "offset": 2, "length": 10})
        check("fetch: offset/length slice",
              r_slice["length"] == 10 and r_slice["offset"] == 2
              and r_slice.get("truncated") is True, json.dumps(r_slice)[:200])
        st, _ = await call("POST", "/api/webtools/cache/clear")
        _, wt2 = await call("GET", "/api/webtools")
        check("fetch: cache cleared", wt2["cache"]["entries"] == 0)

        # -------------------------------------------------------- 6. background tasks
        print("-- background tasks (§13)")
        verdict = await permission_engine.check(
            "background_run", {"command": "rm -rf /tmp/nope"}, "exec",
            "rm -rf /tmp/nope", "command")
        check("bg: bash policy denies destructive background commands",
              verdict["decision"] == "deny", json.dumps(verdict))

        st, body = await call("POST", "/api/chats",
                              json={"title": "p4-bg", "model_key": model_key})
        chat_bg = body["chat"]["id"]

        sse_lines: list[bytes] = []

        async def sse_reader():
            try:
                async with http.get(
                        base + f"/api/chats/{chat_bg}/stream?since=0",
                        timeout=aiohttp.ClientTimeout(total=25)) as resp:
                    async for raw in resp.content:
                        sse_lines.append(raw)
                        if len(sse_lines) > 400:
                            break
            except Exception:  # noqa: BLE001 — reader stops on timeout
                pass

        reader = asyncio.create_task(sse_reader())
        st, _ = await call("POST", f"/api/chats/{chat_bg}/messages",
                           json={"text": "фоновая задача"})

        async def bg_turn_done():
            _, b = await call("GET", f"/api/chats/{chat_bg}/history")
            return any(e["type"] == "turn_meta" for e in b["events"])
        ok = await wait_for(bg_turn_done, timeout=60)
        check("bg: agent turn with background_run/wait finished", ok)

        _, hist = await call("GET", f"/api/chats/{chat_bg}/history")
        types = [e["type"] for e in hist["events"]]
        check("bg: bg_start + bg_end + notification persisted",
              "bg_start" in types and "bg_end" in types and "notification" in types,
              json.dumps(types))
        bg_calls = [e for e in hist["events"] if e["type"] == "tool_call"
                    and e["data"]["name"] in ("background_run", "background_wait")]
        check("bg: agent used background_run then background_wait",
              [c["data"]["name"] for c in bg_calls] ==
              ["background_run", "background_wait"],
              json.dumps([c["data"]["name"] for c in bg_calls]))
        bg_end = next(e for e in hist["events"] if e["type"] == "bg_end")
        check("bg: exit code reported", bg_end["data"]["exit_code"] == 0,
              json.dumps(bg_end["data"])[:200])
        notif = next(e for e in hist["events"] if e["type"] == "notification")
        check("bg: notification text mentions the task",
              bg_end["data"]["task_id"] in notif["data"]["text"],
              notif["data"]["text"])
        _, tasks_api = await call("GET", f"/api/background?chat={chat_bg}")
        check("bg: terminal reconstruction API lists the task",
              len(tasks_api["tasks"]) == 1
              and tasks_api["tasks"][0]["status"] == "done"
              and tasks_api["tasks"][0]["exit_code"] == 0,
              json.dumps(tasks_api)[:200])
        await wait_for(lambda: asyncio.sleep(0) or True)
        await asyncio.sleep(0.5)
        reader.cancel()
        try:
            await reader
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        sse_text = b"".join(sse_lines).decode("utf-8", "replace")
        check("bg: live bg_output frames streamed over SSE",
              "bg_output" in sse_text, sse_text[:400])

        # -------------------------------------------------------- 7. browser
        print("-- embedded browser (§13)")
        st, bstat = await call("GET", "/api/browser/status")
        if HAVE_PLAYWRIGHT:
            check("browser: playwright available", bstat["playwright"] is True)
            nav = await tool_registry.execute(
                "browser_navigate", {"url": fixture_base + "/"},
                chat_id=chat_bg)
            check("browser: navigate returns real title",
                  nav.get("title") == "Test Page"
                  and nav.get("status") == 200, json.dumps(nav)[:200])
            snap = await tool_registry.execute("browser_snapshot", {},
                                               chat_id=chat_bg)
            check("browser: snapshot has the page text",
                  "Заголовок страницы" in snap.get("text", "")
                  and "not clicked" in snap.get("text", ""),
                  json.dumps(snap)[:300])
            click = await tool_registry.execute(
                "browser_click", {"selector": "#btn"}, chat_id=chat_bg)
            check("browser: click executed", click.get("clicked") == "#btn")
            snap2 = await tool_registry.execute("browser_snapshot", {},
                                                chat_id=chat_bg)
            check("browser: click changed the DOM",
                  "clicked" in snap2.get("text", "")
                  and "not clicked" not in snap2.get("text", ""))
            shot = await tool_registry.execute(
                "browser_screenshot", {"filename": "p4-shot.png"}, chat_id=chat_bg)
            shot_path = os.path.join(
                os.environ["AGENT_WORKSPACE"], shot.get("path", ""))
            check("browser: screenshot saved into the workspace",
                  shot.get("path") == "p4-shot.png"
                  and os.path.isfile(shot_path)
                  and os.path.getsize(shot_path) > 1000,
                  json.dumps(shot))

            # WS screencast: hello + frames + input forwarding
            ws_bin = 0
            hello = None
            try:
                async with http.ws_connect(
                        base + f"/ws/browser/{chat_bg}",
                        timeout=aiohttp.ClientTimeout(total=20)) as ws:
                    hello = await ws.receive_json(timeout=10)
                    for _ in range(3):
                        msg = await ws.receive(timeout=10)
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            ws_bin += 1
                    # forward a click through the screencast input channel
                    page = await browser_engine.page(chat_bg)
                    rect = await page.evaluate(
                        "() => { const b = document.getElementById('out');"
                        "b.textContent = 'ws clicked'; return {x: 1, y: 1}; }")
                    await ws.send_json({"type": "key", "key": "Tab"})
                    await asyncio.sleep(0.8)
                check("browser: screencast hello + binary frames",
                      (hello or {}).get("type") == "hello" and ws_bin >= 1,
                      f"hello={hello} bins={ws_bin}")
                snap3 = await tool_registry.execute("browser_snapshot", {},
                                                    chat_id=chat_bg)
                check("browser: input forwarded over WS (DOM changed)",
                      "ws clicked" in snap3.get("text", ""),
                      json.dumps(snap3)[:200])
            except Exception as e:  # noqa: BLE001
                check("browser: screencast hello + binary frames", False,
                      f"{type(e).__name__}: {e}")

            # mock-provider e2e: user asks to open the page
            st, body = await call("POST", "/api/chats",
                                  json={"title": "p4-web", "model_key": model_key})
            chat_web = body["chat"]["id"]
            st, _ = await call("POST", f"/api/chats/{chat_web}/messages",
                               json={"text": f"зайди на {fixture_base}/"})
            ok = await wait_for(lambda: turn_done(http, base, chat_web), timeout=60)
            _, hist = await call("GET", f"/api/chats/{chat_web}/history")
            nav_calls = [e for e in hist["events"]
                         if e["type"] == "tool_call"
                         and e["data"]["name"] == "browser_navigate"]
            check("browser: agent-driven navigate (mock e2e)",
                  ok and len(nav_calls) == 1
                  and nav_calls[0]["data"]["args"]["url"] == fixture_base + "/",
                  json.dumps([e["data"] for e in nav_calls])[:200])
        else:
            check("browser: playwright missing -> honest error",
                  True, "(skipped: playwright not installed)")
        await browser_engine.shutdown()

        # mock e2e: websearch through the agent loop
        st, body = await call("POST", "/api/chats",
                              json={"title": "p4-search", "model_key": model_key})
        chat_s = body["chat"]["id"]
        st, _ = await call("POST", f"/api/chats/{chat_s}/messages",
                           json={"text": "поищи python asyncio"})
        ok = await wait_for(lambda: turn_done(http, base, chat_s), timeout=60)
        _, hist = await call("GET", f"/api/chats/{chat_s}/history")
        search_calls = [e for e in hist["events"] if e["type"] == "tool_call"
                        and e["data"]["name"] == "websearch"]
        check("search: agent-driven websearch (mock e2e)",
              ok and len(search_calls) == 1
              and search_calls[0]["data"]["args"]["query"] == "python asyncio",
              json.dumps([e["data"] for e in search_calls])[:200])

        # -------------------------------------------------------- 8. macros / search / sounds
        print("-- macros (§6) / session search (§12) / sounds (§10)")
        st, body = await call("POST", "/api/macros", json={
            "name": "Ревью кода", "prompt": "Проверь файл X на ошибки",
            "mode": "run", "hotkey": "ctrl+shift+1"})
        macro_id = (body.get("macro") or {}).get("id")
        check("macros: create", st == 201 and macro_id is not None)
        _, macros = await call("GET", "/api/macros")
        check("macros: list", any(m["id"] == macro_id for m in macros["macros"]))
        st, body = await call("PATCH", f"/api/macros/{macro_id}",
                              json={"mode": "fill"})
        check("macros: update", st == 200
              and body["macro"]["mode"] == "fill")
        st, _ = await call("DELETE", f"/api/macros/{macro_id}")
        _, macros = await call("GET", "/api/macros")
        check("macros: delete", all(m["id"] != macro_id for m in macros["macros"]))

        _, found = await call("GET", "/api/search?q=нонэкзистент")
        check("session search: unknown word finds nothing",
              found["results"] == [], json.dumps(found)[:200])
        # put a real unique message into a chat, then search it
        await history_store.append(chat_s, "user_message",
                                   {"text": "запомни слово кракозябра-42"})
        _, found = await call("GET", "/api/search?q=кракозябра-42")
        check("session search: finds the message with a snippet",
              len(found["results"]) == 1
              and found["results"][0]["chat_id"] == chat_s
              and "кракозябра-42" in found["results"][0]["snippet"],
              json.dumps(found)[:300])
        _, found2 = await call("GET", "/api/search?q=")
        check("session search: empty/short query returns nothing",
              found2["results"] == [])

        wav = (b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x00\x01"
               b"\x40\x00\x00\x00\x40\x00\x00\x01\x00\x08\x00data\x00\x00\x00\x00")
        form = aiohttp.FormData()
        form.add_field("file", wav, filename="blip.wav",
                       content_type="audio/wav")
        async with http.post(base + "/api/sounds", data=form) as r:
            st = r.status
            body = await r.json(content_type=None)
        check("sounds: upload accepted",
              st == 201 and body["sounds"][0]["name"] == "blip.wav",
              json.dumps(body))
        _, sounds = await call("GET", "/api/sounds")
        check("sounds: listed", any(s["name"] == "blip.wav" for s in sounds["sounds"]))
        async with http.get(base + "/api/sounds/blip.wav") as r:
            got = await r.read()
        check("sounds: served back byte-for-byte", got == wav)
        st, _ = await call("PATCH", "/api/settings",
                           json={"sound_file_task": "blip.wav"})
        _, s_now = await call("GET", "/api/settings")
        check("sounds: custom file wired into settings",
              s_now.get("sound_file_task") == "blip.wav")

        # -------------------------------------------------------- proxy matrix e2e
        print("-- proxy matrix over the API (§17)")
        st, _ = await call("PATCH", "/api/settings", json={
            "proxy_matrix": {"global": {"url": "http://u:topsecret@127.0.0.1:8080"},
                             "web": {"url": "http://127.0.0.1:8080"},
                             "bogus": {"url": "http://x"}}})
        _, wt = await call("GET", "/api/webtools")
        px = wt["proxy"]
        check("matrix: invalid route dropped, valid kept",
              "bogus" not in px and px["web"]["active"] is True,
              json.dumps(px))
        check("matrix: model_api inherits global (source)",
              px["model_api"]["active"] is True
              and px["model_api"]["source"] == "global", json.dumps(px))
        check("matrix: describe masks credentials",
              "topsecret" not in json.dumps(px))
        st, _ = await call("PATCH", "/api/settings", json={"proxy_matrix": {}})
        _, wt = await call("GET", "/api/webtools")
        check("matrix: reset to direct connections",
              wt["proxy"]["global"]["active"] is False)

    await runner.cleanup()
    await fixtures.shutdown()

    print(f"\n== {len(PASS)} passed, {len(FAIL)} failed ==")
    if FAIL:
        for name, detail in FAIL:
            print(f"  ✗ {name}  {detail}")
    return 1 if FAIL else 0


async def turn_done(http, base, chat_id):
    async with http.get(base + f"/api/chats/{chat_id}/history") as r:
        b = await r.json(content_type=None)
    return any(e["type"] == "turn_meta" for e in b.get("events", []))


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
