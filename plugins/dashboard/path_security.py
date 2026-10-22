"""Dashboard 输入与磁盘路径边界；同时拒绝 Windows 和 POSIX 路径写法。"""

import re
from pathlib import Path

from fastapi import HTTPException

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


def validate_name(value: str) -> str:
    if (
        not value or len(value) > 128 or value in (".", "..")
        or value != value.strip() or value.endswith(".")
        or any(ch in '/\\:<>"|?*' or ord(ch) < 32 or ord(ch) == 127 for ch in value)
        or value.split(".", 1)[0].upper() in _RESERVED
    ):
        raise HTTPException(400, "名称无效，不能包含路径、控制字符或保留名称")
    return value


def validate_group_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,20}", value):
        raise HTTPException(400, "群号必须是 1 至 20 位数字")
    return value


def confined_path(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts)
    try:
        inside = path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError, RuntimeError):
        inside = False
    if not inside:
        raise HTTPException(400, "路径超出允许访问的目录")
    # 保持相对路径返回值，供现有路径接口共用。
    return path
