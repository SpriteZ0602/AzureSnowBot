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

from nonebot.log import logger

from .manager import register_tool
from ..tool_history import summarize_web_search, summarize_web_read

# ──────────────────────────────────────────────────────
# 文件系统工具（私聊 Admin 限定，群聊限定本群目录）
# ──────────────────────────────────────────────────────

# 私聊允许操作的目录白名单（相对于项目根目录）
_ALLOWED_ROOTS = [
    Path("data/admin"),
    Path("data/skills"),
    Path("data/personas"),
]

# 群聊记忆根目录：data/groups/<群号>/
GROUP_MEMORY_ROOT = Path("data/groups")

# 群聊单次写入上限（群成员可影响 LLM，限制爆炸半径；私聊 Admin 不限）
GROUP_WRITE_MAX_CHARS = 20_000


def _check_scope(context: dict | None) -> tuple[str, str | None]:
    """校验工具调用场景，返回 (chat_type, error_msg)。

    私聊和群聊都可以使用文件工具（范围不同）：
      - private → 私聊白名单目录
      - group   → 仅本群 data/groups/<群号>/ 目录
    """
    if not context:
        return "", "[错误] 此工具仅限私聊或群聊使用"
    chat_type = context.get("_chat_type")
    if chat_type not in ("private", "group"):
        return "", "[错误] 此工具仅限私聊或群聊使用"
    return chat_type, None


def _check_private_only(context: dict | None) -> str | None:
    """校验是否 Admin 私聊场景，返回错误信息或 None（用于 run_command 等敏感工具）"""
    if not context or context.get("_chat_type") != "private":
        return "[错误] 此工具仅限 Admin 私聊使用"
    return None


def _scope_roots(chat_type: str, context: dict | None) -> tuple[list[Path], str | None]:
    """根据场景返回允许的根目录列表，返回 (roots, error_msg)"""
    if chat_type == "private":
        return list(_ALLOWED_ROOTS), None

    # 群聊：仅本群记忆目录（群号必须是纯数字，防止路径注入）
    group_id = str((context or {}).get("_target_id", ""))
    if not group_id or not group_id.isdigit():
        return [], f"[错误] 无效的群号: {group_id or '空'}"
    return [GROUP_MEMORY_ROOT / group_id], None


def _resolve_safe_path(filepath: str, context: dict | None = None) -> tuple[Path, str | None]:
    """
    解析文件路径并检查是否在允许目录内（按场景动态决定）。
    返回 (resolved_path, error_msg)，error_msg 为 None 表示安全。

    群聊额外支持相对本群目录的路径（如 "MEMORY.md"）——LLM 不需要知道群号；
    最终路径仍必须落在 data/groups/<群号>/ 内，".." 与绝对路径逃逸一律拒绝。
    """
    chat_type, err = _check_scope(context)
    if err:
        return Path(), err

    roots, err = _scope_roots(chat_type, context)
    if err:
        return Path(), err

    # 候选路径：先按原样解析；群聊再尝试按"相对本群目录"解析。
    # 以 data/ 开头的路径视为项目根相对路径，保持严格校验不 fallback
    # （防止 "data/groups/<其他群>/..." 被嵌套解析进本群目录）。
    candidates = [filepath]
    if (
        chat_type == "group"
        and filepath
        and not filepath.startswith("data/")
        and not Path(filepath).is_absolute()
    ):
        candidates.append(str(roots[0] / filepath))

    last_target = Path()
    for candidate in candidates:
        try:
            target = Path(candidate).resolve()
        except Exception:
            return Path(), f"[错误] 无效路径: {filepath}"

        last_target = target
        # 检查是否在允许目录内
        for allowed in roots:
            allowed_abs = allowed.resolve()
            try:
                target.relative_to(allowed_abs)
                return target, None
            except ValueError:
                continue

    allowed_str = ", ".join(str(r) for r in roots)
    return last_target, f"[错误] 路径不在允许范围内。允许的目录: {allowed_str}"


