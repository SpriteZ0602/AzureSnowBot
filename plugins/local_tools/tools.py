"""
内置本地工具
──────────
在此文件中用 @register_tool 注册本地工具。
模块导入时自动注册，无需额外配置。

添加新工具只需：
1. 写一个 async def 函数
2. 加 @register_tool 装饰器
3. 重启 Bot 即可
"""

from datetime import datetime

from .manager import register_tool


@register_tool(
    name="current_time",
    description="获取当前的日期和时间。当用户询问现在几点、今天是几号、当前日期等时间相关问题时使用。",
)
async def current_time(**kwargs) -> str:
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}"


@register_tool(
    name="calculate",
    description="执行数学计算。当用户需要算术运算、数学表达式求值时使用。",
    parameters={
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "要计算的数学表达式，例如: 2**10, 3.14*5**2, 100/3",
            },
        },
        "required": ["expression"],
    },
)
async def calculate(expression: str = "", **kwargs) -> str:
    # 安全的数学表达式求值（只允许数字和运算符）
    import re
    safe_pattern = re.compile(r"^[\d\s+\-*/().,%^e]+$", re.IGNORECASE)
    expr = expression.replace("^", "**").replace("%", "/100*")
    if not safe_pattern.match(expr):
        return f"[错误] 不安全的表达式: {expression}"
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return f"{expression} = {result}"
    except Exception as e:
        return f"[计算出错] {e}"


@register_tool(
    name="random_number",
    description="生成随机数。当用户需要抽签、掷骰子、随机选择时使用。",
    parameters={
        "type": "object",
        "properties": {
            "min": {
                "type": "integer",
                "description": "最小值（包含），默认 1",
            },
            "max": {
                "type": "integer",
                "description": "最大值（包含），默认 100",
            },
        },
    },
)
async def random_number(min: int = 1, max: int = 100, **kwargs) -> str:
    import random
    if min > max:
        min, max = max, min
    result = random.randint(min, max)
    return f"随机数 [{min}, {max}]: {result}"
