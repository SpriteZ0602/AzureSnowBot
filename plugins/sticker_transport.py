"""有界图片传输：小图内联，大图分块传到 NapCat 的文件系统。"""

import asyncio
import base64
import math
import uuid
from weakref import WeakKeyDictionary
from botcore.sessions import ensure_current

INLINE_BYTES = 4 * 1024 * 1024
CHUNK_BYTES = 512 * 1024
_transfers = WeakKeyDictionary()


def _read_chunk(file):
    return file.read(CHUNK_BYTES)


async def image_source(bot, gid: str, sticker_id: str, *, library=None) -> tuple[dict, str]:
    if library is None:
        from . import stickers as library
    loop = asyncio.get_running_loop()
    gate = _transfers.setdefault(loop, asyncio.Semaphore(1))
    async with gate:
        item = await asyncio.to_thread(library.get, gid, sticker_id)
        if item is None:
            raise library.StickerError("这张表情已经删除或不属于本群")
        path = library.image_path(gid, item)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if size <= INLINE_BYTES:
            _, content = await asyncio.to_thread(library.read_image, gid, sticker_id)
            source = await asyncio.to_thread(lambda: "base64://" + base64.b64encode(content).decode("ascii"))
            return item, source
        stream_id = uuid.uuid4().hex
        total = math.ceil(size / CHUNK_BYTES)
        digest = await asyncio.to_thread(library.content_hash, path)
        try:
            with path.open("rb") as file:
                for index in range(total):
                    ensure_current()
                    chunk = await asyncio.to_thread(_read_chunk, file)
                    if not chunk:
                        raise library.StickerError("上传期间图片发生变化")
                    result = await bot.call_api("upload_file_stream", stream_id=stream_id,
                        chunk_data=base64.b64encode(chunk).decode("ascii"), chunk_index=index,
                        total_chunks=total, file_size=size, expected_sha256=digest,
                        filename=f"sticker-{stream_id}{path.suffix}", file_retention=300_000)
                    if not isinstance(result, dict) or result.get("status") != "chunk_received" or result.get("received_chunks") != index + 1:
                        raise library.StickerError("NapCat 未确认图片分块上传，请检查是否支持 upload_file_stream")
            ensure_current()
            result = await bot.call_api("upload_file_stream", stream_id=stream_id, is_complete=True)
            if (not isinstance(result, dict) or result.get("status") != "file_complete"
                    or result.get("file_size") != size or result.get("sha256") != digest
                    or not isinstance(result.get("file_path"), str) or not result["file_path"]):
                raise library.StickerError("NapCat 未确认图片完整上传")
            return item, result["file_path"]
        except BaseException:
            # 只回收本次上传，不使用清空 NapCat 全部临时文件的接口。
            try:
                await bot.call_api("upload_file_stream", stream_id=stream_id, reset=True)
            except Exception:
                pass
            raise
