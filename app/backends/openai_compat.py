"""OpenAI-compatible chat-completions backends (native tool calling).

Used for the xAI API and for a generic slot that works with OpenAI, OpenRouter,
Groq, Google Gemini (OpenAI endpoint), DeepSeek, Mistral, Together, or a local
LM Studio / llama.cpp / vLLM server. Darkmoon's own tools run with that model."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import Backend, BackendError, ToolCall, Turn


class OpenAICompatBackend(Backend):
    name = "openai_compat"
    label = "OpenAI-compatible"
    section = "openai_compat"
    env_key = "OPENAI_API_KEY"
    key_required = False      # local servers (LM Studio) need no key

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def conf(self) -> dict[str, Any]:
        return self.cfg[self.section]

    @property
    def key(self) -> str:
        return self.conf.get("api_key") or os.environ.get(self.env_key, "")

    @property
    def display_label(self) -> str:
        return self.conf.get("label") or self.label

    def headers(self) -> dict[str, str]:
        h = {k: str(v) for k, v in (self.conf.get("headers") or {}).items()}
        if self.key:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [{
                        "id": tc["id"], "type": "function",
                        "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
                    } for tc in m["tool_calls"]],
                })
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]})
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    async def chat(self, messages, tools, on_token, model: str | None = None, **_):
        x = self.conf
        if self.key_required and not self.key:
            raise BackendError(f"No API key set for {self.display_label} (Settings › Backends, or {self.env_key}).")
        if not x.get("model"):
            raise BackendError(f"No model chosen for {self.display_label} (Settings › Backends).")
        payload: dict[str, Any] = {
            "model": model or x["model"],
            "messages": self._convert(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        turn = Turn(model=payload["model"])
        calls: dict[int, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10)) as client:
                async with client.stream(
                    "POST", f"{x['base_url'].rstrip('/')}/chat/completions", json=payload,
                    headers=self.headers(),
                ) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="replace")
                        raise BackendError(f"{self.display_label} HTTP {r.status_code}: {body[:400]}")
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                turn.content += delta["content"]
                                await on_token(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["args"] += fn.get("arguments") or ""
        except httpx.HTTPError as e:
            raise BackendError(f"{self.display_label} request failed: {e}") from e
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"]) if c["args"] else {}
            except json.JSONDecodeError:
                args = {"_raw": c["args"]}
            turn.tool_calls.append(ToolCall(id=c["id"] or f"call_{i}", name=c["name"], arguments=args))
        return turn

    async def health(self):
        x = self.conf
        if not x.get("enabled"):
            return {"ok": False, "detail": "disabled (optional)"}
        if not x.get("base_url"):
            return {"ok": False, "detail": "no base URL"}
        if self.key_required and not self.key:
            return {"ok": False, "detail": "no API key"}
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(f"{x['base_url'].rstrip('/')}/models", headers=self.headers())
        except httpx.HTTPError as e:
            return {"ok": False, "detail": f"unreachable: {e}"}
        if r.status_code == 200:
            try:
                ids = [m.get("id", "") for m in r.json().get("data", [])]
            except ValueError:
                ids = []
            ids = [i.removeprefix("models/") for i in ids]
            model = x.get("model") or ""
            if not model:
                return {"ok": False, "detail": "connected — pick a model", "models": ids}
            note = "" if not ids or model in ids else f" — '{model}' not in model list"
            return {"ok": True, "detail": f"connected — {model}{note}", "models": ids}
        if r.status_code in (401, 403):
            return {"ok": False, "detail": f"key rejected (HTTP {r.status_code})"}
        # Some providers don't implement /models; treat as usable if a model is set.
        return {"ok": bool(x.get("model")), "detail": f"/models HTTP {r.status_code}"}


class XAIBackend(OpenAICompatBackend):
    name = "xai_api"
    label = "Grok API"
    section = "xai_api"
    env_key = "XAI_API_KEY"
    key_required = True
