"""The local "darkmoon" model: an Ollama model built from a base (default
qwen3.5:9b) with Darkmoon's identity baked in, so it's called darkmoon in
Ollama, the model picker, and `ollama run darkmoon`.

Equivalent manual command:  ollama create darkmoon -f models/darkmoon.Modelfile
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx

log = logging.getLogger("darkmoon.model")
MODEL_NAME = "darkmoon"
IDENTITY = ("You are Darkmoon, a sharp, capable personal AI agent running locally on the user's PC. "
            "You are direct, concise and genuinely helpful. If asked who you are, you are Darkmoon.")


def modelfile(base: str, num_ctx: int = 8192, temperature: float = 0.4) -> str:
    return (f"# Darkmoon — build with: ollama create darkmoon -f models/darkmoon.Modelfile\n"
            f"FROM {base}\n"
            f"PARAMETER temperature {temperature}\n"
            f"PARAMETER num_ctx {num_ctx}\n"
            f'SYSTEM """{IDENTITY}"""\n')


def _names(models: list[dict[str, Any]]) -> set[str]:
    out = set()
    for m in models:
        n = m.get("name") or m.get("model") or ""
        out.add(n)
        out.add(n.removesuffix(":latest"))
    return out


async def status(cfg) -> dict[str, Any]:
    host = cfg["ollama"]["host"].rstrip("/")
    base = cfg["darkmoon"]["base_model"]
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{host}/api/tags")
        names = _names(r.json().get("models", []))
    except Exception as e:  # noqa: BLE001
        return {"ollama": False, "exists": False, "base": base, "base_pulled": False, "error": str(e)}
    return {"ollama": True, "exists": MODEL_NAME in names, "base": base, "base_pulled": base in names}


async def create(cfg) -> AsyncIterator[dict[str, Any]]:
    """Stream progress dicts while (re)building darkmoon from the base model."""
    host = cfg["ollama"]["host"].rstrip("/")
    base = cfg["darkmoon"]["base_model"]
    o = cfg["ollama"]
    num_ctx, temp = int(o.get("num_ctx", 8192)), float(o.get("temperature", 0.4))
    body = {"model": MODEL_NAME, "from": base, "system": IDENTITY, "stream": True,
            "parameters": {"num_ctx": num_ctx, "temperature": temp}}
    legacy = {"name": MODEL_NAME, "modelfile": modelfile(base, num_ctx, temp), "stream": True}
    async with httpx.AsyncClient(timeout=None) as c:
        for payload in (body, legacy):
            async with c.stream("POST", f"{host}/api/create", json=payload) as r:
                if r.status_code == 400 and payload is body:
                    continue  # older Ollama: retry with the Modelfile form
                if r.status_code != 200:
                    yield {"error": f"HTTP {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}"}
                    return
                async for line in r.aiter_lines():
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            pass
                return


async def ensure(cfg) -> str:
    """Create darkmoon at startup when it's the selected model, missing, and the base is pulled."""
    if cfg["ollama"]["model"] != MODEL_NAME or not cfg["darkmoon"].get("auto_create", True):
        return "skipped"
    st = await status(cfg)
    if not st["ollama"] or st["exists"]:
        return "ok" if st.get("exists") else "ollama offline"
    if not st["base_pulled"]:
        return f"base model {st['base']} not pulled"
    last: dict[str, Any] = {}
    async for ev in create(cfg):
        last = ev
    log.info("darkmoon model create: %s", last)
    return "created" if not last.get("error") else f"error: {last['error']}"
