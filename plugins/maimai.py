"""
舞萌 DX 查分
────────────
查分走水鱼（diving-fish）公开 API，乐曲别名走柚子别名库 v2。
用户可通过 /bind <导入Token> 绑定自己的水鱼账号：查自己时走本人通道
（GET /player/records + Import-Token），不受隐私遮蔽影响。

安全约定：
- Token 等同水鱼账号的成绩读写凭证（写端点也吃它），因此：
  - 只落盘 data/maimai/tokens.json（gitignored）
  - 只调读端点，写端点在代码路径中不存在
  - 不写入日志、工具返回值、对话历史
- 对第三方公开查询（query/player）遵循对方隐私设置，不做任何绕过。
"""

import json
import time
import threading
from pathlib import Path

import httpx
from nonebot.log import logger

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "maimai"
TOKENS_PATH = DATA_DIR / "tokens.json"
ALIAS_CACHE_PATH = DATA_DIR / "alias_cache.json"

DF_BASE = "https://www.diving-fish.com/api/maimaidxprober"
YUZU_BASE = "https://www.yuzuchan.moe/api/v2/aliases/maimaidx"

# 柚子别名库对非浏览器 UA 直接断连（CDN 过滤，非认证），带上常规 UA 即可
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

ALIAS_TTL = 7 * 86400          # 别名表 7 天刷新
QUERY_CACHE_TTL = 300          # 查分结果进程内缓存 5 分钟（防群友连刷触发限流）

_B50_NEW = 15                  # b50 = 新版本 ra 前 15 + 旧版本前 35
_B50_OLD = 35

_token_lock = threading.Lock()
_alias_table: list[dict] | None = None
_alias_fetched_at: float = 0.0
_query_cache: dict[str, tuple[float, str]] = {}


class MaimaiError(Exception):
    pass


# ──────────────────── Token 存储 ────────────────────

