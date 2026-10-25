"""Run subprocesses with live line streaming, timeouts and cancellation.

Uses a reader thread instead of asyncio subprocesses so it works with any
event loop on Windows, macOS and Linux.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Awaitable, Callable, Sequence

LineCallback = Callable[[str], Awaitable[None]]

_WIN = sys.platform == "win32"
_active: set[subprocess.Popen] = set()


def _kill_tree(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        if _WIN:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           capture_output=True, creationflags=0x08000000)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            pass


def kill_all() -> None:
    for p in list(_active):
        _kill_tree(p)


async def stream_process(
    cmd: str | Sequence[str],
    *,
    cwd: str | os.PathLike | None = None,
    timeout: float = 120,
    shell: bool = False,
    env: dict[str, str] | None = None,
    on_line: LineCallback | None = None,
    stdin_text: str | None = None,
) -> tuple[int, str, bool]:
    """Run a process, streaming merged stdout/stderr lines.

    Returns (returncode, full_output, timed_out).
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    kwargs: dict = dict(
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=shell,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
    )
    if _WIN:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    p = subprocess.Popen(cmd, **kwargs)
    _active.add(p)

    def reader() -> None:
        try:
            if stdin_text is not None and p.stdin:
                try:
                    p.stdin.write(stdin_text)
                    p.stdin.close()
                except OSError:
                    pass
            assert p.stdout
            for line in p.stdout:
                loop.call_soon_threadsafe(queue.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True).start()

    out: list[str] = []
    timed_out = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_tree(p)
                break
            try:
                line = await asyncio.wait_for(queue.get(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
            if line is None:
                break
            out.append(line)
            if on_line:
                await on_line(line)
        rc = await loop.run_in_executor(None, p.wait)
    except asyncio.CancelledError:
        _kill_tree(p)
        raise
    finally:
        _active.discard(p)
    return rc, "".join(out), timed_out
