"""舞萌 DX 与中二节奏国服公开查询。"""

from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from dataclasses import dataclass

import httpx

DIVING_FISH = "https://www.diving-fish.com/api"
LXNS = "https://maimai.lxns.net/api/v0"
YUZU = "https://www.yuzuchan.moe/api/v2/aliases"
GAMES = {"mai": "舞萌 DX", "chu": "中二节奏"}
SOURCES = {"divingfish": "水鱼", "lxns": "落雪"}
GAME_PATHS = {"mai": ("maimaidxprober", "maimai", "maimaidx"),
              "chu": ("chunithmprober", "chunithm", "chunithm")}
DIFFICULTIES = ("BAS", "ADV", "EXP", "MAS", "Re:MAS", "宴")
CHUNI_DIFFICULTIES = (*DIFFICULTIES[:4], "ULT", "WE")
QQ_PATTERN = re.compile(r"[1-9][0-9]{4,11}")
CATALOG_TTL = 86400
RETRY_DELAY = 300


class ProberError(Exception):
    pass


@dataclass(frozen=True)
class Score:
    title: str
    chart: str
    result: str
    rating: str
    badge: str

    def line(self) -> str:
        rating = f"  RA {self.rating}" if self.rating else ""
        badge = f"  {self.badge}" if self.badge else ""
        return f"{self.title} [{self.chart}] {self.result}{rating}{badge}"


@dataclass(frozen=True)
class BestScores:
    game: str
    source: str
    player: str
    rating: str
    groups: tuple[tuple[str, tuple[Score, ...]], ...]

    @property
    def title(self) -> str:
        return f"{self.player} · {GAMES[self.game]}国服 · {SOURCES[self.source]}"

    def text(self) -> str:
        lines = [self.title, f"Rating {self.rating}"]
        for name, scores in self.groups:
            if scores:
                lines.append(f"{name}（{len(scores)} 张）")
                lines.extend(f"{i}. {row.line()}" for i, row in enumerate(scores, 1))
        if len(lines) == 2:
            lines.append("查分器未返回公开成绩明细，可能尚未同步或设置了隐私。")
        return "\n".join(lines)


def _text(value, limit: int = 100) -> str:
    if not isinstance(value, str):
        return ""
    # NapCat 可把合并转发中的纯字符串解析为 CQ 码。
    return " ".join(value.split()).replace("[CQ:", "［CQ:")[:limit]


def _number(value, spec: str = "g") -> str:
    if type(value) in (int, float) and -1e12 < value < 1e12 and math.isfinite(value):
        return format(value, spec)
    return ""


def _id(value) -> str:
    return str(value) if type(value) is int and 0 < value < 10**20 else ""


async def _json(client: httpx.AsyncClient, method: str, url: str, source: str, **kwargs):
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ProberError(f"暂时连不上{source}，请稍后再试。") from exc
    data = None
    if response.is_success:
        try:
            data = response.json()
        except ValueError as exc:
            raise ProberError(f"{source}返回了无法解析的数据。") from exc
    code = response.status_code
    if response.is_success and isinstance(data, dict) and "success" in data:
        if data["success"] is True and ("code" not in data or data["code"] == 200):
            return data.get("data")
        code = data.get("code") or 502
    elif response.is_success:
        return data
    if code in (400, 404):
        raise ProberError(f"{source}未找到账号或数据，请确认已绑定 QQ 并同步成绩。")
    if code == 401:
        raise ProberError(f"{source}拒绝鉴权，请联系 Bot 管理员。")
    if code == 403:
        raise ProberError(f"{source}限制了第三方查询，请检查查分器的隐私设置。")
    if code == 429:
        raise ProberError(f"{source}查询频率受限，请稍后再试。")
    raise ProberError(f"{source}暂时无法提供数据，请稍后再试。")


def _mapping(data, source: str) -> dict:
    if not isinstance(data, dict):
        raise ProberError(f"{source}返回了无法解析的数据。")
    return data


