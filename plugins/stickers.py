"""按群隔离的收藏图库，文件有界、索引原子写入，不依赖 NoneBot 生命周期。"""

import asyncio
import hashlib
import io
import json
import os
import re
import random
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import RLock

import httpx

ROOT = Path("data/stickers")
MAX_COUNT = 200
MAX_TOTAL_BYTES = 5_000_000_000
MAX_TAGS = 64
MAX_ITEM_TAGS = 5
TAG_LENGTH = 16
_lock = RLock()
_downloads = asyncio.Semaphore(2)


class StickerError(ValueError):
    pass


class StickerLimitError(StickerError):
    """图库已满；批量收藏保留已完成项目，停止继续下载。"""


def group_dir(gid: str) -> Path:
    gid = str(gid)
    if not gid.isascii() or not gid.isdigit():
        raise StickerError("无效群号")
    return ROOT / gid


def normalize_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags):
        raise StickerError("请提供标签列表")
    result = list(dict.fromkeys(t.strip() for t in tags))
    if len(result) > MAX_ITEM_TAGS or any(not t or len(t) > TAG_LENGTH or re.search(r"[\[\]<>\r\n,，、]", t) for t in result):
        raise StickerError(f"每张最多 {MAX_ITEM_TAGS} 个标签，每个 1–{TAG_LENGTH} 字，不含换行或分隔符")
    return result


def _load(gid: str) -> dict:
    path = group_dir(gid) / "index.json"
    if not path.exists():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError()
        return data
    except (ValueError, OSError) as exc:
        raise StickerError("图库索引读取失败，请管理员检查，未覆盖原文件") from exc


def _save(gid: str, data: dict) -> None:
    directory = group_dir(gid)
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=directory)
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, directory / "index.json")
    finally:
        temp.unlink(missing_ok=True)


def items(gid: str) -> list[dict]:
    with _lock:
        return [dict(item, tags=list(item["tags"])) for item in _load(gid)["items"]]


def groups() -> list[str]:
    return sorted(p.name for p in ROOT.iterdir() if p.is_dir() and p.name.isascii() and p.name.isdigit()) if ROOT.exists() else []


def total_bytes() -> int:
    with _lock:
        return sum(p.stat().st_size for gid in groups() for p in (group_dir(gid) / "images").glob("*") if p.is_file())


def tag_counts(gid: str) -> dict[str, int]:
    counts = {}
    for item in items(gid):
        for tag in item["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items()))


def get(gid: str, sticker_id: str) -> dict | None:
    return next((item for item in items(gid) if item["id"] == sticker_id), None)


def image_path(gid: str, item: dict) -> Path:
    name = item.get("file", "")
    if not re.fullmatch(r"[a-f0-9]{64}\.(?:gif|png|jpg|webp)", name):
        raise StickerError("图库文件名无效")
    return group_dir(gid) / "images" / name


def read_image(gid: str, sticker_id: str) -> tuple[dict, bytes]:
    with _lock:
        item = get(gid, sticker_id)
        if not item:
            raise StickerError("这张表情包已经删除或不属于本群")
        return item, image_path(gid, item).read_bytes()


def _prepare(raw: bytes | Path) -> tuple[bytes | Path, str, str]:
    from PIL import Image, ImageOps, UnidentifiedImageError
    source = raw if isinstance(raw, Path) else io.BytesIO(raw)
    try:
        with Image.open(source) as image:
            fmt = image.format
            if fmt not in ("GIF", "JPEG", "PNG", "WEBP") or image.width * image.height > 20_000_000:
                raise StickerError("图片格式不支持或尺寸过大")
            image.verify()
        if fmt == "GIF":
            return raw, "gif", "image/gif"
        source = raw if isinstance(raw, Path) else io.BytesIO(raw)
        with Image.open(source) as image:
            if getattr(image, "is_animated", False):
                ext = "webp" if fmt == "WEBP" else "png"
                return raw, ext, f"image/{ext}"
            image = ImageOps.exif_transpose(image)
            image.thumbnail((1024, 1024))
            out = io.BytesIO()
            if "A" in image.getbands() or "transparency" in image.info:
                image.convert("RGBA").save(out, format="WEBP", quality=85)
                ext, mime = "webp", "image/webp"
            else:
                image.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
                ext, mime = "jpg", "image/jpeg"
            return out.getvalue(), ext, mime
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError) as exc:
        raise StickerError("图片损坏或不是支持的图片格式") from exc


def _check_tags(data: dict, tags: list[str], exclude: str = "") -> None:
    all_tags = {tag for item in data["items"] if item["id"] != exclude for tag in item["tags"]} | set(tags)
    if len(all_tags) > MAX_TAGS:
        raise StickerError(f"本群最多 {MAX_TAGS} 个不同标签，请先整理标签")


