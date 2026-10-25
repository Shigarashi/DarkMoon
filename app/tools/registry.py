"""Tool registry, @tool decorator and plugin loader.

Adding a tool is as small as:

    from app.tools import tool

    @tool
    def word_count(text: str) -> str:
        '''Count words in a piece of text.'''
        return str(len(text.split()))

Parameter schemas are inferred from type hints; descriptions from the docstring
(an "Args:" section gives per-parameter descriptions). Add `ctx` as the first
parameter to get the ToolContext (workspace, config, path resolution, approvals).
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, get_args, get_origin

from ..safety import SafetyError, resolve_path, truncate

log = logging.getLogger("darkmoon.tools")

ApprovalKind = Literal["shell", "write", "python", "tests", "mcp", "network", "other"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    approval: str | None = None          # None or an ApprovalKind
    wants_ctx: bool = False
    source: str = "builtin"
    preview: Callable[[dict], str] | None = None  # text shown in the approval dialog

    def schema(self) -> dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}

    def info(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "approval": self.approval,
                "source": self.source, "parameters": self.parameters}


@dataclass
class ToolContext:
    cfg: Any
    workspace: Path
    approve: Callable[[str, str, str], Awaitable[bool]]
    emit: Callable[[dict], Awaitable[None]]
    conversation_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def path(self, p: str) -> Path:
        return resolve_path(p, self.workspace, self.cfg["safety"]["allow_outside_workspace"])


REGISTRY: dict[str, ToolSpec] = {}
PLUGIN_ERRORS: dict[str, str] = {}
_current_source = "builtin"

_PY2JSON = {str: "string", int: "integer", float: "number", bool: "boolean",
            list: "array", dict: "object"}


def _json_type(ann: Any) -> dict[str, Any]:
    if ann is inspect.Parameter.empty:
        return {"type": "string"}
    origin = get_origin(ann)
    if origin is Literal:
        vals = list(get_args(ann))
        return {"type": _PY2JSON.get(type(vals[0]), "string"), "enum": vals}
    if origin in (list, tuple):
        args = get_args(ann)
        return {"type": "array", "items": _json_type(args[0]) if args else {}}
    if origin is not None:  # Optional[X] / X | None
        args = [a for a in get_args(ann) if a is not type(None)]
        if args:
            return _json_type(args[0])
    if isinstance(ann, str):
        ann = {"str": str, "int": int, "float": float, "bool": bool, "list": list, "dict": dict}.get(
            ann.split("|")[0].strip(), str)
    return {"type": _PY2JSON.get(ann, "string")}


def _parse_doc(doc: str) -> tuple[str, dict[str, str]]:
    doc = inspect.cleandoc(doc or "")
    parts = re.split(r"\n\s*(?:Args|Arguments|Parameters):\s*\n", doc, maxsplit=1)
    desc = parts[0].strip()
    params: dict[str, str] = {}
    if len(parts) > 1:
        for line in parts[1].splitlines():
            m = re.match(r"\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)", line)
            if m:
                params[m.group(1)] = m.group(2).strip()
    return desc, params


def tool(_func: Callable | None = None, *, name: str | None = None, description: str | None = None,
         parameters: dict | None = None, approval: str | None = None,
         preview: Callable[[dict], str] | None = None):
    """Register a function as an agent tool. Usable as @tool or @tool(...)."""

    def deco(func: Callable) -> Callable:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        wants_ctx = bool(params) and params[0].name == "ctx"
        if wants_ctx:
            params = params[1:]
        desc, pdocs = _parse_doc(func.__doc__ or "")
        schema = parameters
        if schema is None:
            props, required = {}, []
            for p in params:
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                prop = _json_type(p.annotation)
                if p.name in pdocs:
                    prop["description"] = pdocs[p.name]
                if p.default is inspect.Parameter.empty:
                    required.append(p.name)
                else:
                    if p.default is not None:
                        prop["default"] = p.default
                props[p.name] = prop
            schema = {"type": "object", "properties": props, "required": required}
        spec = ToolSpec(name=name or func.__name__, description=description or desc or func.__name__,
                        parameters=schema, func=func, approval=approval, wants_ctx=wants_ctx,
                        source=_current_source, preview=preview)
        REGISTRY[spec.name] = spec
        return func

    return deco(_func) if callable(_func) else deco


def schemas(disabled: list[str] | None = None) -> list[dict[str, Any]]:
    disabled = set(disabled or [])
    return [s.schema() for s in REGISTRY.values() if s.name not in disabled]


def _default_preview(args: dict) -> str:
    return json.dumps(args, indent=2, ensure_ascii=False)[:4000]


async def call_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    spec = REGISTRY.get(name)
    limit = int(ctx.cfg["safety"].get("max_output_chars", 12000))
    if spec is None:
        return f"Error: unknown tool '{name}'. Available: {', '.join(REGISTRY)}"
    if name in set(ctx.cfg["tools"].get("disabled", [])):
        return f"Error: tool '{name}' is disabled by the user."
    if not isinstance(args, dict) or "_raw" in args:
        return f"Error: arguments for '{name}' were not valid JSON: {args}"

    # Drop unknown args (small models sometimes invent some); keep declared ones.
    props = spec.parameters.get("properties", {})
    if props:
        args = {k: v for k, v in args.items() if k in props}
    missing = [r for r in spec.parameters.get("required", []) if r not in args]
    if missing:
        return f"Error: missing required argument(s) for {name}: {', '.join(missing)}"

    # Refuse blocked commands before bothering the user with an approval prompt.
    if spec.approval in ("shell", "tests") and isinstance(args.get("command"), str) and args["command"]:
        from ..safety import check_command
        try:
            check_command(args["command"], ctx.cfg["safety"].get("blocked_commands", []))
        except SafetyError as e:
            return f"Blocked: {e}"

    if spec.approval:
        needs = {
            "shell": ctx.cfg["safety"]["require_approval_shell"],
            "write": ctx.cfg["safety"]["require_approval_write"],
            "python": ctx.cfg["safety"]["require_approval_python"],
            "mcp": ctx.cfg["safety"].get("require_approval_mcp", True),
            "tests": ctx.cfg["safety"].get("require_approval_tests", True),
        }.get(spec.approval, True)
        if needs:
            detail = (spec.preview or _default_preview)(args)
            ok = await ctx.approve(spec.approval, f"{name}", detail)
            if not ok:
                return "The user denied this action. Do not retry it; ask the user how to proceed."

    try:
        call_args = ([ctx] if spec.wants_ctx else [])
        if inspect.iscoroutinefunction(spec.func):
            result = await spec.func(*call_args, **args)
        else:
            result = await asyncio.to_thread(spec.func, *call_args, **args)
    except SafetyError as e:
        return f"Blocked: {e}"
    except TypeError as e:
        return f"Error: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001
        log.exception("tool %s failed", name)
        return f"Error running {name}: {type(e).__name__}: {e}"
    if result is None:
        result = "Done."
    if not isinstance(result, str):
        try:
            result = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        except TypeError:
            result = str(result)
    return truncate(result, limit)


def load_plugins(dirs: list[Path]) -> list[str]:
    """Import every *.py in the given folders. Returns loaded file names."""
    global _current_source
    for n in [n for n, s in REGISTRY.items() if s.source != "builtin" and not s.source.startswith("mcp:")]:
        del REGISTRY[n]
    PLUGIN_ERRORS.clear()
    loaded = []
    for d in dirs:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_"):
                continue
            _current_source = f.name
            try:
                spec = importlib.util.spec_from_file_location(f"darkmoon_plugin_{f.stem}", f)
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                loaded.append(f.name)
            except Exception:  # noqa: BLE001
                PLUGIN_ERRORS[f.name] = traceback.format_exc(limit=3)
                log.error("plugin %s failed:\n%s", f.name, PLUGIN_ERRORS[f.name])
            finally:
                _current_source = "builtin"
    return loaded
