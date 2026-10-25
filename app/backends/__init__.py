from .base import Backend, BackendError, ToolCall, ToolsUnsupported, Turn
from .claude_api import ClaudeAPIBackend
from .claude_cli import ClaudeCLIBackend
from .grok_cli import GrokCLIBackend
from .ollama import OllamaBackend
from .openai_compat import OpenAICompatBackend, XAIBackend

# Display/routing metadata for every backend. `family` groups backends that
# answer to the same keyword (/grok → Grok CLI or Grok API, /claude → …).
BACKEND_META: dict[str, dict] = {
    "ollama":        {"label": "Darkmoon (local)", "short": "Darkmoon",      "family": "local",  "cost": "free"},
    "grok_cli":      {"label": "Grok CLI",         "short": "Grok CLI",   "family": "grok",   "cost": "subscription"},
    "xai_api":       {"label": "Grok API",         "short": "Grok API",   "family": "grok",   "cost": "per token"},
    "claude_cli":    {"label": "Claude Code",      "short": "Claude Code", "family": "claude", "cost": "subscription"},
    "claude_api":    {"label": "Claude API",       "short": "Claude API", "family": "claude", "cost": "per token"},
    "openai_compat": {"label": "OpenAI-compatible", "short": "OpenAI-compat", "family": "openai", "cost": "varies"},
}


def make_backends(cfg) -> dict[str, Backend]:
    return {
        "ollama": OllamaBackend(cfg),
        "grok_cli": GrokCLIBackend(cfg),
        "xai_api": XAIBackend(cfg),
        "claude_cli": ClaudeCLIBackend(cfg),
        "claude_api": ClaudeAPIBackend(cfg),
        "openai_compat": OpenAICompatBackend(cfg),
    }


__all__ = ["Backend", "BackendError", "ToolCall", "ToolsUnsupported", "Turn", "BACKEND_META", "make_backends",
           "OllamaBackend", "GrokCLIBackend", "XAIBackend", "ClaudeCLIBackend", "ClaudeAPIBackend",
           "OpenAICompatBackend"]