def add(gid: str, raw: bytes | Path, tags: list[str], uploader: str = "", description: str = "") -> tuple[dict, bool]:
    tags = normalize_tags(tags)
    content, ext, mime = _prepare(raw)
    if isinstance(raw, Path):
        hasher = hashlib.sha256()
        with raw.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    else:
        digest = hashlib.sha256(raw).hexdigest()
    size = content.stat().st_size if isinstance(content, Path) else len(content)
    with _lock:
        data = _load(gid)
        existing = next((i for i in data["items"] if i["hash"] == digest), None)
        if existing:
            merged = normalize_tags(existing["tags"] + tags)
            _check_tags(data, merged, existing["id"])
            existing["tags"] = merged
            _save(gid, data)
            return dict(existing), False
        if len(data["items"]) >= MAX_COUNT:
            raise StickerLimitError(f"本群已有 {MAX_COUNT} 张表情，请先删除再收藏")
        _check_tags(data, tags)
        if total_bytes() + size > MAX_TOTAL_BYTES:
            raise StickerLimitError("所有群图库已达到 5 GB 上限，请先清理")
        directory = group_dir(gid) / "images"
        directory.mkdir(parents=True, exist_ok=True)
        item = {"id": uuid.uuid4().hex, "hash": digest, "file": f"{digest}.{ext}", "mime": mime,
                "tags": tags, "description": str(description)[:300], "uploader": str(uploader),
                "size": size, "created_at": time.time(), "last_used_at": None}
        path = image_path(gid, item)
        fd, name = tempfile.mkstemp(prefix=".image-", suffix=".tmp", dir=directory)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as file:
                if isinstance(content, Path):
                    with content.open("rb") as source_file:
                        shutil.copyfileobj(source_file, file, length=1024 * 1024)
                else:
                    file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, path)
            data["items"].append(item)
            try:
                _save(gid, data)
            except Exception:
                path.unlink(missing_ok=True)
                raise
        finally:
            temp.unlink(missing_ok=True)
        return dict(item), True


async def collect(gid: str, url: str, tags: list[str], uploader: str = "", description: str = "") -> tuple[dict, bool]:
    normalize_tags(tags)
    parsed = httpx.URL(url)
    if parsed.scheme not in ("https", "http") or not parsed.host:
        raise StickerError("引用图片没有可下载的 HTTP 地址，请重新发送图片")
    async with _downloads:
        directory = group_dir(gid)
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".download-", suffix=".tmp", dir=directory)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as file:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        size = 0
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > MAX_TOTAL_BYTES:
                                raise StickerLimitError("图片下载已超过全库容量上限，已停止")
                            file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            task = asyncio.create_task(asyncio.to_thread(add, gid, temp, tags, uploader, description))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # 文件仍由工作线程使用时，先等线程退出再清理临时文件。
                try:
                    await task
                except Exception:
                    pass
                raise
        finally:
            temp.unlink(missing_ok=True)


def update_tags(gid: str, sticker_id: str, tags: list[str]) -> dict:
    tags = normalize_tags(tags)
    with _lock:
        data = _load(gid)
        item = next((i for i in data["items"] if i["id"] == sticker_id), None)
        if not item:
            raise StickerError("这张表情包已经删除")
        _check_tags(data, tags, sticker_id)
        item["tags"] = tags
        _save(gid, data)
        return dict(item)


def delete(gid: str, sticker_id: str) -> bool:
    with _lock:
        data = _load(gid)
        item = next((i for i in data["items"] if i["id"] == sticker_id), None)
        if not item:
            return False
        data["items"].remove(item)
        _save(gid, data)
        image_path(gid, item).unlink(missing_ok=True)
        return True


def mark_used(gid: str, sticker_id: str) -> None:
    with _lock:
        data = _load(gid)
        item = next((i for i in data["items"] if i["id"] == sticker_id), None)
        if item:
            item["last_used_at"] = time.time()
            _save(gid, data)


def cleanup() -> None:
    """仅回收超过一天的临时/无索引文件，正常收藏没有到期时间。"""
    with _lock:
        cutoff = time.time() - 86400
        for gid in groups():
            referenced = {i["file"] for i in _load(gid)["items"]}
            for path in group_dir(gid).rglob("*"):
                if path.is_file() and path.stat().st_mtime < cutoff and (
                    path.name.endswith(".tmp") or (path.parent.name == "images" and path.name not in referenced)
                ):
                    path.unlink()


def enabled(gid: str) -> bool:
    from .persona.manager import get_group_config
    return bool(get_group_config(gid).get("auto_stickers", True))


def set_enabled(gid: str, value: bool) -> None:
    from .persona.manager import get_group_config, _save_group_config
    group_dir(gid)
    with _lock:
        cfg = get_group_config(gid)
        cfg["auto_stickers"] = bool(value)
        _save_group_config(gid, cfg)


def select(gid: str, tags: list[str], *, automatic: bool = False) -> dict | None:
    tags = normalize_tags(tags)
    with _lock:
        data = _load(gid)
        if automatic and not enabled(gid):
            return None
        matches = [i for i in data["items"] if set(tags) <= set(i["tags"]) and image_path(gid, i).is_file()]
        if not matches:
            return None
        oldest = min(i.get("last_used_at") or 0 for i in matches)
        item = random.choice([i for i in matches if (i.get("last_used_at") or 0) == oldest])
        return dict(item, _automatic=automatic)
