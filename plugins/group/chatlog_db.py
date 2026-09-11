"""
群聊全量记录 — SQLite 存储层
────────────────────────────
白名单群内所有消息（不仅限 @Bot）统一存入单一 SQLite 库：
data/sessions/chatlog.db

为什么从 JSONL 迁到 SQLite：
- 旧实现每次查询把整个 _chatlog.jsonl 读入内存逐行过滤，且清理靠
  整文件重写，保留期被压在 7 天；
- SQLite 走 (group_id, ts) 索引，先按群+时间窗圈定切片再过滤关键词，
  保留期放宽到一年后查询仍是毫秒级。

本模块不依赖 nonebot，可被 scripts/migrate_chatlog_to_db.py 独立加载。

表结构:
    messages(id, group_id, ts, uid, name, text)
    UNIQUE(group_id, ts, uid, text)：
    - 迁移脚本重复执行幂等（INSERT OR IGNORE）；
    - 副作用：同一秒内完全相同的消息只存一条，对检索无影响。
"""

import json
import sqlite3
import time
from pathlib import Path

# 项目根目录（plugins/group/chatlog_db.py → 上两级），与 CWD 无关
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "sessions" / "chatlog.db"

# 保留天数，超过的记录在 bot 启动时清理（旧 JSONL 时代是 7 天）
RETENTION_DAYS = 365

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    uid      TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL DEFAULT '',
    text     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_messages_group_uid ON messages(group_id, uid);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages(group_id, ts, uid, text);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    # 建表语句幂等（IF NOT EXISTS），每次连接执行的开销可忽略
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ──────────────────── 写入 ────────────────────

def append_chatlog(group_id: str, user_id: str, nickname: str, text: str) -> None:
    """追加一条群聊记录"""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO messages(group_id, ts, uid, name, text) VALUES (?,?,?,?,?)",
            (str(group_id), int(time.time()), str(user_id), str(nickname), str(text)),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────── 读取 ────────────────────

def load_chatlog(
    group_id: str,
    *,
    hours: float = 24,
    user_name: str | None = None,
    user_id: str | None = None,
    keyword: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    按条件加载群聊记录，返回按时间正序的最新 limit 条。

    参数:
        group_id:  群号
        hours:     只返回最近 N 小时的记录（默认24）
        user_name: 按发送者昵称模糊过滤（可选，LIKE，ASCII 不区分大小写）
        user_id:   按发送者 QQ 号精确过滤（可选）
        keyword:   按消息内容关键词过滤（可选，LIKE）
        limit:     最多返回条数（默认200）
    """
    cutoff = int(time.time() - hours * 3600)
    sql = ["SELECT ts, uid, name, text FROM messages WHERE group_id = ? AND ts >= ?"]
    params: list = [str(group_id), cutoff]

    if user_name:
        sql.append("AND name LIKE ?")
        params.append(f"%{user_name}%")
    if user_id:
        sql.append("AND uid = ?")
        params.append(str(user_id))
    if keyword:
        sql.append("AND text LIKE ?")
        params.append(f"%{keyword}%")

    # 先取最新的 limit 条，再反转为时间正序（与旧 JSONL 实现行为一致）
    sql.append("ORDER BY ts DESC, id DESC LIMIT ?")
    params.append(int(limit))

    conn = _connect()
    try:
        rows = conn.execute(" ".join(sql), params).fetchall()
    finally:
        conn.close()

    rows.reverse()
    return [{"ts": r[0], "uid": r[1], "name": r[2], "text": r[3]} for r in rows]


# ──────────────────── 清理过期记录 ────────────────────

def purge_old_entries(group_id: str) -> int:
    """删除单个群超过 RETENTION_DAYS 天的旧记录，返回清理条数"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM messages WHERE group_id = ? AND ts < ?",
            (str(group_id), int(cutoff)),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def purge_all_old_entries() -> int:
    """删除所有群超过 RETENTION_DAYS 天的旧记录，返回清理条数（启动清理用）"""
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM messages WHERE ts < ?", (int(cutoff),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ──────────────────── 旧 JSONL 迁移 ────────────────────

def migrate_jsonl_to_db(root: Path | None = None) -> dict:
    """
    把 data/sessions/groups/<gid>/_chatlog.jsonl 导入 SQLite。

    INSERT OR IGNORE + 唯一索引保证幂等：重复执行不会产生重复数据。
    原 JSONL 文件保留不动，确认迁移无误后可手动删除。

    返回: {"<gid>": {"read": 读取行数, "imported": 实际新入库条数}, ...}
    """
    root = Path(root) if root else ROOT
    groups_dir = root / "data" / "sessions" / "groups"
    report: dict = {}
    if not groups_dir.exists():
        return report

    for jsonl in sorted(groups_dir.glob("*/_chatlog.jsonl")):
        gid = jsonl.parent.name
        rows: list[tuple] = []
        seen = 0
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen += 1
            rows.append((
                gid,
                int(entry.get("ts", 0)),
                str(entry.get("uid", "")),
                str(entry.get("name", "")),
                str(entry.get("text", "")),
            ))

        imported = 0
        if rows:
            conn = _connect()
            try:
                before = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE group_id = ?", (gid,)
                ).fetchone()[0]
                conn.executemany(
                    "INSERT OR IGNORE INTO messages(group_id, ts, uid, name, text) VALUES (?,?,?,?,?)",
                    rows,
                )
                conn.commit()
                after = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE group_id = ?", (gid,)
                ).fetchone()[0]
                imported = after - before
            finally:
                conn.close()

        report[gid] = {"read": seen, "imported": imported}

    return report
