"""Guards for file and shell access. See SAFETY section in the README."""
from __future__ import annotations

import re
from pathlib import Path


class SafetyError(Exception):
    pass


def resolve_path(p: str, workspace: Path, allow_outside: bool) -> Path:
    """Resolve a user/model-supplied path. Relative paths are inside the workspace.

    Blocks escapes such as '../../Windows' or absolute paths outside the
    workspace unless allow_outside_workspace is enabled.
    """
    if not p or not str(p).strip():
        p = "."
    raw = Path(str(p).strip().strip('"').strip("'")).expanduser()
    full = (raw if raw.is_absolute() else workspace / raw).resolve()
    if not allow_outside:
        try:
            full.relative_to(workspace.resolve())
        except ValueError as e:
            raise SafetyError(
                f"'{p}' is outside the workspace ({workspace}). "
                "Enable 'allow outside workspace' in Settings > Safety to permit this.") from e
    return full


_WS = re.compile(r"\s+")


def check_command(cmd: str, blocked: list[str]) -> None:
    """Reject obviously destructive commands. This is a seatbelt, not a sandbox —
    approvals are the real protection."""
    norm = _WS.sub(" ", cmd.lower())
    for b in blocked:
        if b and _WS.sub(" ", b.lower()) in norm:
            raise SafetyError(f"Blocked by safety list: contains '{b.strip()}'. "
                              "Edit safety.blocked_commands if you really need this.")


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n… [{len(text) - limit} characters truncated] …\n\n{text[-half:]}"
