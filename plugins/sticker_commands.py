"""群内 /表情列表，以及仅 Admin 私聊可用的 /自动表情。"""

import asyncio
import base64
import json

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.rule import Rule, startswith
from nonebot.log import logger

from . import stickers as library
from .group.utils import in_whitelist

MAX_BATCH_IMAGES = 20
MAX_BATCH_BYTES = 8 * 1024 * 1024


def list_batches(gid: str, user_id: int, nickname: str):
    """流式建立有界请求；图片字节从不进入聊天历史或工具管理日志。"""
    entries = library.items(gid)
    tags = sorted({tag for item in entries for tag in item["tags"]})
    batch = []
    previous_tag = None
    for tag in tags:
        for item in entries:
            if tag not in item["tags"]:
                continue
            _, content = library.read_image(gid, item["id"])
            image = {"type": "image", "data": {"file": "base64://" + base64.b64encode(content).decode("ascii")}}
            def node(first):
                return {"type": "node", "data": {"user_id": user_id, "nickname": nickname,
                    "content": ([{"type": "text", "data": {"text": f"[{tag}]：\n"}}] if first else []) + [image]}}
            candidate = node(not batch or previous_tag != tag)
            size = len(json.dumps({"action": "send_group_forward_msg", "params": {
                "group_id": int(gid), "messages": batch + [candidate]}}).encode("utf-8"))
            if batch and (len(batch) >= MAX_BATCH_IMAGES or size > MAX_BATCH_BYTES):
                yield batch
                batch = []
                candidate = node(True)
            batch.append(candidate)
            previous_tag = tag
    if batch:
        yield batch


async def send_list(bot, gid: str) -> tuple[str, int]:
    from .chunker import _session_locks, _get_self_nickname, _delivery_error, _remember_delivery
    completed = 0
    nickname = await _get_self_nickname(bot)
    async with _session_locks[f"g:{gid}"]:
        try:
            for batch in list_batches(gid, int(bot.self_id), nickname):
                try:
                    response = await bot.call_api("send_group_forward_msg", group_id=int(gid), messages=batch)
                except Exception as exc:
                    return _delivery_error(exc).status, completed
                _remember_delivery("group", int(gid), response, f"[表情列表，第 {completed + 1} 批，{len(batch)} 张]", nickname)
                completed += 1
                await asyncio.sleep(0.5)
        except (OSError, library.StickerError) as exc:
            logger.warning(f"表情列表读取失败: {type(exc).__name__}")
            return "failed", completed
    return "sent", completed


list_command = on_message(rule=Rule(lambda event: isinstance(event, GroupMessageEvent)) & startswith("/表情列表"), priority=8, block=True)


@list_command.handle()
async def handle_list(bot: Bot, event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    gid = str(event.group_id)
    if not library.items(gid):
        await list_command.finish("本群还没有表情包。引用一张图片并 @我说“把这张图片存成开心”即可收藏。")
    status, completed = await send_list(bot, gid)
    if status != "sent":
        await list_command.finish(f"表情列表已完成 {completed} 批，后续发送{'状态未知' if status == 'unknown' else '失败'}，已停止，请稍后重试。")


auto_command = on_message(rule=Rule(lambda event: isinstance(event, PrivateMessageEvent)) & startswith("/自动表情"), priority=8, block=True)


@auto_command.handle()
async def handle_auto(event: PrivateMessageEvent):
    admin = str(getattr(get_driver().config, "admin_number", ""))
    if not admin or str(event.user_id) != admin:
        await auto_command.finish("仅 Bot 管理员可以设置自动表情。")
    args = event.get_plaintext().strip().split()
    if len(args) != 3 or not args[1].isascii() or not args[1].isdigit() or args[2].lower() not in ("on", "off"):
        await auto_command.finish("用法：/自动表情 <群号> on|off")
    library.set_enabled(args[1], args[2].lower() == "on")
    await auto_command.finish(f"群 {args[1]} 自动表情已{'开启' if args[2].lower() == 'on' else '关闭'}。")
