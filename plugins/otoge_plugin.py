"""NoneBot 指令适配层，成绩和曲库逻辑由 otoge 服务提供。"""

import asyncio

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.log import logger

from .chunker import send_chunked
from .group.utils import in_whitelist
from .otoge.prober import QQ_PATTERN, ProberError, query_best_scores, song_catalog
from .otoge.render import render_best_scores


COMMANDS = {"/mai": "mai", "/maimai": "mai", "/舞萌": "mai", "/chu": "chu", "/chuni": "chu", "/中二": "chu"}
SOURCE_ARGS = {"水鱼": "divingfish", "divingfish": "divingfish", "落雪": "lxns", "lxns": "lxns"}
HELP = """国服音游查分 · 舞萌 /mai，中二 /chu
/mai b50 [水鱼|落雪] [@玩家|QQ号] [文字] — 默认查自己的水鱼成绩，优先发成绩图
/mai b50 水鱼 用户名 <水鱼用户名> [文字] — 按水鱼用户名查
/mai 查歌 <曲名|别名|ID> — 查国服曲目和定数
中二把 /mai 换成 /chu。成绩需先在对应查分器同步，并允许公开查询。"""


def is_otoge_command(event: MessageEvent) -> bool:
    head = event.get_plaintext().strip().split(maxsplit=1)
    if not head or head[0].lower() not in COMMANDS or event.user_id == event.self_id:
        return False
    if isinstance(event, GroupMessageEvent):
        return in_whitelist(event.group_id)
    admin = str(getattr(get_driver().config, "admin_number", ""))
    return isinstance(event, PrivateMessageEvent) and str(event.user_id) == admin


def _score_arguments(text: str, sender: str, mentions: list[str]) -> tuple[str, str, str, bool]:
    args = text.split()
    source = SOURCE_ARGS[args.pop(0).lower()] if args and args[0].lower() in SOURCE_ARGS else "divingfish"
    as_text = bool(args and args[-1].lower() in ("文字", "text"))
    if as_text:
        args.pop()
    if len(mentions) > 1 or (mentions and args):
        raise ProberError("一次只查一个玩家，请指定一个 @、QQ 号或水鱼用户名。")
    if len(args) > 1 and args[0] == "用户名":
        return source, "", " ".join(args[1:]), as_text
    qq = mentions[0] if mentions else args[0] if len(args) == 1 else sender if not args else ""
    if not QQ_PATTERN.fullmatch(qq):
        raise ProberError("请使用 /mai b50 [水鱼|落雪] [@玩家|QQ号] [文字]；查中二用 /chu。")
    return source, qq, "", as_text


def _split_head(text: str) -> tuple[str, str]:
    parts = text.split(maxsplit=1)
    return tuple(parts) if len(parts) == 2 else (parts[0] if parts else "", "")


otoge_cmd = on_message(rule=is_otoge_command, priority=8, block=True)


@otoge_cmd.handle()
async def handle_otoge(bot: Bot, event: MessageEvent):
    command, text = _split_head(event.get_plaintext())
    game = COMMANDS[command.lower()]
    action, args = _split_head(text)
    try:
        if action.lower() in ("查歌", "song"):
            reply = await song_catalog.search(game, args)
            await otoge_cmd.finish(MessageSegment.reply(event.message_id) + MessageSegment.text(reply))
        if action.lower() not in ("b50", "b30", "成绩"):
            await otoge_cmd.finish(HELP)
        mentions = list(dict.fromkeys(str(segment.data.get("qq")) for segment in event.message
                                     if segment.type == "at" and str(segment.data.get("qq")) != str(event.self_id)))
        source, qq, username, as_text = _score_arguments(args, str(event.user_id), mentions)
        config = get_driver().config
        result = await query_best_scores(game, source, qq=qq, username=username,
                                         lxns_token=str(getattr(config, "otoge_lxns_token", "") or ""))
    except ProberError as exc:
        await otoge_cmd.finish(str(exc))
    if not as_text:
        try:
            image = await asyncio.to_thread(render_best_scores, result, str(getattr(config, "otoge_font_path", "") or ""))
        except (OSError, ValueError) as exc:
            logger.warning(f"生成成绩图失败: {type(exc).__name__}")
            image = None
        if image:
            try:
                await otoge_cmd.finish(MessageSegment.reply(event.message_id) + MessageSegment.image(image))
            except ActionFailed:
                logger.warning("NapCat 拒绝发送成绩图，改发文字。")
    lines = result.text().splitlines()
    await send_chunked(bot, event, ["\n".join(lines[i:i + 9]) for i in range(0, len(lines), 9)])
