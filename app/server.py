"""Local HTTP server (REST + Server-Sent Events) that the desktop window talks to.

Binds to 127.0.0.1 only. Every /api call requires a random
per-launch token that is injected into the page, so other websites open in
your browser can't drive the agent through localhost.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (HTMLResponse, JSONResponse, PlainTextResponse, Response,
                                 StreamingResponse)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, darkmoon_model, paths, procs, qr, tools
from .mcp_oauth import OAuthError, oauth
from .tools import code_tools
from .backends import BACKEND_META
from .mcp import MCPManager
from .agent import Agent
from .config import Config
from .storage import Store

log = logging.getLogger("darkmoon.server")
TOKEN = secrets.token_urlsafe(24)

DEFAULT_UI = {
    "theme": "darkmoon",
    "quickcss_enabled": True,
    "enabled_plugins": ["timestamps.js", "slash-commands.js"],
    "accent": "",
    "font": "",
    "mono_font": "",
    "font_size": 15,
    "density": "comfortable",
    "radius": 12,
    "bg_image": "",
    "bg_blur": 0,
    "bg_dim": 0.6,
    "chat_width": 860,
    "show_tool_details": True,
    "show_thinking": False,
    "send_on_enter": True,
    "sidebar_collapsed": False,
    "avatar_user": "🧑",
    "avatar_bot": "🤖",
}

META_RE = re.compile(r"@(\w+)\s+([^\n*]+)")


def _meta(text: str) -> dict[str, str]:
    head = text[:1500]
    m = re.search(r"/\*\*(.*?)\*/", head, re.S)
    block = m.group(1) if m else "\n".join(l for l in head.splitlines() if l.strip().startswith("//"))
    return {k: v.strip() for k, v in META_RE.findall(block)}


def _scan(dirs: list[tuple[str, Path]], ext: str) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for source, d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob(f"*{ext}")):
            try:
                meta = _meta(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            seen[f.name] = {"file": f.name, "source": source, "url": f"/{'themes' if ext == '.css' else 'ui-plugins'}/{source}/{f.name}",
                            "name": meta.get("name", f.stem), "author": meta.get("author", ""),
                            "description": meta.get("description", ""), "version": meta.get("version", ""),
                            "preview": meta.get("preview", "")}
    return list(seen.values())


def _open_path(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


def _safe_name(name: str, ext: str) -> str:
    stem = re.sub(r"[^\w.-]+", "-", Path(name).stem).strip("-.") or "theme"
    return f"{stem[:60]}{ext}"


LOOPBACK = {"127.0.0.1", "::1", "localhost"}
PAIR_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Darkmoon</title><body style="font-family:system-ui;background:#0b0b14;color:#e8e6f4;display:grid;place-items:center;
height:100vh;margin:0;text-align:center"><div><div style="font-size:48px">🌙</div><h2>Pair this device first</h2>
<p style="color:#9592b0;max-width:320px">On your PC open Darkmoon › Settings › Phone and scan the QR code with this phone.</p></div>"""


