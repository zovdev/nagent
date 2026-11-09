"""Embedded browser engine (§13 roadmap): server-side Playwright.

- The browser lives on the SERVER — closing the browser tab in the UI does
  not kill the session; the agent keeps driving it with tools.
- One browser context per chat (isolated cookies/proxy), one active page.
- Engine management uses Playwright's OWN facilities only: installed
  browsers are detected via each engine's `executable_path`, installation
  runs `python -m playwright install <engine>`. NO hardcoded chromium:
  the engine is a setting; default = first installed engine.
- When nothing is installed, tool results carry `browser_missing` + the
  engine list so the UI can offer a choice and an install button.
- Proxy: the `browser` row of the §17 matrix, passed to Playwright launch.
- Downloads land in `<workspace>/downloads/`.
- Screencast: the ONLY WebSocket in the system — JPEG frames pushed to the
  UI at an adaptive fps, with optional input forwarding (click / type /
  key / scroll) from the client.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
from collections import deque

from .providers import ToolSpec

SNAPSHOT_MAX_CHARS = 12000
SCREENCAST_FPS_MIN = 0.5
SCREENCAST_FPS_MAX = 8.0

try:
    from playwright.async_api import async_playwright  # type: ignore
    PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    PLAYWRIGHT_IMPORTABLE = False

# Playwright's three engines, in native order. The FIRST installed one is
# the default selection (§13 browser settings).
ENGINES: tuple[tuple[str, str], ...] = (("chromium", "Chromium"),
                                        ("firefox", "Firefox"),
                                        ("webkit", "WebKit"))


class BrowserError(Exception):
    def __init__(self, message: str, *, missing: bool = False):
        super().__init__(message)
        self.missing = missing


def _no_browser_payload() -> dict:
    """Structured answer for tools/UI: nothing installed, here's the choice."""
    engines = {eid: name for eid, name in ENGINES}
    return {"error": "браузер не установлен: выберите движок в модалке "
                     "(или в Настройках → Браузер) и установите его",
            "browser_missing": True,
            "engines": engines,
            "hint": "python -m playwright install <engine>"}


def _not_installed() -> dict:
    if not PLAYWRIGHT_IMPORTABLE:
        return {"error": "playwright не установлен: pip install playwright",
                "playwright_missing": True}
    return _no_browser_payload()


