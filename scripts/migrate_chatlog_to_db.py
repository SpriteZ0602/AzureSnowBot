#!/usr/bin/env python3
"""
一次性迁移: data/sessions/groups/<gid>/_chatlog.jsonl → data/sessions/chatlog.db

用法（在项目根目录，用 bot 的 python 环境）:
    python scripts/migrate_chatlog_to_db.py

幂等: 重复执行不会产生重复数据（唯一索引 + INSERT OR IGNORE），
迁移后原 JSONL 文件保留不动，确认无误后可手动删除。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_chatlog_db():
    """直接按文件路径加载 chatlog_db（它不依赖 nonebot，但避免触发包 __init__）"""
    spec = importlib.util.spec_from_file_location(
        "chatlog_db", ROOT / "plugins" / "group" / "chatlog_db.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chatlog_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    db = _load_chatlog_db()
    print(f"目标数据库: {db.DB_PATH}")

    report = db.migrate_jsonl_to_db(ROOT)
    if not report:
        print("未找到任何 _chatlog.jsonl，无事可做")
        return

    total_read = total_new = 0
    for gid, r in sorted(report.items()):
        print(f"  群 {gid}: 读取 {r['read']} 条, 新入库 {r['imported']} 条")
        total_read += r["read"]
        total_new += r["imported"]
    print(f"合计: 读取 {total_read} 条, 新入库 {total_new} 条")

    if total_read != total_new:
        diff = total_read - total_new
        print(f"提示: {diff} 条未重复入库（库中已有或坏行；重复执行脚本时属正常幂等跳过）")


if __name__ == "__main__":
    main()