@register_tool(
    name="read_file",
    description=(
        "读取指定文件的内容。"
        "私聊：路径相对于项目根，仅限 data/admin/、data/skills/、data/personas/ 目录（查看 MEMORY.md、USER.md 等）。"
        "群聊：仅限本群记忆目录，直接传相对路径即可（如 MEMORY.md），无需知道群号。"
        "用途：查看记忆文件的当前内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。私聊: 相对于项目根（如 data/admin/MEMORY.md）；群聊: 相对本群目录（如 MEMORY.md）",
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
    chat_type, err = _check_scope(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供文件路径"

    target, err = _resolve_safe_path(path, _context)
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
        "写入内容到指定文件（覆盖写入）。"
        "私聊：路径相对于项目根，仅限 data/admin/、data/skills/、data/personas/ 目录（更新 MEMORY.md 长期记忆、USER.md 用户档案等）。"
        "群聊：仅限本群记忆目录，直接传相对路径即可（如 MEMORY.md），无需知道群号。"
        "注意：会覆盖文件全部内容，写入前建议先 read_file 查看当前内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。私聊: 相对于项目根（如 data/admin/MEMORY.md）；群聊: 相对本群目录（如 MEMORY.md）",
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
    chat_type, err = _check_scope(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供文件路径"

    target, err = _resolve_safe_path(path, _context)
    if err:
        return err
    if chat_type == "group" and len(content) > GROUP_WRITE_MAX_CHARS:
        return (
            f"[错误] 内容过长（{len(content)} 字符），"
            f"群聊单次写入上限 {GROUP_WRITE_MAX_CHARS} 字符"
        )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {path}（{len(content)} 字符）"
    except Exception as e:
        return f"[错误] 写入失败: {e}"


@register_tool(
    name="list_files",
    description=(
        "列出指定目录下的文件和子目录。"
        "私聊：路径相对于项目根，仅限 data/admin/、data/skills/、data/personas/ 目录。"
        "群聊：仅限本群记忆目录，传 . 即可列出本群目录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目录路径。私聊: 相对于项目根（如 data/admin）；群聊: 相对本群目录（如 .）",
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
    chat_type, err = _check_scope(_context)
    if err:
        return err
    if not path:
        return "[错误] 请提供目录路径"

    target, err = _resolve_safe_path(path, _context)
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

    err = _check_private_only(_context)
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
    openai_tools = [t for t in openai_tools if t["function"]["name"] not in {
        "local__run_sub_agent", "local__save_quoted_sticker", "local__delete_quoted_sticker", "local__request_sticker",
    }]
    if openai_tools:
        payload["tools"] = openai_tools

    # 工具调用上下文（继承主 Agent 的上下文）
    tool_context = dict(_context) if _context else {}
    tool_context.pop("_sticker_user_turn", None)
    if tool_context.get("_chat_type") == "group" and not tool_context.get("_persona"):
        from ..persona.manager import get_active_persona
        tool_context["_persona"] = get_active_persona(str(tool_context.get("_target_id", "")))

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
                    log_tool_call("sub_agent", fn_name, fn_args, tool_result, context=tool_context)
                    from ..tool_history import record_tool_execution
                    record_tool_execution(fn_name, fn_args, tool_result, context=tool_context, source="sub_agent")

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
        "当用户要求每天定时提醒**用户本人**去做某事时，你必须调用此工具，不能只口头答应。"
        "你没有定时循环能力，只有调用此工具才能实现每日提醒。"
        "如果用户要求你每天自己完成一件事（如生成摘要、整理记录、播报），"
        "请改用 set_daily_task，不要用本工具。"
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
    name="set_daily_task",
    description=(
        "设置每天定时自动执行的任务：到点后由你自己完成并把结果发出来，不是提醒用户去做。"
        "适用：生成当天群聊摘要、整理讨论、每日播报、定时查询等需要你动手完成的事。"
        "如果用户只是要你提醒他本人去做某事（如“提醒我签到”），请用 set_daily_reminder。"
        "私聊中如任务与某个群相关但用户没给群号，先问清楚群号再设置，不要猜。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "要你定时执行的任务描述，例如：生成当天群聊摘要、汇总今天的讨论。写清楚做什么，不要写成提醒话术。",
            },
            "time": {
                "type": "string",
                "description": "每天执行时刻，HH:MM 格式（24小时制）。例如 09:00、19:00",
            },
            "group_id": {
                "type": "string",
                "description": "任务涉及的群号。群聊中可省略（默认当前群）；私聊中涉及群聊任务时填写。",
            },
        },
        "required": ["task", "time"],
    },
)
async def set_daily_task(
    task: str = "",
    time: str = "",
    group_id: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    from ..reminder.scheduler import add_daily_task
    import re

    if not _context:
        return "[错误] 缺少上下文信息，无法设置任务"
    if not task:
        return "[错误] 任务内容不能为空"
    if not re.match(r"^\d{1,2}:\d{2}$", time):
        return "[错误] 时间格式不正确，请使用 HH:MM 格式（如 09:00、19:00）"

    parts = time.split(":")
    h, m = int(parts[0]), int(parts[1])
    if h < 0 or h > 23 or m < 0 or m > 59:
        return "[错误] 时间超出范围（小时 0-23，分钟 0-59）"
    time_normalized = f"{h:02d}:{m:02d}"

    chat_type = _context.get("_chat_type", "group")
    target_id = _context.get("_target_id", "")
    user_id = _context.get("_user_id", "")
    sender_name = _context.get("_sender_name", "用户")

    group_id = (group_id or "").strip()
    if group_id and not group_id.isdigit():
        return "[错误] 群号必须是纯数字"
    if chat_type == "group":
        if group_id and group_id != str(target_id):
            return "[错误] 群聊中只能为当前群设置任务（group_id 可省略）"
        group_id = str(target_id)

    job_id, fire_str = add_daily_task(
        chat_type=chat_type,
        target_id=target_id,
        user_id=user_id,
        creator_name=sender_name,
        task=task,
        daily_time=time_normalized,
        group_id=group_id,
    )
    return f"已设置每日定时任务 [{job_id}]：每天 {time_normalized} 自动执行「{task}」（下次执行: {fire_str}）"


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
            kind = "执行任务" if j.mode == "task" else "提醒"
            lines.append(f"  [{j.id}] 🔄 每天 {j.daily_time} {kind}「{j.message}」by {j.creator_name}")
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
        "检索群聊的历史消息记录。可按发送者昵称、QQ号、关键词、时间范围筛选。"
        "用途：查看某人最近说了什么、总结群聊内容、回顾讨论等。"
        "返回的是群内所有人的消息（不仅限@Bot的）。"
        "群聊中默认查当前群；私聊中查询群记录必须传 group_id。"
        "当用户@了某人时，你会收到该用户的QQ号，请用 user_id 参数检索。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "group_id": {
                "type": "string",
                "description": "群号。群聊中可省略（默认当前群）；私聊中查询某个群时必须提供。",
            },
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
                "description": "查看最近多少小时的记录，默认 24。today=true 时忽略此项",
            },
            "today": {
                "type": "boolean",
                "description": "只看今天 00:00 之后的记录（生成“当天摘要”时使用）",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数，默认 50。总结当天群聊时建议设大一些（如 1000）",
            },
        },
    },
)
async def get_group_chat_log(
    group_id: str = "",
    user_name: str = "",
    user_id: str = "",
    keyword: str = "",
    hours: float = 24,
    today: bool = False,
    limit: int = 50,
    _context: dict | None = None,
    **kwargs,
) -> str:
    chat_type = (_context or {}).get("_chat_type", "")
    if chat_type == "group":
        group_id = str(_context.get("_target_id", ""))
    elif not (chat_type == "private" and group_id):
        return "[错误] 此工具仅限群聊使用；私聊中查询群聊记录请提供 group_id"

    group_id = str(group_id).strip()
    if not group_id.isdigit():
        return "[错误] 群号必须是纯数字"

    if today:
        now = datetime.now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hours = max((now - midnight).total_seconds() / 3600, 1 / 60)

    from ..group.chatlog import load_chatlog

    entries = load_chatlog(
        group_id,
        hours=hours,
        user_name=user_name or None,
        user_id=user_id or None,
        keyword=keyword or None,
        limit=limit,
    )

    range_label = "今天" if today else f"最近 {hours:g} 小时"

    if not entries:
        parts = []
        if user_name:
            parts.append(f"发送者含「{user_name}」")
        if keyword:
            parts.append(f"内容含「{keyword}」")
        parts.append(range_label)
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
    header += f", {range_label}）:"

    return header + "\n" + "\n".join(lines)


