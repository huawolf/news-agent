"""AI每日资讯推送系统 - 主程序"""

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

# 加载 .env 文件
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from croniter import croniter

from src.config import get_timezone, load_config, merge_sources
from src.fetcher import fetch_all_feeds
from src.llm import (
    check_llm_available,
    generate_immediate_push,
    parse_immediate_push_with_metadata,
    score_batch,
)
from src.processor import html_to_markdown
from src.push import send_to_platforms
from src.sections.github.section import run_github_section
from src.sections.hackernews.section import run_hackernews_section
from src.sections.insights.section import run_insights_section
from src.sections.rss.section import run_rss_section
from src.storage import (
    append_entries,
    assemble_with_sentinels,
    cleanup_old_files,
    get_fetch_file,
    get_notify_file,
    get_push_file,
    load_existing_links,
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
    """发送简单的 LLM 异常通知"""
    if not errors:
        return

    lines = [
        "## LLM异常",
        "",
        f"stage: {stage}",
        f"time: {now_local(config).strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    lines.extend(f"- {error}" for error in errors)

    try:
        await send_to_platforms("\n".join(lines), config["push"])
    except Exception as e:
        print(f"⚠️ LLM异常通知发送失败: {e}")


def now_local(config: Dict = None) -> datetime:
    """获取配置时区的当前时间"""
    return datetime.now(get_timezone(config))


def parse_time_to_local(time_str: str, config: Dict = None) -> Optional[datetime]:
    """解析时间字符串为配置时区的datetime"""
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
    """判定两个关键词是否相似/匹配"""
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
    """计算两组关键词中重合的数量"""
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
    """基于关键词相似度去重，与过去 3 天已抓取的新闻进行比对"""
    tz = get_timezone(config)
    now = datetime.now(tz)
    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    
    history_entries = []
    for i in range(3):
        d = now.date() - timedelta(days=i)
        fetch_file = get_fetch_file(d, data_dir)
        if os.path.exists(fetch_file):
            for entry in read_entries(fetch_file):
                # 排除被置为 0 分的重复项
                if entry.get("keywords") and entry.get("score", 0) > 0:
                    history_entries.append(entry)
                    
    print(f"🔍 关键词比对：载入过去 3 天历史新闻 {len(history_entries)} 条")
    
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
            if overlap >= 5:
                is_dup = True
                matching_history_title = hist_entry.get("title", "")
                matching_history_link = hist_entry.get("link", "")
                break
                
        if is_dup:
            new_entry["score"] = 0
            new_entry["is_duplicate"] = True
            new_entry["duplicate_reason"] = f"与历史新闻《{matching_history_title}》({matching_history_link}) 关键词重合度 >= 5"
            duplicate_count += 1
            print(f"🚫 关键词去重命中：【{new_entry.get('title')}】与已发新闻【{matching_history_title}】重合")
            
    if duplicate_count > 0:
        print(f"🚫 关键词去重拦截了 {duplicate_count} 条消息 (score 被置 0)")


def is_morning_push(now: datetime, config: Dict) -> bool:
    """判定当前 push 是否为「早报」(触发 GH/HN/insights 三段)。

    规则:在 `schedule.push_cron` 列表里,now 离哪条 cron 最近,就归为那条;
    最近的那条若是当天最早的 cron,则视为早报。

    特例:
    - `push_cron` 为空 → 不视为早报
    - `push_cron` 只有一条 → 该条即"最早"也即"最近",任何触发都视为早报
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


def collect_entries_for_push(
    last_push_time: Optional[datetime],
    context_days: int = 2,
    min_score: int = 60,
    data_dir: str = "news-data",
) -> tuple[List[Dict], List[Dict]]:
    """
    收集推送所需的条目，返回 (待推送条目, 上下文条目)

    逻辑：
    1. 获取 context_days 天内的所有条目
    2. 按 min_score 过滤
    3. 加载 sent-history.json 已推送历史
    4. 过滤掉已推送的链接
    5. push_time = max(last_push_time, now - 24h)
    6. 晚于 push_time 的 → 待推送条目
    7. 早于 push_time 的 → 上下文条目（用于LLM去重参考）
    """
    tz = get_timezone()
    now = datetime.now(tz)

    # 获取 context_days 天的所有条目
    all_entries = []
    today = now.date()
    for i in range(context_days):
        d = today - timedelta(days=i)
        fetch_file = get_fetch_file(d, data_dir)
        for entry in read_entries(fetch_file):
            all_entries.append(entry)

    print(
        f"📋 收集总条目: {len(all_entries)} 条 , context_days: {context_days}, min_score:{min_score}"
    )

    # 按 min_score 过滤
    qualified_entries = [e for e in all_entries if (e.get("score") or 0) >= min_score]
    print(f"📋 过滤后条目: {len(qualified_entries)} 条 ")

    # 加载已发送历史去重
    sent_links = load_sent_links(days=3, data_dir=data_dir)
    print(f"📋 已发送历史库: {len(sent_links)} 条已过滤链接")

    # 计算推送时间边界：max(last_push_time, now - 24h)
    past_24h = now - timedelta(hours=24)
    push_cutoff = (
        last_push_time if last_push_time and last_push_time > past_24h else past_24h
    )

    print(f"推送时间边界: {push_cutoff.strftime('%Y-%m-%d %H:%M:%S')}")

    # 分割条目
    to_push = []
    context = []
    # context 只供 LLM 做去重/历史参考,不需要 content/link/fetched_at 等大字段
    CONTEXT_FIELDS = ("title", "source", "score", "summary", "tags", "published")

    for entry in qualified_entries:
        link = entry.get("link")
        if link and link in sent_links:
            continue

        entry_time = parse_time_to_local(entry.get("fetched_at", ""))
        if entry_time and entry_time > push_cutoff:
            to_push.append(entry)
        else:
            context.append({k: entry.get(k) for k in CONTEXT_FIELDS})

    # 上下文按分数排序，取前50
    context = sorted(context, key=lambda x: x.get("score", 0), reverse=True)[:50]

    return to_push, context


async def run_fetch_job(config: Dict):
    print(f"\n{'=' * 50}")
    print(f"🔄 Fetch Job | {now_local().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    interval = config["schedule"]["fetch_interval_minutes"]
    lookback = config["schedule"].get("fetch_lookback_minutes", 120)
    lookback = max(lookback, interval)
    threshold = lookback + interval
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback)

    sources = merge_sources(config["sources"])
    print(f"📂 共 {len(sources)} 个订阅源")

    if not sources:
        print("⚠️ 没有可用的订阅源")
        return

    max_workers = config.get("fetch", {}).get("max_workers", 20)
    timeout = config.get("fetch", {}).get("timeout", 30)
    
    # 1. 抓取 RSS 订阅源
    entries = await fetch_all_feeds(
        sources, cutoff, max_workers=max_workers, timeout=timeout
    )
    print(f"📥 抓取到 {len(entries)} 条 RSS 原始消息")

    # 2. 并发抓取 Hacker News (不走 Jina Reader 抓取外链，直接读 Algolia 热门评论)
    hn_entries = []
    try:
        from src.fetcher import fetch_hackernews_entries
        hn_entries = await fetch_hackernews_entries(config)
        print(f"📥 HN: 抓取到 {len(hn_entries)} 条热门评论富化消息")
    except Exception as e:
        print(f"⚠️ HN 抓取失败: {e}")

    # 合并新闻源
    all_entries = entries + hn_entries
    print(f"📥 汇总抓取到 {len(all_entries)} 条原始消息")

    if not all_entries:
        return

    # 对非 Hacker News 源的普通 RSS 条目进行 HTML 格式转换
    for entry in all_entries:
        if entry.get("source") != "Hacker News":
            entry["content"] = html_to_markdown(
                entry.get("content", ""), entry.get("link", "")
            )

    fetch_file = get_fetch_file()
    existing_links = load_existing_links(fetch_file, threshold)
    new_entries = [
        e for e in all_entries if e.get("link") and e["link"] not in existing_links
    ]
    print(f"🆕 新消息 {len(new_entries)} 条 | 链接数：{len(existing_links)}")

    if not new_entries:
        return

    print("🤖 LLM评分中...")
    # 预处理：将所有 datetime 转换为字符串，避免 JSON 序列化错误
    for entry in new_entries:
        if isinstance(entry.get("published"), datetime):
            entry["published"] = (
                entry["published"].astimezone(get_timezone(config)).isoformat()
            )

    scored, score_errors = await score_batch(new_entries, config["llm"])
    if score_errors:
        print(f"⚠️ [score_batch] {len(score_errors)} 个错误: {score_errors[0]}")
        await notify_llm_errors("score_batch", score_errors, config)

    # 3. 关键词去重校验
    deduplicate_by_keywords(scored, config)

    is_new_file = not os.path.exists(fetch_file)
    if is_new_file:
        cleanup_old_files(days=config["filter"]["keep_days"])

    # 添加 fetched_at 时间戳
    for entry in scored:
        entry["fetched_at"] = now_local().isoformat()
        if isinstance(entry.get("published"), datetime):
            entry["published"] = (
                entry["published"].astimezone(get_timezone(config)).isoformat()
            )

    # 批量保存到 JSON 文件
    from datetime import date

    meta = {"date": date.today().isoformat()}
    append_entries(fetch_file, scored, meta)

    print(f"💾 已保存到 {fetch_file}")

    hot_threshold = config["filter"]["hot_threshold"]
    no_content_marker = config["filter"].get("no_content_marker", "[NO_NEW_CONTENT]")
    hot_entries = [e for e in scored if (e.get("score") or 0) >= hot_threshold]
    if hot_entries:
        print(f"🔥 发现 {len(hot_entries)} 条热点消息，即时推送...")

        # 加载近期已推送内容（仅供 LLM 查重，避免风格趋同）
        context_days = config["filter"]["context_days"]
        recent_notify = load_recent_notify_content(context_days)
        recent_push = load_recent_push_content(context_days)
        recent_context = (
            f"=== 近期即时推送 ===\n{recent_notify}\n\n"
            f"=== 近期汇总推送 ===\n{recent_push}"
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
            print("⚠️ 即时推送内容生成失败，跳过本次热点推送")
            print(
                f"✅ Fetch Job 完成 | 新消息: {len(scored)} 条 | 热点: {len(hot_entries)} 条"
            )
            return

        # 检查是否有实际内容需要推送
        if no_content_marker in push_content:
            print(f"ℹ️ 无新内容需要推送 (LLM判定为重复内容)")
        else:
            # 提取标题并构建 metadata
            now = now_local(config)
            timestamp = now.strftime("%Y-%m-%d %H:%M")
            content_without_title, metadata = parse_immediate_push_with_metadata(
                push_content, f"🚨 AI Daily 快讯 | {timestamp}"
            )
            metadata["pushTime"] = now.isoformat()

            await send_to_platforms(
                content_without_title,
                config["push"],
                "🚨 AI Daily 快讯 | " + metadata["title"],
                metadata=metadata,
            )
            # 保存即时推送内容到notify文件
            notify_file = get_notify_file()
            save_notify_file(notify_file, content_without_title, metadata)
            print(f"💾 已保存即时推送到 {notify_file}")

            # 4. 记录已发送链接到 sent-history.json
            sent_links = [e.get("link") for e in hot_entries if e.get("link")]
            mark_links_as_sent(sent_links, data_dir=config.get("storage", {}).get("data_dir", "news-data"))
            print(f"💾 已把 {len(sent_links)} 条快讯链接记录到已发送历史")

    print(f"✅ Fetch Job 完成 | 新消息: {len(scored)} 条 | 热点: {len(hot_entries)} 条")


async def run_push_job(config: Dict):
    print(f"\n{'=' * 50}")
    print(f"📤 Push Job | {now_local().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 50}")

    if is_morning_push(now_local(config), config):
        await _run_morning_push(config)
    else:
        await _run_default_push(config)


async def _run_default_push(config: Dict):
    """晚报或非早报时段:沿用原有纯 RSS digest 流程,委托给 run_rss_section"""
    now = now_local(config)
    rss_md, metadata, rss_err = await run_rss_section(config, now)

    if rss_err and not rss_md:
        print(f"⚠️ [compose_digest] {rss_err}")
        await notify_llm_errors("compose_digest", [rss_err], config)
        raise RuntimeError(f"RSS section failed: {rss_err}")

    if not rss_md:
        # run_rss_section 在无新消息时已打印 "ℹ️ RSS: 无新消息"
        return

    # metadata 缺失兜底:parse_digest_with_metadata 失败 / LLM 输出无 frontmatter 时可能返回 None
    if not metadata:
        date_str = now.strftime("%Y-%m-%d")
        metadata = {
            "title": f"🌙 AI Daily 晚报 | {date_str}",
            "lead": "",
            "highlights": [],
            "profile": "default",
            "date": date_str,
        }
    metadata.setdefault("pushTime", now.isoformat())

    await send_to_platforms(
        rss_md,
        config["push"],
        title="📰 AI Daily 每日精选 | " + metadata["title"],
        metadata=metadata,
    )
    last_push_file = get_last_push_file()
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    push_file = get_push_file()
    rss_count = rss_md.count("###")
    save_push_file(
        push_file,
        rss_md,
        rss_count,
        rss_count,
        profile="default",
        metadata=metadata,
    )
    print(f"💾 已保存到 {push_file}")
    
    # 记录已发送历史
    try:
        min_score = config["filter"]["min_score"]
        context_days = config["filter"]["context_days"]
        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        to_push, _ = collect_entries_for_push(
            last_push_time=last_push_time,
            context_days=context_days,
            min_score=min_score,
            data_dir=data_dir,
        )
        sent_links = [e.get("link") for e in to_push if e.get("link")]
        mark_links_as_sent(sent_links, data_dir=data_dir)
        print(f"💾 已把 {len(sent_links)} 条晚报链接记录到已发送历史")
    except Exception as e:
        print(f"⚠️ 记录已发送历史失败: {e}")

    print(f"✅ Push Job 完成 | 推送条目: {rss_count}")



async def _run_morning_push(config: Dict):
    """早报四模块编排:RSS/GH/HN 并发 → insights 串行 → sentinel 拼装 → 推送 → 落盘。

    失败语义:
    - RSS 失败 → 整体抛 RuntimeError (核心承诺不变)
    - GH/HN/insights 失败 → 该段省略 + 告警,其他段照推
    """
    now = now_local(config)

    rss_result, gh_result, hn_result = await asyncio.gather(
        run_rss_section(config, now),
        run_github_section(config, now),
        run_hackernews_section(config, now),
    )

    # 早报场景:digest 的 metadata 通常会被 insights 段覆盖,
    # 但保留以便在 insights 失败时作为兜底来源(title 关键词 / lead / highlights)
    rss_md, digest_meta, rss_err = rss_result
    gh_md, gh_err = gh_result
    hn_md, hn_err = hn_result

    if gh_err:
        print(f"⚠️ [section_github] {gh_err}")
        await notify_llm_errors("section_github", [gh_err], config)
    if hn_err:
        print(f"⚠️ [section_hackernews] {hn_err}")
        await notify_llm_errors("section_hackernews", [hn_err], config)

    if rss_err and not rss_md:
        print(f"⚠️ [compose_digest] {rss_err}")
        await notify_llm_errors("compose_digest", [rss_err], config)
        raise RuntimeError(f"RSS section failed: {rss_err}")

    insights_md, metadata, insights_err = await run_insights_section(
        rss_md, gh_md, hn_md, config, now
    )
    if insights_err:
        print(f"⚠️ [insights] {insights_err}")
        await notify_llm_errors("insights", [insights_err], config)

    # 如果 insights 失败,优先用 digest metadata 兜底;两者都缺再走默认
    if not metadata:
        date_str = now.strftime("%Y-%m-%d")
        fallback = digest_meta or {}
        digest_title = fallback.get("title", "")

        title = digest_title if digest_title else f"📰 AI Daily 每日精选 | {date_str}"
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

    final = assemble_with_sentinels(
        {
            "rss": rss_md,
            "github": gh_md,
            "hackernews": hn_md,
            "insights": insights_md,
        }
    )

    if not final.strip():
        print("ℹ️ 早报无任何段输出,跳过推送")
        return

    await send_to_platforms(
        final,
        config["push"],
        title="📰 AI Daily 每日精选 | " + metadata["title"],
        metadata=metadata,
    )
    last_push_file = get_last_push_file()
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    push_file = get_push_file()
    rss_count = rss_md.count("###") if rss_md else 0
    save_push_file(
        push_file, final, rss_count, rss_count, profile="morning", metadata=metadata
    )
    print(f"💾 已保存早报到 {push_file}")
    
    # 记录已发送历史
    try:
        min_score = config["filter"]["min_score"]
        context_days = config["filter"]["context_days"]
        data_dir = config.get("storage", {}).get("data_dir", "news-data")
        to_push, _ = collect_entries_for_push(
            last_push_time=last_push_time,
            context_days=context_days,
            min_score=min_score,
            data_dir=data_dir,
        )
        sent_links = [e.get("link") for e in to_push if e.get("link")]
        mark_links_as_sent(sent_links, data_dir=data_dir)
        print(f"💾 已把 {len(sent_links)} 条早报链接记录到已发送历史")
    except Exception as e:
        print(f"⚠️ 记录已发送历史失败: {e}")



async def fetch_loop(config: Dict):
    """Fetch循环 - 修复时间漂移并支持优雅退出"""
    import time

    interval_seconds = config["schedule"]["fetch_interval_minutes"] * 60
    print(f"🔄 Fetch循环已启动 | 严格间隔: {interval_seconds / 60}分钟")

    while True:
        start_time = time.monotonic()  # 使用 monotonic 避免系统时间修改影响

        try:
            await run_fetch_job(config)
        except asyncio.CancelledError:
            print("⚠️ Fetch循环被外部取消，正在安全退出...")
            break  # 允许外部取消任务
        except Exception as e:
            print(f"❌ Fetch Job 失败: {e}")

        # 计算任务耗时
        elapsed = time.monotonic() - start_time
        # 计算还需要睡多久（如果任务耗时超过间隔，则不睡，立刻进入下一次）
        sleep_time = max(0.0, interval_seconds - elapsed)

        if sleep_time > 0:
            print(f"⏰ 下次抓取: {sleep_time / 60:.1f}分钟后")

        try:
            await asyncio.sleep(sleep_time)
        except asyncio.CancelledError:
            print("⚠️ 睡眠被中断，Fetch循环安全退出...")
            break


async def push_loop(config: Dict):
    """Push循环 - 无状态 croniter + 原生异步睡眠"""
    cron_list = config["schedule"]["push_cron"]
    tz = get_timezone(config)

    # 1. 启动前预校验 cron 表达式，过滤掉无效配置
    valid_crons = []
    for cron in cron_list:
        if croniter.is_valid(cron):
            valid_crons.append(cron)
        else:
            print(f"⚠️ 忽略无效的 cron 表达式: '{cron}'")

    if not valid_crons:
        print("❌ 没有有效的推送时间配置，Push循环退出")
        return

    print(f"📤 Push循环已启动 | 定时: {', '.join(valid_crons)} | 时区: {tz}")

    # 2. 主循环
    while True:
        try:
            now = datetime.now(tz)

            # 💡 核心优化：无状态计算。
            # 每次都基于此刻的真实时间，动态计算所有有效 cron 的下一次时间，取最近的一个。
            # 这样无论 run_push_job 执行多久，或者系统休眠过，永远都不会算错。
            next_push = min(
                croniter(cron, now).get_next(datetime) for cron in valid_crons
            )

            wait_seconds = (next_push - datetime.now(tz)).total_seconds()

            if wait_seconds > 0:
                print(
                    f"⏰ 下次推送: {next_push.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds / 60:.1f} 分钟)"
                )

                # 💡 核心优化：直接 Sleep。asyncio 天生支持被 CancelledError 瞬间打断
                await asyncio.sleep(wait_seconds)

            # 到达推送时间，执行推送
            print(f"📤 执行推送: {datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')}")
            await run_push_job(config)

            # 增加 1 秒缓冲：防止 run_push_job 执行过快（不到 1 秒），
            # 导致下一个循环的 now 仍停留在当前秒，croniter 算出重复的时间点。
            await asyncio.sleep(1)

        except asyncio.CancelledError:
            print("⚠️ Push循环收到取消信号，安全退出...")
            break  # 直接 break 退出循环即可
        except Exception as e:
            print(f"❌ Push 循环异常: {e}")
            # 遇到未知异常时休眠 60 秒，防止死循环疯狂报错打满日志
            await asyncio.sleep(60)


async def cmd_check(config: Dict) -> int:
    """校验 LLM 接口可达性（部署期使用，运行期不再校验）"""
    print("🔍 校验 LLM 接口...")
    try:
        await check_llm_available(config["llm"])
    except Exception as e:
        print(f"❌ LLM 接口不可用: {e}")
        return 1
    print("✅ LLM 接口可用")
    return 0


async def cmd_fetch(config: Dict) -> int:
    """单次抓取（systemd timer 调用）"""
    try:
        await run_fetch_job(config)
        return 0
    except Exception as e:
        print(f"❌ Fetch 任务失败: {e}")
        return 1


async def cmd_push(config: Dict) -> int:
    """单次推送（systemd timer 调用）"""
    try:
        await run_push_job(config)
        return 0
    except Exception as e:
        print(f"❌ Push 任务失败: {e}")
        return 1


async def cmd_loop(config: Dict) -> int:
    """长跑模式（本地开发/调试用）"""
    print("🔍 检查 LLM 接口可用性...")
    try:
        await check_llm_available(config["llm"])
        print("✅ LLM 接口可用")
    except Exception as e:
        print(f"❌ LLM 接口不可用: {e}")
        return 1
    await asyncio.gather(fetch_loop(config), push_loop(config))
    return 0


async def cmd_rss(config: Dict) -> int:
    """单独跑一次 RSS digest 板块,打印结果不推送"""
    print("📰 RSS Digest 单板块运行")
    try:
        md, meta, err = await run_rss_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ RSS 板块失败: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ 本次无内容")
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
    """单独跑一次 GitHub trending 板块,打印结果不推送"""
    print("⭐ GitHub Trending 单板块运行")
    try:
        md, err = await run_github_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ GitHub 板块失败: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ 本次无内容")
        return 0
    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    return 0


async def cmd_hackernews(config: Dict) -> int:
    """单独跑一次 Hacker News 板块,打印结果不推送"""
    print("🟧 Hacker News 单板块运行")
    try:
        md, err = await run_hackernews_section(config, now=now_local(config))
    except Exception as e:
        print(f"❌ Hacker News 板块失败: {e}")
        return 1
    if err:
        print(f"❌ {err}")
        return 1
    if not md:
        print("ℹ️ 本次无内容")
        return 0
    print("\n" + "=" * 60)
    print(md)
    print("=" * 60)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="daily-news",
        description="AI 每日资讯推送系统",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="校验 LLM 接口可达性")
    sub.add_parser("fetch", help="单次抓取并退出")
    sub.add_parser("push", help="单次推送并退出")
    sub.add_parser("loop", help="长跑模式（开发/调试用）")
    sub.add_parser("rss", help="单独跑一次 RSS Digest 板块（仅打印,不推送）")
    sub.add_parser("github", help="单独跑一次 GitHub Trending 板块（仅打印,不推送）")
    sub.add_parser("hackernews", help="单独跑一次 Hacker News 板块（仅打印,不推送）")
    return parser.parse_args()


def main() -> int:
    print("🚀 AI每日资讯推送系统")
    args = _parse_args()

    try:
        config = load_config()
        print("✅ 配置加载成功")
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
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
        print("\n👋 程序已退出")
        sys.exit(0)
