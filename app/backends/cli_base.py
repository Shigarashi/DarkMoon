"""Shared base for "agent CLI" backends (Grok Build CLI, Claude Code CLI).

These CLIs are complete agents with their own tools, so Darkmoon sends one
headless prompt, runs it inside the workspace folder and streams the answer.
Subclasses provide: how to find the binary, the argv, and how to parse a line.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .. import procs
from .base import Backend, BackendError, Turn

MAX_PROMPT_CHARS = 24000  # stay well under the Windows command-line limit


def find_binary(configured: str, default_name: str, candidates: list[Path]) -> str | None:
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser())
    found = shutil.which(configured or default_name)
    if found:
        return found
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def build_prompt(messages: list[dict[str, Any]], since_index: int) -> str:
    """Latest user message + any turns this CLI hasn't seen yet (handled by other backends)."""
    user_idx = [i for i, m in enumerate(messages) if m["role"] == "user" and (m.get("content") or "").strip()]
    if not user_idx:
        return ""
    last = user_idx[-1]
    latest = messages[last]["content"]
    unseen = [m for m in messages[since_index:last]
              if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
    if not unseen:
        return latest
    ctx = "\n\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'].strip()}" for m in unseen)
    budget = MAX_PROMPT_CHARS - len(latest) - 400
    if len(ctx) > budget:
        ctx = "…" + ctx[-max(budget, 2000):]
    return (
        "Context — earlier in this conversation (you did not see these turns):\n"
        f"<earlier_conversation>\n{ctx}\n</earlier_conversation>\n\n"
        f"Current request:\n{latest}"
    )[-MAX_PROMPT_CHARS:]


class CLIAgentBackend(Backend):
    supports_tools = False
    self_contained = True
    section = ""            # config section, e.g. "grok_cli"
    login_hint = ""         # shown when the CLI returns nothing

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def conf(self) -> dict[str, Any]:
        return self.cfg[self.section]

    # Subclass hooks -----------------------------------------------------------
    def exe(self) -> str | None:
        raise NotImplementedError

    def argv(self, exe: str, prompt: str, session: dict[str, Any], model: str | None) -> list[str]:
        raise NotImplementedError

    def parse(self, obj: dict[str, Any], state: dict[str, Any]) -> tuple[str, str | None, str | None]:
        """Return (delta_text, final_text, tool_note) for one JSON event."""
        raise NotImplementedError

    def json_mode(self) -> bool:
        return True

    def after_run(self, session: dict[str, Any], state: dict[str, Any]) -> None:
        """Update persisted session info (e.g. mark a session as started)."""

    # Shared run loop ----------------------------------------------------------
    async def chat(self, messages, tools, on_token, *, session=None, since_index=0,
                   model=None, on_status=None, workspace=None, **_):
        prompt = build_prompt(messages, since_index)
        if not prompt:
            raise BackendError("Nothing to send.")
        exe = self.exe()
        if not exe:
            raise BackendError(f"{self.label} not found. Install it (see README), sign in, "
                               f"or set {self.section}.path in Settings.")
        session = session if session is not None else {}
        argv = self.argv(exe, prompt, session, model)
        streamed: list[str] = []
        final: list[str] = []
        raw_tail: list[str] = []
        state: dict[str, Any] = {}
        jm = self.json_mode()

        async def on_line(line: str):
            raw_tail.append(line)
            del raw_tail[:-40]
            s = line.strip()
            if not jm:
                streamed.append(line)
                await on_token(line)
                return
            if not s:
                return
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return  # update notices / warnings
            if not isinstance(obj, dict):
                return
            delta, fin, tool = self.parse(obj, state)
            if delta:
                streamed.append(delta)
                await on_token(delta)
            if fin is not None:
                final.append(fin)
            if tool and on_status:
                await on_status(f"{self.label} is using a tool: {tool}")

        rc, out, timed_out = await procs.stream_process(
            argv, cwd=workspace, timeout=float(self.conf.get("timeout", 900)), on_line=on_line)

        # What streamed is what the user saw; fall back to the CLI's final result.
        text = "".join(streamed).strip() or (final[-1].strip() if final else "")
        if timed_out:
            raise BackendError(f"{self.label} timed out.")
        if state.get("error") and not text:
            raise BackendError(f"{self.label}: {state['error']}")
        if not text:
            tail = "".join(raw_tail).strip()[-800:]
            low = tail.lower()
            hint = f" {self.login_hint}" if any(w in low for w in ("login", "auth", "unauthorized", "credential")) else ""
            raise BackendError(f"{self.label} returned no answer (exit {rc}).{hint}\n{tail}")
        if not "".join(streamed).strip():
            await on_token(text)
        self.after_run(session, state)
        return Turn(content=text, model=model or self.conf.get("model") or f"{self.label} default")

    async def version(self) -> tuple[bool, str, str | None]:
        if not self.conf.get("enabled", True):
            return False, "disabled in settings", None
        exe = self.exe()
        if not exe:
            return False, "CLI not found", None
        try:
            rc, out, _ = await procs.stream_process([exe, "--version"], timeout=20)
        except Exception as e:  # noqa: BLE001
            return False, f"failed to run: {e}", exe
        line = out.strip().splitlines()[-1] if out.strip() else "installed"
        return rc == 0, line, exe
