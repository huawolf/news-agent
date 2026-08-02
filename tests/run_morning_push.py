"""模拟一次完整早报推送(强制 is_morning=True,但不发送到推送渠道)"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import load_config
from src.main import _run_morning_push


async def main():
    config = load_config()
    async def fake_send(content, push_cfg):
        print("\n" + "=" * 60)
        print("📤 假推送内容(实际不会发送)")
        print("=" * 60)
        print(content)

    with patch("src.main.send_to_platforms", new=AsyncMock(side_effect=fake_send)):
        await _run_morning_push(config)


if __name__ == "__main__":
    asyncio.run(main())