def _score(game: str, row: dict) -> Score:
    is_mai = game == "mai"
    difficulties = DIFFICULTIES if is_mai else CHUNI_DIFFICULTIES
    index = row.get("level_index")
    diff = difficulties[index] if type(index) is int and 0 <= index < len(difficulties) else ""
    chart_type = {"DX": "DX", "SD": "STD", "STANDARD": "STD"}.get(_text(row.get("type")).upper(), "")
    chart = " ".join(filter(None, (chart_type if is_mai else "", diff, _text(row.get("level"), 12))))
    title = _text(row.get("title") or row.get("song_name"))
    song_id = _id(row.get("song_id") or row.get("mid") or row.get("id"))
    result = _number(row.get("achievements"), ".4f") if is_mai else _number(row.get("score"), ",.0f")
    rating = row.get("ra", row.get("dx_rating" if is_mai else "rating"))
    rating_text = _number(rating, ".2f")
    if is_mai and rating_text:
        rating_text = str(math.floor(rating))
    combo = _text(row.get("fc") or row.get("full_combo"), 24).lower()
    combo = {"alljusticecritical": "AJC", "alljustice": "AJ", "fullcombo": "FC"}.get(combo, combo.upper())
    return Score(title or (f"#{song_id}" if song_id else "未知曲目"), chart or "?",
                 result + ("%" if is_mai else "") if result else "—",
                 rating_text, combo)


def _best_scores(game: str, source: str, data: dict, profile: dict, identifier: str) -> BestScores:
    if game == "mai":
        buckets = (("B35", "sd", 35), ("B15", "dx", 15))
        if source == "lxns":
            buckets = (("B35", "standard", 35), ("B15", "dx", 15))
        elif "charts" in data:
            data = _mapping(data["charts"], SOURCES[source])
        elif isinstance(data.get("records"), list):
            # 水鱼也提供平铺的成绩。只有明确提供新旧曲标志时才划分。
            records = data["records"]
            if records and all(isinstance(r, dict) and type(r.get("is_new")) is bool for r in records):
                data = {"sd": [r for r in records if not r["is_new"]], "dx": [r for r in records if r["is_new"]]}
            else:
                buckets = (("B50", "records", 50),)
    elif source == "lxns":
        buckets = (("B30", "bests", 30), ("N20", "new_bests", 20))
    else:
        data = _mapping(data.get("records") or {}, SOURCES[source])
        buckets = (("B30", "b30", 30), ("N20", "n20", 20), ("R10", "r10", 10))
    groups = []
    for label, key, limit in buckets:
        rows = data.get(key) or []
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows[:limit]):
            raise ProberError(f"{SOURCES[source]}返回的成绩列表格式异常。")
        if rows:
            groups.append((label, tuple(_score(game, row) for row in rows[:limit])))
    player = _text(profile.get("nickname") or profile.get("name") or profile.get("username"), 50) or _text(identifier, 50)
    rating = _number(profile.get("rating"), ".0f" if game == "mai" else ".2f")
    return BestScores(game, source, player, rating or "—", tuple(groups))


