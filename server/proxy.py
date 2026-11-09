"""Proxy matrix (§17 roadmap).

Four routes, each may carry a proxy URL with an inline login:password:

    global    — everything outgoing (the fallback)
    model_api — LLM provider requests
    web       — websearch + webfetch
    browser   — embedded browser traffic

A specific route falls back to `global` when it has no proxy of its own.
Schemes: http / https / socks4 / socks5.

Implementation split (honest about what each stack supports):
  - web route      -> httpx.AsyncClient(proxy=...) — http/https always work,
                      socks needs the optional `socksio` package;
  - model_api      -> aiohttp `proxy=` per request (http/https); socks needs
                      the optional `aiohttp-socks` package;
  - browser        -> Playwright launch option {server, username, password}.

Missing optional packages produce a CLEAR error telling the user what to
install — never a silent no-proxy fallback.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

ROUTES = ("global", "model_api", "web", "websearch", "webfetch", "browser")

# websearch/webfetch fall back to the `web` route before `global`
_PARENT = {"websearch": "web", "webfetch": "web"}
_SCHEMES = ("http", "https", "socks4", "socks5")


class ProxyConfigError(Exception):
    """Raised when a configured proxy cannot be honoured (bad scheme /
    missing optional socks dependency). The message is user-facing."""


def parse_proxy_url(url: str) -> dict | None:
    """'socks5://user:pass@host:1080' -> {url, scheme, username, password}
    or None for an empty/invalid entry."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    if scheme not in _SCHEMES:
        raise ProxyConfigError(
            f"прокси со схемой '{scheme}' не поддерживается "
            f"(доступно: {', '.join(_SCHEMES)})")
    if not parsed.hostname:
        raise ProxyConfigError(f"прокси без хоста: {url!r}")
    cfg = {"url": raw, "scheme": scheme,
           "username": parsed.username or None,
           "password": parsed.password or None,
           "host": parsed.hostname, "port": parsed.port}
    return cfg


def parse_matrix(raw: str | dict) -> dict:
    """settings JSON -> {route: cfg}. Invalid routes are dropped, the rest
    still apply (a broken web entry must not kill model_api)."""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
    out: dict[str, dict] = {}
    for route in ROUTES:
        entry = data.get(route)
        if not entry:
            continue
        url = entry.get("url") if isinstance(entry, dict) else str(entry)
        try:
            cfg = parse_proxy_url(url)
        except ProxyConfigError:
            continue
        if cfg:
            out[route] = cfg
    return out


def socks_available_for_httpx() -> bool:
    try:
        import socksio  # noqa: F401
        return True
    except ImportError:
        return False


def socks_available_for_aiohttp() -> bool:
    try:
        import aiohttp_socks  # noqa: F401
        return True
    except ImportError:
        return False


class ProxyMatrix:
    """Holds the parsed matrix. `refresh()` is async (reads settings); the
    per-route getters are sync and use the last parsed snapshot — good enough
    because every outgoing client is short-lived and created per request."""

    def __init__(self, settings_store):
        self.store = settings_store
        self._matrix: dict[str, dict] = {}

    async def refresh(self) -> dict:
        s = await self.store.get_settings()
        self._matrix = parse_matrix(s.get("proxy_matrix", "{}"))
        return self._matrix

    def snapshot(self) -> dict[str, dict]:
        return dict(self._matrix)

    def effective(self, route: str) -> dict | None:
        """Route config; websearch/webfetch fall back to `web`, then `global`."""
        if route in self._matrix:
            return self._matrix[route]
        parent = _PARENT.get(route)
        if parent and parent in self._matrix:
            return self._matrix[parent]
        return self._matrix.get("global")

    # -- per-stack resolution ---------------------------------------------------

    def httpx_proxy(self, route: str) -> str | None:
        """Proxy URL for httpx.AsyncClient(proxy=...)."""
        cfg = self.effective(route)
        if cfg is None:
            return None
        if cfg["scheme"] in ("socks4", "socks5") and not socks_available_for_httpx():
            raise ProxyConfigError(
                f"{route}: socks-прокси для web-тулзов требует пакет "
                f"'socksio' (pip install 'httpx[socks]')")
        return cfg["url"]

    def aiohttp_request_proxy(self, route: str) -> str | None:
        """Request-level `proxy=` for aiohttp (http/https only)."""
        cfg = self.effective(route)
        if cfg is None:
            return None
        if cfg["scheme"] in ("socks4", "socks5"):
            # request-level proxy= does not accept socks; callers that need
            # it must use aiohttp_session_connector() instead
            return "__SOCKS__"
        return cfg["url"]

    def aiohttp_session_connector(self, route: str):
        """Connector for ClientSession when the route proxy is socks.
        Returns None when no socks proxy applies; raises a clear error when
        aiohttp-socks is missing."""
        cfg = self.effective(route)
        if cfg is None or cfg["scheme"] not in ("socks4", "socks5"):
            return None
        if not socks_available_for_aiohttp():
            raise ProxyConfigError(
                f"{route}: socks-прокси для API-запросов требует пакет "
                f"'aiohttp-socks' (pip install aiohttp-socks)")
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(cfg["url"])

    def playwright_proxy(self, route: str) -> dict | None:
        """{server, username, password} for Playwright launch()."""
        cfg = self.effective(route)
        if cfg is None:
            return None
        out = {"server": cfg["url"]}
        if cfg["username"]:
            out["username"] = cfg["username"]
        if cfg["password"]:
            out["password"] = cfg["password"]
        return out

    def describe(self) -> dict:
        """Settings-tab view: effective proxy per route + dependency notes."""
        out = {}
        for route in ROUTES:
            cfg = self.effective(route)
            if cfg is None:
                out[route] = {"active": False, "url": "", "scheme": None,
                              "source": None}
                continue
            if route in self._matrix:
                source = route
            elif _PARENT.get(route) and _PARENT[route] in self._matrix:
                source = _PARENT[route]
            else:
                source = "global"
            masked = re.sub(r"(//[^:/@]+:)[^@]+(@)", r"\1***\2", cfg["url"])
            out[route] = {"active": True, "url": masked, "scheme": cfg["scheme"],
                          "source": source}
        out["socks_httpx"] = socks_available_for_httpx()
        out["socks_aiohttp"] = socks_available_for_aiohttp()
        return out
