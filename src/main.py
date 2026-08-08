"""News Agent main program."""

import argparse
import asyncio
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Load .env file.
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from croniter import croniter

from src.config import get_timezone, load_config, merge_sources
from src.fetcher import fetch_all_feeds
from src.llm import (
    LLMOperationError,
    check_llm_available,
    generate_immediate_push,
    parse_immediate_push_with_metadata,
    score_batch,
)
from src.processor import html_to_markdown, is_own_digest_entry
from src.push import send_to_platforms
from src.sections.github.section import run_github_section
from src.sections.hackernews.section import run_hackernews_section
from src.sections.insights.section import run_insights_section
from src.sections.rss.section import run_rss_section
from src.sections.signals.collector import fetch_signal_entries
from src.sections.signals.google_news import fetch_google_news_entries
from src.storage import (
    append_entries,
    assemble_with_sentinels,
    cleanup_old_files,
    get_fetch_file,
    get_notify_file,
    get_push_file,
    load_existing_links,
    format_recent_push_summary_context,
    limit_delivery_items,
    load_recent_notify_content,
    load_recent_push_content,
    read_entries,
    save_notify_file,
    save_push_file,
    load_sent_links,
    mark_links_as_sent,
    get_last_push_file,
    extract_push_time,
)


async def notify_llm_errors(stage: str, errors: List[str], config: Dict):
    """Notify only when an LLM operation failed due to connectivity."""
    connection_errors = [
        error
        for error in errors
        if isinstance(error, LLMOperationError) and error.connection_failure
    ]
    if not connection_errors:
        return

    lines = [
        "## LLM Error",
        "",
        f"stage: {stage}",
        f"time: {now_local(config).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    lines.extend(f"- {error}" for error in connection_errors)

    try:
        await send_to_platforms("\n".join(lines), config["push"])
    except Exception as e:
        print(f"⚠️ Failed to send LLM error notification: {e}")


def now_local(config: Dict = None) -> datetime:
    """Return the current time in the configured timezone."""
    return datetime.now(get_timezone(config))


def _is_english_output(config: Dict) -> bool:
    return config.get("output_language") == "en"


def _daily_title_prefix(config: Dict) -> str:
    return "📰 News Agent Daily Brief | " if _is_english_output(config) else "📰 News Agent 每日精选 | "


def _delivery_title(config: Dict, title: str) -> str:
    """Build a daily title without mixing Chinese and English fixed copy."""
    clean_title = str(title or "").strip()
    prefixes = (
        "📰 News Agent Daily Brief | ",
        "📰 News Agent 每日精选 | ",
        "News Agent Daily Brief | ",
        "News Agent 每日精选 | ",
    )
    for prefix in prefixes:
        if clean_title.startswith(prefix):
            clean_title = clean_title[len(prefix):].strip()
            break

    if not clean_title:
        clean_title = now_local(config).strftime("%Y-%m-%d")
    prefix_config = {"output_language": "zh" if re.search(r"[\u3400-\u9fff]", clean_title) else "en"}
    return _daily_title_prefix(prefix_config) + clean_title


def _default_digest_title(config: Dict, date_str: str) -> str:
    return (
        f"🌙 News Agent Evening Brief | {date_str}"
        if _is_english_output(config)
        else f"🌙 News Agent 晚报 | {date_str}"
    )


def _default_morning_title(config: Dict, date_str: str) -> str:
    return (
        f"📰 News Agent Daily Brief | {date_str}"
        if _is_english_output(config)
        else f"📰 News Agent 每日精选 | {date_str}"
    )


def _default_immediate_title(config: Dict, timestamp: str) -> str:
    return (
        f"🚨 News Agent Breaking News | {timestamp}"
        if _is_english_output(config)
        else f"🚨 News Agent 快讯 | {timestamp}"
    )


def _immediate_title_prefix(config: Dict) -> str:
    return "🚨 News Agent Breaking News | " if _is_english_output(config) else "🚨 News Agent 快讯 | "


def parse_time_to_local(time_str: str, config: Dict = None) -> Optional[datetime]:
    """Parse a timestamp string into the configured local timezone."""
    try:
        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        return dt.astimezone(get_timezone(config))
    except (ValueError, TypeError):
        return None


def calculate_push_times(
    cron_list: List[str], offset_days: int = 0, config: Dict = None
) -> List[datetime]:
    base_date = datetime.now(get_timezone(config)).date() + timedelta(days=offset_days)
    times = []
    for cron in cron_list:
        try:
            minute, hour, _, _, _ = cron.split()
            t = datetime.combine(
                base_date,
                datetime.strptime(f"{hour}:{minute}", "%H:%M").time(),
                tzinfo=get_timezone(config),
            )
            times.append(t)
        except ValueError:
            continue
    return sorted(times)


def is_keyword_match(k1: str, k2: str) -> bool:
    """Return whether two keywords are similar enough to match."""
    k1_clean = "".join(c for c in k1.lower() if c.isalnum())
    k2_clean = "".join(c for c in k2.lower() if c.isalnum())
    
    if not k1_clean or not k2_clean:
        return False
        
    if k1_clean == k2_clean:
        return True
        
    if len(k1_clean) >= 3 and len(k2_clean) >= 3:
        if k1_clean in k2_clean or k2_clean in k1_clean:
            return True
            
    return False


def count_overlapping_keywords(keywords_a: List[str], keywords_b: List[str]) -> int:
    """Count overlapping keywords between two keyword lists."""
    matches = 0
    matched_in_b = set()
    
    for k_a in keywords_a:
        for k_b in keywords_b:
            if k_b not in matched_in_b and is_keyword_match(k_a, k_b):
                matches += 1
                matched_in_b.add(k_b)
                break
                
    return matches


def deduplicate_by_keywords(new_entries: List[Dict], config: Dict):
    """Deduplicate by keyword similarity against news fetched in the last 3 days."""
    tz = get_timezone(config)
    now = datetime.now(tz)
    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    threshold = config.get("filter", {}).get("keyword_overlap_threshold", 3)
    
    history_entries = []
    for i in range(3):
        d = now.date() - timedelta(days=i)
        fetch_file = get_fetch_file(d, data_dir)
        if os.path.exists(fetch_file):
            for entry in read_entries(fetch_file):
                # Exclude duplicate entries already set to score 0.
                if entry.get("keywords") and entry.get("score", 0) > 0:
                    history_entries.append(entry)
                    
    print(f"🔍 Keyword deduplication: loaded {len(history_entries)} historical entries from the last 3 days (threshold={threshold})")
    
    if not history_entries:
        return
        
    duplicate_count = 0
    for new_entry in new_entries:
        new_keywords = new_entry.get("keywords")
        if not new_keywords or not isinstance(new_keywords, list):
            continue
            
        is_dup = False
        matching_history_title = ""
        matching_history_link = ""
        for hist_entry in history_entries:
            hist_keywords = hist_entry.get("keywords")
            if not hist_keywords or not isinstance(hist_keywords, list):
                continue
                
            overlap = count_overlapping_keywords(new_keywords, hist_keywords)
            if overlap >= threshold:
                is_dup = True
                matching_history_title = hist_entry.get("title", "")
                matching_history_link = hist_entry.get("link", "")
                break
                
        if is_dup:
            new_entry["score"] = 0
            new_entry["is_duplicate"] = True
            new_entry["duplicate_reason"] = f"Keyword overlap >= {threshold} with historical news '{matching_history_title}' ({matching_history_link})"
            duplicate_count += 1
            print(f"🚫 Keyword duplicate detected: '{new_entry.get('title')}' overlaps with sent news '{matching_history_title}'")
            
    if duplicate_count > 0:
        print(f"🚫 Keyword deduplication blocked {duplicate_count} entries by setting score to 0")


def is_morning_push(now: datetime, config: Dict) -> bool:
    """Return whether the current push is the morning brief.

    The closest cron in `schedule.push_cron` is treated as the active schedule.
    If that cron is the earliest cron for the day, it is a morning brief.

    Special cases:
    - Empty `push_cron` means no morning brief.
    - A single `push_cron` is both earliest and closest, so it is a morning brief.
    """
    cron_list = config.get("schedule", {}).get("push_cron", [])
    if not cron_list:
        return False
    if len(cron_list) == 1:
        return True

    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_fires = [croniter(c, base).get_next(datetime) for c in cron_list]
    closest = min(today_fires, key=lambda f: abs(now - f))
    return closest == min(today_fires)


async def collect_entries_for_push(
    last_push_time: Optional[datetime],
    context_days: int = 2,
    min_score: int = 60,
    data_dir: str = "news-data",
    preferences: Optional[Dict] = None,
    max_items: Optional[int] = None,
    config: Optional[Dict] = None,
) -> tuple[List[Dict], List[Dict]]:
    """
    Collect entries for delivery and return (entries_to_push, context_entries).

    Logic:
    1. Retrieve pre-scored news from server if mode is client or mix.
    2. Load local custom feeds entries from the last context_days days.
    3. Merge server and local entries.
    4. Filter by min_score.
    5. Load sent-history.json.
    6. Filter links that have already been sent.
    7. Unsent entries from the last 24 hours become delivery candidates.
    8. Earlier entries become LLM deduplication context.
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            config = {}

    tz = get_timezone(config)
    now = datetime.now(tz)

    # 1. Retrieve pre-scored news from server
    server_entries = []
    mode = config.get("mode_settings", {}).get("mode", "standalone")

    if last_push_time:
        if last_push_time.tzinfo is None:
            last_push_time_tz = last_push_time.replace(tzinfo=tz)
        else:
            last_push_time_tz = last_push_time.astimezone(tz)
        # Calculate hours elapsed since the last push
        hours_since_last_push = int((now - last_push_time_tz).total_seconds() // 3600)
        lookback_hours = min(24, max(1, hours_since_last_push))
    else:
        lookback_hours = min(24, context_days * 24)

    print(f"📋 Last push was at: {last_push_time or 'None'}. Querying server with lookback_hours: {lookback_hours}")

    if mode == "mix":
        try:
            from src.server_cache import server_news_cache
            if not server_news_cache._initialized:
                server_news_cache.initialize(data_dir, config)
            server_entries = server_news_cache.get_news(lookback_hours, config)
            print(f"📋 Retrieved {len(server_entries)} pre-scored entries from in-memory server cache")
        except Exception as e:
            print(f"⚠️ Failed to get news from in-memory cache: {e}")
    elif mode == "client":
        try:
            params = {"hours": lookback_hours}
            res = await query_server_api("/api/server/news", config, params=params)
            if isinstance(res, list):
                server_entries = res
                print(f"📋 Retrieved {len(server_entries)} pre-scored entries from server API")
            else:
                print("⚠️ Server API returned non-list or failed, falling back to local files.")
        except Exception as e:
            print(f"⚠️ Failed to query server news API: {e}")

    # 2. Load all entries from the last context_days days.
    local_entries = []
    today = now.date()
    for i in range(context_days):
        d = today - timedelta(days=i)
        fetch_file = get_fetch_file(d, data_dir)
        for entry in read_entries(fetch_file):
            local_entries.append(entry)

    # Merge entries based on link
    merged = {e.get("link"): e for e in (server_entries + local_entries) if e.get("link")}
    all_entries = list(merged.values())

    print(
        f"📋 Collected entries: {len(all_entries)} (server: {len(server_entries)}, local: {len(local_entries)}), context_days: {context_days}, min_score: {min_score}"
    )

    # Filter by min_score.
    qualified_entries = [e for e in all_entries if (e.get("score") or 0) >= min_score]
    print(f"📋 Entries after score filtering: {len(qualified_entries)}")

    # Load sent history for deduplication.
    sent_links = load_sent_links(days=30, data_dir=data_dir)
    print(f"📋 Sent history loaded: {len(sent_links)} filtered links")

    # Keep unsent candidates eligible for 24 hours. Sent history, rather than the
    # previous push timestamp, is the authority for whether an item was delivered.
    past_24h = now - timedelta(hours=24)
    push_cutoff = past_24h

    print(
        f"Delivery eligibility cutoff: {push_cutoff.strftime('%Y-%m-%d %H:%M:%S')} "
        f"| last push: {last_push_time or 'none'}"
    )

    # Split entries.
    to_push = []
    context = []
    # Context is only for LLM deduplication/history reference; omit large fields.
    CONTEXT_FIELDS = ("title", "source", "score", "summary", "tags", "published")

    sent_filtered = 0
    cutoff_filtered = 0
    for entry in qualified_entries:
        link = entry.get("link")
        if link and link in sent_links:
            sent_filtered += 1
            continue

        entry_time = parse_time_to_local(entry.get("fetched_at", ""), config)
        if entry_time and entry_time > push_cutoff:
            to_push.append(entry)
        else:
            cutoff_filtered += 1
            context.append({k: entry.get(k) for k in CONTEXT_FIELDS})

    print(
        f"📊 Delivery filters: qualified={len(qualified_entries)}, "
        f"sent={sent_filtered}, cutoff={cutoff_filtered}, fresh={len(to_push)}"
    )

    # Sort context by score and keep the top 50.
    context = sorted(context, key=lambda x: x.get("score", 0), reverse=True)[:50]

    fresh_count = len(to_push)
    to_push = rank_entries_for_delivery(to_push, preferences or {})
    print(
        f"📊 Delivery diversity: input={fresh_count}, kept={len(to_push)}, "
        f"removed={fresh_count - len(to_push)}"
    )
    if max_items is not None:
        before_limit = len(to_push)
        to_push = to_push[:max(1, max_items)]
        print(
            f"📊 Delivery candidate limit: input={before_limit}, "
            f"kept={len(to_push)}, max_items={max_items}"
        )
    return to_push, context


def links_present_in_content(entries: List[Dict], content: str) -> List[str]:
    """Return candidate links that are actually present in delivered content."""
    if not content:
        return []
    return [
        link
        for entry in entries
        if (link := entry.get("link")) and link in content
    ]


def rank_entries_for_delivery(entries: List[Dict], preferences: Dict) -> List[Dict]:
    """Apply deterministic personal ranking and diversity after LLM base scoring."""
    interests = [str(value).lower() for value in preferences.get("interests", [])]
    avoid = [str(value).lower() for value in preferences.get("avoid", [])]
    source_weights = preferences.get("source_weights", {})
    diversity = preferences.get("diversity", {})
    max_per_source = int(diversity.get("max_per_source", 0) or 0)
    max_per_topic = int(diversity.get("max_per_topic", 0) or 0)

    def scored(entry: Dict) -> tuple[int, Dict]:
        text = " ".join([
            str(entry.get("title", "")), str(entry.get("summary", "")),
            " ".join(str(tag) for tag in entry.get("tags", [])),
        ]).lower()
        score = int(entry.get("score") or 0) + int(source_weights.get(entry.get("source", ""), 0) or 0)
        score += 8 * sum(1 for term in interests if term and term in text)
        score -= 20 * sum(1 for term in avoid if term and term in text)
        item = dict(entry)
        item["delivery_score"] = score
        return score, item

    ranked = [scored(entry) for entry in entries]
    ranked.sort(key=lambda pair: (pair[0], str(pair[1].get("fetched_at", ""))), reverse=True)
    result, sources, topics = [], {}, {}
    for _, entry in ranked:
        source = entry.get("source", "")
        tags = entry.get("tags", []) or ["untagged"]
        topic = str(tags[0])
        if max_per_source and sources.get(source, 0) >= max_per_source:
            continue
        if max_per_topic and topics.get(topic, 0) >= max_per_topic:
            continue
        sources[source] = sources.get(source, 0) + 1
        topics[topic] = topics.get(topic, 0) + 1
        result.append(entry)
    return result


def active_delivery_schedule(config: Dict, now: Optional[datetime] = None) -> Dict:
    """Return the closest configured delivery policy; supports legacy config as fallback."""
    schedules = config.get("delivery", {}).get("schedules", [])
    if not schedules:
        return {}
    current = now or now_local(config)
    candidates = []
    for schedule in schedules:
        try:
            previous = croniter(schedule["cron"], current).get_prev(datetime)
            following = croniter(schedule["cron"], current).get_next(datetime)
            distance = min(abs(current - previous), abs(following - current))
            candidates.append((distance, schedule))
        except (KeyError, ValueError):
            continue
    return min(candidates, key=lambda item: item[0])[1] if candidates else {}


async def query_server_api(path: str, config: Dict, params: Optional[Dict] = None) -> Optional[any]:
    """Helper to query the server REST API using aiohttp."""
    import aiohttp
    settings = config.get("mode_settings", {})
    server_url = settings.get("server_url", "http://127.0.0.1:12301").rstrip("/")
    token = settings.get("server_api_token", "") or os.environ.get("NEWS_AGENT_LOCAL_TOKEN", "")

    headers = {}
    if token:
        headers["X-News-Agent-Token"] = token

    url = f"{server_url}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=30) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    print(f"⚠️ Server returned status {response.status} for {path}")
    except Exception as e:
        print(f"⚠️ Failed to connect to server API {url}: {e}")
    return None


async def run_fetch_job(config: Dict):
    print(f"\n{'=' * 50}")
    print(f"🔄 Fetch Job | {now_local(config).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    # Check mode settings
    mode = config.get("mode_settings", {}).get("mode", "standalone")
    sources = merge_sources(config["sources"])

    if mode == "client":
        import copy
        fetch_config = copy.deepcopy(config)
        print("🔌 Running in Client Mode. Querying server for active sources...")
        server_sources = await query_server_api("/api/news-sources", config)
        if server_sources:
            # 1. Filter RSS sources (only keep custom feeds not on the server)
            server_rss_urls = {s.get("xmlUrl") for s in server_sources.get("rss", []) if s.get("xmlUrl")}
            sources = [s for s in sources if s.get("xmlUrl") not in server_rss_urls]
            print(f"🔌 Filtered RSS sources. Custom local RSS feeds to fetch: {len(sources)}")

            # 2. Filter integrations/sections
            server_integrations = {i["id"]: i for i in server_sources.get("integrations", []) if "id" in i}

            # GitHub Trending
            if fetch_config.get("sections", {}).get("github_trending", {}).get("enabled"):
                s_gh = server_integrations.get("github_trending")
                if s_gh and s_gh.get("enabled"):
                    print("🔌 GitHub Trending is enabled on the server. Skipping local fetch.")
                    fetch_config["sections"]["github_trending"]["enabled"] = False

            # Hacker News
            if fetch_config.get("sections", {}).get("hackernews", {}).get("enabled"):
                s_hn = server_integrations.get("hackernews")
                if s_hn and s_hn.get("enabled"):
                    print("🔌 Hacker News is enabled on the server. Skipping local fetch.")
                    fetch_config["sections"]["hackernews"]["enabled"] = False

            # Google News (google-news-...)
            if fetch_config.get("sections", {}).get("google_news", {}).get("enabled"):
                any_google_news_server = any(
                    id_str.startswith("google-news-") and i.get("enabled")
                    for id_str, i in server_integrations.items()
                )
                if any_google_news_server:
                    print("🔌 Google News is enabled on the server. Skipping local fetch.")
                    fetch_config["sections"]["google_news"]["enabled"] = False

            # Signals
            if fetch_config.get("sections", {}).get("signals", {}).get("enabled"):
                local_signals_sources = fetch_config["sections"]["signals"].setdefault("sources", {})
                for sig_id in list(local_signals_sources.keys()):
                    s_sig = server_integrations.get(sig_id)
                    if s_sig and s_sig.get("enabled"):
                        local_signals_sources[sig_id] = False
                print("🔌 Filtered signal sources to avoid server duplicate fetching.")

            config = fetch_config
        else:
            print("⚠️ Could not connect to server. Falling back to local standalone fetch.")

    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 1440)
    lookback = max(lookback, interval)
    threshold = lookback + interval
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback)

    if mode != "client":
        sources = merge_sources(config["sources"])
    print(f"📂 Sources configured: {len(sources)}")

    if not sources:
        print("ℹ️ No configured RSS sources; continuing with built-in sources")

    max_workers = config.get("fetch", {}).get("max_workers", 20)
    timeout = config.get("fetch", {}).get("timeout", 30)
    
    # 1. Fetch RSS sources.
    entries = await fetch_all_feeds(
        sources, cutoff, max_workers=max_workers, timeout=timeout
    )
    print(f"📥 Fetched {len(entries)} raw RSS entries")

    # 2. Fetch built-in signal sources.
    signal_entries = []
    try:
        signal_entries = await fetch_signal_entries(config, cutoff_time=cutoff)
        print(f"📥 Signals: fetched {len(signal_entries)} built-in signal entries")
    except Exception as e:
        print(f"⚠️ Signals fetch failed: {e}")

    # 3. Fetch incremental Google News topic feeds using per-feed cursors.
    google_news_entries = []
    try:
        google_news_entries = await fetch_google_news_entries(config)
        print(f"📥 Google News: fetched {len(google_news_entries)} topic entries")
    except Exception as e:
        print(f"⚠️ Google News fetch failed: {e}")

    # 4. Fetch Hacker News concurrently using Algolia comment data.
    hn_entries = []
    try:
        from src.fetcher import fetch_hackernews_entries
        hn_entries = await fetch_hackernews_entries(config, cutoff_time=cutoff)
        print(f"📥 HN: fetched {len(hn_entries)} enriched popular-comment entries")
    except Exception as e:
        print(f"⚠️ HN fetch failed: {e}")

    # Merge sources.
    raw_all_entries = entries + signal_entries + google_news_entries + hn_entries

    # Second validation: discard entries published before the configured cutoff.
    all_entries = []
    for e in raw_all_entries:
        pub = e.get("published")
        if pub and isinstance(pub, datetime):
            # Normalize to UTC for comparison.
            if pub.tzinfo is None:
                pub_utc = pub.replace(tzinfo=timezone.utc)
            else:
                pub_utc = pub.astimezone(timezone.utc)
            if pub_utc < cutoff:
                print(f"🕒 Filtered stale entry outside the lookback window: '{e.get('title')}' ({pub})")
                continue
        all_entries.append(e)

    print(f"📥 Total raw entries after stale filtering: {len(all_entries)}")

    if not all_entries:
        return

    # Convert HTML to Markdown for normal RSS entries outside Hacker News.
    for entry in all_entries:
        if entry.get("source") != "Hacker News":
            entry["content"] = html_to_markdown(
                entry.get("content", ""), entry.get("link", "")
            )

    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    fetch_file = get_fetch_file(data_dir=data_dir)
    existing_links = load_existing_links(fetch_file, threshold, data_dir=data_dir)
    
    new_entries = []
    for e in all_entries:
        if e.get("link") and e["link"] not in existing_links:
            if is_own_digest_entry(e):
                print(f"🚫 Filtered digest/aggregator entry: '{e.get('title')}'")
            else:
                new_entries.append(e)
                
    print(f"🆕 New entries: {len(new_entries)} | existing links: {len(existing_links)}")

    if not new_entries:
        return

    print("🤖 Scoring with LLM...")
    # Convert datetime values to strings before JSON serialization.
    for entry in new_entries:
        if isinstance(entry.get("published"), datetime):
            entry["published"] = (
                entry["published"].astimezone(get_timezone(config)).isoformat()
            )

    scored, score_errors = await score_batch(new_entries, config["llm"])
    if score_errors:
        print(f"⚠️ [score_batch] {len(score_errors)} errors: {score_errors[0]}")
        await notify_llm_errors("score_batch", score_errors, config)

    # 3. Keyword deduplication.
    deduplicate_by_keywords(scored, config)

    is_new_file = not os.path.exists(fetch_file)
    if is_new_file:
        cleanup_old_files(days=config["filter"]["keep_days"], data_dir=data_dir)

    # Add fetched_at timestamp.
    for entry in scored:
        entry["fetched_at"] = now_local().isoformat()
        if isinstance(entry.get("published"), datetime):
            entry["published"] = (
                entry["published"].astimezone(get_timezone(config)).isoformat()
            )

    # Save entries to the JSON file.
    from datetime import date

    meta = {"date": date.today().isoformat()}
    append_entries(fetch_file, scored, meta)

    print(f"💾 Saved to {fetch_file}")

    # Update in-memory cache if we are in server (mix/standalone) mode
    mode = config.get("mode_settings", {}).get("mode", "standalone")
    if mode in ("standalone", "mix"):
        try:
            from src.server_cache import server_news_cache
            server_news_cache.update(scored, config)
            print(f"💾 Updated in-memory cache with {len(scored)} entries")
        except Exception as e:
            print(f"⚠️ Failed to update in-memory cache: {e}")

    immediate_config = config.get("delivery", {}).get("immediate", {})
    immediate_enabled = immediate_config.get("enabled", False)
    hot_threshold = int(immediate_config.get("threshold", config["filter"]["hot_threshold"]))
    no_content_marker = config["filter"].get("no_content_marker", "[NO_NEW_CONTENT]")
    hot_entries = [e for e in scored if (e.get("score") or 0) >= hot_threshold] if immediate_enabled else []
    daily_limit = int(immediate_config.get("daily_limit", 0) or 0)
    if hot_entries and daily_limit:
        notify_file = get_notify_file(data_dir=data_dir)
        existing_notifications = 0
        if os.path.exists(notify_file):
            existing_notifications = Path(notify_file).read_text(encoding="utf-8", errors="ignore").count("------")
        if existing_notifications >= daily_limit:
            print(f"ℹ️ Daily hot-news delivery limit reached ({daily_limit}); skipping immediate delivery")
            hot_entries = []
    if hot_entries:
        print(f"🔥 Found {len(hot_entries)} hot entries; sending immediate delivery...")

        # Load recent sent content for LLM deduplication and style diversity.
        context_days = config["filter"]["context_days"]
        recent_notify = format_recent_push_summary_context(
            load_recent_notify_content(context_days, data_dir=data_dir)
        )
        recent_push = format_recent_push_summary_context(
            load_recent_push_content(context_days, data_dir=data_dir)
        )
        recent_context = (
            f"=== Recent immediate deliveries ===\n{recent_notify}\n\n"
            f"=== Recent digest deliveries ===\n{recent_push}"
        )

        push_content, immediate_push_error = await generate_immediate_push(
            hot_entries, config["llm"], recent_push_context=recent_context
        )

        if immediate_push_error:
            print(f"⚠️ [generate_immediate_push] {immediate_push_error}")
            await notify_llm_errors(
                "generate_immediate_push", [immediate_push_error], config
            )

        if not push_content:
            print("⚠️ Immediate delivery content generation failed; skipping hot-news delivery")
            print(
                f"✅ Fetch job completed | new entries: {len(scored)} | hot entries: {len(hot_entries)}"
            )
            return

        # Check whether there is actual content to deliver.
        if no_content_marker in push_content:
            print("ℹ️ No new content to deliver; LLM marked it as duplicate content")
        else:
            # Extract title and build metadata.
            now = now_local(config)
            timestamp = now.strftime("%Y-%m-%d %H:%M")
            content_without_title, metadata = parse_immediate_push_with_metadata(
                push_content, _default_immediate_title(config, timestamp)
            )
            metadata["pushTime"] = now.isoformat()

            await send_to_platforms(
                content_without_title,
                config["push"],
                _immediate_title_prefix(config) + metadata["title"],
                metadata=metadata,
            )
            # Save immediate delivery content to the notify file.
            notify_file = get_notify_file(data_dir=data_dir)
            save_notify_file(notify_file, content_without_title, metadata)
            print(f"💾 Saved immediate delivery to {notify_file}")

            # 4. Record sent links in sent-history.json.
            sent_links = links_present_in_content(hot_entries, content_without_title)
            mark_links_as_sent(sent_links, data_dir=config.get("storage", {}).get("data_dir", "news-data"))
            print(
                f"💾 Recorded {len(sent_links)} actually delivered immediate links "
                f"({len(hot_entries) - len(sent_links)} candidates not marked)"
            )

    print(f"✅ Fetch job completed | new entries: {len(scored)} | hot entries: {len(hot_entries)}")


async def run_push_job(config: Dict):
    print(f"\n{'=' * 50}")
    print(f"📤 Push Job | {now_local(config).strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    # If in client mode and no local LLM is configured, pull pre-compiled digest directly from server
    mode = config.get("mode_settings", {}).get("mode", "standalone")
    has_local_llm = False
    llm_cfg = config.get("llm", {})
    api_key_name = llm_cfg.get("apiKeyName")
    if api_key_name and os.environ.get(api_key_name):
        has_local_llm = True

    if mode == "client" and not has_local_llm:
        print("🔌 Client has no LLM configured. Fetching pre-compiled digest from server...")
        digest_data = await query_server_api("/api/server/latest-digest", config)
        if digest_data and digest_data.get("found"):
            content = digest_data.get("content", "")
            title = digest_data.get("title", "")
            metadata = digest_data.get("metadata", {})
            if content:
                print(f"🔌 Retrieved pre-compiled digest from server: '{title}'")
                await send_to_platforms(content, config["push"], title=title, metadata=metadata)
                print("✅ Pre-compiled digest successfully delivered")
                return
            else:
                print("⚠️ Pre-compiled digest content is empty")
        else:
            print("⚠️ Failed to retrieve pre-compiled digest from server")
        return

    policy = active_delivery_schedule(config, now_local(config))
    use_extended_sections = bool(policy.get("sections")) and any(
        section != "rss" for section in policy["sections"]
    )
    if use_extended_sections or (not policy and is_morning_push(now_local(config), config)):
        await _run_morning_push(config)
    else:
        await _run_default_push(config)


async def _run_default_push(config: Dict):
    """Run the default RSS-only digest flow for evening or non-morning schedules."""
    now = now_local(config)
    policy = active_delivery_schedule(config, now)
    rss_md, metadata, rss_err = await run_rss_section(config, now, max_items=policy.get("max_items"))

    if rss_err and not rss_md:
        print(f"⚠️ [compose_digest] {rss_err}")
        await notify_llm_errors("compose_digest", [rss_err], config)
        raise RuntimeError(f"RSS section failed: {rss_err}")

    if not rss_md:
        # run_rss_section already logs when there are no new RSS entries.
        return

    # Metadata fallback for missing frontmatter or parse failures.
    if not metadata:
        date_str = now.strftime("%Y-%m-%d")
        metadata = {
            "title": _default_digest_title(config, date_str),
            "lead": "",
            "highlights": [],
            "profile": "default",
            "date": date_str,
        }
    metadata.setdefault("pushTime", now.isoformat())

    delivery_sections = limit_delivery_items(
        {"rss": rss_md}, policy.get("max_items", 10)
    )
    delivery_md = delivery_sections.get("rss", "")
    if not delivery_md:
        print("ℹ️ RSS output contains no valid news items; skipping delivery")
        return

    await send_to_platforms(
        delivery_md,
        config["push"],
        title=_delivery_title(config, metadata["title"]),
        metadata=metadata,
    )
    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    last_push_file = get_last_push_file(data_dir)
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    push_file = get_push_file(data_dir=data_dir)
    rss_count = delivery_md.count("###")
    save_push_file(
        push_file,
        delivery_md,
        rss_count,
        rss_count,
        profile="default",
        metadata=metadata,
    )
    print(f"💾 Saved to {push_file}")
    
    # Record sent history.
    try:
        min_score = config["filter"]["min_score"]
        context_days = config["filter"]["context_days"]
        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        to_push, _ = await collect_entries_for_push(
            last_push_time=last_push_time,
            context_days=context_days,
            min_score=min_score,
            data_dir=data_dir,
            preferences=config.get("preferences"),
            max_items=None,
            config=config,
        )
        sent_links = links_present_in_content(to_push, delivery_md)
        mark_links_as_sent(sent_links, data_dir=data_dir)
        print(f"💾 Recorded {len(sent_links)} actually delivered digest links")
    except Exception as e:
        print(f"⚠️ Failed to record sent history: {e}")

    print(f"✅ Push job completed | delivered entries: {rss_count}")



async def _run_morning_push(config: Dict):
    """Run the morning news, GitHub, and metadata workflow.

    RSS (which already includes Hacker News entries) and GitHub run concurrently.
    Insights then generates delivery metadata before the sections are assembled.

    Failure semantics:
    - RSS failure raises RuntimeError because it is the core section.
    - GitHub/insights failures omit that section and continue.
    """
    now = now_local(config)

    policy = active_delivery_schedule(config, now)
    rss_result, gh_result = await asyncio.gather(
        run_rss_section(config, now, max_items=policy.get("max_items")),
        run_github_section(config, now),
    )

    # In morning briefs, insights usually overrides digest metadata.
    # Keep digest metadata as fallback if insights fails.
    rss_md, digest_meta, rss_err = rss_result
    gh_md, gh_err = gh_result

    if gh_err:
        print(f"⚠️ [section_github] {gh_err}")
        await notify_llm_errors("section_github", [gh_err], config)
    if rss_err and not rss_md:
        print(f"⚠️ [compose_digest] {rss_err}")
        await notify_llm_errors("compose_digest", [rss_err], config)
        raise RuntimeError(f"RSS section failed: {rss_err}")

    _insights_md, metadata, insights_err = await run_insights_section(
        rss_md, gh_md, "", config, now
    )
    if insights_err:
        print(f"⚠️ [insights] {insights_err}")
        await notify_llm_errors("insights", [insights_err], config)

    # If insights fails, prefer digest metadata as fallback, then defaults.
    if not metadata:
        date_str = now.strftime("%Y-%m-%d")
        fallback = digest_meta or {}
        digest_title = fallback.get("title", "")

        title = digest_title if digest_title else _default_morning_title(config, date_str)
        metadata = {
            "date": date_str,
            "pushTime": now.isoformat(),
            "title": title,
            "excerpt": "",
            "seotitle": "",
            "seodescription": "",
            "lead": fallback.get("lead", ""),
            "highlights": fallback.get("highlights", []),
            "profile": "morning",
        }
    else:
        metadata.setdefault("pushTime", now.isoformat())

    delivery_sections = limit_delivery_items(
        {
            "rss": rss_md,
            "github": gh_md,
        },
        policy.get("max_items", 10),
        github_max_items=config.get("sections", {})
        .get("github_trending", {})
        .get("max_items", 3),
    )
    final = assemble_with_sentinels(delivery_sections)

    if not final.strip():
        print("ℹ️ Morning brief has no section output; skipping delivery")
        return

    await send_to_platforms(
        final,
        config["push"],
        title=_delivery_title(config, metadata["title"]),
        metadata=metadata,
    )
    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    last_push_file = get_last_push_file(data_dir)
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    push_file = get_push_file(data_dir=data_dir)
    rss_count = delivery_sections.get("rss", "").count("###")
    delivered_count = sum(body.count("###") for body in delivery_sections.values())
    save_push_file(
        push_file, final, rss_count, delivered_count, profile="morning", metadata=metadata
    )
    print(f"💾 Saved morning brief to {push_file}")
    
    # Record sent history.
    try:
        min_score = config["filter"]["min_score"]
        context_days = config["filter"]["context_days"]
        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        to_push, _ = await collect_entries_for_push(
            last_push_time=last_push_time,
            context_days=context_days,
            min_score=min_score,
            data_dir=data_dir,
            preferences=config.get("preferences"),
            max_items=None,
            config=config,
        )
        sent_links = links_present_in_content(to_push, final)
        mark_links_as_sent(sent_links, data_dir=data_dir)
        print(f"💾 Recorded {len(sent_links)} actually delivered morning-brief links")
    except Exception as e:
        print(f"⚠️ Failed to record sent history: {e}")



async def fetch_loop(config: Dict):
    """Fetch loop with drift correction and graceful cancellation."""
    import time

    interval_seconds = config["schedule"]["fetch_interval_minutes"] * 60
    print(f"🔄 Fetch loop started | strict interval: {interval_seconds / 60} minutes")

    while True:
        start_time = time.monotonic()  # Use monotonic time to avoid system clock changes.

        try:
            await run_fetch_job(config)
        except asyncio.CancelledError:
            print("⚠️ Fetch loop cancelled externally; exiting safely...")
            break
        except Exception as e:
            print(f"❌ Fetch job failed: {e}")

        # Calculate job duration.
        elapsed = time.monotonic() - start_time
        # Calculate remaining sleep time; skip sleep if the job exceeded the interval.
        sleep_time = max(0.0, interval_seconds - elapsed)

        if sleep_time > 0:
            print(f"⏰ Next fetch in {sleep_time / 60:.1f} minutes")

        try:
            await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            print("⚠️ Sleep interrupted; fetch loop exited safely")
            break


async def push_loop(config: Dict):
    """Push loop using stateless croniter scheduling and native async sleep."""
    cron_list = config["schedule"]["push_cron"]
    tz = get_timezone(config)

    # 1. Validate cron expressions before starting and ignore invalid entries.
    valid_crons = []
    for cron in cron_list:
        if croniter.is_valid(cron):
            valid_crons.append(cron)
        else:
            print(f"⚠️ Ignoring invalid cron expression: '{cron}'")

    if not valid_crons:
        print("❌ No valid delivery schedule configured; push loop exited")
        return

    print(f"📤 Push loop started | schedules: {', '.join(valid_crons)} | timezone: {tz}")

    # 2. Main loop.
    while True:
        try:
            now = datetime.now(tz)

            # Stateless calculation: compute the next run from the real current time
            # on each loop so long jobs or system sleep do not drift the schedule.
            next_push = min(
                croniter(cron, now).get_next(datetime) for cron in valid_crons
            )

            wait_seconds = (next_push - datetime.now(tz)).total_seconds()

            if wait_seconds > 0:
                print(
                    f"⏰ Next delivery: {next_push.strftime('%Y-%m-%d %H:%M:%S')} (wait {wait_seconds / 60:.1f} minutes)"
                )

                # Direct async sleep can be interrupted immediately by CancelledError.
                await asyncio.sleep(wait_seconds)

            # Run delivery at the scheduled time.
            print(f"📤 Running delivery: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}")
            await run_push_job(config)

            # Add a 1-second buffer to avoid repeated cron hits within the same second.
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            print("⚠️ Push loop received cancellation; exiting safely...")
            break
        except Exception as e:
            print(f"❌ Push loop error: {e}")
            # Sleep briefly after unexpected errors to avoid a tight error loop.
            await asyncio.sleep(60)


async def cmd_check(config: Dict) -> int:
    """Check LLM API connectivity for setup-time validation."""
    print("🔍 Checking LLM API connectivity...")
    try:
        await check_llm_available(config["llm"])
    except Exception as e:
        print(f"❌ LLM API is not available: {e}")
        return 1
    print("✅ LLM API is available")
    return 0


async def cmd_fetch(config: Dict) -> int:
    """Run one fetch job."""
    try:
        await run_fetch_job(config)
        return 0
    except Exception as e:
        print(f"❌ Fetch job failed: {e}")
        return 1


async def cmd_push(config: Dict) -> int:
    """Run one delivery job."""
    try:
        await run_push_job(config)
        return 0
    except Exception as e:
        print(f"❌ Push job failed: {e}")
        return 1


async def cmd_loop(config: Dict) -> int:
    """Run long-lived loops for local development and debugging."""
    print("🔍 Checking LLM API connectivity...")
    try:
        await check_llm_available(config["llm"])
        print("✅ LLM API is available")
    except Exception as e:
        print(f"❌ LLM API is not available: {e}")
        return 1
    await asyncio.gather(fetch_loop(config), push_loop(config))
    return 0


async def cmd_rss(config: Dict) -> int:
    """Run the RSS digest section once; print only, do not send."""
    print("📰 Running RSS Digest section")
    try:
        md, meta, err = await run_rss_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ RSS section failed: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ No content this time")
        return 0
    print("\n" + "=" * 60)
    print("📑 metadata:")
    if meta:
        import json as _json

        print(_json.dumps(meta, ensure_ascii=False, indent=2))
    else:
        print("(none)")
    print("=" * 60)
    print(md)
    print("=" * 60)
    return 0


async def cmd_github(config: Dict) -> int:
    """Run the GitHub Trending section once; print only, do not send."""
    print("⭐ Running GitHub Trending section")
    try:
        md, err = await run_github_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ GitHub section failed: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ No content this time")
        return 0
    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    return 0


async def cmd_hackernews(config: Dict) -> int:
    """Run the Hacker News section once; print only, do not send."""
    print("🟧 Running Hacker News section")
    try:
        md, err = await run_hackernews_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ Hacker News section failed: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ No content this time")
        return 0
    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="news-agent",
        description="News Agent local news delivery service",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="Check LLM API connectivity")
    sub.add_parser("fetch", help="Run one fetch job and exit")
    sub.add_parser("push", help="Run one delivery job and exit")
    sub.add_parser("loop", help="Run long-lived loops for development/debugging")
    sub.add_parser("rss", help="Run the RSS Digest section once; print only, do not send")
    sub.add_parser("github", help="Run the GitHub Trending section once; print only, do not send")
    sub.add_parser("hackernews", help="Run the Hacker News section once; print only, do not send")
    serve = sub.add_parser("serve", help="Start the local Web/API service and scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=12301, type=int)
    sub.add_parser("mcp", help="Start the stdio MCP service")
    service = sub.add_parser("service", help="Manage the local login-start service")
    service.add_argument(
        "action", choices=["install", "uninstall", "start", "stop", "restart", "status"]
    )
    return parser.parse_args()


def main() -> int:
    print("News Agent local news delivery service")
    args = _parse_args()

    if args.command == "serve":
        from src.server import run_server
        run_server(args.host, args.port)
        return 0
    if args.command == "mcp":
        from src.mcp_server import run_mcp
        run_mcp()
        return 0
    if args.command == "service":
        from src import lifecycle
        print(getattr(lifecycle, args.action)())
        return 0

    try:
        config = load_config()
        print("✅ Configuration loaded successfully")
    except Exception as e:
        print(f"❌ Failed to load configuration: {e}")
        return 1

    handlers = {
        "check": cmd_check,
        "fetch": cmd_fetch,
        "push": cmd_push,
        "loop": cmd_loop,
        "rss": cmd_rss,
        "github": cmd_github,
        "hackernews": cmd_hackernews,
    }
    return asyncio.run(handlers[args.command](config))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n👋 Program exited")
        sys.exit(0)
