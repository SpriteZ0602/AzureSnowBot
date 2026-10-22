"""用户自然语言管理表情，图片及授权来自当前 handler 引用上下文。"""

from .manager import register_tool
from .. import stickers as library


@register_tool(name="save_quoted_sticker", description="用户明确要求收藏引用的单张图片时使用，标签遵从用户命名，不能自行收藏。",
               parameters={"type": "object", "properties": {"tags": {"type": "array", "items": {"type": "string"}}}, "required": ["tags"]},
               chat_types=("group",))
async def save_quoted_sticker(tags=None, _context=None, **kwargs) -> str:
    ctx = _context or {}
    if ctx.get("_chat_type") != "group" or not ctx.get("_sticker_save_allowed"):
        return "[错误] 仅能响应用户当前明确的引用图片收藏请求"
    urls = ctx.get("_quoted_sticker_urls", [])
    if len(urls) != 1:
        return "[错误] 请引用只包含一张图片的消息再收藏"
    try:
        item, created = await library.collect(ctx["_target_id"], urls[0], tags, ctx.get("_user_id", ""))
        return f"{'已收藏' if created else '已存在，已合并标签'} #{item['id'][:8]}，标签：{'、'.join(item['tags'])}"
    except Exception as exc:
        return f"[错误] 收藏失败：{exc}"


@register_tool(name="delete_quoted_sticker", description="用户明确要求删除引用的 Bot 表情包时使用，准确删除该条消息发送的本群收藏。", chat_types=("group",))
async def delete_quoted_sticker(_context=None, **kwargs) -> str:
    ctx = _context or {}
    if ctx.get("_chat_type") != "group" or not ctx.get("_sticker_delete_allowed"):
        return "[错误] 请引用 Bot 单独发送的表情，并明确要求删除"
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
