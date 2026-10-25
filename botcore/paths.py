"""所有文件入口共用的名称和目录边界检查。"""

import re
from pathlib import Path

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


def validate_name(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 128
            or value in (".", "..") or value != value.strip() or value.endswith(".")
            or any(ch in '/\\:<>"|?*' or ord(ch) < 32 or ord(ch) == 127 for ch in value)
            or value.split(".", 1)[0].upper() in _RESERVED):
        raise ValueError("名称无效，不能包含路径、控制字符或保留名称")
    return value


def validate_group_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}", value):
        raise ValueError("群号必须是 1 至 20 位数字")
    return value


def confined_path(base: Path, *parts: str) -> Path:
    path = base.joinpath(*parts)
    try:
        inside = path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError, RuntimeError):
        inside = False
    if not inside:
        raise ValueError("路径超出允许访问的目录")
    return path