# ──────────────────────────────────────────────────────
# 群管理工具（昵称→QQ号解析交给模型，硬护栏写在实现里）
# ──────────────────────────────────────────────────────

# 单次禁言上限（分钟）：高危操作，模型侧的"自觉"不可靠，上限写死在代码里
_MUTE_MAX_MINUTES = 10

_ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "群员"}


def _check_group_only(context: dict | None) -> tuple[str, str | None]:
    """校验群聊场景，返回 (group_id, error_msg)"""
    if not context or context.get("_chat_type") != "group":
        return "", "[错误] 此工具仅限群聊使用"
    group_id = str(context.get("_target_id", ""))
    if not group_id.isdigit():
        return "", "[错误] 无法获取群号"
    return group_id, None


def _member_display(m: dict) -> str:
    """成员展示名：昵称为主，群名片不同时附在括号里"""
    nick = m.get("nickname") or ""
    card = m.get("card") or ""
    if card and card != nick:
        return f"{nick}({card})"
    return nick or str(m.get("user_id", ""))


@register_tool(
    name="get_group_members",
    description=(
        "获取当前群的成员列表（QQ号 昵称(群名片) 角色）。"
        "用途：把用户提到的外号/昵称对应到QQ号、确认某人是否管理员。"
        "禁言等操作前必须先用它确认目标的QQ号。"
    ),
    parameters={"type": "object", "properties": {}},
)
async def get_group_members(_context: dict | None = None, **kwargs) -> str:
    group_id, err = _check_group_only(_context)
    if err:
        return err

    from nonebot import get_bot

    try:
        bot = get_bot()
        members = await bot.get_group_member_list(group_id=int(group_id))
    except Exception as e:
        return f"[错误] 获取群成员列表失败: {e}"

    lines = [f"{m.get('user_id', '?')} {_member_display(m)} {_ROLE_CN.get(m.get('role', 'member'), '群员')}"
             for m in members]
    if not lines:
        return "[错误] 群成员列表为空"
    if len(lines) > 200:
        lines = lines[:200] + [f"...（共 {len(members)} 人，仅显示前 200）"]
    return f"群 {group_id} 成员（QQ号 昵称 角色）:\n" + "\n".join(lines)