class BrowserEngine:
    def __init__(self, proxy_matrix, jail, settings_store=None):
        self.proxy = proxy_matrix
        self.jail = jail
        self.store = settings_store
        self._pw = None                       # async_playwright context
        self._browser = None
        self._contexts: dict[str, object] = {}   # chat_id -> BrowserContext
        self._pages: dict[str, object] = {}      # chat_id -> Page
        self._launch_error: str | None = None
        self._lock = asyncio.Lock()
        self._engine_lock = asyncio.Lock()
        self._installed_cache: dict[str, bool] | None = None
        self._install_tasks: dict[str, asyncio.Task] = {}
        self._install_log: dict[str, deque] = {}

    # -- engine management (native Playwright detection/install) -------------

    async def _pw_ctx(self):
        """One shared async_playwright() context for detection AND launches."""
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self._pw

    async def _installed(self) -> dict[str, bool]:
        """Native detection: each engine's `executable_path` computed by
        Playwright itself (honours PLAYWRIGHT_BROWSERS_PATH)."""
        if not PLAYWRIGHT_IMPORTABLE:
            return {eid: False for eid, _ in ENGINES}
        if self._installed_cache is not None:
            return self._installed_cache
        pw = await self._pw_ctx()
        out: dict[str, bool] = {}
        for eid, _ in ENGINES:
            try:
                out[eid] = os.path.exists(getattr(pw, eid).executable_path)
            except Exception:  # noqa: BLE001 — engine unknown on platform
                out[eid] = False
        self._installed_cache = out
        return out

    async def selected_engine(self) -> str | None:
        """Setting first; else the first INSTALLED engine (§13 default)."""
        stored = None
        if self.store is not None:
            try:
                s = await self.store.get_settings()
                stored = s.get("browser_engine") or None
            except Exception:  # noqa: BLE001 — settings read is best-effort
                stored = None
        if stored and stored in dict(ENGINES):
            return stored
        installed = await self._installed()
        for eid, _ in ENGINES:
            if installed.get(eid):
                return eid
        return None

    async def list_browsers(self) -> dict:
        installed = await self._installed()
        selected = await self.selected_engine()
        engines = []
        for eid, name in ENGINES:
            engines.append({
                "id": eid, "name": name,
                "installed": installed.get(eid, False),
                "selected": eid == selected,
                "installing": eid in self._install_tasks and
                              not self._install_tasks[eid].done(),
                "log": list(self._install_log.get(eid, []))[-6:],
            })
        return {"playwright": PLAYWRIGHT_IMPORTABLE,
                "engines": engines,
                "selected": selected,
                "launched": self._browser is not None,
                "error": self._launch_error}

    async def select_engine(self, engine_id: str) -> dict:
        if engine_id not in dict(ENGINES):
            return {"error": "неизвестный движок"}
        installed = await self._installed()
        if not installed.get(engine_id):
            return {"error": "движок не установлен — сначала установка"}
        if self.store is not None:
            await self.store.set_settings({"browser_engine": engine_id})
        # the running browser keeps serving until the next relaunch;
        # switching mid-session would kill agent pages
        return await self.list_browsers()

    async def install_engine(self, engine_id: str) -> dict:
        """`python -m playwright install <engine>` — Playwright's own
        installer, streamed into a log the UI polls."""
        if not PLAYWRIGHT_IMPORTABLE:
            return {"error": "playwright не установлен: pip install playwright"}
        if engine_id not in dict(ENGINES):
            return {"error": "неизвестный движок"}
        task = self._install_tasks.get(engine_id)
        if task and not task.done():
            return {"error": "установка уже идёт"}
        log = deque(maxlen=30)
        self._install_log[engine_id] = log

        async def _run() -> None:
            log.append(f"$ python -m playwright install {engine_id}")
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", engine_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            except Exception as e:  # noqa: BLE001 — surfaced in the log
                log.append(f"запуск не удался: {e}")
                return
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log.append(line)
            await proc.wait()
            log.append(f"[exit {proc.returncode}]")
            self._installed_cache = None      # re-detect after install

        self._install_tasks[engine_id] = asyncio.create_task(_run())
        return {"started": True, "engine": engine_id}

    # -- lifecycle ----------------------------------------------------------------

    async def status(self) -> dict:
        info = await self.list_browsers()
        info["sessions"] = sorted(self._pages)
        return info

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        if not PLAYWRIGHT_IMPORTABLE:
            raise BrowserError("playwright не установлен")
        async with self._lock:
            if self._browser is not None:
                return self._browser
            engine = await self.selected_engine()
            if engine is None:
                raise BrowserError("ни один браузер playwright не установлен",
                                    missing=True)
            try:
                self._pw = await self._pw_ctx()
                launch_kwargs = {"headless": True}
                pw_proxy = self.proxy.playwright_proxy("browser")
                if pw_proxy:
                    launch_kwargs["proxy"] = pw_proxy
                engine_api = getattr(self._pw, engine)
                self._browser = await engine_api.launch(**launch_kwargs)
                self._launch_error = None
            except BrowserError:
                raise
            except Exception as e:  # noqa: BLE001 — surfaced to the user
                self._launch_error = str(e)[:300]
                raise BrowserError(f"{engine} не запустился: {self._launch_error}")
        return self._browser

    async def _context(self, chat_id: str):
        if chat_id in self._contexts:
            return self._contexts[chat_id]
        browser = await self._ensure_browser()
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=True)
        downloads = os.path.join(self.jail.root, "downloads")
        os.makedirs(downloads, exist_ok=True)

        async def _save(download) -> None:
            try:
                target = os.path.join(downloads, re.sub(
                    r"[^A-Za-z0-9._-]", "_", download.suggested_filename or "file"))
                await download.save_as(target)
            except Exception:  # noqa: BLE001 — best-effort download
                pass
        ctx.on("download", lambda d: asyncio.ensure_future(_save(d)))
        self._contexts[chat_id] = ctx
        return ctx

    async def page(self, chat_id: str):
        """Active page for the chat (created on demand)."""
        if chat_id in self._pages:
            page = self._pages[chat_id]
            if not page.is_closed():
                return page
            self._pages.pop(chat_id, None)
        ctx = await self._context(chat_id)
        page = await ctx.new_page()
        self._pages[chat_id] = page
        return page

    async def close_session(self, chat_id: str) -> dict:
        page = self._pages.pop(chat_id, None)
        if page and not page.is_closed():
            await page.close()
        ctx = self._contexts.pop(chat_id, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001
                pass
        return {"closed": True, "chat_id": chat_id}

    async def shutdown(self) -> None:
        for chat_id in list(self._contexts):
            await self.close_session(chat_id)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None

    # -- agent-facing operations -----------------------------------------------------

    async def navigate(self, chat_id: str, url: str) -> dict:
        if not PLAYWRIGHT_IMPORTABLE:
            return _not_installed()
        url = (url or "").strip()
        if not url:
            return {"error": "пустой url"}
        if "://" not in url:
            url = "https://" + url
        try:
            page = await self.page(chat_id)
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=45000)
        except BrowserError as e:
            return _browser_error(e)
        except Exception as e:  # noqa: BLE001 — playwright errors are verbose
            return {"error": f"навигация не удалась: {str(e)[:300]}"}
        title = await page.title()
        return {"url": page.url, "title": title,
                "status": resp.status if resp else None,
                "hint": "browser_snapshot() вернёт текст страницы"}

    async def snapshot(self, chat_id: str, max_chars: int = SNAPSHOT_MAX_CHARS) -> dict:
        if not PLAYWRIGHT_IMPORTABLE:
            return _not_installed()
        try:
            page = await self.page(chat_id)
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        except BrowserError as e:
            return _browser_error(e)
        except Exception as e:  # noqa: BLE001
            return {"error": f"снапшот не удался: {str(e)[:300]}"}
        text = (text or "").strip()
        return {"url": page.url, "title": await page.title(),
                "text": text[:max_chars],
                "total_chars": len(text),
                "truncated": len(text) > max_chars}

    async def click(self, chat_id: str, selector: str) -> dict:
        if not PLAYWRIGHT_IMPORTABLE:
            return _not_installed()
        try:
            page = await self.page(chat_id)
            await page.click(selector, timeout=15000)
        except BrowserError as e:
            return _browser_error(e)
        except Exception as e:  # noqa: BLE001
            return {"error": f"клик не удался: {str(e)[:300]}"}
        await asyncio.sleep(0.3)              # let the page react
        return {"clicked": selector, "url": page.url}

    async def type_text(self, chat_id: str, selector: str, text: str,
                        press_enter: bool = False) -> dict:
        if not PLAYWRIGHT_IMPORTABLE:
            return _not_installed()
        try:
            page = await self.page(chat_id)
            await page.fill(selector, text or "", timeout=15000)
            if press_enter:
                await page.press(selector, "Enter")
        except BrowserError as e:
            return _browser_error(e)
        except Exception as e:  # noqa: BLE001
            return {"error": f"ввод не удался: {str(e)[:300]}"}
        return {"typed": True, "selector": selector, "url": page.url}

    async def screenshot(self, chat_id: str, filename: str = "") -> dict:
        if not PLAYWRIGHT_IMPORTABLE:
            return _not_installed()
        try:
            page = await self.page(chat_id)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "") or \
                f"screenshot-{int(time.time())}.png"
            if not name.endswith(".png"):
                name += ".png"
            path = os.path.join(self.jail.root, name)
            await page.screenshot(path=path, timeout=20000)
        except BrowserError as e:
            return _browser_error(e)
        except Exception as e:  # noqa: BLE001
            return {"error": f"скриншот не удался: {str(e)[:300]}"}
        return {"path": name, "bytes": os.path.getsize(path),
                "hint": "файл в рабочей папке — юзер видит его во вкладке «Файлы»"}

    # -- screencast (the only WS in the system, §13) --------------------------------

    async def _send_hello(self, page, ws) -> None:
        """URL/title state push — on connect and after every navigation."""
        try:
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            await ws.send_json({"type": "hello", "width": viewport["width"],
                                "height": viewport["height"], "url": page.url,
                                "title": await page.title()})
        except Exception:  # noqa: BLE001 — ws may be closing
            pass

    async def screencast(self, chat_id: str, ws) -> None:
        """Serve a WebSocket: pushes JPEG frames + accepts input events."""
        if not PLAYWRIGHT_IMPORTABLE:
            await ws.send_json({"type": "error", **_not_installed()})
            await ws.close()
            return
        try:
            page = await self.page(chat_id)
        except BrowserError as e:
            await ws.send_json({"type": "error", "message": str(e),
                                **_browser_error(e)})
            await ws.close()
            return

        await self._send_hello(page, ws)

        producer = asyncio.create_task(self._frame_loop(page, ws))
        try:
            async for msg in ws:
                if msg.type == 8:  # aiohttp WSMsgType.CLOSE
                    break
                try:
                    data = json.loads(msg.data) if msg.data else {}
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._handle_input(page, data)
                if data.get("type") == "goto" and isinstance(data.get("url"), str):
                    url = str(data["url"]).strip()
                    if url and "://" not in url \
                            and not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
                        url = "https://" + url    # bare host typed in the bar
                    try:
                        if not url:
                            raise ValueError("пустой url")
                        await page.goto(url, wait_until="domcontentloaded",
                                        timeout=45000)
                    except Exception as e:  # noqa: BLE001 — keep the session alive
                        try:
                            await ws.send_json({"type": "nav-error",
                                                "message": f"не открылось: "
                                                           f"{str(e)[:160]}"})
                        except Exception:  # noqa: BLE001
                            pass
                if data.get("type") in ("goto", "back", "forward", "reload"):
                    await asyncio.sleep(0.3)  # let the URL settle
                    await self._send_hello(page, ws)
        finally:
            producer.cancel()
            try:
                await producer
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _frame_loop(self, page, ws) -> None:
        """Adaptive fps: fast frames raise it, slow ones lower it."""
        fps = 2.0
        while True:
            t0 = time.monotonic()
            try:
                shot = await page.screenshot(type="jpeg", quality=60,
                                             timeout=8000)
                await ws.send_bytes(shot)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — page may be navigating
                await asyncio.sleep(0.5)
                continue
            dt = time.monotonic() - t0
            # frame cost ~ dt: adapt fps toward the 30..2000 ms budget
            if dt < 0.05:
                fps = min(fps + 1.0, SCREENCAST_FPS_MAX)
            elif dt > 0.4:
                fps = max(fps - 1.0, SCREENCAST_FPS_MIN)
            await asyncio.sleep(max(1.0 / fps - dt, 0.0))

    async def _handle_input(self, page, data: dict) -> None:
        """Optional input forwarding from the screencast client (§13).
        Navigation actions (back/forward/reload) come from the browser toolbar."""
        try:
            kind = data.get("type")
            if kind == "click":
                x = float(data.get("x", 0))
                y = float(data.get("y", 0))
                await page.mouse.click(x, y)
            elif kind == "dblclick":
                await page.mouse.dblclick(float(data.get("x", 0)),
                                          float(data.get("y", 0)))
            elif kind == "scroll":
                await page.mouse.wheel(int(data.get("dx", 0)),
                                       int(data.get("dy", 0)))
            elif kind == "type":
                await page.keyboard.type(str(data.get("text", "")), delay=10)
            elif kind == "key":
                await page.keyboard.press(str(data.get("key", "Enter")))
            elif kind == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=15000)
            elif kind == "forward":
                await page.go_forward(wait_until="domcontentloaded", timeout=15000)
            elif kind == "reload":
                await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001 — input forwarding is best-effort
            pass


