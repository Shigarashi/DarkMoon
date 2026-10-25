"""Claude Code CLI backend — uses your Claude Pro/Max (or Console) login.

Runs `claude -p "<prompt>" --output-format stream-json --verbose
--include-partial-messages` headlessly inside the workspace folder. Claude Code
is a full agent with its own tools and permission system; tune what it may do
with extra_args such as ["--permission-mode", "acceptEdits"] or
["--allowedTools", "Read,Grep"]. One Claude session per Darkmoon chat
(--session-id on the first turn, --resume afterwards).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import procs
from .cli_base import CLIAgentBackend, find_binary


class ClaudeCLIBackend(CLIAgentBackend):
    name = "claude_cli"
    label = "Claude Code"
    section = "claude_cli"
    login_hint = "Run `claude` once in a terminal (or `claude auth login`) to sign in."

    def exe(self):
        home = Path.home()
        return find_binary(self.conf.get("path", "claude"), "claude", [
            home / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude"),
            Path(os.environ.get("APPDATA", home)) / "npm" / "claude.cmd",
            Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude"),
        ])

    def argv(self, exe, prompt, session, model):
        argv = [exe, "-p", prompt, "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if model or self.conf.get("model"):
            argv += ["--model", model or self.conf["model"]]
        if session.get("id"):
            argv += ["--resume", session["id"]] if session.get("started") else ["--session-id", session["id"]]
        if self.conf.get("permission_mode"):
            argv += ["--permission-mode", self.conf["permission_mode"]]
        argv += ["--append-system-prompt", "You were invoked from the Darkmoon desktop app. The current "
                 "directory is the user's Darkmoon workspace. Format answers in Markdown."]
        return argv + [str(a) for a in self.conf.get("extra_args", [])]

    def parse(self, obj, state):
        t = obj.get("type")
        if t == "stream_event":
            ev = obj.get("event") or {}
            if ev.get("type") == "content_block_delta":
                d = ev.get("delta") or {}
                if d.get("type") == "text_delta":
                    state["streamed"] = True
                    return d.get("text", ""), None, None
            if ev.get("type") == "content_block_start":
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    return "\n\n", None, blk.get("name", "tool")
            return "", None, None
        if t == "assistant" and not state.get("streamed"):
            # Older CLIs without partial messages: whole assistant messages.
            content = (obj.get("message") or {}).get("content") or []
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            tools = [c.get("name") for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
            return text, None, tools[0] if tools else None
        if t == "result":
            state["session_id"] = obj.get("session_id")
            if obj.get("is_error") or str(obj.get("subtype", "")).startswith("error"):
                state["error"] = obj.get("result") or obj.get("subtype")
            return "", obj.get("result") if isinstance(obj.get("result"), str) else None, None
        return "", None, None

    def after_run(self, session, state):
        if session.get("id"):
            session["started"] = True

    async def health(self):
        ok, detail, exe = await self.version()
        if not ok:
            return {"ok": False, "detail": detail + ("" if exe else " — install Claude Code and sign in")}
        try:
            rc, _, _ = await procs.stream_process([exe, "auth", "status"], timeout=20)
            logged_in = rc == 0
        except Exception:  # noqa: BLE001
            logged_in = bool(os.environ.get("ANTHROPIC_API_KEY"))
        return {"ok": True, "path": exe, "logged_in": logged_in,
                "detail": detail + ("" if logged_in else " — run `claude` once to sign in")}
