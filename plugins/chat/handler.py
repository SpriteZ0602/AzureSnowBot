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
from nonebot.rule import startswith, Rule
from nonebot.adapters.onebot.v11 import PrivateMessageEvent, Bot
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..chunker import chunk_text, send_chunked
from ..runtime_context import build_runtime_context, build_time_context
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
from ..proactive import reset_idle_timer, cancel_idle_timer
from .compaction import compact_history, should_compact
from ..group.utils import fetch_quoted_image_urls
from ..history_io import append_jsonl_message, clear_jsonl_history, load_jsonl_history

# ──────────────────── 配置 ────────────────────
config = get_driver().config
from ..llm import (
    API_KEY as OPENAI_API_KEY, BASE_URL as OPENAI_BASE_URL, MODEL as OPENAI_MODEL,
    SUPPORTS_VISION,
)
ADMIN_NUMBER: str = str(getattr(config, "admin_number", ""))

def is_private_event(event) -> bool:
    """NoneBot Rule：仅匹配私聊消息事件。

    防止私聊 matcher 吞掉群聊事件（handler 类型不符被 skip 后，
    block=True 的 matcher 依然会 StopPropagation，导致群聊回复失效）。
    """
    return isinstance(event, PrivateMessageEvent)

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

    # 注入结构化记忆中的核心身份信息
    try:
        from ..memory.structured import load_identity_memories
        identity_text = load_identity_memories(ADMIN_DIR / "memories.jsonl")
        if identity_text:
            sections.append(identity_text)
    except Exception:
        pass

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


# ──────────────────── 主动对话开关（私聊） ────────────────────

def get_proactive_enabled() -> bool:
    """Admin 私聊主动对话开关（默认开启）"""
    cfg = _load_config(str(ADMIN_NUMBER))
    return bool(cfg.get("proactive_enabled", True))


def set_proactive_enabled(enabled: bool) -> None:
    """设置 Admin 私聊主动对话开关"""
    cfg = _load_config(str(ADMIN_NUMBER))
    cfg["proactive_enabled"] = bool(enabled)
    _save_config(str(ADMIN_NUMBER), cfg)


# ──────────────────── JSONL 会话持久化 ────────────────────

def load_history(user_id: str) -> list[dict]:
    """从 JSONL 文件加载对话历史"""
    return load_jsonl_history(_session_path(user_id))


