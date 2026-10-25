"""MCP connectors — plug external tool servers into Darkmoon.

Implements the client side of the Model Context Protocol (JSON-RPC 2.0):
  * stdio transport: Darkmoon launches the server (e.g. `npx -y @playwright/mcp`)
  * http transport:  "streamable HTTP" servers at a URL (optional bearer header)

Every tool a server offers is registered as a Darkmoon tool named
`<server>__<tool>`, so local and cloud models can call it like any built-in tool
(with an approval prompt by default).
"""
from __future__ import annotations

import asyncio
import atexit
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from typing import Any

import httpx

from . import __version__
from .mcp_oauth import OAuthError, oauth
from .tools.registry import REGISTRY, ToolSpec

log = logging.getLogger("darkmoon.mcp")
PROTOCOL_VERSION = "2025-06-18"
_WIN = sys.platform == "win32"


class MCPError(Exception):
    pass


class MCPAuthRequired(MCPError):
    """The remote server wants an OAuth sign-in (HTTP 401)."""


# ── Transports ──────────────────────────────────────────────────────────────
class StdioTransport:
    def __init__(self, name: str, conf: dict[str, Any], workspace: str):
        self.name, self.conf, self.workspace = name, conf, workspace
        self.proc: subprocess.Popen | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        self.stderr_tail: list[str] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.write_lock = threading.Lock()

    async def open(self) -> None:
        cmd = self.conf.get("command", "").strip()
        if not cmd:
            raise MCPError("no command set")
        exe = shutil.which(cmd) or (cmd if os.path.isfile(cmd) else None)
        if not exe:
            hint = " (install Node.js)" if cmd in ("npx", "node") else " (install uv)" if cmd in ("uvx", "uv") else ""
            raise MCPError(f"command not found: {cmd}{hint}")
        args = [str(a).replace("${workspace}", self.workspace) for a in self.conf.get("args", [])]
        env = {**os.environ, **{k: str(v) for k, v in (self.conf.get("env") or {}).items()}}
        kw: dict[str, Any] = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=env, cwd=self.workspace, text=True, encoding="utf-8", errors="replace",
                                  bufsize=1)
        if _WIN:
            kw["creationflags"] = 0x08000000  # no console window
        self.loop = asyncio.get_running_loop()
        self.proc = subprocess.Popen([exe, *args], **kw)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout and self.loop
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.loop.call_soon_threadsafe(self._dispatch, msg)
        self.loop.call_soon_threadsafe(self._fail_all, MCPError(
            "server exited" + (f": {self.stderr_tail[-1]}" if self.stderr_tail else "")))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_tail.append(line.rstrip()[:300])
            del self.stderr_tail[:-20]

    def _dispatch(self, msg: dict) -> None:
        if "method" in msg and "id" in msg:  # server → client request
            result: dict[str, Any] = {"jsonrpc": "2.0", "id": msg["id"]}
            if msg["method"] == "ping":
                result["result"] = {}
            elif msg["method"] == "roots/list":
                result["result"] = {"roots": [{"uri": "file:///" + self.workspace.replace("\\", "/").lstrip("/"),
                                               "name": "Darkmoon workspace"}]}
            else:
                result["error"] = {"code": -32601, "message": "not supported"}
            self._write(result)
            return
        fut = self.pending.pop(msg.get("id"), None) if "id" in msg else None
        if fut and not fut.done():
            if "error" in msg:
                fut.set_exception(MCPError(msg["error"].get("message", str(msg["error"]))))
            else:
                fut.set_result(msg.get("result") or {})

    def _fail_all(self, err: Exception) -> None:
        for f in self.pending.values():
            if not f.done():
                f.set_exception(err)
        self.pending.clear()

    def _write(self, obj: dict) -> None:
        if not self.proc or not self.proc.stdin or self.proc.poll() is not None:
            raise MCPError("server is not running" + (f": {self.stderr_tail[-1]}" if self.stderr_tail else ""))
        with self.write_lock:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()

    async def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        rid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                if p.stdin:
                    p.stdin.close()
                p.wait(timeout=2)
            except Exception:  # noqa: BLE001
                if _WIN:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True,
                                   creationflags=0x08000000)
                else:
                    p.kill()