@register_tool(
    name="group_mute",
    description=(
        "禁言当前群的指定成员。权限由工具自动校验（调用者须为本群管理员），"
        "无需请求用户确认：目标明确时直接调用执行。"
        "user_id 用 get_group_members 查询；时长上限 10 分钟；"
        "不能禁言群主、管理员和 Bot 自己。"
        "仅当目标无法唯一确定（多个同名/相似昵称）时才向用户确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "user_id": {
                "type": "string",
                "description": "目标QQ号（数字，先用 get_group_members 确认）",
            },
            "duration_minutes": {
                "type": "integer",
                "description": "禁言时长（分钟），1-10，默认 10",
            },
        },
        "required": ["user_id"],
    },
)
async def group_mute(
    user_id: str = "",
    duration_minutes: int = 10,
    _context: dict | None = None,
    **kwargs,
) -> str:
    group_id, err = _check_group_only(_context)
    if err:
        return err

    from nonebot import get_bot

    user_id = str(user_id).strip()
    if not user_id.isdigit():
        return "[错误] user_id 必须是QQ号（数字），请先用 get_group_members 查询"

    caller_id = str((_context or {}).get("_user_id", ""))
    if not caller_id.isdigit():
        return "[错误] 无法确认调用者身份"

    try:
        bot = get_bot()
        # 硬护栏：发起者必须是本群管理员/群主（模型侧判断不可靠，这里强制校验）
        caller = await bot.get_group_member_info(group_id=int(group_id), user_id=int(caller_id))
        if caller.get("role") not in ("admin", "owner"):
            return "[错误] 只有群管理员才能禁言成员"
        # 硬护栏：目标不能是 Bot 自己、群主、管理员
        if user_id == str(bot.self_id):
            return "[错误] 不能禁言 Bot 自己"
        target = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
        if target.get("role") in ("admin", "owner"):
            return f"[错误] 不能禁言{_ROLE_CN.get(target.get('role'), '管理员')}"
        # 时长封顶
        try:
            requested = int(duration_minutes)
        except (TypeError, ValueError):
            requested = 10
        minutes = max(1, min(requested, _MUTE_MAX_MINUTES))
        await bot.set_group_ban(group_id=int(group_id), user_id=int(user_id), duration=minutes * 60)
    except Exception as e:
        return f"[错误] 禁言失败: {e}（确认 Bot 是否有管理员权限、目标是否在群里）"

    note = f"（请求超上限，已按 {_MUTE_MAX_MINUTES} 分钟执行）" if requested > _MUTE_MAX_MINUTES else ""
    return f"已将 {_member_display(target)}({user_id}) 禁言 {minutes} 分钟{note}"


