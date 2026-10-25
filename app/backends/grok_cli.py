"""Grok Build CLI backend — uses your existing Grok / SuperGrok login.

Runs `grok -p "<prompt>" --output-format streaming-json` headlessly inside the
workspace folder. Grok Build is itself an agent with its own tools, so Darkmoon
hands it the task and streams back what it says. One Grok session (`-s`) per
Darkmoon chat, so Grok remembers earlier turns it handled.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .cli_base import CLIAgentBackend, find_binary


def _extract(obj: Any) -> tuple[str, str | None, str | None]:
    """Pull (delta_text, final_text, tool_note) out of one streaming-json event.

    The event schema can change between CLI versions, so this is deliberately
    forgiving: it understands delta-style, message-style and result-style events.
    """
    if not isinstance(obj, dict):
        return "", None, None
    t = str(obj.get("type", "")).lower()

    if t == "result" or "result" in obj and isinstance(obj.get("result"), str):
        return "", obj.get("result") or obj.get("text") or "", None

    if "tool" in t:
        name = obj.get("name") or obj.get("tool") or (obj.get("tool_call") or {}).get("name") or t
        return "", None, str(name)

    for key in ("delta", "text_delta", "content_delta"):
        v = obj.get(key)
        if isinstance(v, str):
            return v, None, None
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            return v["text"], None, None

    if t in ("text", "assistant_text", "message_delta", "content", "chunk", "token"):
        for key in ("text", "content", "data"):
            if isinstance(obj.get(key), str):
                return obj[key], None, None

    msg = obj.get("message")
    if t in ("assistant", "message") and isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return "", content, None
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            tools = [c.get("name", "tool") for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
            return "".join(texts), None, (tools[0] if tools else None)

    # session/update style (ACP-ish)
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    upd = obj.get("update") or params.get("update")
    if isinstance(upd, dict):
        c = upd.get("content")
        if isinstance(c, dict) and isinstance(c.get("text"), str):
            return c["text"], None, None
    return "", None, None


class GrokCLIBackend(CLIAgentBackend):
    name = "grok_cli"
    label = "Grok CLI"
    section = "grok_cli"
    login_hint = "Run `grok login` in a terminal first."

    def exe(self):
        home = Path.home()
        return find_binary(self.conf.get("path", "grok"), "grok", [
            home / ".grok" / "bin" / ("grok.exe" if sys.platform == "win32" else "grok"),
            home / ".local" / "bin" / "grok",
            Path(os.environ.get("LOCALAPPDATA", home)) / "Programs" / "grok" / "grok.exe",
            Path("/usr/local/bin/grok"), Path("/opt/homebrew/bin/grok"),
        ])

    def json_mode(self):
        return (self.conf.get("output_format") or "streaming-json") != "plain"

    def argv(self, exe, prompt, session, model):
        fmt = self.conf.get("output_format") or "streaming-json"
        argv = [exe, "--no-auto-update", "-p", prompt]
        if fmt != "plain":
            argv += ["--output-format", fmt]
        if model or self.conf.get("model"):
            argv += ["-m", model or self.conf["model"]]
        if session.get("id"):
            argv += ["-s", session["id"]]
        argv += ["--rules", "You were invoked from the Darkmoon desktop app. The current directory is the "
                 "user's Darkmoon workspace. Format answers in Markdown."]
        return argv + [str(a) for a in self.conf.get("extra_args", [])]

    def parse(self, obj, state):
        return _extract(obj)

    async def health(self):
        ok, detail, exe = await self.version()
        if not ok:
            return {"ok": False, "detail": detail + ("" if exe else " — install it and run `grok login`")}
        logged_in = (Path.home() / ".grok" / "auth.json").exists() or bool(
            os.environ.get("XAI_API_KEY") or os.environ.get("GROK_CODE_XAI_API_KEY"))
        return {"ok": True, "path": exe, "logged_in": logged_in,
                "detail": detail + ("" if logged_in else " — run `grok login`")}
