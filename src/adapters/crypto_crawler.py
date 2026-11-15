#!/usr/bin/env python3
"""
crypto_crawler.py — 统一多源加密资讯与重要信息流 CDP 采集系统

设计特性与核心规范：
1. 模块化站点架构 (BaseSiteHandler)：
   - 每个网站一个独立处理模块，高内聚低耦合；
   - 支持全网通用 URL 自动路由（给定任意支持的 URL 自动解析并抓取）；
   - 支持 5 大顶级资讯源：CryptoPanic, BlockBeats, Followin, Foresight News, Binance Square。
2. 守护进程检测与异常感知：
   - 抓取时探测本地 BrowserWorker 守护进程 (端口 12321)；
   - 若守护进程未运行，明确抛出/返回 DaemonNotRunningError，记录错误日志，并通过 API 向上游调用透明返回错误字典。
3. 鲁棒 Tab 生命周期与原地复用防封：
   - 一网站一独立安全 Tab (cryptopanic_tab, blockbeats_tab, followin_tab, foresight_tab, binance_square_tab)；
   - Tab 自愈机制：若目标标签页被意外关闭，自动执行 new_tab 重建并等待页面渲染；
   - 原地复用防封：若目标标签页已处于目标站点，直接原地复用执行 evaluate，零重复刷新、零反复跳转；
   - 严禁干扰 main (fomo.family) 或其他业务标签页。
4. 安全熔断与防反复重试：
   - JS 内部原生检测 Cloudflare Turnstile / WAF 质询 (Just a moment 等)；若被封，立即抛出/返回 WAFBlockedError，严禁反复盲目重试；
   - 若 DOM 选择器失效或未能匹配任何条目，抛出/返回 DOMStructureChangedError，报错返回。
5. 24h 时间范围过滤与指定起始时间 (since_time)：
   - 默认最多抓取 24 小时内的 feed；
   - 支持指定起始时间 since_time (支持 Unix 时间戳、ISO 8601 字符串、或 datetime 对象)；
   - 时间全部标准化解析为 UTC 时间 (published_utc)。
6. 独立 CLI 结构化数据存储与增量去重：
   - 保存的核心字段至少包括：published_utc (UTC发布时间), title (标题), content (内容/正文), url (链接)；
   - 状态库 crypto_state.json 维护 48h 去重老化；
   - 自动落盘至 CSV (skills/crawl-crypto/data/crypto_news_YYYYMMDD.csv) 与最新 JSON 快照。
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# =====================================================================
# 日志配置
# =====================================================================
logger = logging.getLogger(__name__)

DEFAULT_DAEMON_PORT = 12321


# =====================================================================
# 核心异常体系
# =====================================================================
class CrawlError(Exception):
    """抓取基础异常"""
    def __init__(self, message: str, error_type: str = "CRAWL_ERROR"):
        super().__init__(message)
        self.message = message
        self.error_type = error_type

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": False,
            "error_type": self.error_type,
            "message": self.message,
        }


class DaemonNotRunningError(CrawlError):
    """Chrome 插件守护进程未运行异常"""
    def __init__(self, message: str = "BrowserWorker 守护进程未启动或端口 12321 无法连通"):
        super().__init__(message, error_type="DAEMON_NOT_RUNNING")


class WAFBlockedError(CrawlError):
    """被 Cloudflare 或 WAF 风控拦截（严禁重试）"""
    def __init__(self, message: str = "检测到 Cloudflare / WAF 质询拦截"):
        super().__init__(message, error_type="WAF_BLOCKED")


class DOMStructureChangedError(CrawlError):
    """DOM 结构发生改变（选择器未命中）"""
    def __init__(self, message: str = "页面 DOM 结构已变化，未匹配到任何资讯条目"):
        super().__init__(message, error_type="DOM_STRUCTURE_CHANGED")


class UnsupportedURLError(CrawlError):
    """不支持的 URL 地址"""
    def __init__(self, message: str):
        super().__init__(message, error_type="UNSUPPORTED_URL")


# =====================================================================
# 数据模型：包含时间 (UTC)、title、content、url 等核心字段
# =====================================================================
@dataclass
class CryptoFeedItem:
    published_utc: str       # 发布时间 (标准 UTC ISO 8601，例如 "2026-10-07T08:32:00+00:00")
    title: str               # 标题
    content: str             # 内容 (摘要或正文观点)
    url: str                 # 对应资讯链接
    source_id: str           # 渠道代码: cryptopanic, blockbeats, followin, foresight, binance
    source_name: str         # 渠道名称
    news_id: str             # 渠道唯一标识符
    currencies: list[str]    # 关联代币 (例如 ["BTC", "ETH"])
    filter_tab: str          # 抓取分类/标签 (Important, Pro Flash, Headlines 等)
    is_important: bool       # 是否为重要新闻/加红新闻
    captured_utc: str        # 本次抓取入库时间 (UTC)
    published_at: float      # 发布时间戳 (秒，用于时间过滤排序)
    captured_at: float       # 抓取时间戳 (秒)
    source_domain: str = ""  # 来源站点或作者昵称

    def to_csv_row(self) -> dict[str, Any]:
        return {
            "published_utc": self.published_utc,
            "title": self.title,
            "content": self.content,
            "url": self.url,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "news_id": self.news_id,
            "currencies": "|".join(self.currencies) if self.currencies else "",
            "filter_tab": self.filter_tab,
            "is_important": "1" if self.is_important else "0",
            "captured_utc": self.captured_utc,
            "source_domain": self.source_domain,
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CSV_HEADER = [
    "published_utc",
    "title",
    "content",
    "url",
    "source_id",
    "source_name",
    "news_id",
    "currencies",
    "filter_tab",
    "is_important",
    "captured_utc",
    "source_domain",
]


# =====================================================================
# 时间标准化解析与范围过滤模块
# =====================================================================
class TimeParser:
    """
    通用时间解析器：
    - 精确识别 ISO 8601、相对时间 (19h ago, 1d ago, 35m, 4h, 15m, 2小时前 等)；
    - 精确识别国内快讯常见格式 (16:32, 10-07 16:32) 并根据当前本地时间校准跨午夜问题；
    - 统一将时间转换为标准的 UTC ISO 8601 字符串 (published_utc)。
    """

    @staticmethod
    def parse_to_utc(time_str: str, base_time: float | None = None) -> tuple[float, str]:
        now = base_time if base_time is not None else time.time()
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        now_local = datetime.fromtimestamp(now).astimezone()
        raw = (time_str or "").strip()

        if not raw or raw in ["实时", "实时/Breaking", "最新", "Just now", "Breaking"]:
            return now, now_dt.isoformat()

        # 1. 尝试 ISO 8601 及带时区格式
        for fmt in [
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
        ]:
            try:
                dt = datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                utc_dt = dt.astimezone(timezone.utc)
                return utc_dt.timestamp(), utc_dt.isoformat()
            except ValueError:
                pass

        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            utc_dt = dt.astimezone(timezone.utc)
            return utc_dt.timestamp(), utc_dt.isoformat()
        except Exception:
            pass

        # 2. 英文/中文相对时间：如 "19h ago", "23h ago", "1d ago", "35m ago", "4h", "15m", "2d", "2小时前", "30秒前"
        rel_m = re.match(
            r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks|秒|分|分钟|小时|天|周)\s*(ago|前)?$",
            raw,
            re.IGNORECASE,
        )
        if rel_m:
            val = int(rel_m.group(1))
            unit = rel_m.group(2).lower()
            delta_sec = 0
            if unit in ["s", "sec", "secs", "second", "seconds", "秒"]:
                delta_sec = val
            elif unit in ["m", "min", "mins", "minute", "minutes", "分", "分钟"]:
                delta_sec = val * 60
            elif unit in ["h", "hr", "hrs", "hour", "hours", "小时"]:
                delta_sec = val * 3600
            elif unit in ["d", "day", "days", "天"]:
                delta_sec = val * 86400
            elif unit in ["w", "week", "weeks", "周"]:
                delta_sec = val * 7 * 86400

            feed_ts = max(0.0, now - delta_sec)
            feed_dt = datetime.fromtimestamp(feed_ts, tz=timezone.utc)
            return feed_ts, feed_dt.isoformat()

        # 3. 纯时间 HH:MM (快讯常见，以客户端本地时区基准对齐并校准跨午夜)
        hm_m = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        if hm_m:
            h, m = int(hm_m.group(1)), int(hm_m.group(2))
            feed_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if feed_local > now_local + timedelta(minutes=5):
                # 超过当前时间，说明跨越了午夜，属于昨天的快讯
                feed_local -= timedelta(days=1)
            feed_utc = feed_local.astimezone(timezone.utc)
            return feed_utc.timestamp(), feed_utc.isoformat()

        # 4. 月日时间 MM-DD HH:MM
        md_m = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", raw)
        if md_m:
            mo, d, h, m = int(md_m.group(1)), int(md_m.group(2)), int(md_m.group(3)), int(md_m.group(4))
            feed_local = now_local.replace(month=mo, day=d, hour=h, minute=m, second=0, microsecond=0)
            if feed_local > now_local + timedelta(minutes=5):
                try:
                    feed_local = feed_local.replace(year=feed_local.year - 1)
                except ValueError:
                    pass
            feed_utc = feed_local.astimezone(timezone.utc)
            return feed_utc.timestamp(), feed_utc.isoformat()

        # 兜底：返回当前 UTC 时间
        return now, now_dt.isoformat()

    @staticmethod
    def parse_since_time(since_val: float | int | datetime | str | None) -> float | None:
        """解析用户传入的指定起始时间"""
        if since_val is None:
            return None
        if isinstance(since_val, (int, float)):
            return float(since_val)
        if isinstance(since_val, datetime):
            return since_val.timestamp()
        if isinstance(since_val, str):
            since_str = since_val.strip()
            # 尝试纯数字时间戳
            try:
                return float(since_str)
            except ValueError:
                pass
            # 尝试 ISO 字符串
            try:
                dt = datetime.fromisoformat(since_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                pass
            # 常用完整格式
            for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(since_str, fmt)
                    # 默认本地时区转换
                    dt = dt.astimezone()
                    return dt.timestamp()
                except ValueError:
                    pass
        logger.warning(f"无法识别的 since_time 参数: {since_val}，将使用默认24小时窗口")
        return None

    @classmethod
    def filter_items_by_time(
        cls,
        items: list[CryptoFeedItem],
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
    ) -> list[CryptoFeedItem]:
        """
        核心时间门禁：
        1. 默认最多抓取 24 小时内的 feed (max_hours = 24.0)；
        2. 若指定了 since_time，则从指定时间开始抓取 (published_at >= cutoff_ts)；
        3. 自动过滤掉任何超出范围的过期历史数据。
        """
        now = time.time()
        max_past_sec = max_hours * 3600.0
        earliest_allowed_ts = now - max_past_sec

        since_ts = cls.parse_since_time(since_time)
        if since_ts is not None:
            # 取指定时间与最大允许过去时间中的较晚者，严格守住 max_hours 门禁
            cutoff_ts = max(since_ts, earliest_allowed_ts)
        else:
            cutoff_ts = earliest_allowed_ts

        filtered = [it for it in items if it.published_at >= cutoff_ts]
        logger.info(
            f"时间过滤完成: 原始 {len(items)} 条 -> 符合时间范围 {len(filtered)} 条 "
            f"(门槛时间 UTC: {datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()})"
        )
        return filtered


# =====================================================================
# 通用 WAF / 防爬拦截原生检测 JS 片段
# =====================================================================
JS_WAF_CHECK_PREAMBLE = r"""
    // 1. 深度检测 Cloudflare Turnstile / Challenge / Block / Human Verification 阻断 (多语言支持)
    const docTitle = (document.title || "").toLowerCase();
    const bText = (document.body && document.body.innerText) ? document.body.innerText.toLowerCase() : "";
    const cfFrame = document.querySelector('iframe[src*="cloudflare"], iframe[src*="turnstile"], #challenge-running, #cf-turnstile');
    
    const isWAFBlocked = !!cfFrame ||
                         docTitle.includes("attention required") ||
                         docTitle.includes("just a moment") ||
                         docTitle.includes("security check") ||
                         docTitle.includes("human verification") ||
                         docTitle.includes("人机验证") ||
                         bText.includes("you have been blocked") ||
                         bText.includes("why have i been blocked") ||
                         bText.includes("just a moment") || 
                         bText.includes("attention required") || 
                         bText.includes("access denied") || 
                         bText.includes("checking your browser") || 
                         bText.includes("please verify you are human") ||
                         bText.includes("我们需要确认您是人类") ||
                         bText.includes("完成安全检查") ||
                         bText.includes("验证您不是机器人") ||
                         bText.includes("human verification");
    if (isWAFBlocked) {
        return {
            __status: "BLOCKED",
            error: `Detected WAF / Human Verification barrier (title='${document.title}')`
        };
    }
