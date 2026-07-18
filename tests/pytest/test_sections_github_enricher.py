"""测试 GitHub REST API enrich 字段映射、archived 过滤、token 鉴权头"""

import base64
import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.sections.github.repo_enricher import enrich_repo, _auth_headers


def test_auth_headers_with_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    headers = _auth_headers(token_env="GITHUB_TOKEN")
    assert headers["Authorization"] == "Bearer ghp_secret"
    assert headers["Accept"] == "application/vnd.github+json"


def test_auth_headers_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    headers = _auth_headers(token_env="GITHUB_TOKEN")
    assert "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_enrich_repo_merges_metadata_and_readme():
    readme_body = "# Title\n\nProject description here."
    readme_b64 = base64.b64encode(readme_body.encode("utf-8")).decode("ascii")

    metadata_payload = {
        "description": "real desc",
        "topics": ["llm", "rag"],
        "license": {"spdx_id": "MIT"},
        "pushed_at": "2026-05-16T10:00:00Z",
        "archived": False,
    }
    readme_payload = {"content": readme_b64, "encoding": "base64"}

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return readme_payload
        return metadata_payload

    base = {
        "url": "https://github.com/o/r",
        "full_name": "o/r",
        "description": "from trending",
        "language": "Python",
        "stars_today": 100,
        "stars_total": 5000,
    }

    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        enriched = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=200
        )

    assert enriched["topics"] == ["llm", "rag"]
    assert enriched["license"] == "MIT"
    assert enriched["pushed_at"] == "2026-05-16T10:00:00Z"
    assert "Project description" in enriched["readme_excerpt"]
    assert enriched["stars_today"] == 100  # trending 已有字段保留


@pytest.mark.asyncio
async def test_enrich_repo_returns_none_when_archived():
    metadata_payload = {"archived": True, "topics": [], "pushed_at": "x"}

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return {"content": ""}
        return metadata_payload

    base = {"url": "https://github.com/o/r", "full_name": "o/r"}
    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        result = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=200
        )
    assert result is None


@pytest.mark.asyncio
async def test_enrich_repo_truncates_readme():
    readme_body = "x" * 5000
    readme_b64 = base64.b64encode(readme_body.encode("utf-8")).decode("ascii")

    async def fake_get_json(session, url, **kwargs):
        if url.endswith("/readme"):
            return {"content": readme_b64, "encoding": "base64"}
        return {"archived": False, "topics": [], "pushed_at": "p"}

    base = {"url": "https://github.com/o/r", "full_name": "o/r"}
    with patch(
        "src.sections.github.repo_enricher._get_json", new=AsyncMock(side_effect=fake_get_json)
    ):
        enriched = await enrich_repo(
            session=MagicMock(), repo=base, token_env="GITHUB_TOKEN", readme_max_chars=100
        )
    assert len(enriched["readme_excerpt"]) == 100
