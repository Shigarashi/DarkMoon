"""Built-in tools: web search, web fetch, files, shell, Python."""
from __future__ import annotations

import asyncio
import datetime as _dt
import fnmatch
import os
import shutil
import sys
import uuid
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .. import procs
from ..safety import SafetyError, check_command
from .registry import ToolContext, tool

IS_WIN = sys.platform == "win32"
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".darkmoon"}


# ── Web ────────────────────────────────────────────────────────────────────
@tool
def web_search(query: str, max_results: int = 6) -> str:
    """Search the web (DuckDuckGo, no API key). Returns titles, URLs and snippets.
    Use fetch_url to read a result in full.

    Args:
        query: what to search for
        max_results: number of results (1-15)
    """
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError:  # older package name
        from duckduckgo_search import DDGS  # type: ignore
    n = max(1, min(int(max_results), 15))
    results = list(DDGS().text(query, max_results=n))
    if not results:
        return "No results."
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n   {r.get('href') or r.get('url', '')}\n   {r.get('body', '')}")
    return "\n".join(lines)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article", "pre"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        import re
        t = "".join(self.parts)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n\n", t)
        return t.strip()


@tool
async def fetch_url(url: str, max_chars: int = 10000) -> str:
    """Download a web page and return its readable text.

    Args:
        url: full http(s) URL
        max_chars: maximum characters of text to return
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    async with httpx.AsyncClient(follow_redirects=True, timeout=25,
                                 headers={"User-Agent": "Mozilla/5.0 Darkmoon/1.0"}) as c:
        r = await c.get(url)
    ctype = r.headers.get("content-type", "")
    if "html" in ctype:
        p = _TextExtractor()
        p.feed(r.text)
        body = p.text()
        head = f"# {p.title.strip()}\n{url}\n\n" if p.title.strip() else f"{url}\n\n"
    elif ctype.startswith("text/") or "json" in ctype or "xml" in ctype:
        body, head = r.text, f"{url}\n\n"
    else:
        return f"{url} returned non-text content ({ctype}, {len(r.content)} bytes)."
    return f"HTTP {r.status_code}\n{head}{body[: int(max_chars)]}"


# ── Files ──────────────────────────────────────────────────────────────────
@tool
def list_dir(ctx: ToolContext, path: str = ".", recursive: bool = False) -> str:
    """List files and folders (relative to the workspace).

    Args:
        path: folder to list
        recursive: include subfolders (up to 400 entries)
    """
    root = ctx.path(path)
    if not root.exists():
        return f"Not found: {path}"
    if root.is_file():
        return f"{path} is a file ({root.stat().st_size} bytes)."
    out = []
    it = root.rglob("*") if recursive else root.iterdir()
    for p in sorted(it, key=lambda x: (not x.is_dir(), str(x).lower())):
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root)
        out.append(f"{rel}/" if p.is_dir() else f"{rel}  ({p.stat().st_size:,} B)")
        if len(out) >= 400:
            out.append("… (truncated)")
            break
    return f"{root}\n" + ("\n".join(out) if out else "(empty)")


@tool
def read_file(ctx: ToolContext, path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """Read a text file. Returns numbered lines.

    Args:
        path: file path (relative to the workspace)
        start_line: first line to return (1-based)
        max_lines: how many lines to return
    """
    f = ctx.path(path)
    if not f.is_file():
        return f"Not a file: {path}"
    data = f.read_bytes()
    if b"\x00" in data[:4096]:
        return f"{path} looks like a binary file ({len(data):,} bytes); not shown."
    lines = data.decode("utf-8", errors="replace").splitlines()
    s = max(1, int(start_line))
    chunk = lines[s - 1: s - 1 + int(max_lines)]
    body = "\n".join(f"{i:>5}  {line}" for i, line in enumerate(chunk, s))
    more = f"\n… {len(lines) - (s - 1 + len(chunk))} more lines" if s - 1 + len(chunk) < len(lines) else ""
    return f"{f.name} ({len(lines)} lines)\n{body}{more}"


def _write_preview(args: dict) -> str:
    c = args.get("content", "")
    return f"{'APPEND to' if args.get('append') else 'WRITE'}: {args.get('path')}\n" \
           f"({len(c):,} chars)\n\n{c[:3000]}{'…' if len(c) > 3000 else ''}"


@tool(approval="write", preview=_write_preview)
def write_file(ctx: ToolContext, path: str, content: str, append: bool = False) -> str:
    """Create or overwrite a text file (parent folders are created). Set append to add to the end.

    Args:
        path: file path (relative to the workspace)
        content: full text to write
        append: append instead of overwrite
    """
    f = ctx.path(path)
    f.parent.mkdir(parents=True, exist_ok=True)
    existed = f.exists()
    with open(f, "a" if append else "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    return f"{'Appended to' if append else ('Overwrote' if existed else 'Created')} {f} ({len(content):,} chars)."


def _edit_preview(args: dict) -> str:
    return f"EDIT {args.get('path')}\n\n--- replace ---\n{args.get('old_text', '')[:1500]}" \
           f"\n\n+++ with +++\n{args.get('new_text', '')[:1500]}"


@tool(approval="write", preview=_edit_preview)
def edit_file(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    """Replace an exact snippet in a file. old_text must appear exactly once — read the file first.

    Args:
        path: file path
        old_text: exact text to find (include enough context to be unique)
        new_text: replacement text
    """
    f = ctx.path(path)
    if not f.is_file():
        return f"Not a file: {path}"
    text = f.read_text(encoding="utf-8")
    n = text.count(old_text)
    if n == 0:
        return "old_text not found. Read the file and copy the snippet exactly."
    if n > 1:
        return f"old_text appears {n} times; include more surrounding text so it is unique."
    f.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {f}."


@tool
def find_files(ctx: ToolContext, pattern: str = "*", path: str = ".", contains: str = "") -> str:
    """Find files by name pattern (e.g. '*.py') and optionally text they contain.

    Args:
        pattern: filename glob
        path: folder to search
        contains: only files containing this text (case-insensitive)
    """
    root = ctx.path(path)
    hits = []
    needle = contains.lower()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if not fnmatch.fnmatch(fn.lower(), pattern.lower()):
                continue
            p = Path(dirpath) / fn
            if needle:
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                matches = [f"    {i}: {line.strip()[:160]}" for i, line in enumerate(lines, 1) if needle in line.lower()]
                if matches:
                    hits.append(f"{p.relative_to(root)}\n" + "\n".join(matches[:8]))
            else:
                hits.append(str(p.relative_to(root)))
            if len(hits) >= 200:
                return "\n".join(hits) + "\n… (truncated)"
    return "\n".join(hits) if hits else "No matches."


# ── Execution ──────────────────────────────────────────────────────────────
def _shell_argv(command: str) -> list[str] | str:
    if IS_WIN:
        exe = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/bash", "-lc", command] if os.path.exists("/bin/bash") else command


@tool(approval="shell", preview=lambda a: f"$ {a.get('command', '')}\n\n(in the workspace folder)")
async def run_shell(ctx: ToolContext, command: str, timeout: int = 0) -> str:
    """Run a shell command in the workspace folder and return its output.
    Windows uses PowerShell; macOS/Linux use bash. Avoid interactive commands.

    Args:
        command: the command line to run
        timeout: seconds before the command is killed (0 = default)
    """
    s = ctx.cfg["safety"]
    check_command(command, s.get("blocked_commands", []))
    t = int(timeout) or int(s.get("shell_timeout", 120))
    argv = _shell_argv(command)

    async def stream(line: str):
        await ctx.emit({"type": "tool_output", "text": line})

    rc, out, timed_out = await procs.stream_process(
        argv, cwd=ctx.workspace, timeout=t, shell=isinstance(argv, str), on_line=stream)
    status = f"[timed out after {t}s]" if timed_out else f"[exit code {rc}]"
    return f"{out.rstrip()}\n{status}" if out.strip() else status


def _python_exe(cfg) -> str:
    configured = cfg["agent"].get("python", "")
    if configured:
        return configured
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "python3", "py"):
        p = shutil.which(name)
        if p:
            return p
    raise SafetyError("No Python interpreter found on PATH. Install Python or set agent.python.")


@tool(approval="python", preview=lambda a: a.get("code", "")[:4000])
async def run_python(ctx: ToolContext, code: str, timeout: int = 60) -> str:
    """Run a Python 3 script in the workspace folder and return stdout/stderr.
    Use print() to show results. Good for calculations, data processing and checks.

    Args:
        code: complete Python source to execute
        timeout: seconds before it is killed
    """
    tmp = ctx.workspace / ".darkmoon" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    f = tmp / f"run_{uuid.uuid4().hex[:8]}.py"
    f.write_text(code, encoding="utf-8")

    async def stream(line: str):
        await ctx.emit({"type": "tool_output", "text": line})

    try:
        rc, out, timed_out = await procs.stream_process(
            [_python_exe(ctx.cfg), "-u", str(f)], cwd=ctx.workspace, timeout=int(timeout), on_line=stream)
    finally:
        try:
            f.unlink()
        except OSError:
            pass
    status = f"[timed out after {timeout}s]" if timed_out else f"[exit code {rc}]"
    return f"{out.rstrip()}\n{status}" if out.strip() else status


@tool
def current_datetime() -> str:
    """Get the current local date, time and timezone."""
    now = _dt.datetime.now().astimezone()
    return now.strftime("%A, %B %d, %Y %H:%M:%S %Z (UTC%z)")


# keep asyncio imported for plugins that copy this file as a template
_ = asyncio
