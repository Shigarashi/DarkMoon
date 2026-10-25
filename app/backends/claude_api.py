"""Claude API backend (Anthropic Messages API, pay-per-token, native tool use).

Claude drives Darkmoon's own tools (with the same approvals). Needs an API key
from console.anthropic.com — a Claude Pro/Max subscription is used through the
Claude Code CLI backend instead.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .base import Backend, BackendError, ToolCall, Turn

API_VERSION = "2023-06-01"
_ID_OK = re.compile(r"^[a-zA-Z0-9_-]+$")


def _tid(i: str) -> str:
    """Anthropic tool ids must match ^[a-zA-Z0-9_-]+$ (ids from other backends may not)."""
    return i if i and _ID_OK.match(i) else "t_" + re.sub(r"[^a-zA-Z0-9_-]", "_", i or "x")


class ClaudeAPIBackend(Backend):
    name = "claude_api"
    label = "Claude API"

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def conf(self) -> dict[str, Any]:
        return self.cfg["claude_api"]

    @property
    def key(self) -> str:
        return self.conf.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.key, "anthropic-version": API_VERSION, "content-type": "application/json"}

    @staticmethod
    def convert(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        def push(role: str, blocks: list[dict[str, Any]]):
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)  # API wants alternating roles
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            text = m.get("content") or ""
            if role == "system":
                system_parts.append(text)
            elif role == "user":
                push("user", [{"type": "text", "text": text or "(empty)"}])
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if text.strip():
                    blocks.append({"type": "text", "text": text})
                for tc in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": _tid(tc["id"]), "name": tc["name"],
                                   "input": tc.get("arguments") or {}})
                push("assistant", blocks or [{"type": "text", "text": "…"}])
            elif role == "tool":
                push("user", [{"type": "tool_result", "tool_use_id": _tid(m.get("tool_call_id", "")),
                               "content": text or "(no output)",
                               **({"is_error": True} if text.startswith(("Error", "Blocked")) else {})}])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
        return "\n\n".join(system_parts), out

    @staticmethod
    def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                 "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                for t in tools or []]

    async def chat(self, messages, tools, on_token, model: str | None = None, **_):
        if not self.key:
            raise BackendError("No Anthropic API key set (Settings › Backends, or ANTHROPIC_API_KEY).")
        system, msgs = self.convert(messages)
        payload: dict[str, Any] = {
            "model": model or self.conf["model"],
            "max_tokens": int(self.conf.get("max_tokens", 8192)),
            "messages": msgs,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = self.convert_tools(tools)
        turn = Turn(model=payload["model"])
        blocks: dict[int, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                async with client.stream("POST", f"{self.conf['base_url'].rstrip('/')}/v1/messages",
                                         json=payload, headers=self._headers()) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="replace")
                        raise BackendError(f"Claude API HTTP {r.status_code}: {body[:400]}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        ev = json.loads(line[5:].strip() or "{}")
                        t = ev.get("type")
                        if t == "message_start":
                            turn.model = (ev.get("message") or {}).get("model", turn.model)
                        elif t == "content_block_start":
                            blk = ev.get("content_block") or {}
                            blocks[ev["index"]] = {"type": blk.get("type"), "id": blk.get("id"),
                                                   "name": blk.get("name"), "json": ""}
                        elif t == "content_block_delta":
                            d = ev.get("delta") or {}
                            if d.get("type") == "text_delta":
                                turn.content += d.get("text", "")
                                await on_token(d.get("text", ""))
                            elif d.get("type") == "input_json_delta":
                                blocks.setdefault(ev["index"], {"json": ""})["json"] += d.get("partial_json", "")
                        elif t == "error":
                            raise BackendError(f"Claude API error: {ev.get('error')}")
        except httpx.HTTPError as e:
            raise BackendError(f"Claude API request failed: {e}") from e
        for i in sorted(blocks):
            b = blocks[i]
            if b.get("type") == "tool_use":
                try:
                    args = json.loads(b["json"]) if b["json"] else {}
                except json.JSONDecodeError:
                    args = {"_raw": b["json"]}
                turn.tool_calls.append(ToolCall(id=b["id"], name=b["name"], arguments=args))
        return turn

    async def health(self):
        if not self.conf.get("enabled"):
            return {"ok": False, "detail": "disabled (optional)"}
        if not self.key:
            return {"ok": False, "detail": "no API key"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{self.conf['base_url'].rstrip('/')}/v1/models", headers=self._headers())
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"unreachable: {e}"}
        if r.status_code == 200:
            ids = [m.get("id", "") for m in r.json().get("data", [])]
            m = self.conf["model"]
            note = "" if not ids or m in ids else f" — '{m}' not in your model list"
            return {"ok": True, "detail": f"key OK — {m}{note}", "models": ids}
        return {"ok": False, "detail": f"HTTP {r.status_code}"}
