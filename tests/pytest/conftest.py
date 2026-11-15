"""pytest fixtures for news-agent project"""

import pytest
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def temp_dir(tmp_path):
    """临时目录fixture"""
    return tmp_path


@pytest.fixture
def sample_config():
    """示例配置"""
    return {
        "mode": "slaver",
        "server_url": "",
        "database": {"url": "", "schema": "news_agent", "sqlite_path": "data/news_agent.db"},
        "personal_preferences": "Track material investment signals.",
        "filter": {
            "min_score": 60,
        },
        "schedule": {
            "fetch_interval_minutes": 30,
        },
        "delivery": {
            "timezone": "Asia/Shanghai",
            "schedules": [{"id": "test", "cron": "0 8 * * *"}],
        },
        "llm": {
            "model": "gpt-4o-mini",
            "baseUrl": "https://api.openai.com/v1",
            "apiKeyName": "OPENAI_API_KEY",
            "max_prompt_chars": 10000,
            "max_concurrent_batches": 3,
        },
        "push": {
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/test/abc",
            },
            "feishu": {"enabled": False, "apiKeyName": "FEISHU_WEBHOOK_URL"},
        },
    }


@pytest.fixture
def sample_entries():
    """示例新闻条目列表"""
    now = datetime.now(timezone.utc)
    return [
        {
            "title": "Article 1",
            "link": "https://example.com/1",
            "published": now.isoformat(),
            "fetched_at": now.isoformat(),
            "source": "Source1",
            "content": "Content 1",
            "summary": "Summary 1",
            "score": 85,
        },
        {
            "title": "Article 2",
            "link": "https://example.com/2",
            "published": (now - timedelta(hours=1)).isoformat(),
            "fetched_at": now.isoformat(),
            "source": "Source2",
            "content": "Content 2",
            "summary": "Summary 2",
            "score": 70,
        },
        {
            "title": "Article 3",
            "link": "https://example.com/3",
            "published": (now - timedelta(hours=2)).isoformat(),
            "fetched_at": now.isoformat(),
            "source": "Source3",
            "content": "Content 3",
            "summary": "Summary 3",
            "score": 55,
        },
    ]
