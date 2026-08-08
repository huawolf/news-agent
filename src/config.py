"""配置加载和源管理"""
import fnmatch
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

from src.source_categories import normalize_source_category


def _get_local_timezone() -> timezone:
    """自动检测本地时区"""
    return datetime.now().astimezone().tzinfo


def get_timezone(config: Dict = None) -> timezone:
    """
    获取配置时区，用于推送消息展示本地化时间
    读取信息源统一使用 UTC 时间
    如果 config 中没有 timezone_hours，则自动检测本地时区
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return _get_local_timezone()

    hours = config.get("schedule", {}).get("timezone_hours")
    if hours is None:
        return _get_local_timezone()

    return timezone(timedelta(hours=hours))


def get_server_api_token(config: Dict) -> str:
    """Return the shared-news API value without consulting local control auth."""
    settings = config.get("mode_settings", {})
    return str(
        settings.get("server_api_token")  # Legacy configuration.
        or settings.get("server_api_token_name")
        or "processednews"
    ).strip()


# 向后兼容的别名
def get_cst(config: Dict = None) -> timezone:
    """向后兼容，使用 get_timezone"""
    return get_timezone(config)


def load_config(config_path: str = "config.json") -> Dict:
    """加载配置文件"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        config = json.load(f)

    config["personal_preferences"] = str(config.get("personal_preferences", "")).strip()
    config.setdefault("llm", {})["output_language"] = config.get(
        "output_language", "en"
    )
    config["llm"]["personal_preferences"] = config["personal_preferences"]

    # Server-Client mode settings
    mode_settings = config.setdefault("mode_settings", {})
    mode_settings.setdefault("mode", "client")
    mode_settings.setdefault("server_url", "http://13.158.182.33:12301")
    mode_settings.setdefault("server_api_token_name", "processednews")

    return config



def parse_opml(opml_path: str) -> List[Dict]:
    """解析OPML文件获取订阅源列表"""
    if not opml_path or not str(opml_path).strip():
        return []
    path = Path(opml_path)
    if not path.exists() or path.is_dir():
        return []

    tree = ET.parse(path)
    root = tree.getroot()

    feeds = []
    for outline in root.findall(".//outline[@type='rss']"):
        title = outline.get("title", "")
        url = outline.get("xmlUrl", "")
        feeds.append({
            "title": title,
            "xmlUrl": url,
            "category": normalize_source_category(
                outline.get("category"), title=title, url=url
            ),
        })

    return feeds


def merge_sources(sources_config: Dict) -> List[Dict]:
    """合并base_opml + add - block，以xmlUrl为key去重"""
    # 1. 解析base OPML
    base = parse_opml(sources_config.get("base_opml", ""))

    # 2. 添加自定义源
    add_list = sources_config.get("add", [])
    all_sources = base + add_list

    # 3. 应用block (以xmlUrl匹配)
    block_list = sources_config.get("block", [])
    block_urls = {b.get("xmlUrl", "") for b in block_list}
    filtered = [s for s in all_sources if s.get("xmlUrl", "") not in block_urls]

    # 4. 应用block_domains (域名屏蔽，支持通配符 *.substack.com)
    block_domains = sources_config.get("block_domains", [])
    if block_domains:
        def is_domain_blocked(url: str) -> bool:
            try:
                domain = urlparse(url).netloc.lower()
                for pattern in block_domains:
                    # 转换通配符模式为匹配格式
                    if pattern.startswith("*."):
                        # *.substack.com 匹配 substack.com 和 addyo.substack.com
                        suffix = pattern[2:]  # substack.com
                        if domain == suffix or domain.endswith("." + suffix):
                            return True
                    elif fnmatch.fnmatch(domain, pattern):
                        return True
                return False
            except Exception:
                return False

        filtered = [s for s in filtered if not is_domain_blocked(s.get("xmlUrl", ""))]

    # 5. 去重 (以xmlUrl为key)
    seen = set()
    result = []
    for s in filtered:
        url = s.get("xmlUrl", "")
        if url and url not in seen:
            seen.add(url)
            item = dict(s)
            item["category"] = normalize_source_category(
                item.get("category"), title=item.get("title", ""), url=url
            )
            result.append(item)

    return result