# ──────────────────────────────────────────────────────
# 舞萌 DX 查分（水鱼 API + 柚子别名库；token 解析在模块内部，模型不可见）
# ──────────────────────────────────────────────────────

@register_tool(
    name="maimai_player_query",
    description=(
        "查询舞萌DX（maimai）玩家的 DX Rating 和 b50（best 50）成绩。"
        "传入玩家QQ号（可先用 get_group_members 把昵称解析成QQ号）。"
        "对方需在水鱼查分器（maimai.diving-fish.com）同步过成绩；"
        "对方开启隐私保护时可能看不到明细。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "qq": {"type": "string", "description": "玩家QQ号（数字）"},
        },
        "required": ["qq"],
    },
)
async def maimai_player_query(qq: str = "", **kwargs) -> str:
    from ..maimai import query_player_b50

    return await query_player_b50(qq)


@register_tool(
    name="maimai_song_search",
    description=(
        "按别名/俗称/曲名关键词模糊搜索舞萌DX曲目，返回曲名、song_id 和全部别名。"
        "用户用外号问歌（如'鸟屎是什么歌'）时使用；也可用于把外号转换成正式曲名。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "song_name": {"type": "string", "description": "别名、俗称或曲名关键词，如: 鸟屎"},
        },
        "required": ["song_name"],
    },
)
async def maimai_song_search(song_name: str = "", **kwargs) -> str:
    from ..maimai import format_songs, search_songs

    if not song_name.strip():
        return "[错误] song_name 不能为空"
    hits = await search_songs(song_name)
    return format_songs(hits)


# ──────────────────────────────────────────────────────
# 网络搜索与网页读取工具（DeepSeek Search / Fetch API）
# ──────────────────────────────────────────────────────

_DEEPSEEK_SEARCH_PATH = "/tools/search"
_DEEPSEEK_FETCH_PATH = "/tools/fetch"


def _truncate(text: str, max_chars: int, url: str) -> str:
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...(内容被截断，共 {len(text)} 字符)"
    return text


def _deepseek_tools_config() -> tuple[str, str]:
    """读取 DeepSeek Search/Fetch Tools API 的 (api_key, base_url)。"""
    from ..llm import get_deepseek_tools_config

    return get_deepseek_tools_config()


async def _deepseek_search(
    query: str,
    num_results: int,
    *,
    need_content: bool = False,
) -> list[dict]:
    """调用 DeepSeek 网页搜索 API，返回结构化结果列表。

    每条结果包含 title / url / snippet / summary / content / site_name /
    date / authority / rerank_score 等字段（见官方文档）。
    """
    import httpx as _httpx

    api_key, base_url = _deepseek_tools_config()
    if not api_key:
        raise RuntimeError("未配置 DeepSeek API Key（DEEPSEEK_API_KEY）")

    payload: dict = {
        "query": query[:128],
        "count": max(1, min(50, num_results)),
    }
    if need_content:
        payload["need_content"] = True

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with _httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}{_DEEPSEEK_SEARCH_PATH}",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    return results if isinstance(results, list) else []


async def _deepseek_fetch(url: str, max_chars: int = 6000) -> str:
    """调用 DeepSeek 网页抓取 API，返回 Markdown 正文。"""
    import httpx as _httpx

    api_key, base_url = _deepseek_tools_config()
    if not api_key:
        return "[错误] 未配置 DeepSeek API Key（DEEPSEEK_API_KEY）"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base_url}{_DEEPSEEK_FETCH_PATH}",
                headers=headers,
                json={"url": url},
            )
            resp.raise_for_status()
            data = resp.json()
    except _httpx.HTTPStatusError as e:
        logger.warning(f"DeepSeek 网页抓取失败: {e}")
        return (
            f"[错误] 网页抓取失败: HTTP {e.response.status_code} "
            f"{e.response.text[:200]}"
        )
    except Exception as e:
        logger.warning(f"DeepSeek 网页抓取失败: {e}")
        return f"[错误] 读取网页失败: {e}"

    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return f"[错误] 页面正文为空: {url}"

    text = f"# {title}\n\n{content}" if title else content
    return _truncate(text, max_chars, url)


