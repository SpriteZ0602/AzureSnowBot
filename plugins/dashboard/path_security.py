"""把共用路径校验错误转换为 Dashboard HTTP 错误。"""

from fastapi import HTTPException
from botcore import paths

def _checked(fn, *args):
    try:
        return fn(*args)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

def validate_name(value):
    return _checked(paths.validate_name, value)

def validate_group_id(value):
    return _checked(paths.validate_group_id, value)

def confined_path(base, *parts):
    return _checked(paths.confined_path, base, *parts)
