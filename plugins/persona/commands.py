"""
/persona 指令插件
────────────────
群聊中通过 /persona 管理人格切换。

用法（需要 @Bot）：
  /persona                         → 列出所有人格 + 当前激活
  /persona list                    → 同上
  /persona <名称>                  → 切换到指定人格
  /persona reset                   → 清除当前人格的对话历史
  /persona info                    → 查看当前人格的 prompt 摘要
  /persona create <名称> <prompt>  → 创建本群私有人格
  /persona delete <名称>            → 删除本群私有人格
"""

from nonebot import on_startswith
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment
from nonebot.log import logger

from . import manager as pm
from ..group.utils import in_whitelist, is_at_bot


# ──────────────────── /persona 指令 ────────────────────
persona_cmd = on_startswith("/persona", priority=5, block=True)


@persona_cmd.handle()
async def handle_persona(event: GroupMessageEvent):
    # 权限检查
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    group_id = str(event.group_id)
    raw_text = event.get_plaintext().strip()

    # 解析子命令：去掉 "/persona" 前缀后取参数
    args = raw_text[len("/persona"):].strip()

    # ─── /persona 或 /persona list ───
    if not args or args == "list":
        await _handle_list(group_id, event)
        return

    # ─── /persona reset ───
    if args == "reset":
        await _handle_reset(group_id, event)
        return

    # ─── /persona info ───
    if args == "info":
        await _handle_info(group_id, event)
        return

    # ─── /persona create <名称> <prompt> ───
    if args.startswith("create "):
        await _handle_create(group_id, args[len("create "):], event)
        return

    # ─── /persona delete <名称> ───
    if args.startswith("delete "):
        await _handle_delete(group_id, args[len("delete "):].strip(), event)
        return

    # ─── /persona <名称> → 切换 ───
    await _handle_switch(group_id, args, event)


async def _handle_list(group_id: str, event: GroupMessageEvent):
    """列出所有人格（通用 + 群私有）"""
    global_names = pm.list_global_personas()
    group_names = pm.list_group_personas(group_id)

    if not global_names and not group_names:
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + "还没有任何人格文件。"
        )
        return

    active = pm.get_active_persona(group_id)
    lines = []

    if global_names:
        lines.append("〜 通用人格：")
        for name in global_names:
            marker = " ← 当前" if name == active else ""
            lines.append(f"  • {name}{marker}")

    if group_names:
        lines.append("〜 本群人格：")
        for name in group_names:
            marker = " ← 当前" if name == active else ""
            lines.append(f"  • {name}{marker}")

    lines.append("")
    lines.append("/persona <名称>  切换")
    lines.append("/persona create <名称> <prompt>  创建本群人格")

    await persona_cmd.finish(
        MessageSegment.reply(event.message_id) + "\n".join(lines)
    )


async def _handle_reset(group_id: str, event: GroupMessageEvent):
    """清除当前人格的对话历史"""
    active = pm.get_active_persona(group_id)
    pm.clear_history(group_id, active)
    await persona_cmd.finish(
        MessageSegment.reply(event.message_id)
        + f"已清除人格 [{active}] 的对话历史。"
    )


async def _handle_info(group_id: str, event: GroupMessageEvent):
    """查看当前人格的 prompt 摘要"""
    active = pm.get_active_persona(group_id)
    prompt = pm.load_persona_prompt(active, group_id)
    scope = "本群私有" if pm.is_group_persona(active, group_id) else "通用"
    preview = prompt[:200] + ("..." if len(prompt) > 200 else "")
    await persona_cmd.finish(
        MessageSegment.reply(event.message_id)
        + f"当前人格：{active}（{scope}）\n\nPrompt 预览：\n{preview}"
    )


async def _handle_switch(group_id: str, target: str, event: GroupMessageEvent):
    """切换人格"""
    if not pm.persona_exists(target, group_id):
        available = ", ".join(pm.list_personas(group_id)) or "（无）"
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"人格 [{target}] 不存在。\n可用人格：{available}"
        )
        return

    active = pm.get_active_persona(group_id)
    if target == active:
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"当前已经是 [{target}] 人格了。"
        )
        return

    pm.set_active_persona(group_id, target)
    logger.info(f"群 {group_id} 人格切换: {active} → {target}")

    await persona_cmd.finish(
        MessageSegment.reply(event.message_id)
        + f"人格已切换：{active} → {target}\n（对话上下文已切换，旧人格的历史保留）"
    )


async def _handle_create(group_id: str, raw_args: str, event: GroupMessageEvent):
    """创建群私有人格"""
    parts = raw_args.strip().split(None, 1)
    if len(parts) < 2:
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + "用法：/persona create <名称> <prompt 内容>"
        )
        return

    name, prompt = parts[0], parts[1]

    # 名称合法性检查
    if not all(c.isalnum() or c in "-_" or '\u4e00' <= c <= '\u9fff' for c in name):
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + "人格名称只能包含字母、数字、中文、- 和 _"
        )
        return

    pm.create_group_persona(group_id, name, prompt)
    logger.info(f"群 {group_id} 创建私有人格: {name}")

    await persona_cmd.finish(
        MessageSegment.reply(event.message_id)
        + f"本群人格 [{name}] 已创建。\n用 /persona {name} 切换过去。"
    )


async def _handle_delete(group_id: str, name: str, event: GroupMessageEvent):
    """删除群私有人格"""
    if not name:
        await persona_cmd.finish(
            MessageSegment.reply(event.message_id)
            + "用法：/persona delete <名称>"
        )
        return

    if not pm.is_group_persona(name, group_id):
        if pm.is_global_persona(name):
            await persona_cmd.finish(
                MessageSegment.reply(event.message_id)
                + f"[{name}] 是通用人格，无法在群内删除。"
            )
        else:
            await persona_cmd.finish(
                MessageSegment.reply(event.message_id)
                + f"本群不存在私有人格 [{name}]。"
            )
        return

    # 如果正在使用该人格，切回 default
    active = pm.get_active_persona(group_id)
    if active == name:
        pm.set_active_persona(group_id, pm.DEFAULT_PERSONA)

    pm.delete_group_persona(group_id, name)
    logger.info(f"群 {group_id} 删除私有人格: {name}")

    extra = "（已自动切回 default）" if active == name else ""
    await persona_cmd.finish(
        MessageSegment.reply(event.message_id)
        + f"本群人格 [{name}] 已删除。{extra}"
    )
