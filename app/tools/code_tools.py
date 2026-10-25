"""Code mode tools: project overview, tests, git and pull requests.

These run in the chat's project folder (Code mode) or the workspace. Helpers
here are also used by the server for the Changes panel.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import procs
from ..safety import SafetyError
from .builtin import SKIP_DIRS, _shell_argv
from .registry import ToolContext, tool

_WIN = sys.platform == "win32"
_NOWIN = {"creationflags": 0x08000000} if _WIN else {}


# ── Helpers (shared with the server) ──────────────────────────────────────
def git(path: Path | str, *args: str, timeout: int = 30, check: bool = False) -> tuple[int, str]:
    if not shutil.which("git"):
        raise SafetyError("git isn't installed — get it from https://git-scm.com/downloads")
    p = subprocess.run(["git", *args], cwd=str(path), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, **_NOWIN)
    out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr and p.returncode else "")
    if check and p.returncode:
        raise SafetyError(f"git {' '.join(args)} failed: {out.strip()[:500]}")
    return p.returncode, out


def is_git(path: Path | str) -> bool:
    try:
        return git(path, "rev-parse", "--is-inside-work-tree")[1].strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def detect_stack(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    has = lambda name: (p / name).exists()  # noqa: E731
    stack: list[str] = []
    test = ""
    if has("package.json"):
        try:
            pkg = json.loads((p / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pkg = {}
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        for name, label in (("next", "Next.js"), ("react", "React"), ("vue", "Vue"), ("svelte", "Svelte"),
                            ("express", "Express"), ("typescript", "TypeScript"), ("vite", "Vite")):
            if name in deps:
                stack.append(label)
        stack.append("Node.js")
        runner = "bun" if has("bun.lockb") or has("bun.lock") else "pnpm" if has("pnpm-lock.yaml") \
            else "yarn" if has("yarn.lock") else "npm"
        script = (pkg.get("scripts") or {}).get("test", "")
        if script and "no test specified" not in script:
            test = f"{runner} test"
    if has("pyproject.toml") or has("requirements.txt") or has("setup.py"):
        stack.append("Python")
        if has("pytest.ini") or has("tests") or has("test") or "pytest" in _read(p / "pyproject.toml"):
            test = test or "python -m pytest -q"
    if has("Cargo.toml"):
        stack.append("Rust"); test = test or "cargo test"
    if has("go.mod"):
        stack.append("Go"); test = test or "go test ./..."
    if list(p.glob("*.sln")) or list(p.glob("*.csproj")):
        stack.append(".NET"); test = test or "dotnet test"
    if has("pom.xml"):
        stack.append("Java (Maven)"); test = test or "mvn -q test"
    if has("build.gradle") or has("build.gradle.kts"):
        stack.append("Java/Kotlin (Gradle)")
        test = test or ((".\\gradlew test" if _WIN else "./gradlew test") if has("gradlew") else "gradle test")
    if has("Gemfile"):
        stack.append("Ruby"); test = test or ("bundle exec rspec" if has("spec") else "bundle exec rake test")
    if has("mix.exs"):
        stack.append("Elixir"); test = test or "mix test"
    if has("deno.json") or has("deno.jsonc"):
        stack.append("Deno"); test = test or "deno test"
    return {"stack": list(dict.fromkeys(stack)) or ["unknown"], "test_command": test}


def _read(f: Path) -> str:
    try:
        return f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def changes(path: Path | str, max_diff: int = 200_000) -> dict[str, Any]:
    if not is_git(path):
        return {"is_git": False, "files": [], "diff": "", "branch": ""}
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    status = git(path, "status", "--porcelain=v1", "-uall")[1]
    files: dict[str, dict[str, Any]] = {}
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2], line[3:].strip().strip('"')
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        st = "added" if code == "??" or "A" in code else "deleted" if "D" in code else \
            "renamed" if "R" in code else "modified"
        files[name] = {"path": name, "status": st, "added": 0, "deleted": 0}
    rc, numstat = git(path, "diff", "HEAD", "--numstat") if git(path, "rev-parse", "HEAD")[0] == 0 else (1, "")
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] in files:
            files[parts[2]]["added"] = int(parts[0]) if parts[0].isdigit() else 0
            files[parts[2]]["deleted"] = int(parts[1]) if parts[1].isdigit() else 0
    has_head = git(path, "rev-parse", "HEAD")[0] == 0
    diff = git(path, "diff", "HEAD")[1] if has_head else ""
    for f in [f for f in files.values() if f["status"] == "added"]:  # untracked files: show as new
        fp = Path(path) / f["path"]
        try:
            if fp.is_file() and fp.stat().st_size < 200_000:
                text = fp.read_text(encoding="utf-8")
                lines = text.splitlines()
                f["added"] = f["added"] or len(lines)
                if f["path"] not in diff:
                    diff += f"\ndiff --git a/{f['path']} b/{f['path']}\nnew file\n--- /dev/null\n+++ b/{f['path']}\n" + \
                            "".join(f"+{ln}\n" for ln in lines)
        except (OSError, UnicodeDecodeError):
            pass
    if len(diff) > max_diff:
        diff = diff[:max_diff] + "\n… diff truncated …"
    return {"is_git": True, "branch": branch, "files": sorted(files.values(), key=lambda f: f["path"]), "diff": diff}


def push_and_open_pr(path: Path | str, title: str, body: str = "", base: str = "", draft: bool = False) -> str:
    if not shutil.which("gh"):
        raise SafetyError("The GitHub CLI (gh) isn't installed. Get it from https://cli.github.com and run `gh auth login`.")
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD", check=True)[1].strip()
    default = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1].strip().removeprefix("origin/")
    if branch in ("main", "master", default) and not base:
        raise SafetyError(f"You're on '{branch}'. Create a feature branch first (git_branch), then open the PR.")
    git(path, "push", "-u", "origin", "HEAD", timeout=120, check=True)
    args = ["gh", "pr", "create", "--title", title, "--body", body or "Opened from Darkmoon."]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    p = subprocess.run(args, cwd=str(path), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, **_NOWIN)
    if p.returncode:
        raise SafetyError(f"gh pr create failed: {(p.stderr or p.stdout).strip()[:500]}")
    m = re.search(r"https://\S+/pull/\d+", p.stdout)
    return m.group(0) if m else p.stdout.strip()


def tree(path: Path, depth: int = 2, limit: int = 120) -> str:
    out: list[str] = []

    def walk(d: Path, level: int):
        try:
            items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError:
            return
        for it in items:
            if it.name in SKIP_DIRS or it.name.startswith(".") and it.name not in (".github", ".env.example"):
                continue
            if len(out) >= limit:
                return
            out.append("  " * level + it.name + ("/" if it.is_dir() else ""))
            if it.is_dir() and level + 1 < depth:
                walk(it, level + 1)

    walk(path, 0)
    return "\n".join(out) + ("\n…" if len(out) >= limit else "")


# ── Tools ─────────────────────────────────────────────────────────────────
@tool
def project_overview(ctx: ToolContext) -> str:
    """Summarize the current project: detected stack, test command, git branch/status and file tree.
    Call this first when starting work on a codebase."""
    root = ctx.workspace
    st = detect_stack(root)
    lines = [f"Project: {root.name}  ({root})", f"Stack: {', '.join(st['stack'])}",
             f"Test command: {ctx.cfg['code'].get('test_command') or st['test_command'] or '(none detected)'}"]
    if is_git(root):
        ch = changes(root, max_diff=0)
        lines.append(f"Git branch: {ch['branch']} · {len(ch['files'])} uncommitted change(s)")
    else:
        lines.append("Git: not a repository")
    readme = next((root / n for n in ("README.md", "readme.md", "README.rst", "README.txt") if (root / n).exists()), None)
    lines += ["", "Files:", tree(root)]
    if readme:
        lines += ["", f"{readme.name} (first lines):", _read(readme)[:1500]]
    return "\n".join(lines)


def _tests_preview(a: dict) -> str:
    return f"$ {a.get('command') or '(auto-detected test command)'}\n\n(runs your project's tests in the project folder)"


@tool(approval="tests", preview=_tests_preview)
async def run_tests(ctx: ToolContext, command: str = "", timeout: int = 600) -> str:
    """Run the project's tests (auto-detected, or a command you pass) and report the result.
    In Code mode, use it after every change and keep fixing until the tests pass.

    Args:
        command: test command to run instead of the detected one
        timeout: seconds before the run is killed
    """
    cmd = command or ctx.cfg["code"].get("test_command") or detect_stack(ctx.workspace)["test_command"]
    if not cmd:
        return "No test command detected. Pass one, e.g. run_tests(command=\"npm test\")."
    argv = _shell_argv(cmd)

    async def stream(line: str):
        await ctx.emit({"type": "tool_output", "text": line})

    rc, out, timed_out = await procs.stream_process(argv, cwd=ctx.workspace, timeout=int(timeout),
                                                    shell=isinstance(argv, str), on_line=stream)
    verdict = "TIMED OUT" if timed_out else "PASSED" if rc == 0 else "FAILED"
    tail = out[-6000:] if len(out) > 6000 else out
    return f"$ {cmd}\nResult: {verdict} (exit code {rc})\n\n{tail.rstrip()}"


@tool
def git_status(ctx: ToolContext) -> str:
    """Show the current branch and uncommitted changes (files with +/- line counts)."""
    ch = changes(ctx.workspace, max_diff=0)
    if not ch["is_git"]:
        return "Not a git repository."
    if not ch["files"]:
        return f"On branch {ch['branch']} — working tree clean."
    rows = [f"  {f['status']:<9} {f['path']}  (+{f['added']} -{f['deleted']})" for f in ch["files"]]
    return f"On branch {ch['branch']}, {len(rows)} changed file(s):\n" + "\n".join(rows)


@tool
def git_diff(ctx: ToolContext, path: str = "") -> str:
    """Show uncommitted changes as a unified diff (optionally for one file).

    Args:
        path: limit to this file
    """
    if path:
        ctx.path(path)
        return git(ctx.workspace, "diff", "HEAD", "--", path)[1] or "No changes."
    return changes(ctx.workspace, max_diff=40_000)["diff"] or "No changes."


@tool
def git_log(ctx: ToolContext, count: int = 10) -> str:
    """Recent commits (one line each).

    Args:
        count: how many commits
    """
    return git(ctx.workspace, "log", f"-{max(1, min(int(count), 100))}", "--oneline", "--decorate")[1] or "No commits."


@tool(approval="write", preview=lambda a: f"Create branch '{a.get('name')}' and switch to it")
def git_branch(ctx: ToolContext, name: str) -> str:
    """Create a new branch from the current one and switch to it (use before committing feature work).

    Args:
        name: branch name, e.g. feature/dark-mode
    """
    if not re.fullmatch(r"[\w./-]+", name):
        return "Invalid branch name."
    git(ctx.workspace, "switch", "-c", name, check=True)
    return f"Switched to new branch '{name}'."


@tool(approval="write", preview=lambda a: f"git add -A && git commit -m {json.dumps(a.get('message', ''))}")
def git_commit(ctx: ToolContext, message: str) -> str:
    """Stage all changes and commit them. Only commit when the user asked you to.

    Args:
        message: commit message (imperative mood, e.g. "Add dark mode toggle")
    """
    git(ctx.workspace, "add", "-A", check=True)
    rc, out = git(ctx.workspace, "commit", "-m", message)
    return out.strip() if rc == 0 else f"Error: {out.strip()[:500]}"


@tool(approval="shell", preview=lambda a: f"git push -u origin HEAD\ngh pr create --title {json.dumps(a.get('title', ''))}"
                                          f"{' --draft' if a.get('draft') else ''}\n\n{a.get('body', '')[:1500]}")
def open_pull_request(ctx: ToolContext, title: str, body: str = "", base: str = "", draft: bool = False) -> str:
    """Push the current branch and open a GitHub pull request (needs the gh CLI). Only when the user asks.

    Args:
        title: PR title
        body: PR description (what changed and why, how it was tested)
        base: target branch (default: repo default)
        draft: open as a draft PR
    """
    return "Opened pull request: " + push_and_open_pr(ctx.workspace, title, body, base, draft)


_ = os  # keep import for plugins copying this file
