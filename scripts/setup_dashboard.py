"""初始化 Dashboard 登录：默认 admin + 随机密码，仅向 .env 保存哈希。"""

import argparse
import json
import os
import secrets
import tempfile
from io import StringIO
from pathlib import Path

import bcrypt
from dotenv.parser import parse_stream


def initialize(env_file: Path, username: str = "admin", *, reset: bool = False) -> tuple[str, str]:
    """原子更新三项 Dashboard 配置，保留其他配置；显式 reset 才覆盖已有密码。"""
    if not username or len(username) > 128 or username != username.strip() or any(ord(ch) < 32 for ch in username):
        raise ValueError("登录账号无效")
    original = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    bindings = list(parse_stream(StringIO(original)))
    if any(binding.error for binding in bindings):
        raise ValueError(".env 存在无法解析的配置，请修正后重试（未修改文件）")
    existing = {binding.key: binding.value for binding in bindings if binding.key}
    if existing.get("DASHBOARD_PASSWORD_HASH") and not reset:
        raise ValueError("已有 Dashboard 登录密码；如需更换，请加 --reset（会使旧登录失效）")

    password = secrets.token_urlsafe(18)
    updates = {
        "DASHBOARD_USER": username,
        "DASHBOARD_PASSWORD_HASH": bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
        "DASHBOARD_SECRET_KEY": secrets.token_urlsafe(48),
    }
    lines = []
    written = set()
    for binding in bindings:
        if binding.key in updates:
            if binding.key not in written:
                lines.append(f"{binding.key}={json.dumps(updates[binding.key], ensure_ascii=False)}\n")
                written.add(binding.key)
        else:
            lines.append(binding.original.string)
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    for key, value in updates.items():
        if key not in written:
            text += f"{key}={json.dumps(value, ensure_ascii=False)}\n"

    # mkstemp 在 Linux 创建 0600 文件，replace 保留该权限，不产生含密钥的备份。
    fd, filename = tempfile.mkstemp(prefix=".dashboard-env-", suffix=".tmp", dir=env_file.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, env_file)
    finally:
        temporary.unlink(missing_ok=True)
    return username, password


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 Dashboard：默认账号 admin，每次生成独立的随机密码")
    parser.add_argument("--username", default="admin", help="登录账号（默认 admin）")
    parser.add_argument("--reset", action="store_true", help="覆盖已有密码并轮换 JWT 密钥，旧登录在重启后失效")
    args = parser.parse_args()
    try:
        username, password = initialize(Path(__file__).resolve().parents[1] / ".env", args.username, reset=args.reset)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"初始化失败：{exc}\n")
    # 仅用户主动运行初始化命令时显示一次；Bot 启动日志不输出密码。
    print(f"Dashboard 账号：{username}\nDashboard 密码：{password}")
    print("已写入项目 .env（只保存密码哈希），请保存本次密码并重启 Bot。")


if __name__ == "__main__":
    main()
