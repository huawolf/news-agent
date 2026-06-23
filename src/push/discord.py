"""Discord推送平台"""

import os
from typing import Dict

import aiohttp

from .base import PushPlatform


class DiscordPlatform(PushPlatform):
    """Discord Webhook推送"""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.api_key_name = config.get("apiKeyName", "DISCORD_WEBHOOK_URL")
        self.webhook_url = os.environ.get(self.api_key_name, "")

    def validate_config(self, config: Dict) -> bool:
        """检查Discord配置是否有效"""
        if not config.get("enabled", False):
            return False
        api_key_name = config.get("apiKeyName", "DISCORD_WEBHOOK_URL")
        webhook = os.environ.get(api_key_name, "")
        return bool(webhook and webhook.startswith("https://discord.com/api/webhooks/"))

    async def send(self, content: str, title: str = None, metadata: Dict = None):
        """发送到Discord（忽略 metadata）"""
        full_content = f"# {title}\n\n{content}" if title else content
        chunks = self._split_content(full_content, limit=2000)

        async with aiohttp.ClientSession(trust_env=True) as session:
            for chunk in chunks:
                payload = {"content": chunk}
                async with session.post(self.webhook_url, json=payload) as resp:
                    if resp.status != 204:
                        text = await resp.text()
                        raise RuntimeError(f"Discord推送失败: {resp.status} - {text}")

    def _split_content(self, content: str, limit: int = 2000) -> list:
        """Discord限制2000字符，需要分割"""
        if len(content) <= limit:
            return [content]

        chunks = []
        lines = content.split("\n")
        current = ""

        for line in lines:
            if len(current) + len(line) + 1 > limit:
                if current:
                    chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line

        if current:
            chunks.append(current)

        return chunks
