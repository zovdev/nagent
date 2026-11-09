"""MCP client with URL auto-discovery (§4 roadmap).

Transport: streamable HTTP (JSON-RPC 2.0 POST, `text/event-stream` or JSON
responses — both parsed). stdio and OAuth transports come in a later phase.

Discovery: `initialize` -> `notifications/initialized` -> `tools/list` ->
`prompts/list` -> `resources/list`; whatever the server supports is stored.

Tool naming: the model sees the tool's OWN name (`echo_time`, `add`, …) —
no `mcp__server__tool` prefixes burning context. The registry is the router:
it maps the public name to (server_id, native tool_name) internally and
proxies the call. Only an actual collision (two servers exposing the same
name, or a clash with a core tool) gets a minimal `_2` suffix.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import aiohttp

from . import events as ev
from .providers import ToolSpec

PROTOCOL_VERSION = "2025-03-26"
RPC_TIMEOUT = 20.0


class McpError(RuntimeError):
    pass


class McpManager:
    def __init__(self, store, registry):
        self.store = store              # SettingsStore
        self.registry = registry        # ToolRegistry
        self._session: aiohttp.ClientSession | None = None
        self._ids = 0
        self._sessions: dict[str, str] = {}      # server_id -> Mcp-Session-Id
        self._tool_index: dict[str, tuple[str, str]] = {}  # tool_id -> (server_id, tool_name)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=RPC_TIMEOUT))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    # -- JSON-RPC over streamable HTTP ----------------------------------------

    async def _rpc(self, url: str, method: str, params: dict | None = None,
                   session_id: str | None = None,
                   notify: bool = False) -> dict | None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = self._next_id()
        if params is not None:
            body["params"] = params
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        s = await self._http()
        async with s.post(url, json=body, headers=headers) as r:
            if r.status >= 400:
                text = (await r.text())[:300]
                raise McpError(f"{method}: HTTP {r.status}: {text}")
            if notify:
                return None
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                return await self._read_sse(r, body["id"])
            obj = await r.json(content_type=None)
        if obj.get("error"):
            raise McpError(f"{method}: {obj['error'].get('message')}")
        return obj.get("result")

    async def _read_sse(self, resp, target_id) -> dict:
        """Read SSE frames until the response with our id arrives."""
        async for raw in resp.content:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == target_id:
                if obj.get("error"):
                    raise McpError(str(obj["error"].get("message")))
                return obj.get("result") or {}
        raise McpError("SSE stream closed without a response")

    async def _handshake(self, url: str) -> tuple[dict, str | None]:
        s = await self._http()
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "agent-framework",
                                          "version": "0.2.0"}}}
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        async with s.post(url, json=body, headers=headers) as r:
            if r.status >= 400:
                raise McpError(f"initialize: HTTP {r.status}: {(await r.text())[:300]}")
            session_id = r.headers.get("Mcp-Session-Id")
            ctype = r.headers.get("Content-Type", "")
            if "text/event-stream" in ctype:
                result = await self._read_sse(r, body["id"])
            else:
                obj = await r.json(content_type=None)
                if obj.get("error"):
                    raise McpError(str(obj["error"].get("message")))
                result = obj.get("result") or {}
        # best-effort initialized notification (some servers require it)
        try:
            await self._rpc(url, "notifications/initialized", session_id=session_id,
                            notify=True)
        except McpError:
            pass
        return result, session_id

    # -- discovery ----------------------------------------------------------------

    async def discover(self, url: str) -> dict:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise McpError("URL должен начинаться с http:// или https://")
        t0 = time.monotonic()
        server_info, session_id = await self._handshake(url)
        tools_result = await self._rpc(url, "tools/list", {}, session_id=session_id) or {}
        tool_list = tools_result.get("tools") or []
        prompts = await self._optional_count(url, session_id, "prompts/list", "prompts")
        resources = await self._optional_count(url, session_id, "resources/list", "resources")

        name = (server_info.get("serverInfo") or {}).get("name") or server_info.get("name") or url
        server_id = ev.new_id()

        rows = []
        for t in tool_list:
            tname = t.get("name") or ""
            if not tname:
                continue
            public = self._public_name(tname)
            rows.append({"id": public, "tool_name": tname,
                         "description": t.get("description"),
                         "schema": t.get("inputSchema") or {}})
        server = await self.store.add_mcp_server(server_id, name, url, server_info,
                                                 prompts, resources)
        await self.store.replace_mcp_tools(server_id, rows)
        self._register(server_id, rows)
        await self.store.set_mcp_health(server_id, "online",
                                        round((time.monotonic() - t0) * 1000), None)
        return {"server": server, "tools": rows}

    def _public_name(self, tname: str) -> str:
        """The name the MODEL will call this tool by. Native name, sanitized to
        the OpenAI function-name charset; `_2` suffix only on a real collision
        with another MCP tool or a core tool."""
        base = re.sub(r"[^A-Za-z0-9_-]", "-", tname or "tool") or "tool"
        name, n = base, 2
        while name in self._tool_index or name in self.registry.names():
            name = f"{base}_{n}"
            n += 1
        return name

    async def _optional_count(self, url, session_id, method, key) -> int:
        try:
            res = await self._rpc(url, method, {}, session_id=session_id) or {}
            return len(res.get(key) or [])
        except (McpError, asyncio.TimeoutError):
            return 0

    # -- registry wiring ------------------------------------------------------------

    def _register(self, server_id: str, rows: list[dict]) -> None:
        for row in rows:
            spec = ToolSpec(row["id"], row.get("description") or row["tool_name"],
                            row.get("schema") or {"type": "object", "properties": {}})
            handler = self._make_handler(server_id, row["tool_name"])
            # §4/§6: MCP tools are DEFERRED — their schemas never bloat the
            # prompt; the model pulls them via tool_search/tool_load
            self.registry.register(spec, handler, perm_class="exec",
                                   deferred=True)
            self._tool_index[row["id"]] = (server_id, row["tool_name"])

    def _make_handler(self, server_id: str, tool_name: str):
        async def handler(**kwargs):
            return await self.call_tool(server_id, tool_name, kwargs)
        return handler

    async def load(self) -> None:
        """Re-register stored MCP tools at startup."""
        for server in await self.store.list_mcp_servers():
            rows = server.get("tools") or []
            self._register(server["id"], rows)

    async def delete(self, server_id: str) -> None:
        rows = await self.store.list_mcp_tools(server_id)
        for row in rows:
            self.registry.unregister(row["id"])
            self._tool_index.pop(row["id"], None)
        await self.store.delete_mcp_server(server_id)
        self._sessions.pop(server_id, None)

    async def refresh(self, server_id: str) -> dict:
        server = await self.store.get_mcp_server(server_id)
        if not server:
            raise McpError("MCP-сервер не найден")
        old_rows = await self.store.list_mcp_tools(server_id)
        for row in old_rows:
            self.registry.unregister(row["id"])
            self._tool_index.pop(row["id"], None)
        self._sessions.pop(server_id, None)
        result = await self.discover(server["url"])
        # discover() created a NEW row; merge into the original server entry
        new_server = result["server"]
        await self.store.delete_mcp_server(new_server["id"])
        server = await self.store.add_mcp_server(
            server_id, new_server["name"], new_server["url"],
            json.loads(new_server["server_info"]), new_server["prompts_count"],
            new_server["resources_count"])
        await self.store.replace_mcp_tools(server_id, result["tools"])
        self._register(server_id, result["tools"])
        await self.store.set_mcp_health(server_id, "online", None, None)
        return {"server": server, "tools": result["tools"]}

    # -- tool calls -------------------------------------------------------------------

    async def call_tool(self, server_id: str, tool_name: str, args: dict) -> dict:
        server = await self.store.get_mcp_server(server_id)
        if not server:
            return {"error": f"MCP-сервер не найден: {server_id}"}
        try:
            res = await self._call_with_session(server, tool_name, args)
        except McpError:
            # session may have expired server-side: re-handshake once
            self._sessions.pop(server_id, None)
            try:
                res = await self._call_with_session(server, tool_name, args)
            except McpError as e:
                return {"error": f"MCP {tool_name}: {e}"}
        content = res.get("content") or []
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if res.get("isError"):
            return {"error": "\n".join(texts) or "MCP tool error"}
        if texts:
            return {"text": "\n".join(texts)}
        return {"content": content}

    async def _call_with_session(self, server: dict, tool_name: str, args: dict) -> dict:
        session_id = self._sessions.get(server["id"])
        if session_id is None:
            _, session_id = await self._handshake(server["url"])
            if session_id:
                self._sessions[server["id"]] = session_id
        return await self._rpc(server["url"], "tools/call",
                               {"name": tool_name, "arguments": args},
                               session_id=session_id) or {}

    # -- health ---------------------------------------------------------------------

    async def probe(self, server_id: str) -> dict:
        server = await self.store.get_mcp_server(server_id)
        if not server:
            return {"status": "error", "latency_ms": None, "error": "not found"}
        t0 = time.monotonic()
        try:
            await self._handshake(server["url"])
            r = {"status": "online",
                 "latency_ms": round((time.monotonic() - t0) * 1000), "error": None}
        except (McpError, asyncio.TimeoutError) as e:
            r = {"status": "error",
                 "latency_ms": round((time.monotonic() - t0) * 1000), "error": str(e)}
        await self.store.set_mcp_health(server_id, r["status"], r["latency_ms"],
                                        r["error"])
        return r

    async def probe_all(self) -> list[dict]:
        servers = await self.store.list_mcp_servers()
        return [{"id": s["id"], "name": s["name"], **await self.probe(s["id"])}
                for s in servers]
