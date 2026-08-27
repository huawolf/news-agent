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
    mode = config.get("mode_settings", {}).get("mode", "client")
    cfg = config.get("sections", {}).get("github_trending", {})
    if mode != "client" and not cfg.get("enabled", False):
        return "", None

    if mode == "client":
        print("🔌 Client Mode: fetching pre-compiled GitHub Trending from server...")
        try:
            from src.main import query_server_api
            res = await query_server_api("/api/server/github-trending", config)
            if res and res.get("found"):
                github_md = res.get("github", "").strip()
                if github_md:
                    # Client-mode GitHub content is precompiled by the shared
                    # server, whose output language may differ from this
                    # client's language setting. Translate it locally when an
                    # LLM is configured so the rendered sections stay consistent.
                    if config.get("output_language") == "en":
                        from src.llm import translate_markdown_section

                        github_md, translation_error = await translate_markdown_section(
                            github_md, config["llm"], "GitHub Trending"
                        )
                        if translation_error:
                            return github_md, translation_error
                    print("🔌 Retrieved pre-compiled GitHub Trending from server")
                    return github_md, None
                else:
                    print("⚠️ Server pre-compiled GitHub Trending is empty")
            else:
                print("⚠️ Failed to retrieve pre-compiled GitHub Trending from server")
        except Exception as e:
            print(f"⚠️ Error querying server for GitHub Trending: {e}")
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
    print("📥 GH: fetching the trending page...")
    try:
        html = await fetch_trending_page(timeout=timeout)
    except Exception as e:
        err_msg = str(e) or e.__class__.__name__
        return "", f"GH fetch failed: {err_msg}"

    all_repos = parse_trending_html(html)
    print(f"📋 GH: parsed {len(all_repos)} repositories")
    if not all_repos:
        return "", None

    # 2. history 加载 (保留永久历史记录，确保历史上处理/发送过的 repo 不重复发送)
    history = load_trending_history(history_path)

    # 3. 候选筛选(按 spec §4.3 语义)
    candidates = []
    already_seen = 0
    for repo in all_repos:
        if repo["url"] in history:
            history.touch(repo["url"], today)
            already_seen += 1
        else:
            candidates.append(repo)
    print(
        f"🔍 GH: history skipped {already_seen} seen repositories; "
        f"{len(candidates)} new candidates"
    )

    if not candidates:
        print("ℹ️ GH: no new candidates; skipping")
        return "", None
    if len(candidates) > max_deep_dive:
        print(
            f"✂️ GH: {len(candidates)} candidates exceed "
            f"max_deep_dive={max_deep_dive}; truncating"
        )
        candidates = candidates[:max_deep_dive]

    # 4. 候选写回 history + 持久化
    for repo in candidates:
        history.touch(repo["url"], today)
    history.save()

    # 5. 并发 enrich
    print(f"🌐 GH: enriching {len(candidates)} repositories concurrently (REST API)...")
    enriched, enrich_errors = await enrich_repos(
        candidates,
        token_env=token_env,
        readme_max_chars=readme_max_chars,
        timeout=timeout,
    )
    for e in enrich_errors:
        print(f"⚠️ GH enrich: {e}")
    print(
        f"✅ GH: enrichment succeeded={len(enriched)} / failed={len(enrich_errors)} / "
        f"input={len(candidates)}"
    )
    if not enriched:
        return "", None

    # 6. LLM 总结
    print(f"🤖 GH: summarizing {len(enriched)} candidates...")
    md, err = await summarize_github_trending(enriched, config["llm"])
    if err:
        return "", err
    print(f"✅ GH: section output is {len(md or '')} characters")
    return md or "", None
