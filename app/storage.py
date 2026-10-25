"""Persistent conversation history (SQLite)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  pinned INTEGER NOT NULL DEFAULT 0,
  meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  backend TEXT,
  model TEXT,
  tool_calls TEXT,
  tool_call_id TEXT,
  name TEXT,
  created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, id);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # Conversations ----------------------------------------------------------
    def create_conversation(self, title: str = "New chat") -> str:
        cid = uuid.uuid4().hex[:16]
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO conversations (id, title, created, updated) VALUES (?,?,?,?)",
                            (cid, title, now, now))
        return cid

    def list_conversations(self, q: str = "") -> list[dict[str, Any]]:
        sql = "SELECT id, title, created, updated, pinned, meta FROM conversations"
        args: tuple = ()
        if q:
            sql += (" WHERE title LIKE ? OR id IN (SELECT conv_id FROM messages WHERE content LIKE ?)")
            args = (f"%{q}%", f"%{q}%")
        sql += " ORDER BY pinned DESC, updated DESC LIMIT 500"
        with self.lock:
            rows = [dict(r) for r in self.db.execute(sql, args)]
        for r in rows:
            meta = json.loads(r.pop("meta") or "{}")
            r["project"] = meta.get("project")
        return rows

    def get_conversation(self, cid: str) -> dict[str, Any] | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["meta"] = json.loads(d["meta"] or "{}")
        return d

    def update_conversation(self, cid: str, *, title: str | None = None, pinned: bool | None = None,
                            meta: dict | None = None) -> None:
        with self.lock, self.db:
            if title is not None:
                self.db.execute("UPDATE conversations SET title=? WHERE id=?", (title[:120], cid))
            if pinned is not None:
                self.db.execute("UPDATE conversations SET pinned=? WHERE id=?", (int(pinned), cid))
            if meta is not None:
                self.db.execute("UPDATE conversations SET meta=? WHERE id=?", (json.dumps(meta), cid))

    def delete_conversation(self, cid: str) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def truncate_after(self, cid: str, message_id: int) -> None:
        """Delete messages from message_id onward (used by 'edit & resend')."""
        with self.lock, self.db:
            self.db.execute("DELETE FROM messages WHERE conv_id=? AND id>=?", (cid, message_id))

    # Messages -------------------------------------------------------------
    def add_message(self, cid: str, msg: dict[str, Any], backend: str = "", model: str = "") -> int:
        now = time.time()
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT INTO messages (conv_id, role, content, backend, model, tool_calls, tool_call_id, name, created)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (cid, msg["role"], msg.get("content") or "", backend, model,
                 json.dumps(msg["tool_calls"]) if msg.get("tool_calls") else None,
                 msg.get("tool_call_id"), msg.get("name"), now))
            self.db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, cid))
            return int(cur.lastrowid)

    def messages(self, cid: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages WHERE conv_id=? ORDER BY id", (cid,)).fetchall()
        out = []
        for r in rows:
            m: dict[str, Any] = {"id": r["id"], "role": r["role"], "content": r["content"],
                                 "backend": r["backend"], "model": r["model"], "created": r["created"]}
            if r["tool_calls"]:
                m["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_call_id"]:
                m["tool_call_id"] = r["tool_call_id"]
            if r["name"]:
                m["name"] = r["name"]
            out.append(m)
        return out

    def export_markdown(self, cid: str) -> str:
        conv = self.get_conversation(cid) or {"title": "Chat"}
        lines = [f"# {conv['title']}\n"]
        for m in self.messages(cid):
            if m["role"] == "user":
                lines.append(f"## You\n\n{m['content']}\n")
            elif m["role"] == "assistant":
                tag = f" ({m['backend']}{' · ' + m['model'] if m['model'] else ''})" if m["backend"] else ""
                if m["content"]:
                    lines.append(f"## Assistant{tag}\n\n{m['content']}\n")
                for tc in m.get("tool_calls") or []:
                    lines.append(f"> 🔧 `{tc['name']}` {json.dumps(tc['arguments'])[:300]}\n")
            elif m["role"] == "tool":
                lines.append(f"<details><summary>{m.get('name', 'tool')} result</summary>\n\n```\n"
                             f"{m['content'][:3000]}\n```\n</details>\n")
        return "\n".join(lines)
