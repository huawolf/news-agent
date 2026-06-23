"""Insights 板块:基于 RSS/GH/HN 三段成品做跨板块小结"""

from datetime import datetime
from typing import Dict, Optional, Tuple

EMPTY_MARKER = "(本次无内容)"


async def run_insights_section(
    rss_md: str,
    gh_md: str,
    hn_md: str,
    config: Dict,
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[Dict], Optional[str]]:
    cfg = config.get("sections", {}).get("insights", {})
    if not cfg.get("enabled", False):
        return "", None, None

    from src.llm import generate_trend_insights, parse_insights_with_metadata

    sections = {
        "rss": rss_md or EMPTY_MARKER,
        "github": gh_md or EMPTY_MARKER,
        "hackernews": hn_md or EMPTY_MARKER,
    }

    md, err = await generate_trend_insights(sections, config["llm"])
    if err:
        return "", None, f"generate_trend_insights: {err}"

    if now is None:
        now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    insights_md, metadata = parse_insights_with_metadata(md or "", date_str)

    return insights_md, metadata, None
