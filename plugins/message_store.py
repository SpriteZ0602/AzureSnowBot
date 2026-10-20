"""QQ 消息引用的有界文本缓存；与 chatlog 共用 SQLite，不存图片二进制。"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data/sessions/chatlog.db"
MAX_BYTES = 50 * 1024 * 1024
MAX_ENTRY_BYTES = 256 * 1024
RETENTION_SECONDS = 365 * 86400
FORWARD_TTL = 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_context (
    group_id TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
    PRIMARY KEY(group_id, ref)
);
CREATE INDEX IF NOT EXISTS idx_message_context_expires ON message_context(expires);
CREATE TABLE IF NOT EXISTS message_context_alias (
    group_id TEXT NOT NULL, alias TEXT NOT NULL, ref TEXT NOT NULL,
    PRIMARY KEY(group_id, alias),
    FOREIGN KEY(group_id, ref) REFERENCES message_context(group_id, ref) ON DELETE CASCADE
);
"""


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _purge(conn, now):
    conn.execute("DELETE FROM message_context WHERE expires <= ?", (now,))
    size = conn.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM message_context").fetchone()[0]
    if size > MAX_BYTES:
        for gid, ref, count in conn.execute(
            "SELECT group_id,ref,length(CAST(payload AS BLOB)) FROM message_context ORDER BY created,rowid"
        ).fetchall():
            conn.execute("DELETE FROM message_context WHERE group_id=? AND ref=?", (gid, ref))
            size -= count
            if size <= MAX_BYTES:
                break


def put_context(group_id: str, ref: str, payload: dict, *, kind: str,
                ttl: float = RETENTION_SECONDS, aliases=()) -> bool:
    if not str(group_id).isdigit() or not ref:
        return False
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_ENTRY_BYTES:
        return False
    now = time.time()
    conn = _connect()
    try:
        with conn:
            conn.execute("""INSERT INTO message_context VALUES (?,?,?,?,?,?)
                ON CONFLICT(group_id,ref) DO UPDATE SET payload=excluded.payload,
                kind=excluded.kind, expires=excluded.expires""",
                (str(group_id), ref, kind, encoded, now, now + min(ttl, RETENTION_SECONDS)))
            for alias in aliases:
                conn.execute("""INSERT INTO message_context_alias VALUES (?,?,?)
                    ON CONFLICT(group_id,alias) DO UPDATE SET ref=excluded.ref""", (str(group_id), alias, ref))
            _purge(conn, now)
        return True
    finally:
        conn.close()


def get_context(group_id: str, ref: str) -> dict | None:
    if not str(group_id).isdigit():
        return None
    conn = _connect()
    try:
        row = conn.execute("""SELECT payload FROM message_context WHERE group_id=? AND expires>? AND ref=
            COALESCE((SELECT ref FROM message_context_alias WHERE group_id=? AND alias=?),?)""",
            (str(group_id), time.time(), str(group_id), ref, ref)).fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def cleanup_context() -> None:
    conn = _connect()
    try:
        with conn:
            _purge(conn, time.time())
    finally:
        conn.close()


def remember_sent_message(group_id: str, response: dict, text: str, author: str) -> None:
    mid = response.get("message_id")
    fid = response.get("res_id") or response.get("forward_id")
    if mid is None:
        return
    aliases = [f"forward:{fid}"] if fid else []
    put_context(str(group_id), f"qq:{mid}", {
        "text": text[:50000], "images": [], "author": author,
    }, kind="outgoing", aliases=aliases)
