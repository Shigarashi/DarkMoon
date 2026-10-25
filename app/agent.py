"""The agent loop: route → call model → run tools → repeat → answer (with escalation)."""
from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import time
import uuid
from typing import Any, Awaitable, Callable

from . import router, tools
from .backends import BACKEND_META, Backend, BackendError, ToolCall, ToolsUnsupported, Turn, make_backends
from .storage import Store
from .tools.code_tools import detect_stack, is_git, git as _git
from pathlib import Path

log = logging.getLogger("darkmoon.agent")
ESCALATE_NAMES = ("escalate_to_cloud", "escalate_to_grok")

Emit = Callable[[dict], Awaitable[None]]
Approve = Callable[[str, str, str], Awaitable[bool]]

def escalate_tool(target_label: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "escalate_to_cloud",
            "description": (
                f"Hand the user's request to {target_label}, a much stronger cloud model. Use it when the "
                "task is too hard for you, needs deep reasoning or long code, or you have failed twice."),
            "parameters": {"type": "object",
                           "properties": {"reason": {"type": "string", "description": "why you are escalating"}},
                           "required": ["reason"]},
        },
    }


class Escalate(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _salvage_tool_call(content: str) -> tuple[str, dict] | None:
    """Some small models print a tool call as JSON instead of using the tool API."""
    m = re.search(r"\{.*\}", content, re.S)
    if not m or len(content) > 4000:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
    args = obj.get("arguments") or obj.get("parameters") or obj.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return None
    if name in tools.REGISTRY or name in ESCALATE_NAMES:
        return name, args if isinstance(args, dict) else {}
    return None


def _for_model(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip UI fields and repair history: every tool call must have a result
    (a Stop or crash mid-tool could otherwise leave dangling calls that APIs reject)."""
    keep = ("role", "content", "tool_calls", "tool_call_id", "name")
    answered = {m.get("tool_call_id") for m in history if m["role"] == "tool"}
    out = []
    for m in history:
        m = {k: m[k] for k in keep if k in m}
        if m["role"] == "assistant" and m.get("tool_calls"):
            m["tool_calls"] = [tc for tc in m["tool_calls"] if tc["id"] in answered]
            if not m["tool_calls"]:
                del m["tool_calls"]
                if not (m.get("content") or "").strip():
                    continue
        out.append(m)
    return out


class Agent:
    def __init__(self, cfg, store: Store):
        self.cfg = cfg
        self.store = store
        self.backends: dict[str, Backend] = make_backends(cfg)
        self._health: dict[str, Any] = {}
        self._health_at = 0.0

    # Health ----------------------------------------------------------------
    async def health(self, force: bool = False) -> dict[str, Any]:
        if force or time.time() - self._health_at > 20 or not self._health:
            names = list(self.backends)
            results = await asyncio.gather(*(self.backends[n].health() for n in names), return_exceptions=True)
            self._health = {n: (r if isinstance(r, dict) else {"ok": False, "detail": str(r)})
                            for n, r in zip(names, results)}
            self._health_at = time.time()
        return self._health

    async def available(self) -> dict[str, bool]:
        h = await self.health()
        return {n: bool(v.get("ok")) for n, v in h.items()}

    def model_for(self, backend: str, override: str | None) -> str:
        if backend == "ollama":
            m = override or self.cfg["ollama"]["model"]
            # Until the "darkmoon" model has been built, run on its base model.
            if m == "darkmoon":
                names = {n.removesuffix(":latest") for n in (self._health.get("ollama") or {}).get("models", [])}
                base = self.cfg["darkmoon"]["base_model"]
                if names and "darkmoon" not in names and base in names:
                    return base
            return m
        return self.cfg[backend].get("model") or "default"

    def label(self, backend: str) -> str:
        be = self.backends[backend]
        return getattr(be, "display_label", None) or BACKEND_META.get(backend, {}).get("label", backend)

    def project_root(self, meta: dict) -> Path | None:
        """The chat's Code-mode project folder, if it's still an approved project."""
        p = meta.get("project")
        if not p:
            return None
        allowed = {str(Path(x).expanduser().resolve()) for x in self.cfg["code"].get("projects", [])}
        rp = Path(p).expanduser().resolve()
        return rp if str(rp) in allowed and rp.is_dir() else None

    def system_prompt(self, root: Path | None = None) -> str:
        shell = "PowerShell" if platform.system() == "Windows" else "bash"
        now = time.strftime("%A %Y-%m-%d %H:%M")
        base = (f"{self.cfg['agent']['system_prompt'].strip()}\n\n"
                f"Environment: {platform.system()} {platform.release()}; shell tool uses {shell}; "
                f"{'project' if root else 'workspace'} folder: {root or self.cfg.workspace}; local time: {now}.")
        if not root:
            return base
        st = detect_stack(root)
        test = self.cfg["code"].get("test_command") or st["test_command"] or "none detected"
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1].strip() if is_git(root) else "not a git repo"
        return base + f"""

CODE MODE — you are working in the project "{root.name}" ({', '.join(st['stack'])}; test command: {test}; git: {branch}).
Work like a careful senior engineer:
1. Understand first: call project_overview, then find_files / read_file the relevant code before editing.
2. Make focused changes with edit_file (write_file only for new files). Match the existing style.
3. Verify: run_tests (or the build) after changing code. If anything fails, read the output, fix the cause and run again — keep iterating until it passes or you're truly stuck.
4. Finish with a short summary: files changed, what you did, and the test result.
Never commit, push or open a pull request unless the user asks; then use git_branch, git_commit and open_pull_request."""

    # Main entry --------------------------------------------------------------
    async def run(self, *, conversation_id: str | None, text: str, emit: Emit, approve: Approve,
                  mode: str = "", model: str | None = None, project: str | None = None) -> str:
        cid = conversation_id or self.store.create_conversation(text.strip().splitlines()[0][:60] or "New chat")
        conv = self.store.get_conversation(cid) or {"meta": {}}
        if project and not conv["meta"].get("project"):
            if self.project_root({"project": project}):
                conv["meta"]["project"] = str(Path(project).expanduser().resolve())
                self.store.update_conversation(cid, meta=conv["meta"])
            else:
                await emit({"type": "error", "message": f"{project} isn't an approved project folder. Add it in Code mode first."})
                return cid
        if not conversation_id:
            await emit({"type": "conversation", "id": cid, "title": conv["title"], "project": conv["meta"].get("project")})

        avail = await self.available()
        route = router.decide(text, mode, self.cfg, avail)
        user_id = self.store.add_message(cid, {"role": "user", "content": route.cleaned_text})
        await emit({"type": "user_saved", "id": user_id})

        backend = route.backend
        reason = route.reason
        escalated_once = False
        while True:
            await emit({"type": "route", "backend": backend, "label": self.label(backend),
                        "model": self.model_for(backend, model), "reason": reason})
            try:
                await self._run_backend(cid, backend, emit, approve, model, allow_escalate=not route.forced)
                return cid
            except Escalate as e:
                target = router.escalation_target(self.cfg, avail, exclude=backend)
                if escalated_once or not target:
                    await emit({"type": "error", "message": f"Wanted to escalate ({e.reason}) but no cloud "
                                "backend is available. Set up Grok CLI, Claude Code or an API key in Settings."})
                    return cid
                await emit({"type": "escalated", "from": backend, "to": target, "reason": e.reason})
                backend, reason, escalated_once = target, f"escalated: {e.reason}", True
            except asyncio.CancelledError:
                await emit({"type": "stopped"})
                raise
            except BackendError as e:
                log.warning("backend %s failed: %s", backend, e)
                target = router.escalation_target(self.cfg, avail, exclude=backend)
                can = (self.cfg["routing"].get("escalate_on_failure", True) and not route.forced
                       and not escalated_once and target)
                if can:
                    await emit({"type": "escalated", "from": backend, "to": target,
                                "reason": f"{self.label(backend)} failed: {str(e)[:200]}"})
                    backend, reason, escalated_once = target, f"escalated after {self.label(backend)} failed", True
                    continue
                await emit({"type": "error", "message": str(e)})
                return cid

    async def _run_backend(self, cid: str, name: str, emit: Emit, approve: Approve,
                           model: str | None, allow_escalate: bool) -> None:
        be = self.backends[name]
        mdl = self.model_for(name, model)
        history = _for_model(self.store.messages(cid))
        conv_meta = (self.store.get_conversation(cid) or {"meta": {}})["meta"]
        root = self.project_root(conv_meta)
        work_dir = root or self.cfg.workspace
        messages = [{"role": "system", "content": self.system_prompt(root)}] + history

        async def on_token(t: str):
            await emit({"type": "token", "text": t})

        async def on_status(s: str):
            await emit({"type": "status", "text": s})

        # Self-contained backend (agent CLIs run their own loop) ------------------
        if be.self_contained:
            conv = self.store.get_conversation(cid) or {"meta": {}}
            meta = conv["meta"]
            seen = int(meta.get(f"{name}_seen", 0))
            session = meta.setdefault(f"{name}_session", {"id": str(uuid.uuid4())})
            turn = await be.chat(messages, None, on_token, session=session, since_index=1 + seen,
                                 on_status=on_status, workspace=str(work_dir), model=None)
            self.store.add_message(cid, {"role": "assistant", "content": turn.content}, name, turn.model)
            meta[f"{name}_seen"] = len(history) + 1
            self.store.update_conversation(cid, meta=meta)
            await emit({"type": "done", "backend": name, "model": turn.model})
            return

        # Tool-calling loop ------------------------------------------------------
        disabled = self.cfg["tools"].get("disabled", [])
        schemas = tools.schemas(disabled)
        avail = await self.available()
        target = router.escalation_target(self.cfg, avail, exclude=name)
        if name == "ollama" and allow_escalate and self.cfg["routing"].get("allow_self_escalation", True) and target:
            schemas = schemas + [escalate_tool(self.label(target))]
        use_tools: list | None = schemas or None
        ctx = tools.ToolContext(cfg=self.cfg, workspace=work_dir, approve=approve,
                                emit=emit, conversation_id=cid)
        max_steps = int(self.cfg["code"].get("max_steps", 30) if root else self.cfg["agent"].get("max_steps", 12))
        failures = 0

        for step in range(max_steps + 1):
            final_round = step == max_steps
            try:
                turn: Turn = await be.chat(messages, None if final_round else use_tools, on_token, model=mdl)
            except ToolsUnsupported:
                await emit({"type": "status", "text": f"{mdl} doesn't support tools — answering without them. "
                            "Pick a tool-capable model for agent features."})
                use_tools = None
                turn = await be.chat(messages, None, on_token, model=mdl)

            if not turn.tool_calls and use_tools and not final_round:
                salvaged = _salvage_tool_call(turn.content)
                if salvaged:
                    turn.tool_calls = [ToolCall(id=uuid.uuid4().hex[:12], name=salvaged[0], arguments=salvaged[1])]
                    turn.content = ""
                    await emit({"type": "discard_stream"})

            if not turn.tool_calls:
                if not turn.content.strip():
                    if name == "ollama" and allow_escalate:
                        raise BackendError("the local model returned an empty answer")
                    turn.content = "_(no response)_"
                self.store.add_message(cid, {"role": "assistant", "content": turn.content}, name, turn.model)
                await emit({"type": "done", "backend": name, "model": turn.model})
                return

            calls = [{"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in turn.tool_calls]
            amsg = {"role": "assistant", "content": turn.content, "tool_calls": calls}
            self.store.add_message(cid, amsg, name, turn.model)
            messages.append(amsg)
            await emit({"type": "assistant_step_end"})

            escalate_reason = None
            for tc in turn.tool_calls:
                if escalate_reason is not None or tc.name in ESCALATE_NAMES:
                    if tc.name in ESCALATE_NAMES and escalate_reason is None:
                        escalate_reason = str(tc.arguments.get("reason", "the local model asked for help"))
                    note = "Handed off to the cloud model." if tc.name in ESCALATE_NAMES else "Skipped (handed off)."
                    self.store.add_message(cid, {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                                 "content": note}, name, turn.model)
                    continue
                await emit({"type": "tool_start", "id": tc.id, "name": tc.name, "arguments": tc.arguments})
                t0 = time.time()
                result = await tools.call_tool(tc.name, tc.arguments, ctx)
                ok = not result.startswith(("Error", "Blocked", "The user denied")) and \
                    "\nResult: FAILED" not in result and "\nResult: TIMED OUT" not in result
                failures = 0 if ok else failures + 1
                await emit({"type": "tool_result", "id": tc.id, "name": tc.name, "result": result,
                            "ok": ok, "seconds": round(time.time() - t0, 2)})
                tmsg = {"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": result}
                self.store.add_message(cid, tmsg, name, turn.model)
                messages.append(tmsg)

            if escalate_reason is not None:
                raise Escalate(escalate_reason)

            if failures >= 4 and name == "ollama" and allow_escalate and self.cfg["routing"].get("escalate_on_failure"):
                raise Escalate("the local model kept hitting tool errors")

            if step == max_steps - 1:
                if name == "ollama" and allow_escalate and self.cfg["routing"].get("escalate_on_failure"):
                    raise Escalate(f"reached the {max_steps}-step limit")
                messages.append({"role": "user", "content":
                                 "Step limit reached. Give your best final answer now without tools."})
