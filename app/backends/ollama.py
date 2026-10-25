"""Ollama backend — local models, zero cost. Uses the native /api/chat endpoint."""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from .base import Backend, BackendError, ToolCall, ToolsUnsupported, Turn


class OllamaBackend(Backend):
    name = "ollama"
    label = "Darkmoon (local)"

    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def host(self) -> str:
        return self.cfg["ollama"]["host"].rstrip("/")

    @property
    def model(self) -> str:
        return self.cfg["ollama"]["model"]

    @staticmethod
    def _convert(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or "",
                    "tool_calls": [
                        {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in m["tool_calls"]
                    ],
                })
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
            else:
                out.append({"role": m["role"], "content": m.get("content") or ""})
        return out

    async def chat(self, messages, tools, on_token, model: str | None = None, **_):
        o = self.cfg["ollama"]
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": self._convert(messages),
            "stream": True,
            "keep_alive": o.get("keep_alive", "10m"),
            "options": {"num_ctx": int(o.get("num_ctx", 8192)),
                        "temperature": float(o.get("temperature", 0.4))},
        }
        if tools:
            payload["tools"] = tools

        turn = Turn(model=payload["model"])
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=5)) as client:
                async with client.stream("POST", f"{self.host}/api/chat", json=payload) as r:
                    if r.status_code != 200:
                        body = (await r.aread()).decode(errors="replace")
                        if "does not support tools" in body:
                            raise ToolsUnsupported(body)
                        if r.status_code == 404 and "not found" in body:
                            raise BackendError(
                                f"Model '{payload['model']}' isn't pulled. Run: ollama pull {payload['model']}")
                        raise BackendError(f"Ollama HTTP {r.status_code}: {body[:400]}")
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise BackendError(chunk["error"])
                        msg = chunk.get("message") or {}
                        text = msg.get("content") or ""
                        if text:
                            # <think> blocks stream to the UI (shown collapsed) and are
                            # stripped from the stored answer below.
                            turn.content += text
                            await on_token(text)
                        for tc in msg.get("tool_calls") or []:
                            fn = tc.get("function", {})
                            args = fn.get("arguments") or {}
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {"_raw": args}
                            turn.tool_calls.append(
                                ToolCall(id=tc.get("id") or uuid.uuid4().hex[:12],
                                         name=fn.get("name", ""), arguments=args))
                        if chunk.get("done"):
                            break
        except httpx.ConnectError as e:
            raise BackendError(f"Can't reach Ollama at {self.host}. Is it running? ({e})") from e
        except httpx.HTTPError as e:
            raise BackendError(f"Ollama request failed: {e}") from e
        turn.content = strip_think(turn.content)
        return turn

    async def list_models(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{self.host}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])

    async def health(self):
        try:
            models = await self.list_models()
            names = [m["name"] for m in models]
            has = self.model in names or f"{self.model}:latest" in names
            return {"ok": True, "detail": f"{len(names)} models" + ("" if has else f" — '{self.model}' not pulled"),
                    "model": self.model, "models": names, "model_ready": has}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"not reachable at {self.host}", "error": str(e),
                    "model": self.model, "models": []}


def strip_think(text: str) -> str:
    """Remove <think>…</think> reasoning blocks (qwen3, deepseek-r1…)."""
    import re
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()