class HTTPTransport:
    """MCP "streamable HTTP" transport."""

    def __init__(self, name: str, conf: dict[str, Any], workspace: str):
        self.name, self.conf = name, conf
        self.session_id: str | None = None
        self.ids = itertools.count(1)
        self.client: httpx.AsyncClient | None = None
        self.bearer: str | None = None

    async def open(self) -> None:
        if not self.conf.get("url", "").startswith(("http://", "https://")):
            raise MCPError("set a http(s) URL")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=15), follow_redirects=True)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
             "MCP-Protocol-Version": PROTOCOL_VERSION}
        h.update({k: str(v) for k, v in (self.conf.get("headers") or {}).items()})
        if self.bearer and not any(k.lower() == "authorization" for k in h):
            h["Authorization"] = f"Bearer {self.bearer}"
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    async def _post(self, body: dict, want_id: int | None, timeout: float) -> dict:
        assert self.client
        async with self.client.stream("POST", self.conf["url"], json=body, headers=self._headers(),
                                      timeout=timeout) as r:
            if r.headers.get("mcp-session-id"):
                self.session_id = r.headers["mcp-session-id"]
            if r.status_code == 202 or want_id is None:
                return {}
            if r.status_code == 401:
                raise MCPAuthRequired(r.headers.get("www-authenticate", ""))
            if r.status_code == 403:
                raise MCPError("HTTP 403 — access denied (check the token's permissions)")
            if r.status_code >= 400:
                raise MCPError(f"HTTP {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
            ctype = r.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                data_lines: list[str] = []
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                    elif not line.strip() and data_lines:
                        msg = json.loads("\n".join(data_lines))
                        data_lines = []
                        if msg.get("id") == want_id:
                            return self._unwrap(msg)
                raise MCPError("stream ended without a response")
            return self._unwrap(json.loads(await r.aread()))

    @staticmethod
    def _unwrap(msg: dict) -> dict:
        if "error" in msg:
            raise MCPError(msg["error"].get("message", str(msg["error"])))
        return msg.get("result") or {}

    async def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        rid = next(self.ids)
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
        self.bearer = await oauth.access_token(self.name)
        try:
            return await self._post(body, rid, timeout)
        except MCPAuthRequired:
            if not oauth.tokens(self.name):
                raise
            try:  # expired token: refresh once and retry
                await oauth.refresh(self.name)
            except OAuthError:
                raise MCPAuthRequired("") from None
            self.bearer = await oauth.access_token(self.name)
            return await self._post(body, rid, timeout)
        except httpx.HTTPError as e:
            raise MCPError(f"request failed: {e}") from e

    async def notify(self, method: str, params: dict | None = None) -> None:
        try:
            await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}}, None, 30)
        except httpx.HTTPError:
            pass

    async def close(self) -> None:
        if self.client:
            if self.session_id:
                try:
                    await self.client.delete(self.conf["url"], headers=self._headers(), timeout=5)
                except httpx.HTTPError:
                    pass
            await self.client.aclose()


# ── Server + manager ────────────────────────────────────────────────────────
def tool_name(server: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{server}__{tool}")[:64]


def _render_result(res: dict) -> str:
    parts: list[str] = []
    for c in res.get("content") or []:
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text", ""))
        elif t == "image":
            parts.append(f"[image: {c.get('mimeType', 'image')}]")
        elif t == "resource":
            r = c.get("resource") or {}
            parts.append(r.get("text") or f"[resource: {r.get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[link: {c.get('name', '')} {c.get('uri', '')}]")
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], indent=2, ensure_ascii=False))
    text = "\n".join(parts) or "(no output)"
    return f"Error: {text}" if res.get("isError") else text


