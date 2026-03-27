"""
私聊对话处理
──────────
私聊消息的 ChatGPT 对话处理。
"""

import json
from datetime import datetime
from pathlib import Path

import httpx
from nonebot import on_message, on_fullmatch, get_driver, get_bot
from nonebot.adapters.onebot.v11 import PrivateMessageEvent, Bot
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked
from ..runtime_context import build_runtime_context
from ..local_tools.manager import (
    get_openai_tools as local_openai_tools,
    handle_tool_call as local_handle_tool_call,
)
from ..mcp.manager import (
    get_openai_tools as mcp_openai_tools,
    call_tool as mcp_call_tool,
    MAX_TOOL_ROUNDS,
)
from ..skill.manager import (
    build_catalog_prompt as skill_catalog_prompt,
    get_openai_tools as skill_openai_tools,
    handle_tool_call as skill_handle_tool_call,
)
from .proactive import reset_idle_timer, cancel_idle_timer
from .compaction import compact_history

# ──────────────────── 配置 ────────────────────
config = get_driver().config
from ..llm import API_KEY as OPENAI_API_KEY, BASE_URL as OPENAI_BASE_URL, MODEL as OPENAI_MODEL
ADMIN_NUMBER: str = str(getattr(config, "admin_number", ""))

# 会话目录
ADMIN_DIR = Path("data/admin")
ADMIN_DIR.mkdir(parents=True, exist_ok=True)

# Admin 上下文文件列表（每次请求时动态读取）
_ADMIN_CONTEXT_FILES = [
    "SOUL.md",             # 人格灵魂（角色设定）
    "AGENTS.md",           # 操作手册
    "USER.md",             # 用户档案
    "MEMORY.md",           # 长期记忆
]


_FALLBACK_PROMPT = "你是一个有用的助手，请用中文回答用户的问题。"


def load_admin_prompt() -> str:
    """动态加载 Admin 上下文（每次调用都从磁盘读取，支持热更新）"""
    sections: list[str] = []
    for filename in _ADMIN_CONTEXT_FILES:
        fpath = ADMIN_DIR / filename
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                sections.append(f"# {filename}\n{content}")
    return "\n\n".join(sections) if sections else ""


# 256K 上下文窗口，预留 20% 安全缓冲 + 4096 给回复
MAX_CONTEXT_TOKENS = 256_000
SAFETY_MARGIN = 0.8
REPLY_RESERVE = 4096
MAX_HISTORY_TOKENS = int(MAX_CONTEXT_TOKENS * SAFETY_MARGIN) - REPLY_RESERVE


# ──────────────────── Token 估算 ────────────────────
def estimate_tokens(text: str) -> int:
    """中文约 1 字 ≈ 1.5 token，英文/数字约 4 字符 ≈ 1 token"""
    cn_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - cn_chars
    return int(cn_chars * 1.5 + other_chars / 4)


def estimate_message_tokens(msg: dict) -> int:
    """估算单条 message 的 token 数（含 role 开销约 4 token）"""
    return estimate_tokens(msg.get("content", "")) + 4


# ──────────────────── 路径工具 ────────────────────

def _user_dir(user_id: str) -> Path:
    """获取用户的数据目录（仅 Admin 私聊可用）"""
    return ADMIN_DIR


def _session_path(user_id: str) -> Path:
    return _user_dir(user_id) / "history.jsonl"


def _config_path(user_id: str) -> Path:
    return _user_dir(user_id) / "config.json"


