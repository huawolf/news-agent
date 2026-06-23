"""GitHub Trending 板块入口。

流程:trending 抓取 → history 过滤 → 候选写回 history → deep-dive → LLM 总结
"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.config import get_timezone
from src.sections.github.repo_enricher import enrich_repos
from src.sections.github.trending_scraper import (
    fetch_trending_page,
    parse_trending_html,
)
from src.storage import load_trending_history


async def run_github_section(
    config: Dict, now: Optional[datetime] = None
) -> Tuple[str, Optional[str]]:
    cfg = config.get("sections", {}).get("github_trending", {})
    if not cfg.get("enabled", False):
        return "", None

    # 延迟 import,Task 11 才提供 summarize_github_trending
    from src.llm import summarize_github_trending

    today = (now or datetime.now(get_timezone())).date()
    keep_days = config["filter"]["keep_days"]
    timeout = cfg.get("request_timeout", 10)
    max_deep_dive = cfg.get("max_deep_dive", 10)
    readme_max_chars = cfg.get("readme_max_chars", 3000)
    history_path = cfg.get("history_file", "news-data/trending-history.json")
    token_env = cfg.get("tokenName", "GITHUB_TOKEN")

    # 1. 抓取
    print("📥 GH: 抓取 trending 页...")
    try:
        html = await fetch_trending_page(timeout=timeout)
    except Exception as e:
        err_msg = str(e) or e.__class__.__name__
        return "", f"GH 抓取失败: {err_msg}"

    all_repos = parse_trending_html(html)
    print(f"📋 GH: 解析 {len(all_repos)} 个 repo")
    if not all_repos:
        return "", None

    # 2. history 加载 + 清理
    history = load_trending_history(history_path)
    before_cleanup = len(history.repos)
    history.cleanup(today=today, keep_days=keep_days)
    after_cleanup = len(history.repos)
    if before_cleanup != after_cleanup:
        print(
            f"🧹 GH: history 清理过期 {before_cleanup - after_cleanup} 条 (剩 {after_cleanup})"
        )

    # 3. 候选筛选(按 spec §4.3 语义)
    candidates = []
    already_seen = 0
    for repo in all_repos:
        if repo["url"] in history:
            history.touch(repo["url"], today)
            already_seen += 1
        else:
            candidates.append(repo)
    print(f"🔍 GH: history 过滤掉 {already_seen} 条已见,新候选 {len(candidates)} 条")

    if not candidates:
        print("ℹ️ GH: 无新候选,跳过")
        return "", None
    if len(candidates) > max_deep_dive:
        print(f"✂️ GH: 候选 {len(candidates)} 超 max_deep_dive={max_deep_dive},截断")
        candidates = candidates[:max_deep_dive]

    # 4. 候选写回 history + 持久化
    for repo in candidates:
        history.touch(repo["url"], today)
    history.save()

    # 5. 并发 enrich
    print(f"🌐 GH: 并发 enrich {len(candidates)} 个 repo (REST API)...")
    enriched, enrich_errors = await enrich_repos(
        candidates,
        token_env=token_env,
        readme_max_chars=readme_max_chars,
        timeout=timeout,
    )
    for e in enrich_errors:
        print(f"⚠️ GH enrich: {e}")
    print(
        f"✅ GH: enrich 成功 {len(enriched)} / 失败 {len(enrich_errors)} / "
        f"输入 {len(candidates)}"
    )
    if not enriched:
        return "", None

    # 6. LLM 总结
    print(f"🤖 GH: summarize {len(enriched)} 个候选...")
    md, err = await summarize_github_trending(enriched, config["llm"])
    if err:
        return "", f"summarize_github_trending: {err}"
    print(f"✅ GH: 板块输出 {len(md or '')} chars")
    return md or "", None
