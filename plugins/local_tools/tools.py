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
from pathlib import Path

from .manager import register_tool

# ──────────────────────────────────────────────────────
# Admin 文件系统工具（仅 Admin 私聊可用）
# ──────────────────────────────────────────────────────

# 允许操作的目录白名单（相对于项目根目录）
_ALLOWED_ROOTS = [
    Path("data/admin"),
    Path("data/skills"),
    Path("data/personas"),
]


def _check_admin_only(context: dict | None) -> str | None:
    """校验是否 Admin 私聊，返回错误信息或 None"""
    if not context or context.get("_chat_type") != "private":
        return "[错误] 此工具仅限 Admin 私聊使用"
    return None


def _resolve_safe_path(filepath: str) -> tuple[Path, str | None]:
    """
    解析文件路径并检查是否在白名单目录内。
    返回 (resolved_path, error_msg)，error_msg 为 None 表示安全。
    """
    try:
        target = Path(filepath).resolve()
    except Exception:
        return Path(), f"[错误] 无效路径: {filepath}"

    # 检查是否在白名单目录内
    for allowed in _ALLOWED_ROOTS:
        allowed_abs = allowed.resolve()
        try:
            target.relative_to(allowed_abs)
            return target, None
        except ValueError:
            continue

    allowed_str = ", ".join(str(r) for r in _ALLOWED_ROOTS)
    return target, f"[错误] 路径不在允许范围内。允许的目录: {allowed_str}"


@register_tool(
    name="read_file",
    description=(
        "读取指定文件的内容。路径相对于项目根目录，仅限 data/admin/、data/skills/、data/personas/ 目录。"
        "用途：查看 MEMORY.md、USER.md 等上下文文件的当前内容。"
    ),
    admin_only=True,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根），例如: data/admin/MEMORY.md",
            },
        },
        "required": ["path"],
    },
)
async def read_file_tool(
    path: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    err = _check_admin_only(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供文件路径"

    target, err = _resolve_safe_path(path)
    if err:
        return err
    if not target.exists():
        return f"[错误] 文件不存在: {path}"
    if not target.is_file():
        return f"[错误] 不是文件: {path}"

    try:
        content = target.read_text(encoding="utf-8")
        if not content.strip():
            return f"文件 {path} 内容为空"
        return content
    except Exception as e:
        return f"[错误] 读取失败: {e}"


@register_tool(
    name="write_file",
    description=(
        "写入内容到指定文件（覆盖写入）。路径相对于项目根目录，仅限 data/admin/、data/skills/、data/personas/ 目录。"
        "用途：更新 MEMORY.md（写入长期记忆）、修改 USER.md（更新用户档案）等。"
        "注意：会覆盖文件全部内容，写入前建议先 read_file 查看当前内容。"
    ),
    admin_only=True,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径（相对于项目根），例如: data/admin/MEMORY.md",
            },
            "content": {
                "type": "string",
                "description": "要写入的完整文件内容",
            },
        },
        "required": ["path", "content"],
    },
)
async def write_file_tool(
    path: str = "",
    content: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    err = _check_admin_only(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供文件路径"

    target, err = _resolve_safe_path(path)
    if err:
        return err

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"[错误] 写入失败: {e}"


@register_tool(
    name="list_files",
    description=(
        "列出指定目录下的文件和子目录。路径相对于项目根目录，仅限 data/admin/、data/skills/、data/personas/ 目录。"
    ),
    admin_only=True,
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径（相对于项目根），例如: data/admin",
            },
        },
        "required": ["path"],
    },
)
async def list_files_tool(
    path: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    err = _check_admin_only(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供目录路径"

    target, err = _resolve_safe_path(path)
    if err:
        return err
    if not target.exists():
        return f"[错误] 目录不存在: {path}"
    if not target.is_dir():
        return f"[错误] 不是目录: {path}"

    try:
        items = sorted(target.iterdir())
        if not items:
            return f"目录 {path} 为空"
        lines = []
        for item in items:
            if item.is_dir():
                lines.append(f"  📁 {item.name}/")
            else:
                size = item.stat().st_size
                lines.append(f"  📄 {item.name} ({size} bytes)")
        return f"{path}/ ({len(items)} 项):\n" + "\n".join(lines)
    except Exception as e:
        return f"[错误] 列目录失败: {e}"


# ──────────────────────────────────────────────────────
# 命令执行工具（仅 Admin 私聊可用）
# ──────────────────────────────────────────────────────

@register_tool(
    name="run_command",
    description=(
        "在本地电脑上执行 shell 命令并返回输出。"
        "用途：运行脚本、查看系统状态、执行 git 操作、安装包等。"
        "重要：执行前必须先告诉用户你要执行什么命令，等用户确认后再调用。"
        "注意：超时 30 秒。"
    ),
    admin_only=True,
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的 shell 命令，例如: dir, git status, python --version",
            },
            "timeout": {
                "type": "integer",
                "description": "超时秒数，默认 30，最大 120",
            },
        },
        "required": ["command"],
    },
)
async def run_command_tool(
    command: str = "",
    timeout: int = 30,
    _context: dict | None = None,
    **kwargs,
) -> str:
    import asyncio
    import platform

    err = _check_admin_only(_context)
    if err:
        return err
    if not command:
        return "[错误] 请提供要执行的命令"

    timeout = max(1, min(120, timeout))

    # Windows 用 powershell，其他用 sh
    if platform.system() == "Windows":
        shell_cmd = ["powershell", "-NoProfile", "-Command", command]
    else:
        shell_cmd = ["sh", "-c", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )

        output_parts: list[str] = []
        if stdout:
            out = stdout.decode("utf-8", errors="replace").strip()
            if out:
                output_parts.append(out)
        if stderr:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            if err_text:
                output_parts.append(f"[stderr]\n{err_text}")

        result = "\n".join(output_parts) if output_parts else "(无输出)"

        # 截断过长输出
        if len(result) > 4000:
            result = result[:4000] + f"\n...(输出被截断，共 {len(result)} 字符)"

        exit_code = proc.returncode
        return f"[exit {exit_code}]\n{result}"

    except asyncio.TimeoutError:
        proc.kill()
        return f"[错误] 命令执行超时（{timeout}秒）"
    except Exception as e:
        return f"[错误] 命令执行失败: {e}"


