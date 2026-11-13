"""把国服查分与查歌接入现有 Agent 工具链。"""

from nonebot import get_driver

from botcore.tools import ToolResult
from ..local_tools.manager import register_tool
from .prober import GAMES, SOURCES, ProberError, query_best_scores, song_catalog


@register_tool(
    name="otoge_best_scores",
    description=(
        "查舞萌DX或中二节奏国服的公开 Rating 和最佳成绩。"
        "查本人时省略账号；查他人时使用明确的 QQ 号（可用 get_group_members 核对）或水鱼用户名。"
        "默认查水鱼，用户指定落雪时传 lxns。需要用户明确提出查分，禁止索取密码或导入 Token。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "game": {"type": "string", "enum": list(GAMES), "description": "mai 舞萌 DX；chu 中二节奏"},
            "source": {"type": "string", "enum": list(SOURCES), "description": "divingfish 水鱼（默认）；lxns 落雪"},
            "qq": {"type": "string", "description": "要查询的 QQ 号。查本人时可省略"},
            "username": {"type": "string", "description": "水鱼用户名，可代替 QQ 号"},
        },
        "required": ["game"],
    },
)
async def otoge_best_scores(game: str = "", source: str = "divingfish", qq: str = "", username: str = "",
                            _context: dict | None = None) -> ToolResult:
    if not all(isinstance(value, str) for value in (game, source, qq, username)):
        return ToolResult("[错误] 游戏、查分器和账号参数应为文字。", ok=False)
    qq, username = qq.strip(), username.strip()
    if not qq and not username:
        qq = str((_context or {}).get("_user_id") or "")
    token = str(getattr(get_driver().config, "otoge_lxns_token", "") or "") if source == "lxns" else ""
    try:
        result = await query_best_scores(game, source, qq=qq, username=username, lxns_token=token)
    except ProberError as exc:
        return ToolResult(f"[错误] {exc}", ok=False)
    return ToolResult(result.text())


@register_tool(
    name="otoge_song_search",
    description="查舞萌DX或中二节奏国服曲目，支持曲名、别名或曲目 ID，返回国服谱面定数。",
    parameters={
        "type": "object",
        "properties": {
            "game": {"type": "string", "enum": list(GAMES), "description": "mai 舞萌 DX；chu 中二节奏"},
            "query": {"type": "string", "description": "曲名、别名或曲目 ID"},
        },
        "required": ["game", "query"],
    },
)
async def otoge_song_search(game: str = "", query: str = "", **kwargs) -> ToolResult:
    if not isinstance(game, str) or not isinstance(query, str):
        return ToolResult("[错误] 游戏和查歌关键词应为文字。", ok=False)
    try:
        return ToolResult(await song_catalog.search(game, query))
    except ProberError as exc:
        return ToolResult(f"[错误] {exc}", ok=False)