# ----------------------------------------------------------------- registration

def _browser_error(e: BrowserError) -> dict:
    """BrowserError -> tool result; keeps the machine-readable missing flag
    so the UI can offer engine choice + install (§13 browser settings)."""
    if getattr(e, "missing", False):
        return _no_browser_payload()
    return {"error": str(e)}


def register_browser_tools(registry, engine: BrowserEngine) -> None:
    """Deferred tools (§6): schemas join the prompt only after tool_load."""

    async def _navigate(chat_id: str = "", url: str = "") -> dict:
        return await engine.navigate(chat_id, url)

    async def _snapshot(chat_id: str = "") -> dict:
        return await engine.snapshot(chat_id)

    async def _click(chat_id: str = "", selector: str = "") -> dict:
        return await engine.click(chat_id, selector)

    async def _type(chat_id: str = "", selector: str = "", text: str = "",
                    press_enter: bool = False) -> dict:
        return await engine.type_text(chat_id, selector, text, press_enter)

    async def _screenshot(chat_id: str = "", filename: str = "") -> dict:
        return await engine.screenshot(chat_id, filename)

    async def _close(chat_id: str = "") -> dict:
        return await engine.close_session(chat_id)

    registry.register(ToolSpec(
        "browser_navigate", "Открыть URL во встроенном серверном браузере "
                            "(headless Chromium). Сессия живёт на сервере — "
                            "закрытие вкладки UI её не убивает.",
        {"type": "object", "properties": {
            "url": {"type": "string"}},
         "required": ["url"], "additionalProperties": False},
        accepts_timeout=True),
        _navigate, perm_class="read", deferred=True, default_timeout=50,
        needs_chat=True)

    registry.register(ToolSpec(
        "browser_snapshot", "Текстовое содержимое текущей страницы "
                            "(body.innerText, обрезается по размеру).",
        {"type": "object", "properties": {}, "additionalProperties": False}),
        _snapshot, perm_class="read", deferred=True, default_timeout=20,
        needs_chat=True)

    registry.register(ToolSpec(
        "browser_click", "Кликнуть по CSS-селектору на странице.",
        {"type": "object", "properties": {
            "selector": {"type": "string"}},
         "required": ["selector"], "additionalProperties": False},
        accepts_timeout=True),
        _click, perm_class="write", deferred=True, default_timeout=25,
        needs_chat=True)

    registry.register(ToolSpec(
        "browser_type", "Ввести текст в поле (CSS-селектор); press_enter — "
                        "нажать Enter после ввода.",
        {"type": "object", "properties": {
            "selector": {"type": "string"}, "text": {"type": "string"},
            "press_enter": {"type": "boolean"}},
         "required": ["selector", "text"], "additionalProperties": False},
        accepts_timeout=True),
        _type, perm_class="write", deferred=True, default_timeout=25,
        needs_chat=True)

    registry.register(ToolSpec(
        "browser_screenshot", "Скриншот страницы -> PNG в рабочую папку "
                              "(юзер увидит во вкладке «Файлы»).",
        {"type": "object", "properties": {
            "filename": {"type": "string"}},
         "additionalProperties": False},
        accepts_timeout=True),
        _screenshot, perm_class="read", deferred=True, default_timeout=30,
        needs_chat=True)

    registry.register(ToolSpec(
        "browser_close", "Закрыть браузерную сессию этого чата "
                         "(куки и страница сбрасываются).",
        {"type": "object", "properties": {}, "additionalProperties": False}),
        _close, perm_class="read", deferred=True, default_timeout=15,
        needs_chat=True)