# ──────────────────────────────────────────────────────
# Sub-Agent（独立 LLM 调用，隔离上下文，带完整工具链）
# ──────────────────────────────────────────────────────

@register_tool(
    name="run_sub_agent",
    description=(
        "启动一个独立的 Sub-Agent 来执行特定任务。"
        "Sub-Agent 有自己的 system prompt，只能看到你传入的 data，看不到当前对话的上下文。"
        "Sub-Agent 拥有和你一样的完整工具链（Skill + 本地工具 + MCP），可以多轮调用工具。"
        "适合需要隔离上下文的任务，比如：根据聊天记录起昵称、分析文本风格、翻译、摘要等。"
        "返回 Sub-Agent 的最终回复文本。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Sub-Agent 的 system prompt，描述它的角色和任务",
            },
            "data": {
                "type": "string",
                "description": "传给 Sub-Agent 的数据（作为 user 消息）",
            },
        },
        "required": ["task", "data"],
    },
)
async def run_sub_agent(
    task: str = "",
    data: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    if not task:
        return "[错误] 请提供 Sub-Agent 的任务描述（task）"
    if not data:
        return "[错误] 请提供要处理的数据（data）"

    import json as _json
    import httpx
    from ..llm import API_KEY, BASE_URL, MODEL
    from ..local_tools.manager import (
        get_openai_tools as local_openai_tools,
        handle_tool_call as local_handle_tool_call,
    )
    from ..mcp.manager import (
        get_openai_tools as mcp_openai_tools,
        call_tool as mcp_call_tool,
        MAX_TOOL_ROUNDS,
    )

    if not API_KEY:
        return "[错误] 未配置 API Key"

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    messages = [
        {"role": "system", "content": task},
        {"role": "user", "content": data},
    ]
    payload: dict = {
        "model": MODEL,
        "messages": messages,
    }

    # 注入工具链（不给 Skill 和 run_sub_agent，sub-agent 靠 task 指令工作）
    chat_type = (_context or {}).get("_chat_type", "private")
    openai_tools = local_openai_tools(chat_type=chat_type) + mcp_openai_tools()
    openai_tools = [t for t in openai_tools if t["function"]["name"] != "local__run_sub_agent"]
    if openai_tools:
        payload["tools"] = openai_tools

    # 工具调用上下文（继承主 Agent 的上下文）
    tool_context = dict(_context) if _context else {}

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for _round in range(MAX_TOOL_ROUNDS):
                resp = await client.post(
                    f"{BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                result = resp.json()
                from ..token_stats import record_usage
                record_usage("sub_agent", result.get("usage"))
                assistant_msg = result["choices"][0]["message"]

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    reply = (assistant_msg.get("content") or "").strip()
                    return reply if reply else "[Sub-Agent 未返回内容]"

                # 处理工具调用
                messages.append(assistant_msg)
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = _json.loads(tc["function"]["arguments"])
                    except _json.JSONDecodeError:
                        fn_args = {}

                    # 分发链路：本地工具 → MCP（sub-agent 无 Skill）
                    local_result = await local_handle_tool_call(
                        fn_name, fn_args, context=tool_context
                    )
                    if local_result is not None:
                        tool_result = local_result
                    else:
                        tool_result = await mcp_call_tool(fn_name, fn_args)

                    from ..tool_log import log_tool_call
                    log_tool_call("sub_agent", fn_name, fn_args, tool_result)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
                payload["messages"] = messages

        return "[Sub-Agent 工具调用轮次达上限]"
    except Exception as e:
        return f"[Sub-Agent 调用失败] {e}"


@register_tool(
    name="current_time",
    description="获取当前的日期和时间。当用户询问现在几点、今天是几号、当前日期等时间相关问题时使用。",
)
async def current_time(**kwargs) -> str:
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekdays[now.weekday()]}"


@register_tool(
    name="get_token_stats",
    description="查看今日 Token 使用量统计，包括各来源的消耗和预估费用。当用户问今天花了多少钱、用了多少 token 时使用。",
    admin_only=True,
)
async def get_token_stats_tool(**kwargs) -> str:
    from ..token_stats import get_stats_summary
    return get_stats_summary()


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
        "你没有定时循环能力，只有调用此工具才能实现每日提醒。"
        "message不要填写生成好的提醒话术，只填事项本身。"
        "只处理用户最新的这条消息中的提醒请求，之前的提醒默认已经设置过。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "要提醒的事项。不要填写生成好的提醒话术，只填事项本身。",
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


# ──────────────────────────────────────────────────────
# 群聊记录检索工具
# ──────────────────────────────────────────────────────

@register_tool(
    name="get_group_chat_log",
    description=(
        "检索当前群聊的历史消息记录。可按发送者昵称、QQ号、关键词、时间范围筛选。"
        "用途：查看某人最近说了什么、总结群聊内容、回顾讨论等。"
        "注意：只能在群聊中使用，返回的是群内所有人的消息（不仅限@Bot的）。"
        "当用户@了某人时，你会收到该用户的QQ号，请用 user_id 参数检索。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_name": {
                "type": "string",
                "description": "按发送者昵称筛选（模糊匹配），例如: 小明",
            },
            "user_id": {
                "type": "string",
                "description": "按发送者QQ号筛选（精确匹配）。当用户@了某人时使用这个参数",
            },
            "keyword": {
                "type": "string",
                "description": "按消息内容关键词筛选，例如: 晚饭",
            },
            "hours": {
                "type": "number",
                "description": "查看最近多少小时的记录，默认 24",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数，默认 50",
            },
        },
    },
)
async def get_group_chat_log(
    user_name: str = "",
    user_id: str = "",
    keyword: str = "",
    hours: float = 24,
    limit: int = 50,
    _context: dict | None = None,
    **kwargs,
) -> str:
    if not _context or _context.get("_chat_type") != "group":
        return "[错误] 此工具仅限群聊使用"

    group_id = _context.get("_target_id", "")
    if not group_id:
        return "[错误] 无法获取群号"

    from ..group.chatlog import load_chatlog

    entries = load_chatlog(
        group_id,
        hours=hours,
        user_name=user_name or None,
        user_id=user_id or None,
        keyword=keyword or None,
        limit=limit,
    )

    if not entries:
        parts = []
        if user_name:
            parts.append(f"发送者含「{user_name}」")
        if keyword:
            parts.append(f"内容含「{keyword}」")
        parts.append(f"最近 {hours} 小时")
        return f"未找到匹配的消息记录（{', '.join(parts)}）"

    lines: list[str] = []
    for e in entries:
        ts = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
        name = e.get("name", "未知")
        text = e.get("text", "")
        lines.append(f"[{ts}] {name}: {text}")

    header = f"群聊记录（{len(entries)} 条"
    if user_name:
        header += f", 发送者含「{user_name}」"
    if keyword:
        header += f", 内容含「{keyword}」"
    header += f", 最近 {hours}h）:"

    return header + "\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────