def _load_tokens() -> dict:
    if not TOKENS_PATH.exists():
        return {}
    try:
        return json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_tokens(tokens: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(
        json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_token(qq: str) -> str | None:
    entry = _load_tokens().get(str(qq))
    return entry.get("token") if entry else None


def bind_token(qq: str, token: str) -> None:
    with _token_lock:
        tokens = _load_tokens()
        tokens[str(qq)] = {"token": token, "bound_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_tokens(tokens)


def unbind_token(qq: str) -> bool:
    with _token_lock:
        tokens = _load_tokens()
        if str(qq) not in tokens:
            return False
        del tokens[str(qq)]
        _save_tokens(tokens)
        return True


# ──────────────────── HTTP ────────────────────

async def _post_json(url: str, payload: dict, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30, trust_env=False, headers=headers) as client:
        return await client.post(url, json=payload)


async def _get_json(url: str, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=60, trust_env=False, headers=headers or {"User-Agent": _BROWSER_UA}
    ) as client:
        return await client.get(url, params=params)


# ──────────────────── 查分 ────────────────────

async def fetch_records(token: str, *, is_new: bool | None = None) -> dict:
    """本人通道：Import-Token 读完整成绩（不受隐私遮蔽影响）。Token 无效抛 MaimaiError。

    is_new=True/False 时用服务端过滤取对应分桶（"新版本"=当前版本新增曲目，
    实测用户成绩记录本体不含 is_new/version 字段，必须靠服务端分桶）。
    """
    params = {"is_new": "true"} if is_new is True else ({"is_new": "false"} if is_new is False else None)
    resp = await _get_json(f"{DF_BASE}/player/records", params=params, headers={"Import-Token": token})
    if resp.status_code in (400, 401, 403):
        raise MaimaiError("Token 无效或已过期")
    resp.raise_for_status()
    return resp.json()


async def verify_token(token: str) -> dict | None:
    """验证 Token 有效性，有效返回 {username, rating}，无效返回 None"""
    try:
        data = await fetch_records(token)
    except Exception as e:
        logger.warning(f"水鱼 Token 验证失败: {e}")
        return None
    if not isinstance(data, dict) or "records" not in data:
        return None
    return {"username": data.get("username"), "rating": data.get("rating")}


def compute_b50(new_records: list[dict], old_records: list[dict]) -> list[dict]:
    """b50 = 新版本(is_new=true) ra 前 15 + 其余前 35，合并按 ra 降序。

    ⚡实测校准（rating=15958 的真实账号精确对齐）：rating = additional_rating + sum(b50 ra)。
    """
    new = sorted(new_records, key=lambda r: r.get("ra", 0), reverse=True)[:_B50_NEW]
    old = sorted(old_records, key=lambda r: r.get("ra", 0), reverse=True)[:_B50_OLD]
    merged = sorted(new + old, key=lambda r: r.get("ra", 0), reverse=True)
    for r in merged:
        r["is_new"] = r in new
    return merged


def format_records(records: list[dict]) -> str:
    lines = []
    for i, r in enumerate(records, 1):
        title = str(r.get("title", "?"))[:24]
        mark = "*" if r.get("is_new") else ""
        lines.append(
            f"{i:>2}. {title} [{r.get('level', '?')}]{mark} "
            f"{r.get('achievements', 0):.4f}% ra={r.get('ra', 0)}"
        )
    return "\n".join(lines)


async def query_player_b50(qq: str) -> str:
    """查分主入口：绑定了 Token 的 QQ 走本人通道，否则走公开查询。返回格式化文本。"""
    qq = str(qq).strip()
    if not qq.isdigit():
        return "[错误] QQ号必须是数字"

    cached = _query_cache.get(qq)
    if cached and time.time() - cached[0] < QUERY_CACHE_TTL:
        return cached[1]

    result = await _query_player_b50_uncached(qq)
    if not result.startswith("[错误]"):
        _query_cache[qq] = (time.time(), result)
    return result


async def _query_player_b50_uncached(qq: str) -> str:
    token = get_token(qq)

    if token:
        try:
            new_data = await fetch_records(token, is_new=True)
            old_data = await fetch_records(token, is_new=False)
        except MaimaiError as e:
            return f"[错误] {e}，请让该用户重新 /bind"
        except Exception as e:
            return f"[错误] 水鱼接口请求失败: {e}"
        b50 = compute_b50(new_data.get("records", []), old_data.get("records", []))
        if not b50:
            return "[错误] 该账号在水鱼没有成绩记录"
        rating = (old_data.get("additional_rating") or 0) + sum(r.get("ra", 0) for r in b50)
        name = old_data.get("nickname") or old_data.get("username") or qq
        return f"{name} 的舞萌DX b50（rating={rating}，*为新曲）:\n{format_records(b50)}"

    try:
        resp = await _post_json(f"{DF_BASE}/query/player", {"qq": qq, "b50": "1"})
    except Exception as e:
        return f"[错误] 水鱼接口请求失败: {e}"
    if resp.status_code == 400:
        return "[错误] 水鱼查无此人：对方没在查分器注册或没同步过成绩"
    if resp.status_code == 403:
        return "[错误] 对方开启了隐私保护，无法查询成绩"
    resp.raise_for_status()
    data = resp.json()
    records = data.get("records", [])
    if not records:
        return "[错误] 对方开启了成绩遮蔽，第三方看不到 b50 明细（本人可 /bind 后查看）"
    name = data.get("nickname") or qq
    return f"{name} 的舞萌DX b50（rating={data.get('rating')}，*为新曲）:\n{format_records(records)}"


# ──────────────────── 别名库 ────────────────────

def _load_alias_cache() -> tuple[list | None, float]:
    if not ALIAS_CACHE_PATH.exists():
        return None, 0.0
    try:
        j = json.loads(ALIAS_CACHE_PATH.read_text(encoding="utf-8"))
        return j.get("songs"), j.get("fetched_at", 0.0)
    except (json.JSONDecodeError, OSError):
        return None, 0.0


async def get_alias_table() -> list[dict]:
    """全量别名表：进程缓存 → 磁盘缓存（7 天）→ 实时拉取；失败时用过期缓存兜底"""
    global _alias_table, _alias_fetched_at
    now = time.time()
    if _alias_table is not None and now - _alias_fetched_at < ALIAS_TTL:
        return _alias_table

    cached, cached_at = _load_alias_cache()
    if cached and now - cached_at < ALIAS_TTL:
        _alias_table, _alias_fetched_at = cached, cached_at
        return cached

    try:
        resp = await _get_json(f"{YUZU_BASE}/aliases")
        resp.raise_for_status()
        songs = resp.json()
        if not isinstance(songs, list):
            raise MaimaiError("别名表格式异常")
        _alias_table, _alias_fetched_at = songs, now
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ALIAS_CACHE_PATH.write_text(
            json.dumps({"fetched_at": now, "songs": songs}, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info(f"舞萌别名库已刷新: {len(songs)} 首")
        return songs
    except Exception as e:
        logger.warning(f"拉取舞萌别名库失败: {e}")
        if cached:
            _alias_table, _alias_fetched_at = cached, cached_at
            return cached
        return []


async def search_songs(keyword: str) -> list[dict]:
    """按别名/曲名子串（不区分大小写）模糊搜索，返回全部命中"""
    kw = keyword.strip().lower()
    if not kw:
        return []
    table = await get_alias_table()
    hits = []
    for s in table:
        hay = [str(s.get("name", "")).lower()] + [str(a).lower() for a in s.get("alias", [])]
        if any(kw in h for h in hay):
            hits.append({
                "song_id": s.get("song_id"),
                "name": s.get("name"),
                "alias": s.get("alias", []),
            })
    return hits


def format_songs(hits: list[dict]) -> str:
    if not hits:
        return "别名库里没有这个叫法（可以让群友去柚子别名库投票添加）"
    lines = []
    for h in hits:
        aliases = h.get("alias", [])
        shown = "、".join(aliases[:6]) + ("…" if len(aliases) > 6 else "")
        lines.append(f"{h.get('name')}（song_id={h.get('song_id')}｜别名: {shown}）")
    return f"找到 {len(hits)} 首:\n" + "\n".join(lines)
