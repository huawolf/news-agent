"""Canonical content categories for configured news sources."""

from __future__ import annotations

from typing import Final


SOURCE_CATEGORIES: Final[tuple[str, ...]] = (
    "ai",
    "developer_open_source",
    "product_startup",
    "business_investment",
    "technology_policy",
    "other",
)

_LEGACY_ALIASES: Final[dict[str, str]] = {
    "ai": "ai",
    "artificial intelligence": "ai",
    "chrome": "developer_open_source",
    "cloudflare": "developer_open_source",
    "developer": "developer_open_source",
    "development": "developer_open_source",
    "open source": "developer_open_source",
    "技术": "developer_open_source",
    "开发者": "developer_open_source",
    "开源": "developer_open_source",
    "product": "product_startup",
    "startup": "product_startup",
    "产品": "product_startup",
    "创业": "product_startup",
    "business": "business_investment",
    "investment": "business_investment",
    "商业": "business_investment",
    "投资": "business_investment",
    "policy": "technology_policy",
    "industry": "technology_policy",
    "政策": "technology_policy",
    "产业": "technology_policy",
    "news": "other",
    "other": "other",
    "综合 / 其他": "other",
}

_KEYWORDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "ai",
        (
            " ai ", "artificial intelligence", "machine learning", "deep learning",
            "llm", "agent", "openai", "anthropic", "deepmind", "deepseek",
            "hugging face", "langchain", "qdrant", "dify", "jina ai", "rwkv",
            "大模型", "人工智能", "机器学习", "深度学习", "智能体", "机器人",
            "算法", "智能说", "ainlp", "ainews",
        ),
    ),
    (
        "developer_open_source",
        (
            "developer", "engineering", "programming", "github", "open source",
            "stack overflow", "docker", "kubernetes", "database", "cloud", "aws",
            "azure", "frontend", "backend", "node.js", "next.js", "spring", "java",
            "python", "devops", "代码", "编程", "开发", "工程", "技术", "架构",
            "数据库", "前端", "后端", "开源", "程序员",
        ),
    ),
    (
        "product_startup",
        (
            "product", "startup", "founder", "saas", "indie", "design", " ux ",
            "app store", "product hunt", "产品", "创业", "创始人", "独立开发",
            "设计", "用户体验", "应用商店",
        ),
    ),
    (
        "business_investment",
        (
            "business", "investment", "investor", "venture", "capital", "finance",
            "market", "economy", "商业", "投资", "创投", "资本", "财经", "金融",
            "股票", "证券", "财富", "融资",
        ),
    ),
    (
        "technology_policy",
        (
            "policy", "regulation", "government", "semiconductor", "chip", "energy",
            "data center", "automotive", "industry", "政策", "监管", "政府", "芯片",
            "半导体", "能源", "数据中心", "汽车", "产业",
        ),
    ),
)


def normalize_source_category(
    category: str | None,
    *,
    title: str = "",
    url: str = "",
) -> str:
    """Return one of the six supported content category IDs."""
    raw = str(category or "").strip()
    if raw in SOURCE_CATEGORIES:
        return raw

    alias = _LEGACY_ALIASES.get(raw.lower()) or _LEGACY_ALIASES.get(raw)
    if alias:
        return alias

    text = f" {raw} {title} {url} ".lower()
    for category_id, keywords in _KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return category_id
    return "other"