@register_tool(
    name="web_read",
    history_formatter=summarize_web_read,
    description=(
        "读取指定网页的内容，返回 Markdown 格式的正文。"
        "用途：查看某个链接的完整内容、阅读文章、获取网页信息。"
        "使用 DeepSeek 网页抓取 API，自动过滤广告和导航。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要读取的网页 URL，例如: https://example.com/article",
            },
        },
        "required": ["url"],
    },
)
async def web_read_tool(url: str = "", **kwargs) -> str:
    if not url:
        return "[错误] 请提供 URL"
    return await _deepseek_fetch(url, max_chars=6000)


@register_tool(
    name="web_search",
    history_formatter=summarize_web_search,
    description=(
        "使用 DeepSeek 搜索互联网获取实时信息。"
        "返回标题、URL、长摘要；默认同时返回前几个结果的高质量正文。"
        "当用户询问最新新闻、实时数据、或你不确定的事实时使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，例如: 今天天气、最新新闻、Python 3.14 新特性",
            },
            "num_results": {
                "type": "integer",
                "description": "返回结果数量，默认 5，最多 10",
            },
            "auto_read": {
                "type": "boolean",
                "description": "是否同时返回前几个结果的正文（默认 true）。只想看摘要列表时设为 false",
            },
        },
        "required": ["query"],
    },
)
async def web_search_tool(query: str = "", num_results: int = 5, auto_read: bool = True, **kwargs) -> str:
    if not query:
        return "[错误] 请提供搜索关键词"

    num_results = max(1, min(10, num_results))

    try:
        results = await _deepseek_search(
            query, num_results, need_content=auto_read,
        )
    except Exception as e:
        logger.warning(f"DeepSeek 搜索失败: {e}")
        return f"[错误] 搜索失败: {e}"

    if not results:
        return f"未找到与「{query}」相关的搜索结果"

    lines: list[str] = []
    for i, item in enumerate(results, 1):
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        summary = (item.get("summary") or item.get("snippet") or "").strip()
        # summary 可能长达上千字（接近正文），列表里只保留摘要；完整正文在下方
        if len(summary) > 300:
            summary = summary[:300] + "…"
        site = (item.get("site_name") or "").strip()
        date = (item.get("date") or "").strip()
        meta_bits = [bit for bit in (site, date[:10] if date else "") if bit]
        meta = f" [{' | '.join(meta_bits)}]" if meta_bits else ""
        lines.append(f"{i}. {title}{meta}\n   {url}\n   {summary}")

    base = f"搜索结果（{query}，{len(results)} 条）:\n\n" + "\n\n".join(lines)

    if not auto_read:
        return base

    # DeepSeek 搜索接口在 need_content=true 时直接返回正文，
    # 只展示前 3 条并截断，避免单次工具结果过大。
    read_limit = min(3, len(results))
    sections: list[str] = []
    for item in results[:read_limit]:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        sections.append(f"--- {title} ({url})\n{_truncate(content, 3000, url)}")

    if not sections:
        return base
    return base + "\n\n[自动读取的前几个链接正文]\n\n" + "\n\n".join(sections)


# ──────────────────────────────────────────────────────
# 合并转发详情（群聊内分页读取）
# ──────────────────────────────────────────────────────

@register_tool(
    name="read_message_content",
    description="读取本群合并转发或 Bot 消息的更多文字。引用预览被截断时，使用其中的 reference 和 offset 分页读取。",
    parameters={"type": "object", "properties": {
        "reference": {"type": "string", "description": "预览提供的引用标识，例如 forward:xxx 或 qq:123"},
        "offset": {"type": "integer", "description": "起始字符位置，默认 0"},
        "limit": {"type": "integer", "description": "本次字符数，默认 3000，最多 6000"},
    }, "required": ["reference"]},
)
async def read_message_content(reference: str = "", offset: int = 0, limit: int = 3000,
                               _context: dict | None = None, **kwargs) -> str:
    from ..message_store import get_context
    if not _context or _context.get("_chat_type") != "group":
        return "[错误] 此工具仅限群聊使用"
    try:
        offset, limit = max(0, int(offset)), max(1, min(6000, int(limit)))
        payload = get_context(str(_context.get("_target_id", "")), reference)
    except (ValueError, TypeError):
        return "[错误] offset 和 limit 必须为整数"
    if not payload or "text" not in payload:
        return "[错误] 本群没有这条引用记录，或内容已经过期"
    from ..image_context import hydrate_text
    text = hydrate_text(str(_context.get("_target_id", "")), payload["text"])
    end = min(len(text), offset + limit)
    return f"引用 {reference}，字符 {offset}:{end}/{len(text)}\n{text[offset:end]}" + (
        f"\n[还有内容，下一页 offset={end}]" if end < len(text) else "\n[内容结束]")