def append_message(user_id: str, message: dict) -> None:
    """追加一条消息到 JSONL 文件（更新 config）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_jsonl_message(_session_path(user_id), message)
    # 更新 last_message_at
    cfg = _load_config(user_id)
    cfg["last_message_at"] = now
    _save_config(user_id, cfg)


def clear_history(user_id: str) -> None:
    """清除用户的对话历史"""
    clear_jsonl_history(_session_path(user_id))


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
reset = on_fullmatch("/reset", rule=is_private_event, priority=10, block=True)


@reset.handle()
async def handle_reset(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    clear_history(user_id)
    if user_id == str(ADMIN_NUMBER):
        cancel_idle_timer("private")
        # 清除该用户的所有定时提醒
        from ..reminder.scheduler import clear_reminders
        cleared = clear_reminders("private", user_id)
    await reset.finish("对话历史已清除。")


# ──────────────────── 手动压缩指令 ────────────────────
compact_cmd = on_fullmatch("/compact", rule=is_private_event, priority=10, block=True)


@compact_cmd.handle()
async def handle_compact(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return
    memory_path = ADMIN_DIR / "MEMORY.md"
    compacted = await compact_history(user_id, _session_path(user_id), memory_path)
    if compacted:
        await compact_cmd.finish("对话历史已压缩。")
    else:
        await compact_cmd.finish("当前历史不需要压缩。")


# ──────────────────── 主动对话开关指令 ────────────────────
proactive_cmd = on_message(rule=Rule(is_private_event) & startswith("/主动对话"), priority=10, block=True)


@proactive_cmd.handle()
async def handle_proactive_cmd(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return  # 非管理员直接忽略，交给后面的聊天处理器

    text = event.get_plaintext().strip()
    if not text.startswith("/主动对话"):
        return

    # 解析参数: /主动对话 [群号] enable|disable（带群号 = 切换该群的主动对话）
    args = text[len("/主动对话"):].strip().split()
    if args and args[0].isdigit():
        target_gid = args[0]
        action = args[1].lower() if len(args) > 1 else None

        from ..persona.manager import get_group_proactive, set_group_proactive
        if action in ("enable", "on", "开"):
            enable = True
        elif action in ("disable", "off", "关"):
            enable = False
        elif action is None:
            enable = not get_group_proactive(target_gid)
        else:
            await proactive_cmd.finish("用法: /主动对话 <群号> enable|disable")

        set_group_proactive(target_gid, enable)
        if enable:
            reset_idle_timer(f"group:{target_gid}")
        else:
            cancel_idle_timer(f"group:{target_gid}")
        await proactive_cmd.finish(f"群 {target_gid} 的主动对话已{'开启' if enable else '关闭'}。")

    # 私聊模式
    arg = text[len("/主动对话"):].strip().lower()
    if arg in ("enable", "on", "开"):
        enable = True
    elif arg in ("disable", "off", "关"):
        enable = False
    elif not arg:
        enable = not get_proactive_enabled()
    else:
        await proactive_cmd.finish("用法: /主动对话 enable|disable")

    set_proactive_enabled(enable)
    if enable:
        reset_idle_timer("private")
    else:
        cancel_idle_timer("private")
    await proactive_cmd.finish(f"私聊主动对话已{'开启' if enable else '关闭'}。")


# ──────────────────── /listen 群全量上下文切换（私聊） ────────────────────
listen_cmd_private = on_message(
    rule=Rule(is_private_event) & startswith("/listen"),
    priority=10, block=True,
)


@listen_cmd_private.handle()
async def handle_listen_private(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return  # 非管理员直接忽略

    text = event.get_plaintext().strip()
    args = text[len("/listen"):].strip().split()
    if not args or not args[0].isdigit():
        await listen_cmd_private.finish("用法: /listen <群号> [on|off]")

    from ..persona.manager import get_listen_all, set_listen_all
    target_gid = args[0]
    arg = args[1].lower() if len(args) > 1 else None
    if arg in ("on", "开"):
        enable = True
    elif arg in ("off", "关"):
        enable = False
    elif arg is None:
        enable = not get_listen_all(target_gid)
    else:
        await listen_cmd_private.finish("用法: /listen <群号> [on|off]")

    set_listen_all(target_gid, enable)
    state = "已开启：回复 @Bot 时加载全量群聊上下文。" if enable else "已关闭：仅记录 @Bot 消息。"
    await listen_cmd_private.finish(f"群 {target_gid} 的{state}")


# ──────────────────── /chatter 群复读与插话开关（私聊） ────────────────────
chatter_cmd_private = on_message(
    rule=Rule(is_private_event) & startswith("/chatter"),
    priority=10, block=True,
)


@chatter_cmd_private.handle()
async def handle_chatter_private(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return  # 非管理员直接忽略

    text = event.get_plaintext().strip()
    args = text[len("/chatter"):].strip().split()

    from ..persona.manager import get_auto_trigger, set_auto_trigger

    # 不带参数：列出白名单各群的复读插话状态
    if not args:
        from ..group.utils import list_whitelist
        groups = list_whitelist()
        if not groups:
            await chatter_cmd_private.finish("白名单为空。")
        lines = [
            f"- {g}　复读插话: {'开' if get_auto_trigger(g) else '关'}"
            for g in groups
        ]
        await chatter_cmd_private.finish("各群复读与插话状态:\n" + "\n".join(lines))

    if not args[0].isdigit():
        await chatter_cmd_private.finish(
            "用法: /chatter <群号> [on|off]，或不带参数查看各群状态"
        )

    target_gid = args[0]
    arg = args[1].lower() if len(args) > 1 else None
    if arg in ("on", "开"):
        enable = True
    elif arg in ("off", "关"):
        enable = False
    elif arg is None:
        enable = not get_auto_trigger(target_gid)
    else:
        await chatter_cmd_private.finish("用法: /chatter <群号> [on|off]")

    set_auto_trigger(target_gid, enable)
    state = "已开启：本群会按概率复读和插话。" if enable else "已关闭：本群不再主动复读或插话。"
    await chatter_cmd_private.finish(f"群 {target_gid} 的复读与插话{state}")


# ──────────────────── /塔罗 塔罗占卜（私聊） ────────────────────
tarot_cmd = on_message(rule=Rule(is_private_event) & startswith("/塔罗"), priority=10, block=True)


@tarot_cmd.handle()
async def handle_tarot_private(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return  # 非管理员忽略，落回主聊天 handler

    text = event.get_plaintext().strip()
    if not text.startswith("/塔罗"):
        return

    from ..local_tools.tarot import (
        parse_tarot_args, draw_cards, format_cards, interpret_cards,
    )

    num, question = parse_tarot_args(text)
    if num == 0:
        await tarot_cmd.finish("牌数需要是 1-5 之间的数字，例如：/塔罗 3 我明天能升职吗")

    cards = draw_cards(num)

    # 第一步：先发牌面
    bot = get_bot()
    cards_text = format_cards(cards) + "\n我来帮你解读一下~"
    await send_chunked(bot, event, chunk_text(cards_text))

    # 第二步：Admin 人格 + 运行时上下文 → LLM 解读（与主 handler 同拼法）
    system_prompt = load_admin_prompt() or _FALLBACK_PROMPT
    cfg = _load_config(user_id)
    # 一次性请求，无跨轮缓存诉求，时间行直接拼在 system 里
    system_prompt += build_runtime_context(chat_type="private") + "\n" + build_time_context(
        cfg.get("last_message_at", "")
    )

    asker = getattr(event, "sender", None)
    asker = getattr(asker, "nickname", None) or str(event.user_id)
    try:
        reply = await interpret_cards(
            system_prompt, cards, question, asker, history=load_history(user_id),
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"塔罗解读 API 错误: {e.response.status_code} {e.response.text}")
        await tarot_cmd.finish(
            f"牌已经抽好了，但解读服务暂时不可用（{e.response.status_code}），稍后再试~"
        )
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"塔罗解读异常: {e}")
        await tarot_cmd.finish("牌已经抽好了，但解读时出了点问题，稍后再试~")

    if reply:
        await send_chunked(bot, event, chunk_text(reply), reply_first=False)
        # 写回历史：记录这次占卜，否则下次完全不记得抽过什么牌、问过什么
        append_message(user_id, {"role": "user", "content": text})
        append_message(user_id, {"role": "assistant", "content": f"{cards_text}\n\n{reply}"})
    else:
        await tarot_cmd.finish("嗯……我好像走神了，让我再看一眼牌面。")


# ──────────────────── /help ────────────────────
PRIVATE_HELP = """/reset — 清除对话历史
/compact — 压缩对话历史（自动提取记忆）
/塔罗 [牌数1-5] [问题] — 塔罗占卜（抽牌并解读）
/主动对话 enable|disable — 切换私聊主动对话（Bot 空闲时主动发消息）
/主动对话 <群号> enable|disable — 切换指定群的主动对话
/listen <群号> [on|off] — 切换指定群的全量上下文模式
/chatter <群号> [on|off] — 开关指定群的复读与插话（不带群号查看各群状态）
/白名单 list|add <群号>|delete <群号> — 管理群白名单
/help — 显示本帮助"""

help_cmd = on_fullmatch("/help", rule=is_private_event, priority=10, block=True)


@help_cmd.handle()
async def handle_help(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return
    await help_cmd.finish(PRIVATE_HELP)


# ──────────────────── /白名单 群白名单管理（私聊） ────────────────────
whitelist_cmd = on_message(rule=Rule(is_private_event) & startswith("/白名单"), priority=10, block=True)


@whitelist_cmd.handle()
async def handle_whitelist_private(event: PrivateMessageEvent):
    user_id = str(event.user_id)
    if not ADMIN_NUMBER or user_id != str(ADMIN_NUMBER):
        return  # 私聊本身仅 Admin 可对话，非管理员直接忽略

    text = event.get_plaintext().strip()
    if not text.startswith("/白名单"):
        return

    from ..group.utils import handle_whitelist_command
    await whitelist_cmd.finish(handle_whitelist_command(text))


# ──────────────────── 主对话处理 ────────────────────
chat = on_message(rule=is_private_event, priority=99, block=False)


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

    # 进 Agentic Loop 之前先推后心跳计时器，避免跑工具调用期间心跳插进来
    # 与主流程并发写同一份历史（与群聊 handler 保持一致）。
    reset_idle_timer("private")

    # 检查是否引用了消息
    quoted_text = ""
    quoted_image_urls: list[str] = []
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
            # 获取引用消息中的图片（仅当模型支持多模态，deepseek 等不支持）
            if SUPPORTS_VISION:
                quoted_image_urls = await fetch_quoted_image_urls(bot, reply_id)
        except Exception as e:
            logger.warning(f"获取引用消息失败: {e}")

    # 上次对话时间必须在 append_message 之前读取，否则拿到的是本次时间
    last = _load_config(user_id).get("last_message_at", "")

    # 记录用户消息（带引用内容，纯文本）
    if quoted_text:
        content = f'(引用了一条消息: "{quoted_text}"): {user_input}'
    else:
        content = user_input
    user_msg = {"role": "user", "content": content}
    append_message(user_id, user_msg)

    # 如果引用的消息包含图片，构建多模态 content（仅用于 LLM 请求，不存历史）
    if quoted_image_urls:
        multimodal_content: list[dict] = [{"type": "text", "text": content}]
        for img_url in quoted_image_urls:
            multimodal_content.append({
                "type": "image_url",
                "image_url": {"url": img_url},
            })
        llm_user_msg = {"role": "user", "content": multimodal_content}
    else:
        llm_user_msg = user_msg

    # 加载历史 → 压缩（如需要） → 截断
    history = load_history(user_id)

    # Compaction: 如果历史 token 过多，压缩旧消息为摘要 + 提取记忆
    if should_compact(history):
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
    prompt += build_runtime_context(chat_type="private")

    # 组装 messages：最后一条用 LLM 版本（可能含图片），其余用纯文本
    messages = [{"role": "system", "content": prompt}] + trimmed[:-1]
    if trimmed:
        if quoted_image_urls and trimmed[-1].get("content") == content:
            messages.append(llm_user_msg)
        else:
            messages.append(trimmed[-1])
    elif quoted_image_urls:
        messages.append(llm_user_msg)

    # 动态时间行每轮都变，作为独立 system 消息放在 messages 最末尾。
    # 绝不能拼进 system prompt——那会让 prefix cache 在 system 处断掉，
    # 整个对话历史每轮缓存全部 miss。
    messages.append({"role": "system", "content": build_time_context(last)})

    # DEBUG: 打印组装好的完整 prompt
    logger.debug("=== 私聊 Prompt 开始 ===")
    for i, m in enumerate(messages):
        logger.debug(f"[{i}] {m['role']}:\n{m.get('content', '')}")
    logger.debug(f"=== 私聊 Prompt 结束 (共 {len(messages)} 条) ===")

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
        from ..llm import call_llm
        for round_idx in range(MAX_TOOL_ROUNDS):
            data = await call_llm(messages, tools=openai_tools or None, source="chat")
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
                    reset_idle_timer("private")
                return

            # 同轮"说话 + 调工具"：文字先发给用户，再执行工具调用
            # （原实现会丢弃与 tool_calls 同现的 content）
            prelude = (assistant_msg.get("content") or "").strip()
            if prelude:
                append_message(user_id, {"role": "assistant", "content": prelude})
                bot = get_bot()
                await send_chunked(bot, event, chunk_text(prelude), reply_first=False)

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

                from ..tool_log import log_tool_call
                log_tool_call("chat", fn_name, fn_args, tool_result, user_id=user_id)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

        # 超过最大轮次 — 不带工具再调一次，让 LLM 根据已有信息总结回复
        messages.append({"role": "user", "content": "你已经无法再调用工具了，请根据上面已经获取到的信息，直接回答用户的问题。"})
        try:
            final_data = await call_llm(messages, source="chat")
            reply = (final_data["choices"][0]["message"].get("content") or "").strip()
        except Exception:
            reply = "（工具调用轮次已达上限，请重新提问）"
        if reply:
            append_message(user_id, {"role": "assistant", "content": reply})
            bot = get_bot()
            chunks = chunk_text(reply)
            await send_chunked(bot, event, chunks, reply_first=False)
        reset_idle_timer("private")
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenAI API 错误: {e.response.status_code} {e.response.text}")
        await chat.finish(f"API 请求失败 ({e.response.status_code})")
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"插件异常: {e}")
        await chat.finish("请求出错，请稍后再试。")
