"""测试 HN enrich(Algolia 评论树 + 外链正文)"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.hackernews.item_enricher import enrich_story


def _kwargs(**overrides):
    base = dict(
        top_comments=3,
        top_l2_per_l1=2,
        comment_max_chars=500,
        comments_total_chars=60000,
        link_content_max_chars=3000,
        algolia_base="https://hn.algolia.com/api/v1",
        timeout=10,
    )
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_enrich_external_link_story_returns_tree():
    story = {
        "id": "111",
        "title": "T",
        "url": "https://example.com/post",
        "site": "example.com",
        "points": 100,
        "comments": 5,
        "comments_url": "https://news.ycombinator.com/item?id=111",
    }
    algolia_payload = {
        "text": None,
        "children": [
            {
                "text": "<p>comment one</p>",
                "children": [
                    {"text": "<p>reply 1a</p>"},
                    {"text": "<p>reply 1b</p>"},
                    {"text": "<p>reply 1c (should be dropped)</p>"},
                ],
            },
            {"text": "<p>comment two</p>", "children": []},
            {"text": "<p>comment three</p>"},
            {"text": "<p>comment four (over top_comments cap)</p>"},
        ],
    }

    async def fake_algolia(session, item_id, **kw):
        return algolia_payload

    async def fake_external(session, url, **kw):
        return "link body"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_external_markdown",
        new=AsyncMock(side_effect=fake_external),
    ):
        enriched = await enrich_story(
            session=MagicMock(), story=story, **_kwargs()
        )

    tree = enriched["top_comments"]
    assert len(tree) == 3
    assert "comment one" in tree[0]["l1"]
    assert len(tree[0]["replies"]) == 2
    assert "reply 1a" in tree[0]["replies"][0]
    assert "reply 1b" in tree[0]["replies"][1]
    assert tree[1]["replies"] == []
    assert tree[2]["replies"] == []
    assert "link body" in enriched["link_content"]


@pytest.mark.asyncio
async def test_enrich_show_hn_uses_root_text_no_external_fetch():
    story = {
        "id": "222",
        "title": "Show HN: T",
        "url": "https://news.ycombinator.com/item?id=222",
        "site": "",
        "points": 200,
        "comments": 10,
        "comments_url": "https://news.ycombinator.com/item?id=222",
    }
    algolia_payload = {
        "text": "<p>post body text</p>",
        "children": [{"text": "<p>c1</p>"}],
    }
    link_calls = []

    async def fake_algolia(session, item_id, **kw):
        return algolia_payload

    async def fake_external(session, url, **kw):
        link_calls.append(url)
        return "should not be called"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_external_markdown",
        new=AsyncMock(side_effect=fake_external),
    ):
        enriched = await enrich_story(
            session=MagicMock(), story=story, **_kwargs()
        )

    assert link_calls == []
    assert "post body text" in enriched["link_content"]
    assert enriched["top_comments"][0]["l1"].startswith("c1") or "c1" in enriched["top_comments"][0]["l1"]


@pytest.mark.asyncio
async def test_enrich_truncates_comments_and_link():
    story = {
        "id": "333",
        "title": "T",
        "url": "https://example.com/a",
        "site": "example.com",
        "points": 100,
        "comments": 2,
        "comments_url": "x",
    }
    long_comment = "<p>" + ("y" * 2000) + "</p>"
    long_reply = "<p>" + ("z" * 2000) + "</p>"
    long_link = "z" * 5000

    async def fake_algolia(session, item_id, **kw):
        return {
            "text": None,
            "children": [
                {"text": long_comment, "children": [{"text": long_reply}]}
            ],
        }

    async def fake_external(session, url, **kw):
        return long_link

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_external_markdown",
        new=AsyncMock(side_effect=fake_external),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            **_kwargs(comment_max_chars=100, link_content_max_chars=200),
        )

    assert len(enriched["top_comments"][0]["l1"]) <= 100
    assert len(enriched["top_comments"][0]["replies"][0]) <= 100
    assert len(enriched["link_content"]) <= 200


@pytest.mark.asyncio
async def test_enrich_failure_returns_partial():
    story = {
        "id": "444",
        "title": "T",
        "url": "https://example.com/x",
        "site": "example.com",
        "points": 100,
        "comments": 2,
        "comments_url": "x",
    }

    async def fake_algolia(session, item_id, **kw):
        raise RuntimeError("algolia down")

    async def fake_external(session, url, **kw):
        return "ok"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_external_markdown",
        new=AsyncMock(side_effect=fake_external),
    ):
        enriched = await enrich_story(
            session=MagicMock(), story=story, **_kwargs()
        )

    assert enriched["top_comments"] == []
    assert "ok" in enriched["link_content"]


@pytest.mark.asyncio
async def test_enrich_total_budget_stops_early():
    """累计字符达 comments_total_chars 立即停止,后续 L1 / L2 都不再加入"""
    story = {
        "id": "555",
        "title": "T",
        "url": "https://example.com/q",
        "site": "example.com",
        "points": 100,
        "comments": 5,
        "comments_url": "x",
    }
    big = "<p>" + ("a" * 500) + "</p>"  # markdown 约 500 chars

    async def fake_algolia(session, item_id, **kw):
        return {
            "text": None,
            "children": [{"text": big, "children": [{"text": big}, {"text": big}]} for _ in range(10)],
        }

    async def fake_external(session, url, **kw):
        return "x"

    with patch(
        "src.sections.hackernews.item_enricher._fetch_algolia_item",
        new=AsyncMock(side_effect=fake_algolia),
    ), patch(
        "src.sections.hackernews.item_enricher._fetch_external_markdown",
        new=AsyncMock(side_effect=fake_external),
    ):
        enriched = await enrich_story(
            session=MagicMock(),
            story=story,
            **_kwargs(
                top_comments=10,
                top_l2_per_l1=2,
                comment_max_chars=500,
                comments_total_chars=1500,
            ),
        )

    tree = enriched["top_comments"]
    total = sum(len(n["l1"]) + sum(len(r) for r in n["replies"]) for n in tree)
    # 累计应该在 1500 附近停下(允许多收一条到 ~2000),不应该收全 10*3=30 条
    assert total <= 2000
    assert len(tree) < 10
