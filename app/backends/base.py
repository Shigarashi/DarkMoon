"""Backend interface.

Messages use one internal format (OpenAI-like), each backend converts it:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "tool_calls": [{"id": str, "name": str, "arguments": dict}],   # assistant only
   "tool_call_id": str, "name": str}                               # tool only
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

TokenCallback = Callable[[str], Awaitable[None]]


class BackendError(Exception):
    """Raised when a backend fails in a way that should trigger escalation."""


class ToolsUnsupported(BackendError):
    """The selected model can't do tool calling."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Turn:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""


class Backend:
    name: str = "base"
    label: str = "Base"
    supports_tools: bool = True
    # True when the backend runs its own agent loop (Grok CLI): Darkmoon sends
    # one prompt and streams the result instead of running tools itself.
    self_contained: bool = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        on_token: TokenCallback,
        **kwargs: Any,
    ) -> Turn:
        raise NotImplementedError

    async def health(self) -> dict[str, Any]:
        """{'ok': bool, 'detail': str, ...}"""
        raise NotImplementedError

    async def cancel(self) -> None:
        """Stop any in-flight work (subprocesses etc.)."""
