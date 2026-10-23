"""群内 /表情列表，以及仅 Admin 私聊可用的 /自动表情。"""

import asyncio
import base64
import json
import re
from dataclasses import dataclass

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.rule import Rule, startswith
from nonebot.log import logger

from . import stickers as library
from .group.utils import in_whitelist

MAX_BATCH_IMAGES = 20
MAX_BATCH_BYTES = 8 * 1024 * 1024
SEND_INTERVAL = 1.0
_ORDINAL = re.compile(r"^表情条目 ([0-9]+)$", re.MULTILINE)


def list_batches(gid: str, user_id: int, nickname: str, *, entries=None, missing=None):
    """流式建立有界请求；图片字节从不进入聊天历史或工具管理日志。"""
    entries = library.items(gid) if entries is None else entries
    missing = set() if missing is None else missing
    tags = sorted({tag for item in entries for tag in item["tags"]})
    batch = []
    ordinal = 0
    for tag in tags:
        for item in entries:
            if tag not in item["tags"]:
                continue
            ordinal += 1
            try:
                _, content = library.read_image(gid, item["id"])
            except (OSError, library.StickerError):
                missing.add(item["id"])
                logger.warning(f"群 {gid} 表情列表条目 {ordinal} 文件不可读，继续发送其他收藏")
                continue
            image = {"type": "image", "data": {"file": "base64://" + base64.b64encode(content).decode("ascii")}}
            candidate = {"type": "node", "data": {"user_id": user_id, "nickname": nickname,
                "content": [{"type": "text", "data": {"text": f"[{tag}]：\n表情条目 {ordinal}\n"}}, image]}}
            size = len(json.dumps({"action": "send_group_forward_msg", "params": {
                "group_id": int(gid), "messages": batch + [candidate]}}).encode("utf-8"))
            if batch and (len(batch) >= MAX_BATCH_IMAGES or size > MAX_BATCH_BYTES):
                yield batch
                batch = []
            batch.append(candidate)
    if batch:
        yield batch


@dataclass
class ListResult:
    status: str = "sent"
    batches: int = 0
    displayed: int = 0
    total: int = 0
    unique: int = 0
    missing: int = 0
    detail: str = ""


def _node_ordinal(node: dict) -> int:
    return int(_ORDINAL.search(node["data"]["content"][0]["data"]["text"]).group(1))


async def _verified_ordinals(bot, response: dict, batch: list[dict]) -> set[int] | None:
    """读取实际转发内容，不走对话解析器的四图上限；无法确认时绝不重发。"""
    from .message_parser import segments
    reference = response.get("forward_id") or response.get("res_id") or response.get("message_id")
    if not reference:
        return None
    for attempt in range(2):
        try:
            payload = await bot.call_api("get_forward_msg", message_id=str(reference))
            nodes = payload.get("messages") if isinstance(payload, dict) else None
            if not isinstance(nodes, list) or not nodes:
                raise ValueError("缺少转发节点")
            found, image_count = set(), 0
            for node in nodes:
                data = node.get("data", {}) if node.get("type") == "node" else node
                content = segments(data.get("message") or data.get("content") or [])
                count = sum(seg.get("type") == "image" for seg in content)
                image_count += count
                text = "".join(str(seg.get("data", {}).get("text", "")) for seg in content if seg.get("type") == "text")
                match = _ORDINAL.search(text)
                if count and match:
                    found.add(int(match.group(1)))
            expected = {_node_ordinal(node) for node in batch}
            if found and not found <= expected:
                raise ValueError("转发条目序号不匹配")
            if not found and image_count == len(batch):
                # 某些适配器不回传节点文字；完整图片数量仍能确认本批。
                return expected
            if len(found) != image_count:
                raise ValueError("无法唯一对应转发图片")
            return found
        except Exception as exc:
            logger.warning(f"核对表情列表失败: {type(exc).__name__}")
            if attempt == 0:
                await asyncio.sleep(SEND_INTERVAL)
    return None


async def send_list(bot, gid: str, *, entries=None) -> ListResult:
    from .chunker import _session_locks, _get_self_nickname, _delivery_error, _remember_delivery
    entries = library.items(gid) if entries is None else entries
    result = ListResult(total=sum(len(item["tags"]) for item in entries), unique=len(entries))
    missing, displayed = set(), set()
    nickname = await _get_self_nickname(bot)

    async def deliver(batch):
        if result.batches:
            await asyncio.sleep(SEND_INTERVAL)
        try:
            response = await bot.call_api("send_group_forward_msg", group_id=int(gid), messages=batch)
        except Exception as exc:
            result.status = _delivery_error(exc).status
            return None
        response = response if isinstance(response, dict) else {}
        ids = _remember_delivery("group", int(gid), response,
                                 f"[表情列表，第 {result.batches + 1} 批，{len(batch)} 项]", nickname)
        result.batches += 1
        if not ids or ids == (0,):
            result.status, result.detail = "unknown", "未收到有效 QQ 消息 ID，未自动重发"
            return None
        verified = await _verified_ordinals(bot, response, batch)
        if verified is None:
            result.status, result.detail = "unknown", "已提交转发，但无法核对完整内容，未自动重发"
            return None
        displayed.update(verified)
        result.displayed = len(displayed)
        logger.info(f"群 {gid} 表情列表第 {result.batches} 批：请求 {len(batch)} 项，确认 {len(verified)} 项，累计 {result.displayed}/{result.total}")
        return verified

    async with _session_locks[f"g:{gid}"]:
        for batch in list_batches(gid, int(bot.self_id), nickname, entries=entries, missing=missing):
            verified = await deliver(batch)
            if verified is None:
                break
            # NapCat 非 packet 路径可能吞掉部分节点。只补发已经核对为缺失的项。
            incomplete = False
            for node in batch:
                if _node_ordinal(node) in verified:
                    continue
                repaired = await deliver([node])
                if repaired != {_node_ordinal(node)}:
                    if repaired is not None:
                        result.status, result.detail = "failed", "已确认部分转发节点未送达，单项补发仍不完整"
                    incomplete = True
                    break
            if incomplete:
                break
    result.missing = len(missing)
    if result.status == "sent" and result.displayed != result.total:
        result.status, result.detail = "failed", f"{result.missing} 张收藏文件不可读，已继续发送其他可用图片"
    return result


list_command = on_message(rule=Rule(lambda event: isinstance(event, GroupMessageEvent)) & startswith("/表情列表"), priority=8, block=True)


@list_command.handle()
async def handle_list(bot: Bot, event: GroupMessageEvent):
    if not in_whitelist(event.group_id):
        return
    gid = str(event.group_id)
    entries = library.items(gid)
    if not entries:
        return await list_command.finish("本群还没有表情包。引用一张图片并 @我说“把这张图片存成开心”即可收藏。")
    total = sum(len(item["tags"]) for item in entries)
    await list_command.send(f"本群共 {len(entries)} 张表情，按标签展示共 {total} 项（多标签图片重复展示），正在分批发送。")
    result = await send_list(bot, gid, entries=entries)
    progress = f"图库 {result.unique} 张，已确认展示 {result.displayed}/{result.total} 项，共发送 {result.batches} 批。"
    if result.status == "sent":
        return await list_command.finish("表情列表发送完成：" + progress)
    await list_command.finish(f"表情列表{'状态未知' if result.status == 'unknown' else '未全部完成'}：{progress}{result.detail}；已停止。")


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
