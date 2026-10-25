"""Smart routing: local Darkmoon first; a cloud model when asked, when local fails,
or for hard tasks.

Decision order (first match wins):
  1. Explicit keyword: /grok, /claude, /gpt, /api, /local (and "use grok" etc.)
  2. Mode picked in the UI / config — anything but auto
  3. Auto: if Ollama is down -> escalate; if the task scores as complex -> escalate;
     otherwise local.
During the run, the agent escalates again if the local model errors, returns
nothing, loops past max_steps, or calls the escalate_to_cloud tool itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

COMPLEX_HINTS = {
    # strong signals
    "refactor": 0.35, "architecture": 0.3, "architect": 0.3, "entire codebase": 0.4,
    "whole project": 0.35, "full project": 0.35, "complete project": 0.35, "from scratch": 0.25,
    "step by step": 0.15, "prove": 0.25, "proof": 0.25, "derive": 0.2, "optimi": 0.15,
    "in depth": 0.2, "in-depth": 0.2, "comprehensive": 0.2, "thorough": 0.15,
    "research": 0.2, "compare": 0.1, "analyze": 0.15, "analyse": 0.15, "debug": 0.2,
    "stack trace": 0.25, "traceback": 0.2, "multi-file": 0.3, "migrate": 0.25,
    "design a": 0.2, "implement": 0.15, "algorithm": 0.15, "security audit": 0.35,
    "legal": 0.15, "contract": 0.1, "business plan": 0.25, "strategy": 0.1,
}
SIMPLE_HINTS = ("hi", "hello", "thanks", "thank you", "what time", "what's the date",
                "translate", "summarize this", "tl;dr", "define ", "spell")


@dataclass
class Route:
    backend: str          # ollama | grok_cli | xai_api | claude_cli | claude_api | openai_compat
    reason: str
    cleaned_text: str     # message with routing keywords removed
    forced: bool = False  # user explicitly chose -> don't auto-escalate away


def complexity_score(text: str) -> float:
    t = text.lower()
    score = 0.0
    for k, w in COMPLEX_HINTS.items():
        if k in t:
            score += w
    n = len(text)
    score += min(n / 3000, 0.35)                       # long prompts
    score += 0.1 * min(text.count("```"), 4) / 2      # pasted code blocks
    score += min(len(re.findall(r"^\s*(\d+[.)]|[-*])\s", text, re.M)) * 0.03, 0.2)  # requirement lists
    score += min(text.count("?") * 0.03, 0.1)
    if n < 80 and any(t.startswith(s) or s in t for s in SIMPLE_HINTS):
        score -= 0.3
    return max(0.0, min(score, 1.0))


def _strip_keywords(text: str, keywords: list[str]) -> tuple[str, bool]:
    hit = False
    out = text
    for k in sorted(keywords, key=len, reverse=True):
        pat = re.compile(r"(?<!\w)" + re.escape(k) + r"(?!\w)[,:]?\s*", re.I)
        if pat.search(out):
            hit = True
            out = pat.sub("", out, count=1)
    return out.strip() or text, hit


FAMILIES = {
    "grok": ["grok_cli", "xai_api"],
    "claude": ["claude_cli", "claude_api"],
    "openai": ["openai_compat"],
    "local": ["ollama"],
}
CLOUD = ["grok_cli", "claude_cli", "xai_api", "claude_api", "openai_compat"]


def escalation_target(cfg, available: dict[str, bool], exclude: str | None = None) -> str | None:
    """Where escalations go: the configured backend if available, else the first
    available backend in routing.escalation_order."""
    r = cfg["routing"]
    pref = r.get("escalation_backend", "auto")
    order = [b for b in r.get("escalation_order", CLOUD) if b in CLOUD] or CLOUD
    candidates = ([pref] if pref in CLOUD else []) + order
    for b in candidates:
        if b != exclude and available.get(b):
            return b
    return None


def _first(family: str, available: dict[str, bool]) -> str | None:
    return next((b for b in FAMILIES[family] if available.get(b)), None)


def decide(text: str, mode: str, cfg, available: dict[str, bool]) -> Route:
    r = cfg["routing"]
    # 1. explicit keywords ----------------------------------------------------
    cleaned, hit = _strip_keywords(text, ["/api", "@api", "use api"])
    if hit:
        target = "xai_api" if available.get("xai_api") else _first("grok", available)
        if target:
            return Route(target, "you asked for the xAI API" if target == "xai_api"
                         else "xAI API isn't set up — using Grok CLI instead", cleaned, True)
        return Route("ollama", "xAI API isn't set up — using local", cleaned, False)
    for family, key in (("grok", "grok_keywords"), ("claude", "claude_keywords"), ("openai", "openai_keywords")):
        cleaned, hit = _strip_keywords(text, r.get(key, []))
        if hit:
            target = _first(family, available)
            if target:
                return Route(target, f"you asked for {family.title() if family != 'openai' else 'the OpenAI-compatible model'}",
                             cleaned, True)
            fallback = escalation_target(cfg, available)
            if fallback:
                return Route(fallback, f"{family.title()} isn't set up — using {fallback} instead", cleaned, True)
            return Route("ollama", f"{family.title()} isn't set up — using Darkmoon locally", cleaned, False)
    cleaned, hit = _strip_keywords(text, r.get("local_keywords", []))
    if hit:
        return Route("ollama", "you asked for local Darkmoon", cleaned, True)

    # 2. mode picker ----------------------------------------------------------
    mode = mode or r.get("mode", "auto")
    if mode in ("ollama", "local"):
        return Route("ollama", "mode: local only", text, True)
    if mode in CLOUD:
        if available.get(mode):
            return Route(mode, f"mode: {mode}", text, True)
        return Route("ollama", f"{mode} unavailable — fell back to local", text, False)

    # 3. auto -----------------------------------------------------------------
    target = escalation_target(cfg, available)
    if not available.get("ollama"):
        if target:
            return Route(target, "Ollama isn't reachable — escalated", text, False)
        return Route("ollama", "no backend is reachable", text, False)
    if r.get("escalate_complex", True) and target:
        s = complexity_score(text)
        if s >= float(r.get("complexity_threshold", 0.65)):
            return Route(target, f"task looks complex (score {s:.2f}) — escalated", text, False)
    return Route("ollama", "local first", text, False)
