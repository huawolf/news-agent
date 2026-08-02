"""测试 run_push_job 的早报/默认路径分发"""

import sys
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.main import run_push_job


@pytest.mark.asyncio
async def test_default_path_when_not_morning(sample_config):
    sample_config["schedule"]["push_cron"] = ["0 8 * * *", "0 17 * * *"]
    sample_config["filter"]["push_context_days"] = 5

    with patch("src.main.is_morning_push", return_value=False), patch(
        "src.main._run_default_push", new=AsyncMock(return_value=None)
    ) as default_path, patch(
        "src.main._run_morning_push", new=AsyncMock(return_value=None)
    ) as morning_path:
        await run_push_job(sample_config)

    default_path.assert_awaited_once()
    morning_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_path_when_morning(sample_config):
    sample_config["schedule"]["push_cron"] = ["0 8 * * *", "0 17 * * *"]
    sample_config["filter"]["push_context_days"] = 5

    with patch("src.main.is_morning_push", return_value=True), patch(
        "src.main._run_default_push", new=AsyncMock(return_value=None)
    ) as default_path, patch(
        "src.main._run_morning_push", new=AsyncMock(return_value=None)
    ) as morning_path:
        await run_push_job(sample_config)

    morning_path.assert_awaited_once()
    default_path.assert_not_awaited()
