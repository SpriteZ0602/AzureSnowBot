"""
群聊指令
──────
/reset, /compact, /取名, /help 等群聊指令处理。
"""

import re
import httpx
from nonebot import on_fullmatch, on_message
from nonebot.rule import startswith, Rule
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.exception import FinishedException
from nonebot.log import logger

from ..persona.manager import clear_history as pm_clear_history
from ..persona.manager import (
    get_active_persona, _session_path as persona_session_path,
    get_listen_all, set_listen_all,
    get_group_proactive, set_group_proactive,
    get_auto_trigger, set_auto_trigger,
    load_persona_prompt, get_group_config, group_memory_path,
)
from ..runtime_context import build_runtime_context
from .utils import in_whitelist, is_at_bot, is_group_event

# ──────────────────── /reset ────────────────────
group_reset = on_fullmatch("/reset", rule=is_group_event, priority=10, block=True)


@group_reset.handle()
async def handle_group_reset(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    group_id = str(event.group_id)
    pm_clear_history(group_id)
    await group_reset.finish("本群对话历史已清除。")


# ──────────────────── /compact ────────────────────
group_compact = on_fullmatch("/compact", rule=is_group_event, priority=10, block=True)


@group_compact.handle()
async def handle_group_compact(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return
    from ..chat.compaction import compact_history

    group_id = str(event.group_id)
    persona = get_active_persona(group_id)
    session_path = persona_session_path(group_id, persona)
    memory_path = group_memory_path(group_id)  # data/groups/<群号>/MEMORY.md
    compacted = await compact_history(group_id, session_path, memory_path)
    if compacted:
        await group_compact.finish("本群对话历史已压缩。")
    else:
        await group_compact.finish("当前历史不需要压缩。")


# ──────────────────── /listen 全量监听切换 ────────────────────
listen_cmd = on_message(rule=Rule(is_group_event) & startswith("/listen") & Rule(is_at_bot), priority=8, block=True)


@listen_cmd.handle()
async def handle_listen(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/listen"):
        return
    if not is_at_bot(event):
        return

    # 仅 Bot 管理员可以切换
    from nonebot import get_driver
    admin_number = str(getattr(get_driver().config, "admin_number", ""))
    if not admin_number or str(event.user_id) != admin_number:
        await listen_cmd.finish(MessageSegment.reply(event.message_id) + "仅 Bot 管理员可以切换监听模式。")

    group_id = str(event.group_id)
    arg = text[len("/listen"):].strip().lower()
    if arg in ("on", "开"):
        enable = True
    elif arg in ("off", "关"):
        enable = False
    else:
        enable = not get_listen_all(group_id)

    set_listen_all(group_id, enable)
    state = "已开启：回复 @Bot 时加载全量群聊上下文。" if enable else "已关闭：仅记录 @Bot 消息。"
    await listen_cmd.finish(MessageSegment.reply(event.message_id) + f"全量上下文模式{state}")


# ──────────────────── /白名单 群白名单管理 ────────────────────
# 注意：不做 in_whitelist 检查 —— 否则管理员无法从新群把它加进白名单。
# 仅管理员可触发，普通群成员（含非白名单群）调用会被拒绝。
whitelist_cmd = on_message(rule=Rule(is_group_event) & startswith("/白名单") & Rule(is_at_bot), priority=8, block=True)


@whitelist_cmd.handle()
async def handle_whitelist(event: GroupMessageEvent):
    text = event.get_plaintext().strip()
    if not text.startswith("/白名单"):
        return
    if not is_at_bot(event):
        return

    # 仅 Bot 管理员
    from nonebot import get_driver
    admin_number = str(getattr(get_driver().config, "admin_number", ""))
    if not admin_number or str(event.user_id) != admin_number:
        await whitelist_cmd.finish(MessageSegment.reply(event.message_id) + "仅 Bot 管理员可以管理白名单。")

    from .utils import handle_whitelist_command
    reply = handle_whitelist_command(text)
    await whitelist_cmd.finish(MessageSegment.reply(event.message_id) + reply)


# ──────────────────── /主动对话 群聊主动发言开关 ────────────────────
group_proactive_cmd = on_message(rule=Rule(is_group_event) & startswith("/主动对话") & Rule(is_at_bot), priority=8, block=True)


@group_proactive_cmd.handle()
async def handle_group_proactive(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/主动对话"):
        return
    if not is_at_bot(event):
        return

    # 仅 Bot 管理员可以切换
    from nonebot import get_driver
    admin_number = str(getattr(get_driver().config, "admin_number", ""))
    if not admin_number or str(event.user_id) != admin_number:
        await group_proactive_cmd.finish(MessageSegment.reply(event.message_id) + "仅 Bot 管理员可以切换主动对话。")

    # 解析参数: /主动对话 [群号] enable|disable（省略群号时作用于当前群）
    args = text[len("/主动对话"):].strip().split()
    target_gid = str(event.group_id)
    action: str | None = None
    if args:
        if args[0].isdigit():
            target_gid = args[0]
            action = args[1].lower() if len(args) > 1 else None
        else:
            action = args[0].lower()

    if action in ("enable", "on", "开"):
        enable = True
    elif action in ("disable", "off", "关"):
        enable = False
    elif action is None:
        enable = not get_group_proactive(target_gid)
    else:
        await group_proactive_cmd.finish(
            MessageSegment.reply(event.message_id) + "用法: /主动对话 [群号] enable|disable"
        )

    set_group_proactive(target_gid, enable)

    # 同步计时器状态
    from ..proactive import reset_idle_timer, cancel_idle_timer
    if enable:
        reset_idle_timer(f"group:{target_gid}")
    else:
        cancel_idle_timer(f"group:{target_gid}")

    state = "已开启：bot 会定时主动在群里发言。" if enable else "已关闭。"
    await group_proactive_cmd.finish(
        MessageSegment.reply(event.message_id) + f"群 {target_gid} 的主动对话{state}"
    )


# ──────────────────── /chatter 复读与插话 ────────────────────
auto_trigger_cmd = on_message(
    rule=Rule(is_group_event) & startswith("/chatter") & Rule(is_at_bot),
    priority=8, block=True,
)


@auto_trigger_cmd.handle()
async def handle_auto_trigger(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/chatter"):
        return
    if not is_at_bot(event):
        return

    from nonebot import get_driver
    admin_number = str(getattr(get_driver().config, "admin_number", ""))
    if not admin_number or str(event.user_id) != admin_number:
        await auto_trigger_cmd.finish(
            MessageSegment.reply(event.message_id) + "仅 Bot 管理员可以开关复读和插话。"
        )

    group_id = str(event.group_id)
    arg = text[len("/chatter"):].strip().lower()
    if arg in ("on", "开"):
        enable = True
    elif arg in ("off", "关"):
        enable = False
    elif arg == "":
        enable = not get_auto_trigger(group_id)
    else:
        await auto_trigger_cmd.finish(
            MessageSegment.reply(event.message_id) + "用法: /chatter [on|off]"
        )

    set_auto_trigger(group_id, enable)
    if enable:
        msg = "已开启：本群会按概率复读和插话。"
    else:
        msg = "已关闭：本群不再主动复读或插话。"
    await auto_trigger_cmd.finish(MessageSegment.reply(event.message_id) + msg)


# ──────────────────── /取名 ────────────────────
nickname_cmd = on_message(rule=Rule(is_group_event) & startswith("/取名") & Rule(is_at_bot), priority=9, block=True)

_NICKNAME_TASK = (
    "你是一个取名专家。根据下面的群聊记录，给这个人起 2-3 个有趣的群昵称。"
    "分析他的用词习惯、话题偏好、说话风格，说明每个昵称的由来。"
    "昵称可以有趣，可以冒犯。直接输出结果，不要客套，一定不要输出markdown格式。"
)


@nickname_cmd.handle()
async def handle_nickname(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/取名"):
        return

    args = text[len("/取名"):].strip()

    # 解析目标 QQ 号（从 @ 消息段提取）
    target_uid: str = ""
    for seg in event.message:
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq and qq != str(event.self_id):
                target_uid = qq
                break

    # 如果没 @ 人，取发送者自己
    if not target_uid:
        target_uid = str(event.user_id)

    # 解析 limit（从文本参数中取数字）
    limit = 200
    num_match = re.search(r"\d+", args)
    if num_match:
        limit = max(10, min(500, int(num_match.group())))

    group_id = str(event.group_id)

    # 检索聊天记录
    from .chatlog import load_chatlog
    entries = load_chatlog(group_id, hours=168, user_id=target_uid, limit=limit)

    if not entries or len(entries) < 3:
        await nickname_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"这个人最近说话太少了（只找到 {len(entries)} 条），取不了名。"
        )

    # 格式化聊天记录
    from datetime import datetime as _dt
    lines: list[str] = []
    target_name = entries[0].get("name", target_uid)
    for e in entries:
        ts = _dt.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
        lines.append(f"[{ts}] {e.get('name', '?')}: {e.get('text', '')}")
    chat_data = f"目标用户: {target_name}\n\n" + "\n".join(lines)

    # 调用 Sub-Agent
    from ..local_tools.tools import run_sub_agent
    result = await run_sub_agent(
        task=_NICKNAME_TASK,
        data=chat_data,
        _context={
            "_chat_type": "group",
            "_target_id": group_id,
            "_user_id": str(event.user_id),
            "_sender_name": event.sender.nickname or str(event.user_id),
        },
    )

    from ..chunker import send_chunked, chunk_text
    from nonebot import get_bot
    bot = get_bot()
    chunks = chunk_text(result)
    await send_chunked(bot, event, chunks)


# ──────────────────── /塔罗 塔罗占卜 ────────────────────
tarot_cmd = on_message(rule=Rule(is_group_event) & startswith("/塔罗") & Rule(is_at_bot), priority=9, block=True)


@tarot_cmd.handle()
async def handle_tarot(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    text = event.get_plaintext().strip()
    if not text.startswith("/塔罗"):
        return

    from ..local_tools.tarot import (
        parse_tarot_args, draw_cards, format_cards, interpret_cards,
    )

    num, question = parse_tarot_args(text)
    if num == 0:
        await tarot_cmd.finish(
            MessageSegment.reply(event.message_id)
            + "牌数需要是 1-5 之间的数字，例如：/塔罗 3 我明天能升职吗"
        )

    cards = draw_cards(num)

    # 第一步：先发牌面
    from nonebot import get_bot
    from ..chunker import chunk_text, send_chunked
    bot = get_bot()
    cards_text = format_cards(cards) + "\n我来帮你解读一下~"
    await send_chunked(bot, event, chunk_text(cards_text))

    # 第二步：人格 + 运行时上下文 → LLM 解读（带上最近的群聊上下文）
    group_id = str(event.group_id)
    active_persona = get_active_persona(group_id)
    system_prompt = load_persona_prompt(active_persona, group_id)
    if not system_prompt:
        system_prompt = "你是一个有用的助手，请用中文回答用户的问题。"
    cfg = get_group_config(group_id)
    system_prompt += build_runtime_context(
        chat_type="group", last_message_at=cfg.get("last_message_at", "")
    )

    # 加载本群 + 当前人格的历史，让解读能结合上下文
    from ..persona.manager import load_history, append_message
    history = load_history(group_id, active_persona)

    asker = event.sender.nickname or str(event.user_id)
    try:
        reply = await interpret_cards(
            system_prompt, cards, question, asker, history=history,
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"塔罗解读 API 错误: {e.response.status_code} {e.response.text}")
        await tarot_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"牌已经抽好了，但解读服务暂时不可用（{e.response.status_code}），稍后再试~"
        )
    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"塔罗解读异常: {e}")
        await tarot_cmd.finish(
            MessageSegment.reply(event.message_id) + "牌已经抽好了，但解读时出了点问题，稍后再试~"
        )

    if reply:
        await send_chunked(bot, event, chunk_text(reply), reply_first=False)
        # 写回历史：记录这次占卜，否则下次完全不记得抽过什么牌、问过什么
        append_message(group_id, {"role": "user", "content": f"[{asker}]: {text}"}, active_persona)
        append_message(group_id, {"role": "assistant", "content": f"{cards_text}\n\n{reply}"}, active_persona)
    else:
        await tarot_cmd.finish(
            MessageSegment.reply(event.message_id) + "嗯……我好像走神了，让我再看一眼牌面。"
        )


# ──────────────────── /help ────────────────────
HELP_TEXT = """/persona — 列出所有人格
/persona <名称> — 切换人格
/persona info — 查看当前人格详情
/persona reset — 清除当前人格的对话历史
/persona create <名称> <prompt> — 创建本群人格
/persona delete <名称> — 删除本群人格
/skill — 列出所有技能
/skill <名称> — 查看技能详情
/skill reload — 重新扫描技能
/compact — 压缩对话历史
/取名 @某人 [条数] — 根据聊天记录起群昵称
/塔罗 [牌数1-5] [问题] — 塔罗占卜（抽牌并解读）
/reset — 清除当前对话历史
/listen [on|off] — 切换全量上下文模式：开启后 @Bot 回复加载全量群聊消息（仅管理员）
/chatter [on|off] — 开关本群复读和热闹插话（仅管理员，默认关）
/主动对话 [群号] enable|disable — 切换群的主动对话（仅管理员）
/白名单 list|add <群号>|delete <群号> — 管理群白名单（仅管理员）
/help — 显示本帮助"""

help_cmd = on_fullmatch("/help", rule=is_group_event, priority=5, block=True)


@help_cmd.handle()
async def handle_help(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    # 群聊 /help 只展示命令列表：工具/技能由模型在对话中按需调用，不展示（太长）
    await help_cmd.finish(
        MessageSegment.reply(event.message_id) + HELP_TEXT
    )