# ──────────────────────────────────────────────────────
# 记忆语义搜索工具
# ──────────────────────────────────────────────────────

@register_tool(
    name="memory_search",
    description=(
        "语义搜索长期记忆和历史对话。"
        "当需要回忆之前聊过的内容、查找用户偏好、回顾过去的决定或约定时使用。"
        "私聊：搜索 Admin 长期记忆和历史；群聊：搜索本群长期记忆（data/groups/<群号>/）和本群会话历史。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索内容，用自然语言描述要找什么，例如: 用户喜欢什么、上次讨论的架构方案",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回几条结果，默认 5",
            },
            "type_filter": {
                "type": "string",
                "description": "按类型过滤结构化记忆（仅私聊），可选: identity/preference/fact/task/emotion，留空搜全部",
            },
        },
        "required": ["query"],
    },
)
async def memory_search_tool(
    query: str = "",
    max_results: int = 5,
    type_filter: str = "",
    _context: dict | None = None,
    **kwargs,
) -> str:
    chat_type, err = _check_scope(_context)
    if err:
        return err
    if not query:
        return "[错误] 请提供搜索内容"

    # 群聊：仅搜本群记忆（data/groups/<群号>/）+ 本群会话历史
    group_id = ""
    if chat_type == "group":
        group_id = str((_context or {}).get("_target_id", ""))
        if not group_id or not group_id.isdigit():
            return "[错误] 无效的群号，无法搜索记忆"

    group_memory_dir = GROUP_MEMORY_ROOT / group_id if group_id else None

    results_parts: list[str] = []

    # 1. 结构化记忆搜索（私聊搜 admin，群聊搜本群）
    try:
        from ..memory.structured import search_memories
        structured_path = (
            Path(f"data/groups/{group_id}/memories.jsonl")
            if group_id else Path("data/admin/memories.jsonl")
        )
        if structured_path.exists():
            structured = search_memories(
                structured_path,
                type_filter=type_filter if not group_id else "",
                keyword=query,
                limit=max_results,
            )
            if structured:
                lines: list[str] = []
                for e in structured:
                    lines.append(
                        f"[{e.get('type', '?')}/{e.get('subject', '?')}] "
                        f"{e.get('value', '')} (更新: {e.get('updated', '?')})"
                    )
                results_parts.append("── 结构化记忆 ──\n" + "\n".join(lines))
    except Exception:
        pass

    # 2. 向量语义搜索（来源按场景切换）
    try:
        from ..memory.indexer import search
        if group_id:
            # 本群记忆目录 + 本群会话历史（排除全量聊天记录 _chatlog）
            sources = [group_memory_dir / "MEMORY.md"]
            session_dir = Path(f"data/sessions/groups/{group_id}")
            if session_dir.exists():
                sources.extend(
                    p for p in sorted(session_dir.glob("*.jsonl"))
                    if p.name != "_chatlog.jsonl"
                )
        else:
            sources = None  # 默认 Admin 记忆源（MEMORY.md + history.jsonl）
        vector_results = await search(query, max_results=max_results, sources=sources)
        if vector_results:
            lines = []
            for r in vector_results:
                source = Path(r["source"]).name
                score = r["score"]
                text = r["text"]
                if len(text) > 500:
                    text = text[:500] + "..."
                lines.append(f"[{source} L{r['start_line']}-{r['end_line']}] (相关度 {score})\n{text}")
            results_parts.append("── 语义搜索 ──\n" + "\n\n".join(lines))
    except Exception as e:
        results_parts.append(f"── 语义搜索失败: {e} ──")

    if not results_parts:
        return f"未找到与「{query}」相关的记忆"

    return f"记忆搜索结果（查询: {query}）:\n\n" + "\n\n".join(results_parts)
