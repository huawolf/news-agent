"""自定义 API 推送平台"""
import os
from typing import Dict, Optional
import aiohttp
from .base import PushPlatform


class CustomPlatform(PushPlatform):
    """自定义 API 推送平台"""

    def validate_config(self, config: Dict) -> bool:
        """验证配置"""
        if not config.get("enabled", False):
            return False

        api_key_name = config.get("apiKeyName")
        token_key_name = config.get("tokenKeyName")

        if not api_key_name or not token_key_name:
            print("❌ Custom platform configuration is missing apiKeyName or tokenKeyName")
            return False

        url = os.getenv(api_key_name)
        token = os.getenv(token_key_name)

        if not url or not token:
            print(f"❌ Environment variable {api_key_name} or {token_key_name} is not set")
            return False

        return True

    async def send(self, content: str, title: str = None, metadata: Optional[Dict] = None):
        """发送到自定义 API"""
        api_key_name = self.config.get("apiKeyName")
        token_key_name = self.config.get("tokenKeyName")

        url = os.getenv(api_key_name)
        token = os.getenv(token_key_name)

        payload = {
            "content": content,
            "metadata": metadata
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=int(self.config.get("request_timeout", 30)))
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"API returned an error {resp.status}: {error_text}")
