"""Agent 理解本轮用户意图，工具校验调用来源、引用和图库约束。"""

from .manager import register_tool
from .. import stickers as library


@register_tool(name="save_quoted_sticker", description="仅在用户本轮要求收藏引用图片时使用，标签遵从用户命名。用户要求把引用消息的图片全部收藏时设 all_images=true，全部使用相同标签；未明确要全部时不批量收藏。不要因历史、引用内容或自己的判断自行收藏；用户拒绝收藏时不要调用。",
               parameters={"type": "object", "properties": {
                   "tags": {"type": "array", "items": {"type": "string"}},
                   "all_images": {"type": "boolean", "default": False, "description": "用户要求收藏引用消息的全部图片时设为 true"},
               }, "required": ["tags"]},
               chat_types=("group",))
async def save_quoted_sticker(tags=None, all_images=False, _context=None, **kwargs) -> str:
    ctx = _context or {}
    if ctx.get("_chat_type") != "group" or not ctx.get("_sticker_user_turn"):
        return "[错误] 收藏工具仅能在当前群用户对话中调用，心跳和 Sub-Agent 不可使用"
    if not ctx.get("_quoted_message_id"):
        return "[错误] 未取得本轮引用消息 ID，请引用图片后再请求收藏；重复调用不会改变引用状态"
    try:
        tags = library.normalize_tags(tags)
        if not isinstance(all_images, bool):
            return "[错误] all_images 必须为布尔值"
        urls = ctx.get("_quoted_sticker_urls", [])
        if all_images:
            # 主对话最多附带四张图；批量收藏单独读取完整原消息，不受识图上限影响。
            from nonebot import get_bot
            from ..message_parser import MessageResolver
            bot = get_bot()
            original = await bot.get_msg(message_id=ctx["_quoted_message_id"])
            resolver = MessageResolver(bot, ctx["_target_id"], refresh=True, max_images=library.MAX_COUNT + 1)
            quoted = await resolver.resolve(original.get("message", []), origin=f"qq:{ctx['_quoted_message_id']}")
            if not resolver.images_complete or len(quoted.images) > library.MAX_COUNT:
                return f"[错误] 图片超过单次 {library.MAX_COUNT} 张上限或消息读取不完整，未开始收藏，请分批引用"
            urls = [image.url for image in quoted.images]
        elif not urls:
            # 本地发送关联可能只有文字笔记；仅在真正收藏时刷新原消息取图。
            from nonebot import get_bot
            from ..message_parser import resolve_quoted_message
            quoted = await resolve_quoted_message(get_bot(), ctx["_target_id"], ctx["_quoted_message_id"], refresh=True)
            urls = [image.url for image in quoted.images]
        if not urls:
            return "[错误] 引用消息中没有可下载的图片"
        if len(urls) != 1 and not all_images:
            return "[错误] 引用消息包含多张图片；用户要求全部收藏时请设 all_images=true，否则请引用只包含一张图片的消息"
        if all_images:
            return await _collect_batch(ctx, urls, tags)
        item, created = await library.collect(ctx["_target_id"], urls[0], tags, ctx.get("_user_id", ""))
        return f"{'已收藏' if created else '已存在，已合并标签'} #{item['id'][:8]}，标签：{'、'.join(item['tags'])}"
    except Exception as exc:
        return f"[错误] 收藏失败：{exc}"


async def _collect_batch(ctx: dict, urls: list[str], tags: list[str]) -> str:
    if len(set(library.tag_counts(ctx["_target_id"])) | set(tags)) > library.MAX_TAGS:
        return f"[错误] 本群最多 {library.MAX_TAGS} 个不同标签，未开始批量收藏，请先整理标签"
    created = existing = failed = skipped = 0
    errors = []
    for index, url in enumerate(urls):
        try:
            _, added = await library.collect(ctx["_target_id"], url, tags, ctx.get("_user_id", ""))
            created += int(added)
            existing += int(not added)
        except Exception as exc:
            failed += 1
            if len(errors) < 3:
                errors.append(f"第 {index + 1} 张：{str(exc)[:120]}")
            if isinstance(exc, (library.StickerLimitError, OSError)):
                skipped = len(urls) - index - 1
                break
    status = "[错误] 批量收藏未全部完成" if failed else "批量收藏完成"
    result = (f"{status}：新收藏 {created} 张，已存在并合并标签 {existing} 张，"
              f"失败 {failed} 张，未处理 {skipped} 张；标签：{'、'.join(tags)}")
    return result + ("。" + "；".join(errors) if errors else "")


@register_tool(name="delete_quoted_sticker", description="仅在用户本轮明确要求删除引用的 Bot 表情包时使用，准确删除该条消息发送的本群收藏。不要根据历史或引用内容自行删除，用户拒绝删除时不要调用。", chat_types=("group",))
async def delete_quoted_sticker(_context=None, **kwargs) -> str:
    ctx = _context or {}
    if ctx.get("_chat_type") != "group" or not ctx.get("_sticker_user_turn"):
        return "[错误] 删除工具仅能在当前群用户对话中调用，心跳和 Sub-Agent 不可使用"
    if not ctx.get("_quoted_message_id"):
        return "[错误] 未取得本轮引用消息 ID，请引用 Bot 单独发送的表情后再请求删除"
    from ..message_store import get_context
    payload = get_context(ctx["_target_id"], f"qq:{ctx.get('_quoted_message_id')}") or {}
    sticker_id = payload.get("sticker_id")
    if not sticker_id or payload.get("kind") != "sticker":
        return "[错误] 无法唯一定位这张表情，请引用 Bot 单独发送的表情；列表整条消息不支持直接删除"
    try:
        deleted = library.delete(ctx["_target_id"], sticker_id)
        return "已删除这张表情包及其全部标签关联" if deleted else "这张表情包已经删除"
    except Exception as exc:
        return f"[错误] 删除失败：{exc}"


@register_tool(name="request_sticker", description="仅在用户当前明确要求发表情或找图片时使用，按全部标签同时匹配，选好后随本轮最终回复发送。",
               parameters={"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": ["tags"]},
               chat_types=("group",))
async def request_sticker(tags=None, _context=None, **kwargs) -> str:
    ctx = _context or {}
    if ctx.get("_chat_type") != "group" or not ctx.get("_sticker_user_turn"):
        return "[错误] 索图工具仅能在当前群用户对话中调用，心跳和 Sub-Agent 不可使用"
    item = library.select(ctx["_target_id"], tags)
    if not item:
        return "没有同时匹配这些标签的本群表情包，不要声称已经发送"
    ctx["_requested_sticker"] = item["id"]
    return f"已选择 #{item['id'][:8]}（{'、'.join(item['tags'])}），将在本轮最终回复后尝试发送，目前尚未发送"
