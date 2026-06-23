"""推送平台基类"""
from abc import ABC, abstractmethod
from typing import Dict


class PushPlatform(ABC):
    """推送平台抽象基类"""

    def __init__(self, config: Dict):
        self.config = config

    @abstractmethod
    def validate_config(self, config: Dict) -> bool:
        """验证配置是否有效"""
        pass

    @abstractmethod
    async def send(self, content: str, title: str = None, metadata: Dict = None):
        """发送内容

        Args:
            content: 正文内容
            title: 标题（可选，兼容旧接口）
            metadata: 元信息（可选，新增参数）
        """
        pass
