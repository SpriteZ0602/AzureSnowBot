"""
Skill 相关指令
────────────
/skill — 列出所有技能
/skill <name> — 查看技能详情
/skill reload — 重新扫描技能目录
"""

from nonebot import on_fullmatch, on_startswith
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageSegment

from ..group.utils import in_whitelist, is_at_bot
from . import manager as sm

# ──────────────────── /skill ────────────────────
skill_cmd = on_startswith("/skill", priority=8, block=True)


@skill_cmd.handle()
async def handle_skill(event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    if not is_at_bot(event):
        return

    raw = event.get_plaintext().strip()
    # 去掉 "/skill" 前缀
    arg = raw[len("/skill"):].strip()

    # /skill (无参数) — 列出所有
    if not arg:
        names = sm.list_skill_names()
        if not names:
            await skill_cmd.finish(
                MessageSegment.reply(event.message_id)
                + "当前没有已加载的技能。\n在 data/skills/<name>/SKILL.md 中创建技能。"
            )
        lines = ["📚 已加载技能：\n"]
        for name in names:
            meta = sm.get_skill_meta(name)
            if meta:
                desc = meta.description[:80]
                ref_info = f" [{len(meta.references)} 参考文档]" if meta.references else ""
                lines.append(f"• {name} — {desc}{ref_info}")
        lines.append(f"\n共 {len(names)} 个技能。使用 /skill <名称> 查看详情。")
        await skill_cmd.finish(
            MessageSegment.reply(event.message_id) + "\n".join(lines)
        )

    # /skill reload — 重新扫描
    if arg == "reload":
        sm.scan_skills()
        names = sm.list_skill_names()
        await skill_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"已重新扫描技能目录，共加载 {len(names)} 个技能。"
        )

    # /skill <name> — 查看详情
    meta = sm.get_skill_meta(arg)
    if not meta:
        await skill_cmd.finish(
            MessageSegment.reply(event.message_id)
            + f"技能 '{arg}' 不存在。使用 /skill 查看所有可用技能。"
        )

    lines = [
        f"📖 技能: {meta.name}",
        f"描述: {meta.description}",
        f"路径: {meta.path}",
    ]
    if meta.references:
        lines.append(f"参考文档: {', '.join(meta.references)}")

    # 显示 SKILL.md 正文摘要（前 500 字）
    body = sm.load_skill_body(meta.name)
    if body:
        preview = body[:500]
        if len(body) > 500:
            preview += "..."
        lines.append(f"\n--- 正文预览 ---\n{preview}")

    await skill_cmd.finish(
        MessageSegment.reply(event.message_id) + "\n".join(lines)
    )
