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


# ──────────────────────────────────────────────────────
# 定时提醒工具
# ──────────────────────────────────────────────────────

@register_tool(
    name="set_reminder",
    description=(
        "当用户要求定时提醒时，你必须调用此工具来实际设置提醒，不能只口头答应。"
        "触发词：提醒我、X分钟后、X小时后、过一会儿、待会儿、稍后、别忘了、记得提醒等。"
        "你没有记忆定时提醒的能力，只有调用此工具才能真正设置提醒。"
        "只处理用户最新的这条消息中的提醒请求，之前的提醒默认已经设置过。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要提醒的事项（简短名词/动词），例如: 开会、喝水、下班、起床、取快递。不要填写生成好的提醒话术，只填事项本身。",
            },
            "delay_minutes": {
                "type": "number",
                "description": "延迟分钟数。例如用户说30分钟后就填30，1小时后就填60，1.5小时就填90",
            },
        },
        "required": ["message", "delay_minutes"],
    },
)
async def set_reminder(
    message: str = "",
    delay_minutes: float = 0,
    _context: dict | None = None,
    **kwargs,
) -> str:
    from ..reminder.scheduler import add_reminder

    if not _context:
        return "[错误] 缺少上下文信息，无法设置提醒"
    if not message:
        return "[错误] 提醒内容不能为空"
    if delay_minutes <= 0:
        return "[错误] 延迟时间必须大于0分钟"

    chat_type = _context.get("_chat_type", "group")
    target_id = _context.get("_target_id", "")
    user_id = _context.get("_user_id", "")
    sender_name = _context.get("_sender_name", "用户")

    job_id, fire_str = add_reminder(
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=sender_name,
        message=message,
        delay_minutes=delay_minutes,
    )
    return f"已设置提醒 [{job_id}]：{delay_minutes}分钟后（{fire_str}）提醒「{message}」"


@register_tool(
    name="set_daily_reminder",
    description=(
        "当用户要求每天定时提醒时，你必须调用此工具，不能只口头答应。"
        "触发词：每天X点、每日提醒、定时提醒、每天早上/晚上等。"
        "你没有定时循环能力，只有调用此工具才能实现每日提醒。"
        "只处理用户最新的这条消息中的提醒请求，之前的提醒默认已经设置过。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要提醒的事项（简短名词/动词），例如: 签到、学英语、锻炼身体、起床。不要填写生成好的提醒话术，只填事项本身。",
            },
            "time": {
                "type": "string",
                "description": "每天的提醒时刻，HH:MM 格式（24小时制）。例如 09:00、14:30、22:00",
            },
        },
        "required": ["message", "time"],
    },
)
async def set_daily_reminder_tool(
    message: str = "",
    time: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    from ..reminder.scheduler import add_daily_reminder
    import re

    if not _context:
        return "[错误] 缺少上下文信息，无法设置提醒"
    if not message:
        return "[错误] 提醒内容不能为空"
    if not re.match(r"^\d{1,2}:\d{2}$", time):
        return "[错误] 时间格式不正确，请使用 HH:MM 格式（如 09:00、14:30）"

    # 验证时间范围
    parts = time.split(":")
    h, m = int(parts[0]), int(parts[1])
    if h < 0 or h > 23 or m < 0 or m > 59:
        return "[错误] 时间超出范围（小时 0-23，分钟 0-59）"
    time_normalized = f"{h:02d}:{m:02d}"

    chat_type = _context.get("_chat_type", "group")
    target_id = _context.get("_target_id", "")
    user_id = _context.get("_user_id", "")
    sender_name = _context.get("_sender_name", "用户")

    job_id, fire_str = add_daily_reminder(
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=sender_name,
        message=message,
        daily_time=time_normalized,
    )
    return f"已设置每日提醒 [{job_id}]：每天 {time_normalized} 提醒「{message}」（下次触发: {fire_str}）"


@register_tool(
    name="cancel_reminder",
    description="取消一个已设置的定时提醒（一次性或每日定时都可取消）。需要提供提醒ID。",
    parameters={
        "type": "object",
        "properties": {
            "reminder_id": {
                "type": "string",
                "description": "要取消的提醒ID（设置提醒时返回的ID）",
            },
        },
        "required": ["reminder_id"],
    },
)
async def do_cancel_reminder(
    reminder_id: str = "",
    **kwargs,
) -> str:
    from ..reminder.scheduler import cancel_reminder

    if not reminder_id:
        return "[错误] 请提供要取消的提醒ID"
    ok = cancel_reminder(reminder_id)
    if ok:
        return f"已取消提醒 [{reminder_id}]"
    return f"[错误] 未找到提醒 [{reminder_id}]，可能已触发或不存在"


@register_tool(
    name="list_reminders",
    description="列出当前对话中所有待触发的定时提醒。",
)
async def do_list_reminders(
    _context: dict | None = None,
    **kwargs,
) -> str:
    from ..reminder.scheduler import get_reminders

    if not _context:
        return "[错误] 缺少上下文信息"

    chat_type = _context.get("_chat_type", "group")
    target_id = _context.get("_target_id", "")

    jobs = get_reminders(chat_type, target_id)
    if not jobs:
        return "当前没有待触发的提醒"

    lines = []
    for j in jobs:
        fire_at = datetime.fromisoformat(j.fire_at)
        if j.recurring == "daily":
            lines.append(f"  [{j.id}] 🔄 每天 {j.daily_time}「{j.message}」by {j.creator_name}")
        else:
            remaining = (fire_at - datetime.now()).total_seconds()
            if remaining > 0:
                mins = int(remaining // 60)
                secs = int(remaining % 60)
                time_str = f"{mins}分{secs}秒后"
            else:
                time_str = "即将触发"
            lines.append(f"  [{j.id}] ⏰ {j.message} — {time_str}（{fire_at.strftime('%H:%M:%S')}）by {j.creator_name}")

    return f"待触发提醒 ({len(jobs)} 条):\n" + "\n".join(lines)