# 记忆语义搜索工具（仅 Admin 私聊）
# ──────────────────────────────────────────────────────

@register_tool(
    name="memory_search",
    description=(
        "语义搜索长期记忆和历史对话。"
        "当需要回忆之前聊过的内容、查找用户偏好、回顾过去的决定或约定时使用。"
        "返回最相关的记忆片段及其来源。"
    ),
    admin_only=True,
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索内容，用自然语言描述要找什么，例如: 碧碧喜欢什么、上次讨论的架构方案",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回几条结果，默认 5",
            },
        },
        "required": ["query"],
    },
)
async def memory_search_tool(
    query: str = "",
    max_results: int = 5,
    _context: dict | None = None,
    **kwargs,
) -> str:
    err = _check_admin_only(_context)
    if err:
        return err
    if not query:
        return "[错误] 请提供搜索内容"

    from ..memory.indexer import search

    try:
        results = await search(query, max_results=max_results)
    except Exception as e:
        return f"[错误] 记忆搜索失败: {e}"

    if not results:
        return f"未找到与「{query}」相关的记忆"

    lines: list[str] = []
    for r in results:
        source = Path(r["source"]).name
        score = r["score"]
        text = r["text"]
        # 截断过长的片段
        if len(text) > 500:
            text = text[:500] + "..."
        lines.append(f"[{source} L{r['start_line']}-{r['end_line']}] (相关度 {score})\n{text}")

    return f"记忆搜索结果（{len(results)} 条，查询: {query}）:\n\n" + "\n\n".join(lines)