class _Guard:
    """Pure-ASGI security middleware.

    * Requests from this PC: Host must be a loopback name (blocks DNS-rebinding
      pages), and /api/* needs the per-launch token.
    * Requests from other devices (phone access): must be paired — /pair?code=…
      sets a cookie; everything else requires that cookie.
    """

    def __init__(self, app, phone=None):
        self.app = app
        self.phone = phone

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        client = (scope.get("client") or ("", 0))[0]
        path = scope["path"]
        if client in LOOPBACK:
            host = headers.get(b"host", b"").decode().rsplit(":", 1)[0].strip("[]").lower()
            if host not in LOOPBACK:
                return await PlainTextResponse("Forbidden host", status_code=403)(scope, receive, send)
        else:
            if path != "/pair":
                cookies = {}
                for part in headers.get(b"cookie", b"").decode().split(";"):
                    k, _, v = part.strip().partition("=")
                    cookies[k] = v
                if not (self.phone and self.phone.check(cookies.get("dm_pair"))):
                    if path.startswith("/api/"):
                        return await JSONResponse({"error": "not paired"}, status_code=401)(scope, receive, send)
                    return await HTMLResponse(PAIR_PAGE, status_code=401)(scope, receive, send)
        if path.startswith("/api/"):
            qs = scope.get("query_string", b"").decode()
            if headers.get(b"x-token", b"").decode() != TOKEN and f"token={TOKEN}" not in qs.split("&"):
                return await JSONResponse({"error": "bad token"}, status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def create_app(cfg: Config, store: Store, agent: Agent, mcp: MCPManager | None = None, phone=None):
    mcp = mcp or MCPManager(cfg)
    def ui_settings() -> dict[str, Any]:
        data = dict(DEFAULT_UI)
        if paths.UI_SETTINGS_FILE.exists():
            try:
                data.update(json.loads(paths.UI_SETTINGS_FILE.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        return data

    # ── Page & assets ──────────────────────────────────────────────────────
    async def index(request: Request):
        html = (paths.WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("__LA_TOKEN__", TOKEN).replace("__LA_VERSION__", __version__),
                            headers={"Cache-Control": "no-store"})

    def _asset(base_user: Path, base_builtin: Path, ext: str, media: str):
        async def handler(request: Request):
            source, file = request.path_params["source"], request.path_params["file"]
            f = (base_user if source == "user" else base_builtin) / Path(file).name
            if f.suffix != ext or not f.is_file():
                return _err(404, "not found")
            return Response(f.read_text(encoding="utf-8", errors="replace"), media_type=media,
                            headers={"Cache-Control": "no-store"})
        return handler

    # ── State / config ─────────────────────────────────────────────────────
    async def state(request: Request):
        force = request.query_params.get("force") in ("1", "true")
        return JSONResponse({
            "version": __version__,
            "config": cfg.public(),
            "health": await agent.health(force=force),
            "ui": ui_settings(),
            "themes": _scan([("builtin", paths.BUILTIN_THEMES_DIR), ("user", paths.USER_THEMES_DIR)], ".css"),
            "ui_plugins": _scan([("builtin", paths.BUILTIN_UI_PLUGINS_DIR), ("user", paths.USER_UI_PLUGINS_DIR)], ".js"),
            "tools": [t.info() for t in tools.REGISTRY.values()],
            "plugin_errors": tools.PLUGIN_ERRORS,
            "backends": BACKEND_META,
            "mcp": mcp.info(),
            "phone": phone.status() if phone else None,
            "remote": (request.client.host if request.client else "") not in LOOPBACK,
            "data_dir": str(paths.DATA_DIR),
        })

    async def health(request: Request):
        return JSONResponse(await agent.health(force=True))

    async def set_config(request: Request):
        patch = await _json(request)
        for section in ("xai_api", "claude_api", "openai_compat"):
            sec = patch.get(section, {})
            if str(sec.get("api_key", "")).startswith("•"):  # masked value echoed back — keep the old key
                sec.pop("api_key")
        patch.pop("mcp", None)  # connectors are saved through /api/mcp
        cfg.update(patch)
        await agent.health(force=True)
        return JSONResponse(cfg.public())

    async def ui_get_set(request: Request):
        data = ui_settings()
        if request.method == "POST":
            data.update(await _json(request))
            paths.UI_SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return JSONResponse(data)

    async def quickcss(request: Request):
        if request.method == "POST":
            body = await _json(request)
            paths.QUICKCSS_FILE.write_text(body.get("css", ""), encoding="utf-8")
            return JSONResponse({"ok": True})
        css = paths.QUICKCSS_FILE.read_text(encoding="utf-8") if paths.QUICKCSS_FILE.exists() else ""
        return PlainTextResponse(css)

    # ── Themes ─────────────────────────────────────────────────────────────
    async def install_theme(request: Request):
        body = await _json(request)
        url = str(body.get("url", "")).strip()
        css = body.get("css")
        name = body.get("name") or ""
        if url:
            if not url.startswith("https://"):
                return _err(400, "Theme URLs must start with https://")
            url = re.sub(r"^https://github\.com/([^/]+/[^/]+)/blob/", r"https://raw.githubusercontent.com/\1/", url)
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
                    r = await c.get(url)
            except httpx.HTTPError as e:
                return _err(400, f"Download failed: {e}")
            if r.status_code != 200:
                return _err(400, f"Download failed: HTTP {r.status_code}")
            css = r.text
            name = name or url.rsplit("/", 1)[-1]
        if not css or "<script" in css.lower() or "<html" in css[:500].lower() or len(css) > 2_000_000:
            return _err(400, "That doesn't look like a CSS theme.")
        meta = _meta(css)
        fname = _safe_name(meta.get("name") or name or "theme", ".css")
        (paths.USER_THEMES_DIR / fname).write_text(css, encoding="utf-8")
        return JSONResponse({"ok": True, "file": fname})

    async def delete_theme(request: Request):
        f = paths.USER_THEMES_DIR / Path(request.path_params["file"]).name
        if f.suffix == ".css" and f.is_file():
            f.unlink()
        return JSONResponse({"ok": True})

    async def open_folder(request: Request):
        which = (await _json(request)).get("which", "data")
        target = {"data": paths.DATA_DIR, "themes": paths.USER_THEMES_DIR,
                  "ui_plugins": paths.USER_UI_PLUGINS_DIR, "plugins": paths.USER_TOOL_PLUGINS_DIR,
                  "workspace": cfg.workspace}.get(which, paths.DATA_DIR)
        try:
            _open_path(target)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "path": str(target), "error": str(e)})
        return JSONResponse({"ok": True, "path": str(target)})

    # ── Tools ──────────────────────────────────────────────────────────────
    async def reload_tools(request: Request):
        loaded = tools.load_plugins([paths.BUILTIN_TOOL_PLUGINS_DIR, paths.USER_TOOL_PLUGINS_DIR])
        return JSONResponse({"loaded": loaded, "errors": tools.PLUGIN_ERRORS,
                             "tools": [t.info() for t in tools.REGISTRY.values()]})

    # ── Ollama model management ────────────────────────────────────────────
    async def ollama_models(request: Request):
        try:
            return JSONResponse({"models": await agent.backends["ollama"].list_models()})  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"models": [], "error": str(e)})

    async def ollama_pull(request: Request):
        name = str((await _json(request)).get("name", "")).strip()
        if not re.fullmatch(r"[\w.\-/:]+", name):
            return _err(400, "bad model name")
        host = cfg["ollama"]["host"].rstrip("/")

        async def gen():
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    async with c.stream("POST", f"{host}/api/pull", json={"name": name, "stream": True}) as r:
                        async for line in r.aiter_lines():
                            if line.strip():
                                yield line + "\n"
            except httpx.HTTPError as e:
                yield json.dumps({"error": str(e)}) + "\n"
            agent._health_at = 0

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # ── Conversations ──────────────────────────────────────────────────────
    async def list_convs(request: Request):
        return JSONResponse(store.list_conversations(request.query_params.get("q", "")))

    async def conv(request: Request):
        cid = request.path_params["cid"]
        if request.method == "DELETE":
            store.delete_conversation(cid)
            return JSONResponse({"ok": True})
        if request.method == "PATCH":
            body = await _json(request)
            store.update_conversation(cid, title=body.get("title"), pinned=body.get("pinned"))
            return JSONResponse({"ok": True})
        c = store.get_conversation(cid)
        if not c:
            return _err(404, "not found")
        return JSONResponse({"conversation": c, "messages": store.messages(cid)})

    async def truncate(request: Request):
        cid = request.path_params["cid"]
        store.truncate_after(cid, int((await _json(request))["message_id"]))
        c = store.get_conversation(cid)
        if c:  # agent CLIs must be re-told about the edited history
            c["meta"] = {k: v for k, v in c["meta"].items() if not k.endswith(("_session", "_seen"))}
            store.update_conversation(cid, meta=c["meta"])
        return JSONResponse({"ok": True})

    async def export(request: Request):
        return PlainTextResponse(store.export_markdown(request.path_params["cid"]))

    # ── Chat: Server-Sent Events stream + POST commands ───────────────────
    # One agent run at a time (it's a single-user desktop app). Every open
    # window subscribes to the same event stream, so approvals work anywhere.
    hub: dict[str, Any] = {"subs": set(), "task": None, "pending": {}, "always": set()}

    async def emit(ev: dict):
        for q in list(hub["subs"]):
            q.put_nowait(ev)

    async def approve(kind: str, title: str, detail: str) -> bool:
        if kind in hub["always"]:
            return True
        aid = uuid.uuid4().hex[:10]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        hub["pending"][aid] = fut
        await emit({"type": "approval_request", "id": aid, "kind": kind, "title": title, "detail": detail})
        try:
            decision = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            decision = "deny"
        finally:
            hub["pending"].pop(aid, None)
        await emit({"type": "approval_resolved", "id": aid, "decision": decision})
        if decision == "always":
            hub["always"].add(kind)
        return decision in ("allow", "always")

    async def events(request: Request):
        q: asyncio.Queue = asyncio.Queue()
        hub["subs"].add(q)

        async def gen():
            try:
                yield f"data: {json.dumps({'type': 'hello', 'busy': bool(hub['task'] and not hub['task'].done())})}\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                    if await request.is_disconnected():
                        break
            finally:
                hub["subs"].discard(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def chat(request: Request):
        msg = await _json(request)
        if not str(msg.get("text", "")).strip():
            return _err(400, "empty message")
        if hub["task"] and not hub["task"].done():
            return _err(409, "Still working on the previous message.")

        async def run_chat():
            try:
                await agent.run(conversation_id=msg.get("conversation_id") or None, text=msg["text"],
                                emit=emit, approve=approve, mode=msg.get("mode", ""),
                                model=msg.get("model") or None, project=msg.get("project") or None)
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                log.exception("chat failed")
                await emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                await emit({"type": "idle"})

        hub["task"] = asyncio.create_task(run_chat())
        return JSONResponse({"ok": True})

    async def stop(request: Request):
        for f in hub["pending"].values():
            if not f.done():
                f.set_result("deny")
        t = hub["task"]
        if t and not t.done():
            t.cancel()
            procs.kill_all()
        return JSONResponse({"ok": True})

    async def approval(request: Request):
        body = await _json(request)
        f = hub["pending"].get(body.get("id", ""))
        if f and not f.done():
            f.set_result(body.get("decision", "deny"))
        return JSONResponse({"ok": True})

    # ── MCP connectors ─────────────────────────────────────────────────────
    async def mcp_list(request: Request):
        return JSONResponse({"servers": mcp.info()})

    async def mcp_save(request: Request):
        body = await _json(request)
        name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(body.get("name", "")).strip()).strip("-")[:40]
        conf = body.get("conf") or {}
        if not name:
            return _err(400, "Give the connector a name (letters, numbers, - or _).")
        old = cfg["mcp"]["servers"].get(body.get("old_name") or name, {})
        for field in ("env", "headers"):  # keep secrets that were masked in the UI
            vals = conf.get(field) or {}
            for k, v in list(vals.items()):
                if str(v).startswith("•"):
                    vals[k] = (old.get(field) or {}).get(k, "")
        clean = {"enabled": bool(conf.get("enabled", True)), "transport": conf.get("transport", "stdio"),
                 "require_approval": bool(conf.get("require_approval", True)),
                 "timeout": int(conf.get("timeout") or 120)}
        if clean["transport"] == "http":
            clean.update(url=str(conf.get("url", "")).strip(), headers=conf.get("headers") or {})
        else:
            clean.update(command=str(conf.get("command", "")).strip(),
                         args=[str(a) for a in conf.get("args") or []], env=conf.get("env") or {})
        if body.get("old_name") and body["old_name"] != name:
            oauth.rename(body["old_name"], name)
            await mcp.remove(body["old_name"])
            cfg.set(f"mcp.servers.{body['old_name']}", None)
        cfg.set(f"mcp.servers.{name}", clean)
        srv = await mcp.start_one(name)
        return JSONResponse({"server": srv.info()})

    async def mcp_one(request: Request):
        name = request.path_params["name"]
        if request.method == "DELETE":
            await mcp.remove(name)
            cfg.set(f"mcp.servers.{name}", None)
            return JSONResponse({"ok": True})
        if name not in cfg["mcp"]["servers"]:
            return _err(404, "no such connector")
        srv = await mcp.start_one(name)  # POST = (re)connect
        return JSONResponse({"server": srv.info()})

    # ── darkmoon local model ───────────────────────────────────────────────
    async def dm_status(request: Request):
        return JSONResponse(await darkmoon_model.status(cfg))

    async def dm_create(request: Request):
        body = await _json(request)
        if body.get("base_model"):
            cfg.update({"darkmoon": {"base_model": str(body["base_model"]).strip()}})

        async def gen():
            async for ev in darkmoon_model.create(cfg):
                yield json.dumps(ev) + "\n"
            agent._health_at = 0

        return StreamingResponse(gen(), media_type="application/x-ndjson")

    # ── Phone access ───────────────────────────────────────────────────────
    async def pair(request: Request):
        code = request.query_params.get("code", "")
        if not (phone and phone.check(code)):
            return HTMLResponse(PAIR_PAGE, status_code=401)
        resp = HTMLResponse('<meta http-equiv="refresh" content="0;url=/">Paired — opening Darkmoon…')
        resp.set_cookie("dm_pair", code, max_age=365 * 86400, httponly=True, samesite="lax")
        return resp

    async def phone_api(request: Request):
        if not phone:
            return _err(404, "phone access unavailable")
        if request.method == "POST":
            if request.client and request.client.host not in LOOPBACK:
                return _err(403, "Change phone access from the PC.")
            on = bool((await _json(request)).get("enabled"))
            cfg.update({"phone": {"enabled": on}})
            await asyncio.to_thread(phone.start if on else phone.stop)
        return JSONResponse(phone.status())

    async def phone_reset(request: Request):
        if not phone or (request.client and request.client.host not in LOOPBACK):
            return _err(403, "Reset pairing from the PC.")
        phone.reset_pairing()
        return JSONResponse(phone.status())

    async def phone_qr(request: Request):
        st = phone.status() if phone else {"urls": []}
        urls = st.get("urls") or []
        i = int(request.query_params.get("i", 0) or 0)
        if not urls:
            return _err(404, "phone access is off")
        return Response(qr.svg(urls[min(i, len(urls) - 1)], scale=6), media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"})

    # ── Code mode ──────────────────────────────────────────────────────────
    def _project(path: str) -> Path | None:
        if not path:
            return None
        rp = Path(path).expanduser().resolve()
        allowed = {str(Path(x).expanduser().resolve()) for x in cfg["code"].get("projects", [])}
        return rp if str(rp) in allowed and rp.is_dir() else None

    async def code_projects(request: Request):
        projects = [str(Path(x).expanduser().resolve()) for x in cfg["code"].get("projects", [])]
        if request.method == "POST":
            body = await _json(request)
            rp = Path(str(body.get("path", "")).strip().strip('"')).expanduser()
            if not rp.is_dir():
                return _err(400, f"Folder not found: {rp}")
            rp = rp.resolve()
            if rp == Path(rp.anchor) or rp == Path.home():
                return _err(400, "Pick a project folder, not a whole drive or your home folder.")
            if str(rp) not in projects:
                projects.append(str(rp))
                cfg.set("code.projects", projects)
            return JSONResponse({"path": str(rp)})
        if request.method == "DELETE":
            body = await _json(request)
            target = str(Path(str(body.get("path", ""))).expanduser().resolve())
            cfg.set("code.projects", [x for x in projects if x != target])
            return JSONResponse({"ok": True})

        def info(p: str) -> dict:
            pp = Path(p)
            d = {"path": p, "name": pp.name, "exists": pp.is_dir(), "branch": "", "changes": 0, "git": False}
            if d["exists"] and code_tools.is_git(pp):
                ch = code_tools.changes(pp, max_diff=0)
                d.update(git=True, branch=ch["branch"], changes=len(ch["files"]))
            if d["exists"]:
                d["stack"] = code_tools.detect_stack(pp)["stack"]
            return d
        return JSONResponse({"projects": await asyncio.to_thread(lambda: [info(p) for p in projects])})

    async def code_changes(request: Request):
        root = _project(request.query_params.get("path", ""))
        if not root:
            return _err(404, "not an approved project")
        try:
            return JSONResponse(await asyncio.to_thread(code_tools.changes, root))
        except Exception as e:  # noqa: BLE001
            return _err(400, str(e))

    async def code_action(request: Request):
        action = request.path_params["action"]
        body = await _json(request)
        root = _project(body.get("path", ""))
        if not root:
            return _err(404, "not an approved project")
        try:
            if action == "git-init":
                code_tools.git(root, "init", check=True)
                return JSONResponse({"ok": True, "message": "Initialized a git repository."})
            if action == "commit":
                msg = str(body.get("message", "")).strip() or "Update from Darkmoon"
                code_tools.git(root, "add", "-A", check=True)
                rc, out = code_tools.git(root, "commit", "-m", msg)
                if rc:
                    return _err(400, out.strip()[:400])
                return JSONResponse({"ok": True, "message": out.strip().splitlines()[0] if out.strip() else "Committed."})
            if action == "discard":
                # Restores tracked files only; new untracked files are left in place (never deleted).
                code_tools.git(root, "restore", "--staged", "--worktree", "--", ".", check=True)
                return JSONResponse({"ok": True, "message": "Reverted changes to tracked files. New files were kept."})
            if action == "branch":
                name = str(body.get("name", "")).strip()
                if not re.fullmatch(r"[\w./-]+", name):
                    return _err(400, "Invalid branch name")
                code_tools.git(root, "switch", "-c", name, check=True)
                return JSONResponse({"ok": True, "message": f"Switched to {name}"})
            if action == "pr":
                url = await asyncio.to_thread(code_tools.push_and_open_pr, root, str(body.get("title", "")).strip()
                                              or "Changes from Darkmoon", str(body.get("body", "")), "", bool(body.get("draft")))
                return JSONResponse({"ok": True, "url": url, "message": f"Opened {url}"})
        except Exception as e:  # noqa: BLE001
            return _err(400, str(e))
        return _err(404, "unknown action")

    # ── Connector sign-in (OAuth) ──────────────────────────────────────────
    async def mcp_auth(request: Request):
        name = request.path_params["name"]
        conf = cfg["mcp"]["servers"].get(name)
        if not conf or conf.get("transport") != "http":
            return _err(400, "Sign-in is for remote (HTTP) connectors.")
        if request.method == "DELETE":
            oauth.sign_out(name)
            await mcp.start_one(name)
            return JSONResponse({"ok": True})
        try:
            url = await oauth.begin(name, conf["url"])
        except (OAuthError, httpx.HTTPError) as e:
            return _err(400, str(e))
        if request.client and request.client.host in LOOPBACK:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass
        return JSONResponse({"url": url})

    async def oauth_callback(request: Request):
        q = request.query_params
        name, ok, message = await oauth.finish(q.get("state", ""), q.get("code"), q.get("error"))
        if ok and name:
            asyncio.create_task(mcp.start_one(name))
        color = "#3ecf8e" if ok else "#f2555a"
        return HTMLResponse(f"""<!doctype html><title>Darkmoon sign-in</title><body style="font-family:system-ui;
background:#0b0b14;color:#e8e6f4;display:grid;place-items:center;height:100vh;margin:0;text-align:center">
<div><div style="font-size:46px">{'🌙' if ok else '⚠'}</div><h2 style="color:{color}">{'Connected ' + name if ok else 'Sign-in failed'}</h2>
<p style="color:#9592b0">{message} You can close this tab and return to Darkmoon.</p></div>""")

    routes = [
        Route("/", index),
        Mount("/static", app=StaticFiles(directory=str(paths.WEB_DIR)), name="static"),
        Route("/themes/{source}/{file}", _asset(paths.USER_THEMES_DIR, paths.BUILTIN_THEMES_DIR, ".css", "text/css")),
        Route("/ui-plugins/{source}/{file}", _asset(paths.USER_UI_PLUGINS_DIR, paths.BUILTIN_UI_PLUGINS_DIR, ".js", "text/javascript")),
        Route("/api/state", state),
        Route("/api/health", health),
        Route("/api/config", set_config, methods=["POST"]),
        Route("/api/ui-settings", ui_get_set, methods=["GET", "POST"]),
        Route("/api/quickcss", quickcss, methods=["GET", "POST"]),
        Route("/api/themes/install", install_theme, methods=["POST"]),
        Route("/api/themes/{file}", delete_theme, methods=["DELETE"]),
        Route("/api/open-folder", open_folder, methods=["POST"]),
        Route("/api/tools/reload", reload_tools, methods=["POST"]),
        Route("/api/ollama/models", ollama_models),
        Route("/api/ollama/pull", ollama_pull, methods=["POST"]),
        Route("/api/conversations", list_convs),
        Route("/api/conversations/{cid}", conv, methods=["GET", "PATCH", "DELETE"]),
        Route("/api/conversations/{cid}/truncate", truncate, methods=["POST"]),
        Route("/api/conversations/{cid}/export", export),
        Route("/api/events", events),
        Route("/pair", pair),
        Route("/oauth/callback", oauth_callback),
        Route("/api/phone", phone_api, methods=["GET", "POST"]),
        Route("/api/phone/reset", phone_reset, methods=["POST"]),
        Route("/api/phone/qr.svg", phone_qr),
        Route("/api/code/projects", code_projects, methods=["GET", "POST", "DELETE"]),
        Route("/api/code/changes", code_changes),
        Route("/api/code/{action}", code_action, methods=["POST"]),
        Route("/api/mcp/{name}/auth", mcp_auth, methods=["POST", "DELETE"]),
        Route("/api/mcp", mcp_list),
        Route("/api/mcp/save", mcp_save, methods=["POST"]),
        Route("/api/mcp/{name}", mcp_one, methods=["POST", "DELETE"]),
        Route("/api/darkmoon", dm_status),
        Route("/api/darkmoon/create", dm_create, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/stop", stop, methods=["POST"]),
        Route("/api/approval", approval, methods=["POST"]),
    ]
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async def startup():
            try:
                log.info("darkmoon model: %s", await darkmoon_model.ensure(cfg))
            except Exception as e:  # noqa: BLE001
                log.warning("darkmoon model check failed: %s", e)
            agent._health_at = 0
            await mcp.start_all()
        task = asyncio.create_task(startup())
        yield
        task.cancel()
        await mcp.stop_all()

    return _Guard(Starlette(routes=routes, lifespan=lifespan), phone)
