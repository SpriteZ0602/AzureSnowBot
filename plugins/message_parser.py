"""统一解析 OneBot 消息、引用和合并转发；网络解析只读，图片不落盘。"""

import hashlib
import re
from dataclasses import asdict, dataclass, field

from nonebot.log import logger

from . import message_store as store

MAX_DEPTH = 3
MAX_NODES = 100
MAX_IMAGES = 4
MAX_TEXT = 50000
PREVIEW_CHARS = 6000
_CQ = re.compile(r"\[CQ:([\w-]+)((?:,[^\]]*)?)\]")


def _unescape(value: str) -> str:
    return value.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&amp;", "&")


def segments(raw) -> list[dict]:
    """兼容数组、MessageSegment 和 CQ 码字符串；实体仅解码一次。"""
    if isinstance(raw, str):
        result, cursor = [], 0
        for match in _CQ.finditer(raw):
            if match.start() > cursor:
                result.append({"type": "text", "data": {"text": _unescape(raw[cursor:match.start()])}})
            data = {}
            for item in match.group(2).lstrip(",").split(","):
                if "=" in item:
                    key, value = item.split("=", 1)
                    data[key] = _unescape(value)
            result.append({"type": match.group(1), "data": data})
            cursor = match.end()
        if cursor < len(raw):
            result.append({"type": "text", "data": {"text": _unescape(raw[cursor:])}})
        return result
    if isinstance(raw, dict):
        raw = [raw]
    return [item if isinstance(item, dict) else {"type": item.type, "data": item.data}
            for item in (raw or [])]


@dataclass
class ImageRef:
    image_id: str
    url: str


@dataclass
class ResolvedMessage:
    text: str = ""
    images: list[ImageRef] = field(default_factory=list)
    author: str = ""

    @classmethod
    def from_payload(cls, payload):
        return cls(payload.get("text", ""), [ImageRef(**i) for i in payload.get("images", [])], payload.get("author", ""))


def image_marker(image_id: str) -> str:
    return f"[图片 {image_id}，尚未识别]"


def _cached(group_id, ref):
    try:
        return store.get_context(group_id, ref)
    except Exception as exc:
        logger.warning(f"读取消息索引失败: {type(exc).__name__}")
        return None


def _save(group_id, ref, payload, **kwargs):
    try:
        return store.put_context(group_id, ref, payload, **kwargs)
    except Exception as exc:
        logger.warning(f"保存消息索引失败: {type(exc).__name__}")
        return False


def _preview(text: str, ref: str) -> str:
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[:PREVIEW_CHARS] + f"\n[内容已截断；使用 local__read_message_content(reference={ref!r}, offset={PREVIEW_CHARS}) 继续读取]"