"""


# =====================================================================
# 站点处理模块抽象基类与各平台 Handler
# =====================================================================
class BaseSiteHandler:
    source_id: str = ""
    source_name: str = ""
    default_url: str = ""
    tab_key: str = ""
    domain_keywords: list[str] = []
    filter_tab_default: str = ""

    def matches_url(self, url: str) -> bool:
        """检查给定 URL 是否由当前 Handler 处理"""
        url_lower = url.lower()
        return any(k.lower() in url_lower for k in self.domain_keywords)

    def get_extract_js(self) -> str:
        """返回注入执行的 JS 代码，必须包含前置 WAF 检测"""
        raise NotImplementedError

    def parse_raw_item(self, raw: dict, now: float, now_utc: str) -> CryptoFeedItem:
        """将 JS 返回的原始字典转化为标准的 CryptoFeedItem"""
        time_raw = str(raw.get("time_str", "")).strip()
        pub_ts, pub_utc = TimeParser.parse_to_utc(time_raw, base_time=now)
        title = str(raw.get("title", "")).strip()
        summary = str(raw.get("summary", "")).strip() or title
        link = str(raw.get("link", "")).strip()
        news_id = str(raw.get("news_id", "")).strip() or link

        currencies = raw.get("currencies", [])
        if not isinstance(currencies, list):
            currencies = [str(currencies)]

        return CryptoFeedItem(
            published_utc=pub_utc,
            title=title,
            content=summary,
            url=link,
            source_id=self.source_id,
            source_name=self.source_name,
            news_id=news_id,
            currencies=currencies,
            filter_tab=str(raw.get("filter_tab", self.filter_tab_default)),
            is_important=bool(raw.get("is_important", True)),
            captured_utc=now_utc,
            published_at=pub_ts,
            captured_at=now,
            source_domain=str(raw.get("source_domain", "")).strip(),
        )


class CryptoPanicHandler(BaseSiteHandler):
    source_id = "cryptopanic"
    source_name = "CryptoPanic (Important)"
    default_url = "https://cryptopanic.com/news?filter=important"
    tab_key = "cryptopanic_tab"
    domain_keywords = ["cryptopanic.com"]
    filter_tab_default = "Important"

    def get_extract_js(self) -> str:
        return f"""(() => {{
            {JS_WAF_CHECK_PREAMBLE}

            const rows = Array.from(document.querySelectorAll('.news-row:not(.sponsored)'));
            if (rows.length === 0 && document.readyState === "complete") {{
                if (document.querySelectorAll('.news-row').length === 0) {{
                    return {{ __status: "DOM_CHANGED", error: "No news-row found on CryptoPanic" }};
                }}
            }}

            const items = [];
            for (const r of rows) {{
                const titleSpan = r.querySelector('.nc-title .title-text > span:first-child');
                const title = titleSpan ? titleSpan.innerText.trim() : "";
                if (!title) continue;

                // 优先从 time 标签属性提取标准 ISO 时间或相对时间
                const timeEl = r.querySelector('.nc-date time');
                const timeStr = timeEl ? (timeEl.getAttribute('datetime') || timeEl.innerText.trim() || "") : "";

                const currEls = Array.from(r.querySelectorAll('.nc-currency a'));
                const currencies = currEls.map(a => a.innerText.trim()).filter(Boolean);

                const srcEl = r.querySelector('.si-source-domain');
                const sourceDomain = srcEl ? srcEl.innerText.trim() : "";

                const clickArea = r.querySelector('a.click-area, a.nc-title');
                const link = clickArea ? clickArea.href : "";
                const idMatch = link.match(/\\/news\\/(\\d+)/);
                const newsId = idMatch ? idMatch[1] : link;

                items.push({{
                    news_id: newsId,
                    title: title,
                    summary: title,
                    time_str: timeStr,
                    currencies: currencies,
                    link: link,
                    filter_tab: "Important",
                    is_important: true,
                    source_domain: sourceDomain
                }});
            }}
            return items;
        }})()"""


class BlockBeatsHandler(BaseSiteHandler):
    source_id = "blockbeats"
    source_name = "BlockBeats (Pro Flash)"
    default_url = "https://en.theblockbeats.news/newsflash"
    tab_key = "blockbeats_tab"
    domain_keywords = ["theblockbeats.news", "blockbeats"]
    filter_tab_default = "Pro Flash"

    def get_extract_js(self) -> str:
        return f"""(async () => {{
            {JS_WAF_CHECK_PREAMBLE}

            try {{
                const tabs = Array.from(document.querySelectorAll('.flash-tab-item'));
                const proTab = tabs.find(t => t.innerText.includes('Pro Flash'));
                if (proTab && !proTab.className.includes('active')) {{
                    proTab.click();
                    await new Promise(r => setTimeout(r, 1500));
                }}
            }} catch(e) {{}}

            const rows = Array.from(document.querySelectorAll('.news-flash-item-top'));
            if (rows.length === 0 && document.readyState === "complete") {{
                return {{ __status: "DOM_CHANGED", error: "No news-flash-item-top found on BlockBeats" }};
            }}

            const items = [];
            for (const r of rows) {{
                const titleEl = r.querySelector('.news-flash-title-text');
                const title = titleEl ? titleEl.innerText.trim() : "";
                if (!title) continue;

                const timeEl = r.querySelector('.time-ready');
                const timeStr = timeEl ? timeEl.innerText.trim() : "";

                const linkEl = r.querySelector('a.news-flash-title');
                let link = linkEl ? (linkEl.getAttribute('href') || linkEl.href) : "";
                if (link && !link.startsWith('http')) {{
                    link = `https://en.theblockbeats.news${{link}}`;
                }}
                const idMatch = link.match(/\\/flash\\/(\\d+)/);
                const newsId = idMatch ? idMatch[1] : link;

                const currencies = [];
                const cryptoMatches = title.match(/\\b(BTC|ETH|SOL|BNB|USDT|USDC|DOGE|XRP|ADA|ZEC|LINK|AVAX|TON|SUI|APT|TRUMP|HOOD)\\b/g);
                if (cryptoMatches) {{
                    Array.from(new Set(cryptoMatches)).forEach(c => currencies.push(c));
                }}

                items.push({{
                    news_id: newsId,
                    title: title,
                    summary: title,
                    time_str: timeStr,
                    currencies: currencies,
                    link: link,
                    filter_tab: "Pro Flash",
                    is_important: true,
                    source_domain: "BlockBeats"
                }});
            }}
            return items;
        }})()"""


class FollowinHandler(BaseSiteHandler):
    source_id = "followin"
    source_name = "Followin (Headlines)"
    default_url = "https://followin.io/en?tab=headlines"
    tab_key = "followin_tab"
    domain_keywords = ["followin.io"]
    filter_tab_default = "Headlines"

    def get_extract_js(self) -> str:
        return f"""(async () => {{
            {JS_WAF_CHECK_PREAMBLE}

            let feeds = Array.from(document.querySelectorAll('a[href*="/feed/"]'));
            if (feeds.length === 0) {{
                await new Promise(r => setTimeout(r, 2500));
                feeds = Array.from(document.querySelectorAll('a[href*="/feed/"]'));
            }}

            if (feeds.length === 0 && document.readyState === "complete") {{
                return {{ __status: "DOM_CHANGED", error: "No feed links found on Followin" }};
            }}

            const items = [];
            for (const f of feeds) {{
                const text = f.innerText || "";
                const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);
                if (lines.length === 0) continue;

                let author = "";
                let timeStr = "";
                let content = "";
                let currencies = [];

                // 智能时间锚点定位算法
                const timeIdx = lines.findIndex(l => /^[0-9]+[mhd]\\s+ago$/i.test(l) || /^[0-9]{{1,2}}:[0-9]{{2}}$/.test(l));
                if (timeIdx > 0) {{
                    author = lines.slice(0, timeIdx).join(' ');
                    timeStr = lines[timeIdx];
                    const remaining = lines.slice(timeIdx + 1);
                    const contentLines = [];
                    for (const r of remaining) {{
                        if (/^[+-]?[0-9.]+%/i.test(r)) continue;
                        if (r.length <= 6 && r === r.toUpperCase() && !r.includes(' ')) {{
                            currencies.push(r);
                            continue;
                        }}
                        contentLines.push(r);
                    }}
                    content = contentLines.join(' ');
                }} else {{
                    content = lines.join(' ');
                    timeStr = "实时/Breaking";
                }}

                if (!content || content.length < 8) continue;

                const cryptoMatches = content.match(/\\b(BTC|ETH|SOL|BNB|USDT|USDC|DOGE|XRP|ADA|ZEC|LINK|AVAX|TON|SUI|APT|ORCA|CAP|RH)\\b/g);
                if (cryptoMatches) {{
                    cryptoMatches.forEach(c => {{ if (!currencies.includes(c)) currencies.push(c); }});
                }}

                const title = content.length > 80 ? content.slice(0, 80) + "..." : content;
                const idMatch = f.href.match(/\\/feed\\/(\\d+)/);
                const newsId = idMatch ? idMatch[1] : f.href;

                items.push({{
                    news_id: newsId,
                    title: title,
                    summary: content,
                    time_str: timeStr,
                    currencies: currencies,
                    link: f.href,
                    filter_tab: "Headlines",
                    is_important: true,
                    source_domain: author || "Followin"
                }});
            }}
            return items;
        }})()"""


class ForesightHandler(BaseSiteHandler):
    source_id = "foresight"
    source_name = "Foresight News (Important)"
    default_url = "https://foresightnews.pro/news"
    tab_key = "foresight_tab"
    domain_keywords = ["foresightnews.pro", "foresightnews"]
    filter_tab_default = "Important"

    def get_extract_js(self) -> str:
        return f"""(() => {{
            {JS_WAF_CHECK_PREAMBLE}

            const timelineItems = Array.from(document.querySelectorAll('.timeline-item'));
            if (timelineItems.length === 0 && document.readyState === "complete") {{
                return {{ __status: "DOM_CHANGED", error: "No timeline-item found on Foresight News" }};
            }}

            const items = [];
            for (const t of timelineItems) {{
                const timeEl = t.querySelector('.timeline-time');
                const timeStr = timeEl ? timeEl.innerText.trim() : "";

                const card = t.querySelector('.news-card, a[href*="/news/detail/"]');
                if (!card) continue;

                const titleEl = card.querySelector('.news-title') || card;
                const title = (titleEl.innerText || "").trim();
                if (!title) continue;

                const isImportant = titleEl.classList ? titleEl.classList.contains('important') : false;
                const contentEl = card.querySelector('.news-content');
                const summary = contentEl ? contentEl.innerText.trim() : title;

                const linkHref = card.getAttribute('href') || (card.querySelector('a') ? card.querySelector('a').getAttribute('href') : "");
                const link = linkHref.startsWith('http') ? linkHref : `https://foresightnews.pro${{linkHref}}`;
                const idMatch = link.match(/\\/detail\\/(\\d+)/);
                const newsId = idMatch ? idMatch[1] : link;

                const tagEls = Array.from(card.querySelectorAll('.tag, .market-symbol'));
                const tags = [];
                for (const el of tagEls) {{
                    const txt = el.innerText.split('\\n')[0].trim();
                    if (txt && txt.length <= 8 && !tags.includes(txt)) tags.push(txt);
                }}

                const cryptoMatches = (title + " " + summary).match(/\\b(BTC|ETH|SOL|BNB|USDT|USDC|DOGE|XRP|ADA|BGB|TRUMP)\\b/g);
                if (cryptoMatches) {{
                    cryptoMatches.forEach(c => {{ if (!tags.includes(c)) tags.push(c); }});
                }}

                items.push({{
                    news_id: newsId,
                    title: title,
                    summary: summary,
                    time_str: timeStr,
                    currencies: tags,
                    link: link,
                    filter_tab: isImportant ? "Important" : "News",
                    is_important: isImportant,
                    source_domain: "Foresight News"
                }});
            }}
            return items;
        }})()"""


class BinanceSquareHandler(BaseSiteHandler):
    source_id = "binance"
    source_name = "Binance Square (News)"
    default_url = "https://www.binance.com/en/square/news/all"
    tab_key = "binance_square_tab"
    domain_keywords = ["binance.com", "binance"]
    filter_tab_default = "Official News"

    def get_extract_js(self) -> str:
        return f"""(() => {{
            {JS_WAF_CHECK_PREAMBLE}

            const cards = Array.from(document.querySelectorAll('[datatype="squareCard"]'));
            if (cards.length === 0 && document.readyState === "complete") {{
                return {{ __status: "DOM_CHANGED", error: "No squareCard elements found on Binance Square" }};
            }}

            const items = [];
            for (const card of cards) {{
                const titleEl = card.querySelector('.card__title h3, .card__title');
                const title = titleEl ? titleEl.innerText.trim() : "";
                if (!title) continue;

                const descEl = card.querySelector('.card__description');
                const summary = descEl ? descEl.innerText.trim() : title;

                const aEl = card.querySelector('a');
                const linkHref = aEl ? aEl.getAttribute('href') : "";
                const link = linkHref.startsWith('http') ? linkHref : `https://www.binance.com${{linkHref}}`;
                const idMatch = link.match(/\\/post\\/(\\d+)/);
                const newsId = idMatch ? idMatch[1] : link;

                // 提取祖先节点时间
                let wrap = card;
                for (let i = 0; i < 4; i++) {{
                    if (wrap.parentElement) wrap = wrap.parentElement;
                }}
                const timeEl = wrap.querySelector('time, [class*="time"], [class*="date"]');
                let timeStr = timeEl ? timeEl.innerText.trim() : "";
                if (!timeStr) {{
                    const m = wrap.innerText.match(/·\\s*([0-9]+[mhd])/i);
                    if (m) timeStr = m[1];
                }}

                const currencies = [];
                const cryptoMatches = (title + " " + summary).match(/\\b(BTC|ETH|SOL|BNB|USDT|USDC|DOGE|XRP|ADA|ZEC|LINK|AVAX|TON|SUI|APT|TRUMP)\\b/g);
                if (cryptoMatches) {{
                    Array.from(new Set(cryptoMatches)).forEach(c => currencies.push(c));
                }}

                items.push({{
                    news_id: newsId,
                    title: title,
                    summary: summary,
                    time_str: timeStr || "最新",
                    currencies: currencies,
                    link: link,
                    filter_tab: "Official News",
                    is_important: true,
                    source_domain: "Binance Square"
                }});
            }}
            return items;
        }})()"""


# =====================================================================
# URL 路由分发器
# =====================================================================
class URLRouter:
    """给定任意 URL，自动正则匹配对应的 SiteHandler"""
    def __init__(self):
        self._handlers: list[BaseSiteHandler] = [
            CryptoPanicHandler(),
            BlockBeatsHandler(),
            FollowinHandler(),
            ForesightHandler(),
            BinanceSquareHandler(),
        ]

    def resolve_by_url(self, url: str) -> BaseSiteHandler:
        for handler in self._handlers:
            if handler.matches_url(url):
                return handler
        supported = [h.source_name for h in self._handlers]
        raise UnsupportedURLError(f"无法识别的抓取地址: {url}。当前支持的站点: {', '.join(supported)}")

    def resolve_by_id(self, source_id: str) -> BaseSiteHandler:
        for handler in self._handlers:
            if handler.source_id == source_id:
                return handler
        raise UnsupportedURLError(f"未知的渠道 source_id: {source_id}")

    def all_handlers(self) -> list[BaseSiteHandler]:
        return self._handlers


# =====================================================================
# CDP 浏览器守护进程通信与 Tab 自愈管理器
# =====================================================================
class BrowserWorkerManager:
    """
    负责与 BrowserWorker 通信：
    - 守护进程探活（若未运行直接抛出 DaemonNotRunningError）；
    - Tab 存在性探测与自愈（若被关则 new_tab 重新打开）；
    - 原地复用（已存在则直接 evaluate，彻底防封）；
    - 严格隔离（专属 tab_key，绝不触碰 main 或其他业务标签页）。
    """
    def __init__(self, daemon_port: int | None = None):
        self.daemon_port = daemon_port or self._find_daemon_port()

    @staticmethod
    def _find_daemon_port() -> int:
        if os.environ.get("BrowserWorker_PORT"):
            try:
                return int(os.environ["BrowserWorker_PORT"])
            except ValueError:
                pass
        lock_candidates = [
            Path(os.environ.get("BrowserWorker_LOCK_FILE", "")),
            Path(__file__).resolve().parents[3]
            / "manai_worker"
            / "storage"
            / "browser_daemon.lock",
            Path.home() / ".BrowserWorker" / "BrowserWorker.lock",
        ]
        for lock in lock_candidates:
            if lock and lock.exists():
                try:
                    data = json.loads(lock.read_text(encoding="utf-8"))
                    return int(data.get("port", DEFAULT_DAEMON_PORT))
                except Exception:
                    pass
        return DEFAULT_DAEMON_PORT

    def check_daemon_alive(self) -> dict[str, Any]:
        """探活 BrowserWorker 守护进程，如果未开启则抛出 DaemonNotRunningError"""
        url = f"http://127.0.0.1:{self.daemon_port}/status"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=4) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("service") != "BrowserWorker" or data.get("status") != "ok":
                    raise DaemonNotRunningError(
                        f"BrowserWorker returned an unexpected health response: {data}"
                    )
                return data
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
            msg = f"Chrome 插件守护进程未开启或端口 {self.daemon_port} 不可达 ({e})"
            logger.error(f"[CDP] {msg}")
            raise DaemonNotRunningError(msg) from e

    def get_active_session(
        self,
        status_data: dict[str, Any] | None = None,
        preferred_session: str | None = None,
    ) -> str:
        st = status_data or self.check_daemon_alive()
        tabs_map = st.get("tabs", {})
        if preferred_session:
            return preferred_session
        if "fomo_tracker_session" in tabs_map:
            return "fomo_tracker_session"
        elif tabs_map:
            return next(iter(tabs_map))
        return "fomo_tracker_session"

    def execute_steps(self, steps: list[dict], session_id: str | None = None) -> list[dict]:
        active_session = session_id or self.get_active_session()
        payload = {
            "session_id": active_session,
            "action": "flexible_access",
            "args": {"steps": steps},
        }
        url = f"http://127.0.0.1:{self.daemon_port}/execute"
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=40) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            if not res.get("success"):
                raise CrawlError(
                    f"BrowserWorker action failed: {res}",
                    error_type="BROWSER_ACTION_FAILED",
                )
            return res.get("result", {}).get("results", [])
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError) as e:
            msg = f"与 Chrome 守护进程通信失败: {e}"
            logger.error(f"[CDP] {msg}")
            raise DaemonNotRunningError(msg) from e


CDPManager = BrowserWorkerManager


# =====================================================================
# 数据去重与持久化存储层
# =====================================================================
class CryptoFeedStore:
    def __init__(self, data_dir: str | Path | None = None):
        if data_dir is None:
            self.data_dir = Path(__file__).resolve().parents[2] / "data"
        else:
            self.data_dir = Path(data_dir)
        self.state_file = self.data_dir / "crypto_state.json"
        self._state: dict[str, float] = self._load_state()

    def _load_state(self) -> dict[str, float]:
        if not self.state_file.exists():
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            # 自动老化：清理 48 小时之前的老记录，防止状态文件膨胀
            cutoff = time.time() - 48 * 3600
            clean_state = {k: v for k, v in self._state.items() if v >= cutoff}
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(clean_state, f, ensure_ascii=False, indent=2)
            self._state = clean_state
        except Exception as e:
            logger.warning(f"保存去重状态库失败: {e}")

    def deduplicate(self, items: list[CryptoFeedItem]) -> list[CryptoFeedItem]:
        new_items = []
        now = time.time()
        for it in items:
            key = f"{it.source_id}:{it.news_id}"
            if key not in self._state:
                self._state[key] = now
                new_items.append(it)
        self._save_state()
        return new_items

    def save(self, items: list[CryptoFeedItem]) -> tuple[Path, Path]:
        """将采集到的条目保存到当日 CSV 和最新快照 JSON"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
        csv_file = self.data_dir / f"crypto_news_{date_str}.csv"
        json_file = self.data_dir / "crypto_news_latest.json"

        if items:
            file_exists = csv_file.exists()
            with open(csv_file, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if not file_exists:
                    writer.writeheader()
                for it in items:
                    writer.writerow(it.to_csv_row())

            with open(json_file, "w", encoding="utf-8") as f:
                json.dump([it.to_dict() for it in items], f, ensure_ascii=False, indent=2)

        return csv_file, json_file


# =====================================================================
# 统一加密资讯采集主引擎
# =====================================================================
class CryptoCrawler:
    """
    统一加密货币重要资讯 CDP 采集主引擎：
    - 支持给定任意 URL 抓取；
    - 守护进程探活与熔断；
    - Tab 关闭自愈重建；
    - 原地复用彻底防封；
    - WAF 质询熔断；
    - DOM 结构变化报错返回；
    - 24h 时间限制与 since_time 过滤；
    - CLI 模式可选择落盘标准格式；项目管线调用时可关闭文件存储。
    """
    def __init__(
        self,
        data_dir: str | Path | None = None,
        daemon_port: int | None = None,
        session_id: str | None = None,
        persist_store: bool = True,
    ):
        self.router = URLRouter()
        self.cdp = BrowserWorkerManager(daemon_port=daemon_port)
        self.session_id = session_id
        self.store = CryptoFeedStore(data_dir=data_dir) if persist_store else None

    def _prepare_tab_steps(self, handler: BaseSiteHandler, target_url: str, st: dict[str, Any], session_id: str) -> list[dict]:
        """检查标签页状态：若不存在则重建 (自愈)，若在目标域名则原地复用 (防封)"""
        tab_key = handler.tab_key
        has_tab = False
        is_on_target = False

        s_info = st.get("tabs", {}).get(session_id, {})
        for t in s_info.get("tabs", []):
            if t.get("tab_key") == tab_key:
                has_tab = True
                curr_url = t.get("url", "").lower()
                if any(kw.lower() in curr_url for kw in handler.domain_keywords):
                    is_on_target = True
                break

        steps = []
        if not has_tab:
            logger.info(f"[{handler.source_name}] 标签页未找到或已被关闭，执行自愈重建: tab_key={tab_key} -> {target_url}")
            steps.append({"op": "new_tab", "args": [tab_key, target_url]})
            # SPA 页面多预留一些水合渲染时间
            wait_time = 5000 if handler.source_id in ["followin", "blockbeats"] else 4000
            steps.append({"op": "wait_for_timeout", "args": [wait_time], "tab_key": tab_key})
        elif not is_on_target:
            logger.info(f"[{handler.source_name}] 标签页存在但不在目标域名，执行安全跳转: {target_url}")
            steps.append({"op": "goto", "args": [target_url], "tab_key": tab_key})
            steps.append({"op": "wait_for_timeout", "args": [4000], "tab_key": tab_key})
        else:
            logger.info(f"[{handler.source_name}] 原地复用专属独立标签页 (零重连/零刷新，彻底防封)")
            steps.append({"op": "wait_for_timeout", "args": [800], "tab_key": tab_key})

        return steps

    def crawl_handler(
        self,
        handler: BaseSiteHandler,
        url_override: str | None = None,
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
    ) -> list[CryptoFeedItem]:
        """抓取指定 Handler 对应站点的数据"""
        target_url = url_override or handler.default_url

        # 1. 守护进程检查
        st = self.cdp.check_daemon_alive()
        session_id = self.cdp.get_active_session(st, self.session_id)

        # 2. 准备 Tab 与导航步骤 (支持 Tab 自愈与原地复用)
        steps = self._prepare_tab_steps(handler, target_url, st, session_id)

        # 3. 追加执行抽取 JS
        steps.append({
            "op": "evaluate",
            "args": [handler.get_extract_js()],
            "tab_key": handler.tab_key,
            "kwargs": {"timeout_ms": 25000},
        })

        now = time.time()
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

        # 4. 执行 CDP 操作
        results = self.cdp.execute_steps(steps, session_id=session_id)

        # 5. 分析执行结果与熔断检测
        raw_items: list[dict] = []
        for step_res in results:
            if step_res.get("op") == "evaluate":
                val = step_res.get("value")
                # 质询与熔断检查
                if isinstance(val, dict):
                    status = val.get("__status")
                    if status == "BLOCKED":
                        err_msg = val.get("error", "Cloudflare WAF blocked")
                        logger.error(f"[{handler.source_name}] 触发防爬/WAF拦截: {err_msg}。立即报错返回，严禁重试！")
                        raise WAFBlockedError(f"[{handler.source_name}] {err_msg}")
                    elif status == "DOM_CHANGED":
                        err_msg = val.get("error", "DOM structure changed")
                        logger.error(f"[{handler.source_name}] DOM 结构发生变化: {err_msg}。报错返回！")
                        raise DOMStructureChangedError(f"[{handler.source_name}] {err_msg}")
                elif isinstance(val, list):
                    raw_items = val
                    break

        if not raw_items:
            # 如果成功获取但列表为空，视为结构变化
            raise DOMStructureChangedError(f"[{handler.source_name}] 页面未匹配到任何有效资讯条目 (选择器可能已失效)")

        # 6. 解析结构化字段
        feed_items: list[CryptoFeedItem] = []
        for r in raw_items:
            try:
                item = handler.parse_raw_item(r, now, now_utc)
                feed_items.append(item)
            except Exception as e:
                logger.warning(f"[{handler.source_name}] 解析单条数据异常: {e}")

        # 7. 24h 时间范围过滤与指定 since_time 过滤
        filtered_items = TimeParser.filter_items_by_time(
            feed_items,
            since_time=since_time,
            max_hours=max_hours,
        )

        logger.info(f"[{handler.source_name}] 成功提取 {len(filtered_items)} 条有效资讯 (原始 {len(feed_items)} 条)")
        return filtered_items

    def crawl_url(
        self,
        url: str,
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
        persist: bool = True,
    ) -> dict[str, Any]:
        """
        核心对外 API：给定一个 URL 即可完成抓取并返回
        """
        try:
            handler = self.router.resolve_by_url(url)
            items = self.crawl_handler(
                handler,
                url_override=url,
                since_time=since_time,
                max_hours=max_hours,
            )

            new_items = self.store.deduplicate(items) if persist and self.store else items
            csv_path = None
            if persist and self.store and new_items:
                csv_file, _json_file = self.store.save(new_items)
                csv_path = str(csv_file)

            return {
                "success": True,
                "url": url,
                "source_id": handler.source_id,
                "source_name": handler.source_name,
                "total_count": len(items),
                "new_count": len(new_items),
                "csv_path": csv_path,
                "items": [it.to_dict() for it in items],
            }
        except CrawlError as ce:
            logger.error(f"[API] 抓取失败 ({ce.error_type}): {ce.message}")
            err_res = ce.to_dict()
            err_res["url"] = url
            return err_res
        except Exception as e:
            logger.exception(f"[API] 抓取未知异常: {e}")
            return {
                "success": False,
                "error_type": "UNKNOWN_ERROR",
                "message": str(e),
                "url": url,
            }

    def fetch_feed_json(
        self,
        url: str,
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
        persist: bool = True,
    ) -> str:
        """
        上游核心 API 方法：
        输入 url、起始时间；返回结果的 JSON 字符串 (包含数据条目或详细错误信息)
        """
        res = self.crawl_url(
            url=url,
            since_time=since_time,
            max_hours=max_hours,
            persist=persist,
        )
        return json.dumps(res, ensure_ascii=False, indent=2)

    def crawl_source(
        self,
        source_id: str,
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
        persist: bool = True,
    ) -> dict[str, Any]:
        """按渠道标识抓取"""
        try:
            handler = self.router.resolve_by_id(source_id)
            items = self.crawl_handler(
                handler,
                since_time=since_time,
                max_hours=max_hours,
            )
            new_items = self.store.deduplicate(items) if persist and self.store else items
            csv_path = None
            if persist and self.store and new_items:
                csv_file, _ = self.store.save(new_items)
                csv_path = str(csv_file)
            return {
                "success": True,
                "source_id": handler.source_id,
                "source_name": handler.source_name,
                "total_count": len(items),
                "new_count": len(new_items),
                "csv_path": csv_path,
                "items": [it.to_dict() for it in items],
            }
        except CrawlError as ce:
            logger.error(f"[API] 抓取渠道 {source_id} 失败: {ce.message}")
            return ce.to_dict()
        except Exception as e:
            return {
                "success": False,
                "error_type": "UNKNOWN_ERROR",
                "message": str(e),
            }

    def crawl_all(
        self,
        since_time: float | int | datetime | str | None = None,
        max_hours: float = 24.0,
    ) -> dict[str, Any]:
        """串行遍历抓取全部 5 大支持站点"""
        # 前置守护进程探活
        try:
            self.cdp.check_daemon_alive()
        except DaemonNotRunningError as de:
            return de.to_dict()

        handlers = self.router.all_handlers()
        all_items: list[CryptoFeedItem] = []
        source_summary: dict[str, int] = {}
        errors: dict[str, str] = {}

        for h in handlers:
            try:
                items = self.crawl_handler(
                    h,
                    since_time=since_time,
                    max_hours=max_hours,
                )
                source_summary[h.source_id] = len(items)
                all_items.extend(items)
            except CrawlError as ce:
                logger.error(f"渠道 {h.source_id} 抓取异常 ({ce.error_type}): {ce.message}")
                source_summary[h.source_id] = 0
                errors[h.source_id] = f"{ce.error_type}: {ce.message}"
            except Exception as e:
                logger.error(f"渠道 {h.source_id} 未知异常: {e}")
                source_summary[h.source_id] = 0
                errors[h.source_id] = str(e)

            # 渠道间留出 2 秒安全缓冲，降低并发与流量冲击
            time.sleep(2)

        if self.store is None:
            raise RuntimeError("crawl_all requires persistent storage")
        new_items = self.store.deduplicate(all_items)
        csv_file, json_file = self.store.save(new_items)

        # 生成汇报摘要
        lines = [
            "### 🚀 多源加密资讯聚合采集完成",
            f"- **总计抓取**: **{len(all_items)}** 条 (新发现 **{len(new_items)}** 条)",
            f"- **时间范围**: 过去 **{max_hours:.1f}** 小时内 (统一转为 UTC 时间)",
            f"- **持久化文件**: `{csv_file}`",
            "\n**各渠道统计**:",
        ]
        for h in handlers:
            cnt = source_summary.get(h.source_id, 0)
            err = f" (❌ {errors[h.source_id]})" if h.source_id in errors else ""
            lines.append(f"- {h.source_name}: **{cnt}** 条{err}")

        if all_items:
            lines.append("\n| UTC时间 | 渠道 | 关联代币 | 标题 | 链接 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for it in all_items[:15]:
                curr = " ".join([f"`{c}`" for c in it.currencies]) if it.currencies else "—"
                lines.append(
                    f"| `{it.published_utc[11:19]}` | {it.source_name.split()[0]} | {curr} | **{it.title[:65].replace('|', '/')}** | [详情]({it.url}) |"
                )

        return {
            "success": True,
            "total_count": len(all_items),
            "new_count": len(new_items),
            "source_summary": source_summary,
            "errors": errors,
            "csv_path": str(csv_file),
            "items": [it.to_dict() for it in all_items],
            "message": "\n".join(lines),
        }


# =====================================================================
# 顶层标准 API 函数 (供上游无须实例化直接调用)
# =====================================================================
def fetch_crypto_feed_json(
    url: str,
    since_time: float | int | datetime | str | None = None,
    max_hours: float = 24.0,
    persist: bool = True,
    data_dir: str | Path | None = None,
    daemon_port: int | None = None,
) -> str:
    """
    统一上游调用 API：
    输入：
      - url: 目标抓取网页 URL (支持 CryptoPanic, BlockBeats, Followin, Foresight News, Binance Square)
      - since_time: 起始时间 (支持 Unix 时间戳、ISO 8601 字符串、datetime 对象、或 "YYYY-MM-DD HH:MM:SS")
      - max_hours: 最大跨度小时数，默认 24.0 小时 (默认最多抓取 24h 内)
      - persist: 是否同时将抓取到的新数据落盘去重保存至 CSV 与 JSON (默认 True)
    返回：
      - 结果的 JSON 字符串。
        若成功: 返回包含 items 列表 (每项含 published_utc, title, content, url 等) 的 JSON 字符串；
        若失败: 返回包含 success: false, error_type, message, url 的 JSON 字符串。
    """
    crawler = CryptoCrawler(data_dir=data_dir, daemon_port=daemon_port)
    return crawler.fetch_feed_json(
        url=url,
        since_time=since_time,
        max_hours=max_hours,
        persist=persist,
    )


# =====================================================================
# 命令行 CLI 入口
# =====================================================================
def main():
    parser = argparse.ArgumentParser(description="多源加密资讯 CDP 统一采集器")
    parser.add_argument("--url", type=str, help="指定抓取的网页 URL")
    parser.add_argument("--source", type=str, help="指定抓取的渠道 source_id (cryptopanic, blockbeats, followin, foresight, binance)")
    parser.add_argument("--since", type=str, help="指定抓取的起始时间 (例如 2026-10-07T12:00:00 或 时间戳)")
    parser.add_argument("--max-hours", type=float, default=24.0, help="最多抓取过去多少小时内的资讯 (默认 24.0)")
    parser.add_argument("--all", action="store_true", help="抓取所有支持的资讯源")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    crawler = CryptoCrawler()

    if args.url:
        res = crawler.crawl_url(args.url, since_time=args.since, max_hours=args.max_hours)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.source:
        res = crawler.crawl_source(args.source, since_time=args.since, max_hours=args.max_hours)
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        res = crawler.crawl_all(since_time=args.since, max_hours=args.max_hours)
        if "message" in res:
            print("\n" + res["message"])
        else:
            print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
