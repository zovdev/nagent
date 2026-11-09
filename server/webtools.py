"""Web tools (§12 roadmap): websearch + webfetch with a GLOBAL server cache.

Everything honors the "real data only" principle:
  - search results come from the configured REAL backend (duckduckgo HTML /
    SearXNG / Brave API) — never fabricated; an unset backend is an honest
    error, not a guess;
  - webfetch returns what the wire actually returned (raw) plus a local
    Markdown rendering of it — clearly two different things, both labelled.

Cache (main.db, global for the whole server — queries are anonymous):
  - websearch: query+backend -> results;
  - webfetch : normalized URL -> {raw, md, content_type, status};
  - `fresh=true` bypasses the read and overwrites the entry;
  - eviction: TTL + LRU-by-last_used + total size cap.

Proxy: the `web` route of the §17 matrix, via httpx.AsyncClient.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

from .providers import ToolSpec

FETCH_CAP_BYTES = 2 * 1024 * 1024      # a fetched page is capped at 2 MB
FETCH_DEFAULT_CHARS = 4000             # default slice returned to the model
FETCH_MAX_CHARS = 20000
SEARCH_RESULTS_CAP = 8

BACKENDS = ("off", "duckduckgo", "searxng", "brave")


class WebToolsError(Exception):
    pass


# ----------------------------------------------------------------- html -> md

class _MdExtractor(HTMLParser):
    """Stdlib HTML -> Markdown renderer. Structure over fidelity: headings,
    lists, links, code blocks, quotes and paragraphs survive; scripts/styles
    are dropped. The RAW fetch mode always shows the original bytes."""

    BLOCK = {"p", "div", "section", "article", "header", "footer", "nav",
             "table", "tr", "figure", "main", "aside", "form"}
    HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "####",
                "h5": "#####", "h6": "######"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0                    # inside script/style/noscript
        self._pre = 0                     # inside pre: keep text verbatim
        self._list: list[str] = []        # "ul" | "ol" stack
        self._ol_n: list[int] = []        # item counters for nested <ol>
        self._link: dict | None = None    # collecting <a> text
        self._img_alt: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in self.HEADINGS:
            self._nl(2)
            self.out.append(self.HEADINGS[tag] + " ")
        elif tag == "pre":
            self._nl(2)
            self.out.append("```\n")
            self._pre += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "blockquote":
            self._nl(2)
            self.out.append("> ")
        elif tag in ("ul", "ol"):
            self._nl(2)
            self._list.append(tag)
            if tag == "ol":
                self._ol_n.append(0)
        elif tag == "li":
            self._nl(1)
            if self._list and self._list[-1] == "ol" and self._ol_n:
                self._ol_n[-1] += 1
                self.out.append(f"{self._ol_n[-1]}. ")
            else:
                self.out.append("- ")
        elif tag == "a" and a.get("href"):
            self._link = {"href": a["href"], "text": []}
        elif tag == "img":
            alt = a.get("alt") or "image"
            src = a.get("src") or ""
            self.out.append(f"![{alt}]({src})")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "hr":
            self._nl(2)
            self.out.append("---")
        elif tag in self.BLOCK:
            self._nl(2)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._nl(1)
            self.out.append("```")
            self._nl(1)
        elif tag in ("ul", "ol"):
            if self._list:
                self._list.pop()
            if tag == "ol" and self._ol_n:
                self._ol_n.pop()
            self._nl(1)
        elif tag == "li":
            self._nl(1)
        elif tag == "a" and self._link is not None:
            text = "".join(self._link["text"]).strip()
            href = self._link["href"]
            self._link = None
            if text:
                self.out.append(f"[{text}]({href})")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in self.HEADINGS or tag in self.BLOCK or tag == "blockquote":
            self._nl(2)

    def handle_data(self, data):
        if self._skip:
            return
        if self._link is not None:
            self._link["text"].append(data)
            return
        if self._pre:
            self.out.append(data)
            return
        text = re.sub(r"\s+", " ", data)
        if text:
            self.out.append(text)

    def _nl(self, count=1):
        if not self.out:
            return
        # collapse to at most one blank line between blocks
        trailing = len(self.out[-1]) and self.out[-1].endswith("\n")
        if trailing and count <= 1:
            return
        self.out.append("\n" * count)

    def markdown(self) -> str:
        md = "".join(self.out)
        md = re.sub(r"\n{3,}", "\n\n", md)
        return md.strip()


def html_to_md(html: str) -> str:
    p = _MdExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 — malformed html still yields partial md
        pass
    return p.markdown()


def looks_like_html(content_type: str, body: str) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    head = (body or "")[:512].lstrip().lower()
    return head.startswith(("<!doctype html", "<html"))


# ----------------------------------------------------------------- ddg parsing

class _DdgParser(HTMLParser):
    """duckduckgo.com/html results: title/url pairs + snippets."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._cur: dict | None = None
        self._mode: str | None = None      # 'title' | 'snippet'
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and "result__a" in cls:
            self._flush()
            self._cur = {"url": self._unwrap_ddg(a.get("href") or ""),
                         "title": "", "snippet": ""}
            self._mode = "title"
            self._buf = []
        elif tag == "a" and "result__snippet" in cls and self._cur is not None:
            self._mode = "snippet"
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "a" and self._mode:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if self._cur is not None:
                if self._mode == "title":
                    self._cur["title"] = text
                else:
                    self._cur["snippet"] = text
            self._mode = None
            self._buf = []

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)

    def _flush(self):
        if self._cur and self._cur.get("title"):
            self.results.append(self._cur)
        self._cur = None

    @staticmethod
    def _unwrap_ddg(href: str) -> str:
        """DDG links come as //duckduckgo.com/l/?uddg=<urlencoded>."""
        if "uddg=" in href:
            q = parse_qs(href.split("?", 1)[1] if "?" in href else "")
            if q.get("uddg"):
                return unquote(q["uddg"][0])
        if href.startswith("//"):
            return "https:" + href
        return href