class MCPServer:
    def __init__(self, name: str, conf: dict[str, Any], workspace: str):
        self.name, self.conf, self.workspace = name, conf, workspace
        self.status = "off"
        self.error = ""
        self.tools: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}
        self.transport: StdioTransport | HTTPTransport | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.status, self.error = "starting", ""
        T = HTTPTransport if self.conf.get("transport") == "http" else StdioTransport
        self.transport = T(self.name, self.conf, self.workspace)
        try:
            await self.transport.open()
            init = await self.transport.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "Darkmoon", "version": __version__}}, timeout=180)
            self.server_info = init.get("serverInfo") or {}
            await self.transport.notify("notifications/initialized")
            tools: list[dict] = []
            cursor = None
            while True:
                res = await self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
                tools += res.get("tools") or []
                cursor = res.get("nextCursor")
                if not cursor:
                    break
            self.tools = tools
            self.status = "connected"
        except MCPAuthRequired:
            self.status, self.error = "needs_auth", "Sign-in required — click Sign in."
            await self.stop(keep_status=True)
        except Exception as e:  # noqa: BLE001
            self.status, self.error = "error", str(e) or type(e).__name__
            tail = getattr(self.transport, "stderr_tail", [])
            if tail and tail[-1] not in self.error:
                self.error += f" — {tail[-1]}"
            log.warning("MCP %s failed: %s", self.name, self.error)
            await self.stop(keep_status=True)

    async def stop(self, keep_status: bool = False) -> None:
        if self.transport:
            try:
                await self.transport.close()
            except Exception:  # noqa: BLE001
                pass
        self.transport = None
        if not keep_status:
            self.status, self.tools = "off", []

    async def call(self, tool: str, args: dict[str, Any]) -> str:
        if self.status != "connected" or not self.transport:
            await self.start()
            if self.status != "connected":
                return f"Error: connector '{self.name}' is not connected: {self.error}"
        timeout = float(self.conf.get("timeout", 120))
        try:
            res = await self.transport.request("tools/call", {"name": tool, "arguments": args}, timeout=timeout)
        except asyncio.TimeoutError:
            return f"Error: {self.name}/{tool} timed out after {timeout:.0f}s"
        except MCPError as e:
            return f"Error from {self.name}: {e}"
        return _render_result(res)

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "error": self.error,
                "signed_in": bool(oauth.tokens(self.name)),
                "transport": self.conf.get("transport", "stdio"), "enabled": self.conf.get("enabled", True),
                "server": self.server_info, "tools": [{"name": t.get("name"), "registered": tool_name(self.name, t.get("name", "")),
                                                       "description": (t.get("description") or "")[:200]}
                                                      for t in self.tools]}


class MCPManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.servers: dict[str, MCPServer] = {}
        atexit.register(self._kill_sync)

    def _kill_sync(self) -> None:
        for s in self.servers.values():
            t = s.transport
            if isinstance(t, StdioTransport) and t.proc and t.proc.poll() is None:
                try:
                    t.proc.kill()
                except Exception:  # noqa: BLE001
                    pass

    def _unregister(self, server: str | None = None) -> None:
        for n in [n for n, s in REGISTRY.items()
                  if s.source.startswith("mcp:") and (server is None or s.source == f"mcp:{server}")]:
            del REGISTRY[n]

    def _register(self, srv: MCPServer) -> None:
        self._unregister(srv.name)
        approval = "mcp" if srv.conf.get("require_approval", True) else None
        for t in srv.tools:
            tname = t.get("name", "")
            schema = dict(t.get("inputSchema") or {"type": "object", "properties": {}})
            schema.pop("$schema", None)
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})

            async def run(_tool=tname, _srv=srv, **kwargs):
                return await _srv.call(_tool, kwargs)

            REGISTRY[tool_name(srv.name, tname)] = ToolSpec(
                name=tool_name(srv.name, tname),
                description=f"[{srv.name}] {(t.get('description') or tname).strip()}"[:1024],
                parameters=schema, func=run, approval=approval, source=f"mcp:{srv.name}",
                preview=lambda a, _n=f"{srv.name} › {tname}": f"{_n}\n\n{json.dumps(a, indent=2, ensure_ascii=False)[:3500]}")

    async def start_one(self, name: str) -> MCPServer:
        conf = self.cfg["mcp"]["servers"].get(name)
        old = self.servers.pop(name, None)
        if old:
            await old.stop()
            self._unregister(name)
        if not conf:
            raise KeyError(name)
        srv = MCPServer(name, conf, str(self.cfg.workspace))
        self.servers[name] = srv
        if conf.get("enabled", True):
            await srv.start()
            if srv.status == "connected":
                self._register(srv)
        return srv

    async def start_all(self) -> None:
        names = list(self.cfg["mcp"]["servers"])
        await asyncio.gather(*(self.start_one(n) for n in names), return_exceptions=True)

    async def remove(self, name: str) -> None:
        srv = self.servers.pop(name, None)
        if srv:
            await srv.stop()
        self._unregister(name)

    async def stop_all(self) -> None:
        for s in list(self.servers.values()):
            await s.stop()
        self._unregister()

    def info(self) -> list[dict[str, Any]]:
        out = []
        for name, conf in self.cfg["mcp"]["servers"].items():
            srv = self.servers.get(name)
            d = srv.info() if srv else {"name": name, "status": "off", "error": "", "tools": [],
                                        "transport": conf.get("transport", "stdio"), "enabled": conf.get("enabled", True)}
            out.append(d)
        return out
