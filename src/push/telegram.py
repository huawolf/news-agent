"""Telegram 推送平台"""

import os
from typing import Dict

import aiohttp

from .base import PushPlatform

TELEGRAM_API = "https://api.telegram.org"


class TelegramPlatform(PushPlatform):
    """Telegram Bot 推送（bot token + chat id）"""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.api_key_name = config.get("apiKeyName", "TELEGRAM_BOT_TOKEN")
        self.bot_token = os.environ.get(self.api_key_name, "").strip()
        self.chat_id = str(config.get("chatId") or "").strip()

    def validate_config(self, config: Dict) -> bool:
        """检查 Telegram 配置是否有效"""
        if not config.get("enabled", False):
            return False
        api_key_name = config.get("apiKeyName", "TELEGRAM_BOT_TOKEN")
        return bool(os.environ.get(api_key_name, "").strip() and str(config.get("chatId") or "").strip())

    async def send(self, content: str, title: str = None, metadata: Dict = None):
        """发送到 Telegram（忽略 metadata）"""
        full_content = f"{title}\n\n{content}" if title else content
        chunks = self._split_content(full_content, limit=4000)

        timeout = aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 30)))
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for chunk in chunks:
                # Plain text on purpose: digest markdown regularly contains
                # unbalanced `*` / `_` / backticks, and Telegram rejects the
                # whole message with "can't parse entities" when parse_mode is
                # set. Delivery beats formatting.
                payload = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }
                async with session.post(url, json=payload) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status != 200 or not (isinstance(data, dict) and data.get("ok")):
                        detail = data.get("description") if isinstance(data, dict) else data
                        raise RuntimeError(f"Telegram推送失败: {resp.status} - {detail}")

    def _split_content(self, content: str, limit: int = 4000) -> list:
        """Telegram 单条消息限制 4096 字符，留出余量后分割"""
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