class MessageResolver:
    def __init__(self, bot, group_id: str, self_id: str = "", *, refresh: bool = False):
        self.bot, self.group_id, self.self_id = bot, str(group_id), str(self_id)
        self.refresh = refresh
        self.nodes = 0
        self.images: list[ImageRef] = []
        self.visited: set[str] = set()

    async def resolve(self, raw, *, origin: str = "", depth: int = 0) -> ResolvedMessage:
        parts = []
        for index, seg in enumerate(segments(raw)):
            kind, data = seg.get("type"), seg.get("data") or {}
            if kind == "text":
                parts.append(str(data.get("text", "")))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                if qq == "all":
                    parts.append("@全体成员")
                elif qq == self.self_id:
                    parts.append("@Bot")
                elif qq:
                    try:
                        info = await self.bot.get_group_member_info(group_id=int(self.group_id), user_id=int(qq))
                        nickname = info.get("nickname") or ""
                    except Exception:
                        nickname = ""
                    parts.append(f"@{nickname}({qq})" if nickname else f"@{qq}")
            elif kind == "image":
                url = str(data.get("url") or "")
                identity = f"{origin}:{index}:{data.get('file') or url.split('?', 1)[0]}"
                iid = "img_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
                if len(self.images) < MAX_IMAGES and url and all(i.image_id != iid for i in self.images):
                    self.images.append(ImageRef(iid, url))
                parts.append(image_marker(iid))
            elif kind == "forward":
                parts.append(await self._forward(data, origin=f"{origin}:{index}", depth=depth))
            elif kind == "face":
                parts.append(f"[表情:{data.get('id', '?')}]")
            elif kind in ("record", "video", "file"):
                parts.append({"record": "[语音]", "video": "[视频]", "file": "[文件]"}[kind])
            if sum(map(len, parts)) > MAX_TEXT:
                parts.append("[内容已截断]")
                break
        return ResolvedMessage("".join(parts).strip()[:MAX_TEXT], list(self.images))

    async def _forward(self, data: dict, *, origin: str, depth: int) -> str:
        fid = str(data.get("id") or "")
        ref = f"forward:{fid}" if fid else f"forward:inline_{hashlib.sha256(origin.encode()).hexdigest()[:20]}"
        if depth >= MAX_DEPTH or ref in self.visited:
            return f"[合并转发 {ref}：达到嵌套上限或循环引用]"
        try:
            cached = None if self.refresh else _cached(self.group_id, ref)
            if cached:
                for img in cached.get("images", []):
                    if len(self.images) < MAX_IMAGES and all(i.image_id != img["image_id"] for i in self.images):
                        self.images.append(ImageRef(**img))
                return f"\n[合并转发 {ref}]\n{_preview(cached['text'], ref)}\n[转发结束]\n"
            nodes = data.get("content")
            if nodes is None:
                response = await self.bot.get_forward_msg(id=fid)
                nodes = response.get("messages", response.get("message", []))
            self.visited.add(ref)
            lines = []
            before = len(self.images)
            for node in nodes or []:
                if self.nodes >= MAX_NODES:
                    lines.append("[转发节点已截断：达到条数上限]")
                    break
                self.nodes += 1
                body = node.get("data", {}) if node.get("type") == "node" else node
                sender = body.get("sender") or {}
                author = str(body.get("nickname") or sender.get("nickname") or "未知作者")
                uid = str(body.get("user_id") or sender.get("user_id") or "")
                content = body.get("content", body.get("message"))
                if content is None and str(body.get("id", "")).lstrip("-").isdigit():
                    original = await self.bot.get_msg(message_id=int(body["id"]))
                    content = original.get("message", "")
                    author = (original.get("sender") or {}).get("nickname") or author
                resolved = await self.resolve(content or [], origin=f"{ref}:{self.nodes}", depth=depth + 1)
                lines.append(f"[转发中的作者 {author}{'(' + uid + ')' if uid else ''}]: {resolved.text}")
                if sum(map(len, lines)) >= MAX_TEXT:
                    lines.append("[转发内容已截断]")
                    break
            self.visited.remove(ref)
            text = "\n".join(lines)[:MAX_TEXT] or "[合并转发内容为空]"
            _save(self.group_id, ref, {"text": text, "images": [asdict(i) for i in self.images[before:]]},
                              kind="forward", ttl=store.FORWARD_TTL)
            return f"\n[合并转发 {ref}]\n{_preview(text, ref)}\n[转发结束]\n"
        except Exception as exc:
            logger.warning(f"读取合并转发失败: {type(exc).__name__}")
            return f"[合并转发 {ref}：读取失败或已过期]"


async def resolve_quoted_message(bot, group_id: str, message_id: int, *, self_id: str = "",
                                 reply=None, refresh: bool = False) -> ResolvedMessage:
    try:
        cached = None if refresh else _cached(str(group_id), f"qq:{message_id}")
        if cached:
            value = ResolvedMessage.from_payload(cached)
            value.text = _preview(value.text, f"qq:{message_id}")
            return value
        if reply is not None and getattr(reply, "message", None) is not None:
            raw, sender = reply.message, getattr(reply, "sender", None) or {}
            author = sender.get("nickname") if isinstance(sender, dict) else getattr(sender, "nickname", "")
        else:
            original = await bot.get_msg(message_id=message_id)
            raw = original.get("message", "")
            author = (original.get("sender") or {}).get("nickname") or "某人"
        resolved = await MessageResolver(bot, group_id, self_id, refresh=refresh).resolve(raw, origin=f"qq:{message_id}")
        resolved.author = author or "某人"
        return resolved
    except Exception as exc:
        logger.warning(f"读取引用消息失败: {type(exc).__name__}")
        return ResolvedMessage("[引用消息读取失败或已过期]", author="某人")
