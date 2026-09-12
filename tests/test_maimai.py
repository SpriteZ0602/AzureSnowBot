"""
tests/test_maimai.py
────────────────────
测试舞萌DX查分模块 plugins/maimai.py:
  - Token 存取（落盘读写/解绑）
  - b50 计算（新桶 top15 + 旧桶 top35 合并排序）
  - 别名模糊搜索（子串、大小写不敏感、多命中）
  - 查分主入口：绑定走本人通道 / 未绑定走公开查询 / 错误文案 / 5 分钟缓存
  - 工具层透传与参数校验
"""

import sys
import os
import json
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Mock nonebot（maimai.py 只依赖 nonebot.log；tools.py 经 memory_search 拉起 llm 校验）
sys.modules.setdefault("nonebot", MagicMock())
sys.modules.setdefault("nonebot.log", MagicMock(logger=MagicMock()))
sys.modules.setdefault("nonebot.adapters", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot", MagicMock())
sys.modules.setdefault("nonebot.adapters.onebot.v11", MagicMock())

_mock_config = MagicMock()
_mock_config.group_whitelist = []
_mock_config.llm_provider = "deepseek"
_mock_config.deepseek_api_key = ""
_mock_config.llm_base_url = ""
_mock_config.llm_model = ""
_mock_driver = MagicMock()
_mock_driver.config = _mock_config
sys.modules["nonebot"].get_driver = MagicMock(return_value=_mock_driver)

import pytest
import plugins.maimai as m
from plugins.local_tools.tools import maimai_player_query, maimai_song_search


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _rec(song_id, title, ra, level="14", achievements=100.0):
    return {"song_id": song_id, "title": title, "ra": ra,
            "level": level, "achievements": achievements}


# ──────────────────── Token 存储 ────────────────────

class TestTokenStore:

    def test_bind_get_unbind(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "TOKENS_PATH", tmp_path / "tokens.json")
        assert m.get_token("123") is None

        m.bind_token("123", "tok-abc")
        assert m.get_token("123") == "tok-abc"

        assert m.unbind_token("123") is True
        assert m.get_token("123") is None
        assert m.unbind_token("123") is False

    def test_persist_across_loads(self, tmp_path, monkeypatch):
        monkeypatch.setattr(m, "TOKENS_PATH", tmp_path / "tokens.json")
        m.bind_token("123", "tok-abc")
        # 模拟重新加载（重新读文件）
        assert m.get_token("123") == "tok-abc"
        content = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
        assert content["123"]["token"] == "tok-abc"


# ──────────────────── b50 计算 ────────────────────

class TestComputeB50:

    def test_merge_top15_top35(self):
        new = [_rec(10000 + i, f"new{i}", 100 + i) for i in range(20)]   # ra 100..119
        old = [_rec(i, f"old{i}", 1 + i) for i in range(40)]             # ra 1..40
        b50 = m.compute_b50(new, old)

        assert len(b50) == 50
        assert b50[0]["ra"] == 119                       # 降序
        new_ras = {r["ra"] for r in b50 if r["is_new"]}
        old_ras = {r["ra"] for r in b50 if not r["is_new"]}
        assert new_ras == set(range(105, 120))           # 新桶 top15
        assert old_ras == set(range(6, 41))              # 旧桶 top35

    def test_fewer_than_capacity(self):
        new = [_rec(1, "a", 300)]
        old = [_rec(2, "b", 200), _rec(3, "c", 100)]
        b50 = m.compute_b50(new, old)
        assert [r["ra"] for r in b50] == [300, 200, 100]


class TestFormatRecords:

    def test_line_shape(self):
        recs = [_rec(8, "True Love Song", 260, level="13+", achievements=99.7107)]
        recs[0]["is_new"] = True
        out = m.format_records(recs)
        assert " 1. True Love Song [13+]* 99.7107% ra=260" in out

    def test_long_title_truncated(self):
        out = m.format_records([_rec(1, "x" * 40, 100)])
        assert len(out.split(". ", 1)[1].split(" [")[0]) <= 24


# ──────────────────── 别名搜索 ────────────────────

FAKE_TABLE = [
    {"song_id": 11920, "name": "Hurtling Boys",
     "alias": ["hurtling boys", "神之右手", "大手", "hb", "猛冲男孩", "鸟屎"]},
    {"song_id": 11789, "name": "Abstruse Dilemma",
     "alias": ["abstruse dilemma", "ad", "鸟屎2", "摩耶"]},
    {"song_id": 8, "name": "True Love Song",
     "alias": ["true love song", "会员制餐厅", "真爱", "糖糖"]},
]


@pytest.fixture
def fake_alias_table(monkeypatch):
    async def _get():
        return FAKE_TABLE
    monkeypatch.setattr(m, "get_alias_table", _get)


class TestSearchSongs:

    async def test_substring_multi_hit(self, fake_alias_table):
        hits = await m.search_songs("鸟屎")
        assert {h["song_id"] for h in hits} == {11920, 11789}

    async def test_case_insensitive(self, fake_alias_table):
        hits = await m.search_songs("HB")
        assert [h["song_id"] for h in hits] == [11920]

    async def test_name_matched(self, fake_alias_table):
        hits = await m.search_songs("true love")
        assert [h["song_id"] for h in hits] == [8]

    async def test_no_hit(self, fake_alias_table):
        assert await m.search_songs("不存在的东西") == []

    async def test_empty_keyword(self, fake_alias_table):
        assert await m.search_songs("  ") == []


class TestFormatSongs:

    def test_multi_hit(self):
        hits = [{"song_id": 11920, "name": "Hurtling Boys",
                 "alias": ["a", "b", "c", "d", "e", "f", "g", "h"]}]
        out = m.format_songs(hits)
        assert "找到 1 首" in out
        assert "Hurtling Boys（song_id=11920" in out
        assert "…" in out                                # 别名截断

    def test_empty(self):
        assert "没有" in m.format_songs([])


# ──────────────────── 查分主入口 ────────────────────

class TestQueryPlayerB50:

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        m._query_cache.clear()
        yield
        m._query_cache.clear()

    async def test_non_digit_qq(self):
        assert "QQ号必须是数字" in await m.query_player_b50("chaos")

    async def test_bound_token_uses_self_channel(self, monkeypatch):
        monkeypatch.setattr(m, "get_token", lambda qq: "tok-xyz")
        monkeypatch.clear = None

        async def fake_fetch(token, *, is_new=None):
            assert token == "tok-xyz"
            if is_new is True:
                return {"nickname": "测试", "additional_rating": 10,
                        "records": [_rec(1, "新歌", 400), _rec(2, "新歌2", 390)]}
            return {"nickname": "测试", "additional_rating": 10,
                    "records": [_rec(3, "旧歌", 350), _rec(4, "旧歌2", 100)]}

        monkeypatch.setattr(m, "fetch_records", fake_fetch)
        out = await m.query_player_b50("373900859")
        # rating = 10 + (400+390) + (350+100) = 1250
        assert "rating=1250" in out
        assert "tok-xyz" not in out                       # Token 绝不回显
        assert out.splitlines()[1].startswith(" 1. 新歌 [14]*")  # ra=400 排第一且标新

    async def test_unbound_public_query(self, monkeypatch):
        monkeypatch.setattr(m, "get_token", lambda qq: None)
        calls = []

        async def fake_post(url, payload, headers=None):
            calls.append(url)
            return FakeResp(200, {"nickname": "路人", "rating": 9000,
                                  "records": [_rec(1, "歌", 300)]})

        monkeypatch.setattr(m, "_post_json", fake_post)
        out = await m.query_player_b50("111")
        assert "rating=9000" in out and "路人" in out
        assert calls == [f"{m.DF_BASE}/query/player"]

    async def test_unbound_400(self, monkeypatch):
        monkeypatch.setattr(m, "get_token", lambda qq: None)

        async def fake_post(url, payload, headers=None):
            return FakeResp(400, {"message": "user not exists"})

        monkeypatch.setattr(m, "_post_json", fake_post)
        out = await m.query_player_b50("111")
        assert "没在查分器" in out

    async def test_unbound_403(self, monkeypatch):
        monkeypatch.setattr(m, "get_token", lambda qq: None)

        async def fake_post(url, payload, headers=None):
            return FakeResp(403, {"message": "已设置隐私"})

        monkeypatch.setattr(m, "_post_json", fake_post)
        out = await m.query_player_b50("111")
        assert "隐私" in out

    async def test_query_cache(self, monkeypatch):
        monkeypatch.setattr(m, "get_token", lambda qq: None)
        n_calls = 0

        async def fake_post(url, payload, headers=None):
            nonlocal n_calls
            n_calls += 1
            return FakeResp(200, {"nickname": "路人", "rating": 9000,
                                  "records": [_rec(1, "歌", 300)]})

        monkeypatch.setattr(m, "_post_json", fake_post)
        m._query_cache.clear()
        await m.query_player_b50("222")
        await m.query_player_b50("222")
        assert n_calls == 1                               # 第二次命中 5 分钟缓存


class TestVerifyToken:

    async def test_invalid(self, monkeypatch):
        async def fake_fetch(token, *, is_new=None):
            raise m.MaimaiError("Token 无效或已过期")

        monkeypatch.setattr(m, "fetch_records", fake_fetch)
        assert await m.verify_token("bad") is None

    async def test_valid(self, monkeypatch):
        async def fake_fetch(token, *, is_new=None):
            return {"username": "雪碧", "rating": 15958, "records": [_rec(1, "x", 1)]}

        monkeypatch.setattr(m, "fetch_records", fake_fetch)
        info = await m.verify_token("good")
        assert info == {"username": "雪碧", "rating": 15958}


# ──────────────────── 工具层 ────────────────────

class TestTools:

    async def test_player_query_non_digit(self):
        assert "QQ号必须是数字" in await maimai_player_query(qq="abc")

    async def test_player_query_passthrough(self, monkeypatch):
        async def fake_query(qq):
            return f"b50 of {qq}"

        monkeypatch.setattr(m, "query_player_b50", fake_query)
        assert await maimai_player_query(qq="123") == "b50 of 123"

    async def test_song_search_empty(self):
        out = await maimai_song_search(song_name="  ")
        assert out.startswith("[错误]")

    async def test_song_search_passthrough(self, monkeypatch):
        async def fake_search(kw):
            return [{"song_id": 1, "name": "X", "alias": ["x"]}]

        monkeypatch.setattr(m, "search_songs", fake_search)
        out = await maimai_song_search(song_name="x")
        assert "找到 1 首" in out