def parse_ddg(html: str) -> list[dict]:
    p = _DdgParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    p._flush()
    return [r for r in p.results if r.get("url", "").startswith("http")]


# ----------------------------------------------------------------- the tools

class WebTools:
    def __init__(self, settings_store, proxy_matrix):
        self.store = settings_store          # main.db: settings + web cache
        self.proxy = proxy_matrix

    async def _client(self, route: str = "web") -> httpx.AsyncClient:
        proxy = self.proxy.httpx_proxy(route)   # raises ProxyConfigError if socks w/o socksio
        return httpx.AsyncClient(proxy=proxy, timeout=httpx.Timeout(30.0),
                                 follow_redirects=True,
                                 headers={"User-Agent":
                                          "Mozilla/5.0 (X11; Linux x86_64) "
                                          "agent-framework/0.6"})

    # -- settings ----------------------------------------------------------------

    async def _s(self, key: str, default: str = "") -> str:
        s = await self.store.get_settings()
        return str(s.get(key, default) or default)

    async def status(self) -> dict:
        backend = await self._s("search_backend", "off")
        return {
            "search_backend": backend if backend in BACKENDS else "off",
            "searxng_url": await self._s("search_searxng_url"),
            "brave_key_set": bool(await self._s("search_brave_key")),
            "cache": await self.store.web_cache_stats(),
        }

    # -- cache -------------------------------------------------------------------

    async def _ttl_cap(self) -> tuple[float, int]:
        try:
            ttl_h = max(float(await self._s("web_cache_ttl_hours", "24")), 0.01)
        except (TypeError, ValueError):
            ttl_h = 24.0
        try:
            cap_mb = max(float(await self._s("web_cache_max_mb", "50")), 1)
        except (TypeError, ValueError):
            cap_mb = 50.0
        return ttl_h * 3600.0, int(cap_mb * 1024 * 1024)

    async def _evict(self) -> None:
        ttl, cap = await self._ttl_cap()
        await self.store.web_cache_evict(ttl, cap)

    # -- websearch -----------------------------------------------------------------

    async def search(self, query: str, fresh: bool = False) -> dict:
        query = (query or "").strip()
        if not query:
            return {"error": "пустой запрос"}
        backend = await self._s("search_backend", "off")
        if backend not in BACKENDS:
            backend = "off"
        if backend == "off":
            return {"error": "поиск не настроен: выберите backend во вкладке "
                             "«Веб» настроек (duckduckgo без ключа / searxng / brave)",
                    "backend": "off"}

        key = f"{backend}::{json.dumps([query], ensure_ascii=False)}"
        if not fresh:
            hit = await self.store.web_cache_get("search", key)
            if hit is not None:
                payload = json.loads(hit["payload"])
                payload["cached"] = True
                return payload

        t0 = time.monotonic()
        try:
            async with await self._client("websearch") as client:
                if backend == "duckduckgo":
                    results = await self._ddg(client, query)
                elif backend == "searxng":
                    results = await self._searxng(client, query)
                else:
                    results = await self._brave(client, query)
        except httpx.HTTPError as e:
            return {"error": f"поиск недоступен: {type(e).__name__}: {e}",
                    "backend": backend}
        latency_ms = round((time.monotonic() - t0) * 1000)

        payload = {
            "query": query, "backend": backend,
            "results": results[:SEARCH_RESULTS_CAP],
            "count": len(results), "latency_ms": latency_ms,
            "cached": False, "ts": time.time(),
        }
        if not results:
            payload["note"] = ("бэкенд ответил пусто — нет совпадений или "
                               "он заблокирован (проверьте backend/прокси)")
        await self.store.web_cache_put(
            "search", key, json.dumps(payload, ensure_ascii=False))
        await self._evict()
        return payload

    async def _ddg(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        r = await client.get("https://html.duckduckgo.com/html/",
                             params={"q": query})
        if r.status_code != 200:
            raise WebToolsError(f"duckduckgo ответил {r.status_code}")
        out = []
        for item in parse_ddg(r.text):
            out.append({"title": item["title"][:200],
                        "url": item["url"][:500],
                        "snippet": (item.get("snippet") or "")[:400]})
        return out

    async def _searxng(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        base = (await self._s("search_searxng_url")).rstrip("/")
        if not base:
            raise WebToolsError("не задан URL SearXNG (вкладка «Веб»)")
        r = await client.get(f"{base}/search",
                             params={"q": query, "format": "json"})
        if r.status_code != 200:
            raise WebToolsError(f"searxng ответил {r.status_code}")
        try:
            body = r.json()
        except ValueError as e:
            raise WebToolsError(f"searxng вернул не-JSON ({e}); "
                                f"format=json включён?") from e
        out = []
        for item in (body.get("results") or [])[:SEARCH_RESULTS_CAP * 2]:
            url = item.get("url") or ""
            if not url.startswith("http"):
                continue
            out.append({"title": (item.get("title") or url)[:200],
                        "url": url[:500],
                        "snippet": (item.get("content") or "")[:400]})
        return out

    async def _brave(self, client: httpx.AsyncClient, query: str) -> list[dict]:
        key = await self._s("search_brave_key")
        if not key:
            raise WebToolsError("не задан ключ Brave Search API (вкладка «Веб»)")
        r = await client.get("https://api.search.brave.com/res/v1/web/search",
                             params={"q": query, "count": SEARCH_RESULTS_CAP},
                             headers={"X-Subscription-Token": key,
                                      "Accept": "application/json"})
        if r.status_code != 200:
            raise WebToolsError(f"brave ответил {r.status_code}")
        out = []
        for item in ((r.json().get("web") or {}).get("results") or []):
            url = item.get("url") or ""
            if not url.startswith("http"):
                continue
            out.append({"title": (item.get("title") or url)[:200],
                        "url": url[:500],
                        "snippet": (item.get("description") or "")[:400]})
        return out

    # -- webfetch ----------------------------------------------------------------

    @staticmethod
    def normalize_url(url: str) -> str:
        u = (url or "").strip()
        if not u:
            raise WebToolsError("пустой url")
        if "://" not in u:
            u = "http://" + u
        parsed = urlparse(u)
        if parsed.scheme not in ("http", "https"):
            raise WebToolsError(f"схема {parsed.scheme!r} не поддерживается "
                                f"(http/https)")
        return u

    async def fetch(self, url: str, mode: str = "md", offset: int = 0,
                    length: int = FETCH_DEFAULT_CHARS, fresh: bool = False) -> dict:
        url = self.normalize_url(url)
        if mode not in ("md", "raw"):
            mode = "md"
        offset = max(0, int(offset or 0))
        length = max(1, min(int(length or FETCH_DEFAULT_CHARS), FETCH_MAX_CHARS))

        if not fresh:
            hit = await self.store.web_cache_get("fetch", url)
            if hit is not None:
                payload = json.loads(hit["payload"])
                return self._slice(payload, mode, offset, length, cached=True)

        t0 = time.monotonic()
        try:
            async with await self._client("webfetch") as client:
                r = await client.get(url)
        except httpx.HTTPError as e:
            return {"error": f"запрос не удался: {type(e).__name__}: {e}",
                    "url": url}

        body_bytes = r.content[:FETCH_CAP_BYTES]
        truncated_bytes = len(r.content) > FETCH_CAP_BYTES
        ctype = r.headers.get("content-type", "")
        if "charset" not in ctype.lower():
            text = body_bytes.decode("utf-8", "replace")
        else:
            text = body_bytes.decode("utf-8", "replace")

        is_html = looks_like_html(ctype, text)
        md = html_to_md(text) if is_html else text
        payload = {"url": url, "final_url": str(r.url),
                   "status": r.status_code, "content_type": ctype,
                   "html": is_html, "raw": text, "md": md,
                   "total_chars": len(text), "bytes": len(body_bytes),
                   "truncated_bytes": truncated_bytes,
                   "latency_ms": round((time.monotonic() - t0) * 1000),
                   "ts": time.time()}
        await self.store.web_cache_put(
            "fetch", url, json.dumps(payload, ensure_ascii=False))
        await self._evict()
        return self._slice(payload, mode, offset, length, cached=False)

    @staticmethod
    def _slice(payload: dict, mode: str, offset: int, length: int,
               cached: bool) -> dict:
        source = payload.get("md" if mode == "md" else "raw") or ""
        chunk = source[offset:offset + length]
        return {"url": payload.get("final_url") or payload.get("url"),
                "status": payload.get("status"),
                "content_type": payload.get("content_type"),
                "mode": mode, "content": chunk,
                "offset": offset, "length": len(chunk),
                "total_chars": payload.get("total_chars"),
                "truncated": offset + length < (payload.get("total_chars") or 0),
                "cached": cached, "ts": payload.get("ts")}


# ----------------------------------------------------------------- registration

def register_web_tools(registry, webtools: WebTools) -> None:
    async def _websearch(query: str, fresh: bool = False) -> dict:
        return await webtools.search(query, fresh)

    async def _webfetch(url: str, mode: str = "md", offset: int = 0,
                        length: int = FETCH_DEFAULT_CHARS,
                        fresh: bool = False) -> dict:
        return await webtools.fetch(url, mode, offset, length, fresh)

    registry.register(ToolSpec(
        "websearch", "Поиск в интернете. Возвращает заголовки, URL и сниппеты "
                     "реальных результатов. fresh=true — мимо кеша. Если поиск "
                     "не настроен — вернёт подсказку, как настроить.",
        {"type": "object", "properties": {
            "query": {"type": "string"},
            "fresh": {"type": "boolean"}},
         "required": ["query"], "additionalProperties": False},
        accepts_timeout=True),
        _websearch, perm_class="read", default_timeout=30)

    registry.register(ToolSpec(
        "webfetch", "Загрузить веб-страницу: mode='md' (читаемый Markdown) или "
                    "'raw' (исходник как есть). offset/length — слайс по символам "
                    "(длинные страницы читай кусками). Кеш глобальный; fresh=true — "
                    "обновить. Если вызов не успел — повтори с timeout=N.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "mode": {"type": "string", "enum": ["md", "raw"]},
            "offset": {"type": "integer", "minimum": 0},
            "length": {"type": "integer", "minimum": 1, "maximum": FETCH_MAX_CHARS},
            "fresh": {"type": "boolean"},
            "timeout": {"type": "number", "minimum": 1, "maximum": 600}},
         "required": ["url"], "additionalProperties": False},
        accepts_timeout=True),
        _webfetch, perm_class="read", default_timeout=30)