def _load_config(user_id: str) -> dict:
    path = _config_path(user_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save_config(user_id: str, cfg: dict) -> None:
    path = _config_path(user_id)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_config(user_id: str) -> dict:
    """获取用户配置（供外部读取）"""
    return _load_config(user_id)


# ──────────────────── JSONL 会话持久化 ────────────────────

def load_history(user_id: str) -> list[dict]:
    """从 JSONL 文件加载对话历史"""
    path = _session_path(user_id)
    if not path.exists():
        return []
    messages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def append_message(user_id: str, message: dict) -> None:
    """追加一条消息到 JSONL 文件（更新 config）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = _session_path(user_id)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(message, ensure_ascii=False) + "\n")
    # 更新 last_message_at
    cfg = _load_config(user_id)
    cfg["last_message_at"] = now
    _save_config(user_id, cfg)


def clear_history(user_id: str) -> None:
    """清除用户的对话历史"""
    path = _session_path(user_id)
    if path.exists():
        path.unlink()


# ──────────────────── 时间上下文 ────────────────────

_WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

def build_time_context(user_id: str) -> str:
    """生成时间上下文字符串，追加到 system prompt 末尾。"""
    now = datetime.now()
    now_str = f"{now.strftime('%Y-%m-%d %H:%M:%S')}（{_WEEKDAYS[now.weekday()]}）"
    cfg = _load_config(user_id)
    last = cfg.get("last_message_at", "")
    if last:
        return f"\n当前时间: {now_str}，上次对话: {last}"
    return f"\n当前时间: {now_str}"


# ──────────────────── 历史截断 ────────────────────
def trim_history(messages: list[dict]) -> list[dict]:
    """从最新消息向前保留，直到累计 token 接近上限"""
    # 预估 system prompt 占用（Admin 上下文约 3000~5000 tokens）
    system_tokens = 5000
    budget = MAX_HISTORY_TOKENS - system_tokens
    trimmed: list[dict] = []
    total = 0
    for msg in reversed(messages):
        cost = estimate_message_tokens(msg)
        if total + cost > budget:
            break
        trimmed.append(msg)
        total += cost
    trimmed.reverse()
    return trimmed


# ──────────────────── 清除对话指令 ────────────────────
reset = on_fullmatch("/reset", priority=10, block=True)


@reset.handle()
async def handle_reset(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    clear_history(user_id)
    if user_id == str(ADMIN_NUMBER):
        cancel_idle_timer()
    await reset.finish("对话历史已清除。")


# ──────────────────── 主对话处理 ────────────────────
chat = on_message(priority=99, block=False)


@chat.handle()
async def handle_chat(event: PrivateMessageEvent):
    user_input = event.get_plaintext().strip()
    if not user_input:
        return

    if not OPENAI_API_KEY:
        await chat.finish("未配置 OpenAI API Key，请联系管理员。")

    user_id = str(event.user_id)

    # 仅 Admin 可以私聊
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        await chat.finish("请在群里跟我聊天哦~")

    # 检查是否引用了消息
    quoted_text = ""
    reply_id = None
    if event.reply:
        reply_id = event.reply.message_id
    else:
        for seg in event.message:
            if seg.type == "reply":
                reply_id = int(seg.data["id"])
                break
    if reply_id:
        try:
            bot = get_bot()
            msg_data = await bot.get_msg(message_id=reply_id)
            raw_msg = msg_data.get("message", "")
            if isinstance(raw_msg, str):
                quoted_text = raw_msg.strip()
            elif isinstance(raw_msg, list):
                parts = [seg.get("data", {}).get("text", "") for seg in raw_msg if isinstance(seg, dict) and seg.get("type") == "text"]
                quoted_text = "".join(parts).strip()
        except Exception as e:
            logger.warning(f"获取引用消息失败: {e}")

    # 记录用户消息（带引用内容）
    if quoted_text:
        content = f"(引用了一条消息: \"{quoted_text}\"): {user_input}"
    else:
        content = user_input
    user_msg = {"role": "user", "content": content}
    append_message(user_id, user_msg)

    # 加载历史 → 压缩（如需要） → 截断
    history = load_history(user_id)

    # Compaction: 如果历史 token 过多，压缩旧消息为摘要 + 提取记忆
    memory_path = ADMIN_DIR / "MEMORY.md"
    compacted = await compact_history(user_id, _session_path(user_id), memory_path)
    if compacted:
        history = load_history(user_id)  # 重新加载压缩后的历史

    trimmed = trim_history(history)

    # 组装 messages（动态上下文）
    prompt = load_admin_prompt() or _FALLBACK_PROMPT
    skill_catalog = skill_catalog_prompt()
    if skill_catalog:
        prompt += "\n" + skill_catalog
    cfg = _load_config(user_id)
    last = cfg.get("last_message_at", "")
    prompt += build_runtime_context(chat_type="private", last_message_at=last)
    messages = [{"role": "system", "content": prompt}] + trimmed

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
    }

    # 工具注入（完整工具链：Skill + 本地 + MCP）
    openai_tools = skill_openai_tools() + local_openai_tools() + mcp_openai_tools()
    if openai_tools:
        payload["tools"] = openai_tools

    # 工具调用上下文
    sender_name = getattr(event, "sender", None)
    sender_name = getattr(sender_name, "nickname", None) or str(event.user_id)
    _tool_context = {
        "_chat_type": "private",
        "_target_id": user_id,
        "_user_id": user_id,
        "_sender_name": sender_name,
    }

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for round_idx in range(MAX_TOOL_ROUNDS):
                resp = await client.post(
                    f"{OPENAI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                assistant_msg = choice["message"]

                tool_calls = assistant_msg.get("tool_calls")
                if not tool_calls:
                    reply = (assistant_msg.get("content") or "").strip()
                    if reply:
                        append_message(user_id, {"role": "assistant", "content": reply})
                        bot = get_bot()
                        chunks = chunk_text(reply)
                        await send_chunked(bot, event, chunks, reply_first=False)
                        reset_idle_timer()
                    return

                # 处理工具调用
                messages.append(assistant_msg)
                logger.info(f"私聊 LLM 请求工具调用 (round {round_idx + 1}): "
                            f"{[tc['function']['name'] for tc in tool_calls]}")

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    # 分发链路：Skill → 本地工具 → MCP
                    skill_result = skill_handle_tool_call(fn_name, fn_args)
                    if skill_result is not None:
                        tool_result = skill_result
                    else:
                        local_result = await local_handle_tool_call(fn_name, fn_args, context=_tool_context)
                        if local_result is not None:
                            tool_result = local_result
                        else:
                            tool_result = await mcp_call_tool(fn_name, fn_args)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })

                payload["messages"] = messages

            # 超过最大轮次
            reply = "（工具调用轮次已达上限，请重新提问）"
            append_message(user_id, {"role": "assistant", "content": reply})
            bot = get_bot()
            await send_chunked(bot, event, [reply], reply_first=False)
            reset_idle_timer()
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await chat.finish(f"API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"ChatGPT 插件异常: {e}")
        await chat.finish("请求出错，请稍后再试。")
