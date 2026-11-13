"""音游查分的数据形状、第三方隐私和曲库退化路径。"""

import asyncio
from io import BytesIO

import httpx
import pytest
from PIL import Image, ImageFont

from plugins.otoge import prober, render


@pytest.mark.parametrize("payload, expected_groups", [
    ({"charts": {"sd": [{"title": "Old", "type": "DX"}], "dx": [{"title": "New", "type": "SD"}]}},
     ["B35", "B15"]),
    ({"records": [{"title": "Flat", "type": "DX"}]}, ["B50"]),
    ({"records": [{"title": "New", "type": "SD", "is_new": True}]}, ["B15"]),
    ({"records": []}, []),
])
async def test_waterfish_maimai_public_shapes(payload, expected_groups):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/maimaidxprober/query/player"
        assert request.read() == b'{"qq":"123456789","b50":true}'
        assert "authorization" not in request.headers and "import-token" not in request.headers
        return httpx.Response(200, json={"nickname": "Player [CQ:at,qq=all]", "rating": 16000, **payload})

    result = await prober.query_best_scores("mai", qq="123456789", transport=httpx.MockTransport(handler))
    assert [name for name, _ in result.groups] == expected_groups
    assert result.rating == "16000"
    assert "[CQ:" not in result.text()
    if not expected_groups:
        assert "未返回公开成绩明细" in result.text()


@pytest.mark.parametrize("bucket, title", [("n20", "N20"), ("r10", "R10")])
async def test_waterfish_chunithm_current_and_legacy_buckets(bucket, title):
    def handler(request):
        assert request.url.path == "/api/chunithmprober/query/player"
        assert request.read() == b'{"username":"player"}'
        row = {"title": "A song", "level": "14+", "level_index": 3, "score": 1009876, "ra": 16.2}
        return httpx.Response(200, json={"rating": 16.5, "records": {"b30": [row], bucket: [row]}})

    result = await prober.query_best_scores("chu", username="player", transport=httpx.MockTransport(handler))
    assert [label for label, _ in result.groups] == ["B30", title]
    assert "1,009,876  RA 16.20" in result.text()


@pytest.mark.parametrize("privacy", [False, True])
async def test_lxns_respects_score_permissions(privacy):
    paths = []

    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "test-dev-token"
        assert request.url.host == "maimai.lxns.net"
        paths.append(request.url.path)
        if len(paths) == 1:
            return httpx.Response(200, json={"success": True, "code": 200, "data": {"friend_code": 42, "rating": 16000}})
        if privacy:
            return httpx.Response(200, json={"success": False, "code": 403, "message": "do not echo upstream text"})
        row = {"song_name": "A song", "type": "dx", "achievements": 100.1234, "dx_rating": 310.9}
        return httpx.Response(200, json={"success": True, "code": 200, "data": {"standard": [row], "dx": []}})

    args = {"qq": "123456789", "lxns_token": "test-dev-token", "transport": httpx.MockTransport(handler)}
    if privacy:
        with pytest.raises(prober.ProberError, match="隐私设置"):
            await prober.query_best_scores("mai", "lxns", **args)
    else:
        result = await prober.query_best_scores("mai", "lxns", **args)
        assert "100.1234%  RA 310" in result.text()
    assert paths == ["/api/v0/maimai/player/qq/123456789", "/api/v0/maimai/player/42/bests"]


@pytest.mark.parametrize("kwargs, error", [
    ({"qq": "１２３４５"}, "QQ 号"),
    ({"qq": "123456789", "username": "player"}, "只填一个"),
    ({"qq": "123456789", "source": "lxns"}, "OTOGE_LXNS_TOKEN"),
])
async def test_invalid_lookup_stays_local(kwargs, error):
    def unexpected(request):
        pytest.fail("invalid parameters reached the network")

    with pytest.raises(prober.ProberError, match=error):
        await prober.query_best_scores("mai", transport=httpx.MockTransport(unexpected), **kwargs)


async def test_catalog_merges_aliases_and_reuses_data_when_refresh_fails(monkeypatch):
    calls = []
    now = [1000.0]
    monkeypatch.setattr(prober, "time", type("Clock", (), {"monotonic": staticmethod(lambda: now[0])}))

    def handler(request):
        calls.append(request.url.path)
        if now[0] > 1000:
            return httpx.Response(503)
        if request.url.path.endswith("/song/list"):
            return httpx.Response(200, json={"songs": [{"id": 8, "title": "True Love Song", "artist": "Kai",
                "difficulties": {"dx": [{"level_value": 13.7}]}}]})
        if request.url.path.endswith("/alias/list"):
            return httpx.Response(200, json={"aliases": [{"song_id": 8, "aliases": ["糖糖"]}]})
        return httpx.Response(200, json=[{"song_id": 10008, "name": "True Love Song", "alias": ["测试外号", "糖糖"]}])

    catalog = prober.SongCatalog()
    transport = httpx.MockTransport(handler)
    first, second = await asyncio.gather(*(catalog.search("mai", "测试外号", transport=transport) for _ in range(2)))
    assert first == second and "True Love Song #8" in first
    assert "DX 13.7" in first and first.count("糖糖") == 1
    assert len(calls) == 3
    now[0] += prober.CATALOG_TTL
    assert await catalog.search("mai", "测试外号", transport=transport) == first
    assert await catalog.search("mai", "测试外号", transport=transport) == first
    assert len(calls) == 6


def test_render_best_scores_produces_jpeg_and_falls_back_without_font(monkeypatch):
    font = ImageFont.load_default()
    monkeypatch.setattr(render.ImageFont, "truetype", lambda *_: font)
    row = prober.Score("A song", "MAS 14", "100.0000%", "300", "FC")
    scores = prober.BestScores("mai", "lxns", "Player", "16000", (("B35", (row,)),))
    with Image.open(BytesIO(render.render_best_scores(scores))) as image:
        assert image.format == "JPEG" and image.width == 1000

    def missing(*_):
        raise OSError("missing font")

    monkeypatch.setattr(render.ImageFont, "truetype", missing)
    assert render.render_best_scores(scores) is None