async def query_best_scores(
    game: str, source: str = "divingfish", *, qq: str = "", username: str = "", lxns_token: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> BestScores:
    if game not in GAMES or source not in SOURCES:
        raise ProberError("请选择舞萌或中二，以及水鱼或落雪。")
    if username and (qq or source != "divingfish" or len(username) > 64 or not username.isprintable()):
        raise ProberError("水鱼可按用户名查询，请只填一个有效的用户名或 QQ 号。")
    if not username and not QQ_PATTERN.fullmatch(qq):
        raise ProberError("请指定有效的 QQ 号。")
    token = lxns_token.strip()
    if source == "lxns" and (not token or not token.isascii() or not token.isprintable()):
        raise ProberError("请联系 Bot 管理员配置落雪开发者令牌（OTOGE_LXNS_TOKEN）。")
    df_path, lx_path, _ = GAME_PATHS[game]
    label = SOURCES[source]
    async with httpx.AsyncClient(timeout=12, transport=transport, follow_redirects=False) as client:
        if source == "divingfish":
            body = {"username": username} if username else {"qq": qq}
            if game == "mai":
                body["b50"] = True
            profile = _mapping(await _json(client, "POST", f"{DIVING_FISH}/{df_path}/query/player", label, json=body), label)
            data = profile
        else:
            headers = {"Authorization": token}
            path = f"{LXNS}/{lx_path}/player"
            profile = _mapping(await _json(client, "GET", f"{path}/qq/{qq}", label, headers=headers), label)
            friend_code = _id(profile.get("friend_code"))
            if not friend_code:
                raise ProberError("落雪没有返回有效好友码。")
            data = _mapping(await _json(client, "GET", f"{path}/{friend_code}/bests", label, headers=headers), label)
    return _best_scores(game, source, data, profile, username or qq)


def _norm(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _song_key(game: str, song_id: int) -> int:
    # 柚子舞萌 DX 谱面的编号比基础曲目编号多 10000，宴会场编号独立。
    return song_id % 10000 if game == "mai" and song_id < 100000 else song_id


def _rows(data, key: str) -> list[dict]:
    if isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ProberError("曲库返回的数据格式异常。")
    return data


class SongCatalog:
    def __init__(self):
        self._cache: dict[str, list[dict]] = {}
        self._refresh_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _load(self, client: httpx.AsyncClient, url: str, key: str, source: str) -> list[dict]:
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            if now >= self._refresh_at.get(url, 0):
                try:
                    rows = _rows(await _json(client, "GET", url, source), key)
                except ProberError:
                    self._refresh_at[url] = now + RETRY_DELAY
                else:
                    self._cache[url] = rows
                    self._refresh_at[url] = now + CATALOG_TTL
            return self._cache.get(url, [])

    async def search(self, game: str, query: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
        if game not in GAMES:
            raise ProberError("请选择舞萌或中二。")
        key = _norm(query)
        if not key or len(key) > 100:
            raise ProberError("请输入 1 到 100 字的曲名、别名或曲目 ID。")
        _, lx_path, yuzu_path = GAME_PATHS[game]
        async with httpx.AsyncClient(timeout=12, transport=transport, headers={"User-Agent": "Mozilla/5.0"}) as client:
            songs, lx_aliases, yuzu_aliases = await asyncio.gather(
                self._load(client, f"{LXNS}/{lx_path}/song/list", "songs", "落雪曲库"),
                self._load(client, f"{LXNS}/{lx_path}/alias/list", "aliases", "落雪别名库"),
                self._load(client, f"{YUZU}/{yuzu_path}/aliases", "aliases", "柚子别名库"),
            )
        if not songs and not yuzu_aliases:
            raise ProberError("曲库暂时不可用，请稍后再试。")
        return _format_song_search(game, key, songs, lx_aliases, yuzu_aliases)


def _format_song_search(game: str, query: str, songs: list[dict], lx_aliases: list[dict], yuzu_aliases: list[dict]) -> str:
    catalog = {}
    for row in songs:
        if _id(row.get("id")) and _text(row.get("title")):
            catalog[_song_key(game, row["id"])] = row
    aliases: dict[int, dict[str, str]] = {}
    for rows in (lx_aliases, yuzu_aliases):
        for row in rows:
            if not _id(row.get("song_id")):
                continue
            sid = _song_key(game, row["song_id"])
            title = _text(row.get("name"))
            if title:
                catalog.setdefault(sid, {"title": title})
            names = row.get("aliases", row.get("alias"))
            if isinstance(names, list):
                entries = aliases.setdefault(sid, {})
                for value in names:
                    name = _text(value, 60)
                    if name:
                        entries.setdefault(_norm(name), name)
    matches = []
    for sid, song in catalog.items():
        title = _text(song["title"])
        names = aliases.get(sid, {})
        normalized = _norm(title)
        if query == str(sid) or query == normalized or query in names:
            rank = 0
        elif query in normalized or any(query in name for name in names):
            rank = 1
        else:
            continue
        matches.append((rank, normalized, sid, song, names))
    if not matches:
        return f"{GAMES[game]}国服曲库里没找到这首歌。"
    lines = [f"{GAMES[game]}国服查歌 · {len(matches)} 首" + ("（显示前 5 首）" if len(matches) > 5 else "")]
    for _, _, sid, song, names in sorted(matches)[:5]:
        details = [_text(song.get("artist")), f"BPM {_number(song.get('bpm'))}" if _number(song.get("bpm")) else ""]
        shown = sorted(names, key=lambda name: query not in name)[:5]
        if shown:
            details.append("别名 " + "、".join(names[name] for name in shown))
        lines.append(f"{_text(song['title'])} #{sid}  " + " · ".join(filter(None, details)))
        charts = song.get("difficulties")
        if isinstance(charts, list):
            charts = {"": charts}
        if isinstance(charts, dict):
            for chart_type, label in (("standard", "STD"), ("dx", "DX"), ("", "谱面")):
                rows = charts.get(chart_type)
                if not isinstance(rows, list):
                    continue
                constants = [_number(row.get("level_value"), ".1f") or _text(row.get("level"), 12)
                             for row in rows[:6] if isinstance(row, dict)]
                if any(constants):
                    lines.append(f"  {label} {' / '.join(value or '—' for value in constants)}")
    return "\n".join(lines)


song_catalog = SongCatalog()
