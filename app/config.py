"""Configuration: defaults merged with the user's config.toml."""
from __future__ import annotations

import copy
import os
import threading
import tomllib
from pathlib import Path
from typing import Any

from . import paths

PERSONA = (
    "You are Darkmoon, the user's personal AI agent running on their own PC.\n"
    "Use tools when they help: search the web for current facts, read files before editing them, "
    "and run code to verify answers. Paths are relative to the workspace folder.\n"
    "Be concise and direct. When a task is beyond you, call escalate_to_cloud."
)

DEFAULTS: dict[str, Any] = {
    "agent": {
        "name": "Darkmoon",
        "workspace": "~/DarkmoonWorkspace",
        "max_steps": 12,
        "system_prompt": PERSONA,
    },
    "ollama": {
        "host": "http://localhost:11434",
        "model": "darkmoon",
        "num_ctx": 8192,
        "temperature": 0.4,
        "keep_alive": "10m",
    },
    "darkmoon": {
        # The local "darkmoon" model is built from this base with `ollama create`.
        "base_model": "qwen3.5:9b",
        "auto_create": True,
    },
    "grok_cli": {
        "enabled": True, "path": "grok", "model": "", "output_format": "streaming-json",
        "timeout": 900, "extra_args": [],
    },
    "xai_api": {
        "enabled": False, "api_key": "", "base_url": "https://api.x.ai/v1", "model": "grok-4",
    },
    "claude_cli": {
        "enabled": True, "path": "claude", "model": "", "permission_mode": "",
        "timeout": 900, "extra_args": [],
    },
    "claude_api": {
        "enabled": False, "api_key": "", "base_url": "https://api.anthropic.com",
        "model": "claude-sonnet-5-5", "max_tokens": 8192,
    },
    "openai_compat": {
        "enabled": False, "preset": "openai", "label": "OpenAI", "api_key": "",
        "base_url": "https://api.openai.com/v1", "model": "",
    },
    "routing": {
        "mode": "auto",
        # auto = first available in escalation_order
        "escalation_backend": "auto",
        "escalation_order": ["grok_cli", "claude_cli", "xai_api", "claude_api", "openai_compat"],
        "escalate_on_failure": True,
        "escalate_complex": True,
        "complexity_threshold": 0.65,
        "allow_self_escalation": True,
        "grok_keywords": ["use grok", "ask grok", "/grok", "@grok"],
        "claude_keywords": ["use claude", "ask claude", "/claude", "@claude"],
        "openai_keywords": ["/gpt", "/openai", "@gpt"],
        "local_keywords": ["use local", "/local", "@local", "/darkmoon"],
    },
    "safety": {
        "require_approval_shell": True,
        "require_approval_write": True,
        "require_approval_python": True,
        "require_approval_mcp": True,
        "require_approval_tests": True,
        "allow_outside_workspace": False,
        "shell_timeout": 120,
        "max_output_chars": 12000,
        "blocked_commands": [
            "format ", "mkfs", "rm -rf /", "rm -rf ~", "del /s /q c:\\", "rd /s /q c:\\",
            "shutdown", "reboot", "diskpart", "reg delete", "bcdedit", "cipher /w",
            ":(){", "Remove-Item -Recurse C:\\", "Set-ExecutionPolicy",
        ],
    },
    "code": {
        # Folders you've opened in Code mode (each one is allowed as a sandbox root).
        "projects": [],
        "max_steps": 30,          # coding tasks need more tool rounds
        "test_command": "",       # override auto-detection, e.g. "npm test"
    },
    "phone": {"enabled": False, "port": 8766},
    "mcp": {
        # [mcp.servers.<name>] transport = "stdio" (command/args/env) or "http" (url/headers)
        "servers": {},
    },
    "ui": {
        "port": 0,
        "hotkey": "<ctrl>+<alt>+<space>",
        "minimize_to_tray": True,
        "start_hidden": False,
        "width": 1180,
        "height": 800,
    },
    "tools": {"disabled": []},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Thread-safe config holder. Access sections like cfg['ollama']['model']."""

    def __init__(self, path: Path | None = None):
        self.path = path or paths.CONFIG_FILE
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        with self._lock:
            user: dict[str, Any] = {}
            if self.path.exists():
                with open(self.path, "rb") as f:
                    user = tomllib.load(f)
            self.data = _deep_merge(DEFAULTS, user)

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(dump_toml(self.data), encoding="utf-8")

    def update(self, patch: dict[str, Any]) -> None:
        with self._lock:
            self.data = _deep_merge(self.data, patch)
            self.save()

    def set(self, path: str, value: Any) -> None:
        """Replace a value outright (unlike update(), which merges dicts)."""
        with self._lock:
            keys = path.split(".")
            d = self.data
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            if value is None:
                d.pop(keys[-1], None)
            else:
                d[keys[-1]] = value
            self.save()

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self.data[key]

    # Convenience -----------------------------------------------------------
    @property
    def workspace(self) -> Path:
        ws = Path(os.path.expandvars(self.data["agent"]["workspace"])).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    @property
    def xai_key(self) -> str:
        return self.data["xai_api"].get("api_key") or os.environ.get("XAI_API_KEY", "")

    SECRET_FIELDS = (("xai_api", "XAI_API_KEY"), ("claude_api", "ANTHROPIC_API_KEY"),
                     ("openai_compat", "OPENAI_API_KEY"))

    def public(self) -> dict[str, Any]:
        """Config safe to send to the UI (API key masked)."""
        d = copy.deepcopy(self.data)
        for section, env in self.SECRET_FIELDS:
            key = self.data[section].get("api_key") or os.environ.get(env, "")
            d[section]["api_key"] = ("•" * 8 + key[-4:]) if key else ""
            d[section]["has_key"] = bool(key)
        for srv in d.get("mcp", {}).get("servers", {}).values():  # mask secrets in MCP env/headers
            for field in ("env", "headers"):
                for k, v in (srv.get(field) or {}).items():
                    if v and any(w in k.lower() for w in ("key", "token", "secret", "auth", "password")):
                        srv[field][k] = "•" * 8 + str(v)[-4:]
        d["agent"]["workspace_resolved"] = str(self.workspace)
        return d


# Minimal TOML writer (tables, strings, numbers, bools, lists) — keeps deps small.
def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v)
    if "\n" in s:
        return '"""' + s.replace("\\", "\\\\").replace('"""', '\\"""') + '"""'
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_toml(data: dict[str, Any], prefix: str = "") -> str:
    lines: list[str] = []
    tables = []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{k} = {_toml_value(v)}")
    out = "\n".join(lines) + ("\n" if lines else "")
    for k, v in tables:
        name = f"{prefix}.{k}" if prefix else k
        out += f"\n[{name}]\n" + dump_toml(v, name)
    return out
