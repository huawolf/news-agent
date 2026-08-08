"""RSS 板块:沿用现有 collect_entries_for_push + compose_digest 流程

迁移自 src/main.py::run_push_job 中 RSS digest 部分,行为保持一致。
"""

import re
from datetime import datetime
from typing import Dict, Optional, Tuple

from src.llm import _llm_error_message, compose_digest, parse_digest_with_metadata
from src.storage import (
    extract_push_time,
    format_recent_push_summary_context,
    get_last_push_file,
    load_recent_push_content,
)


_ITEM_HEADING_RE = re.compile(r"(?m)^###\s+.+?\s*$")


def _fill_digest_items(
    body: str, entries: list[Dict], max_items: int, output_language: str
) -> str:
    """Fill an undersized model digest from already ranked and scored entries."""
    target = max(1, int(max_items))
    current = len(_ITEM_HEADING_RE.findall(body or ""))
    if current >= target:
        return body

    parts = [body.strip()] if body and body.strip() else []
    represented_links = {
        entry.get("link")
        for entry in entries
        if entry.get("link") and entry.get("link") in (body or "")
    }
    link_label = "Read original" if output_language == "en" else "查看原文"

    for entry in entries:
        if current >= target:
            break
        link = str(entry.get("link") or "").strip()
        title = " ".join(str(entry.get("title") or "").split())
        summary = " ".join(str(entry.get("summary") or "").split())
        if not link or link in represented_links or not title or not summary:
            continue
        current += 1
        represented_links.add(link)
        parts.append(f"### {current}. {title}\n{summary} [{link_label}]({link})")

    if current < target:
        print(f"⚠️ Digest fill stopped at {current}/{target}: insufficient complete candidates")
    elif len(parts) > 1:
        print(f"✅ Digest filled to {current}/{target} items from ranked candidates")
    return "\n\n".join(parts)


async def run_rss_section(
    config: Dict, now: Optional[datetime] = None, max_items: Optional[int] = None
) -> Tuple[str, Optional[Dict], Optional[str]]:
    """生成 RSS digest markdown 段(不含 sentinel)。

    返回:
        (markdown_body, metadata, error)
        - 无新内容时返回 ("", None, None)
        - compose_digest 失败时返回 ("", None, error_message)
        - metadata 字段:title / lead / highlights / profile=default / date
          早报场景下调用方可丢弃 metadata(由 insights 段覆盖)
    """
    # 延迟 import 避免循环:Task 20-21 后 main.py 会反向 import run_rss_section
    from src.main import collect_entries_for_push

    data_dir = config.get("storage", {}).get("data_dir", "news-data")
    last_push_file = get_last_push_file(data_dir)
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    min_score = config["filter"]["min_score"]
    context_days = config["filter"]["context_days"]

    target_items = max(1, int(max_items or 10))

    to_push, context = await collect_entries_for_push(
        last_push_time=last_push_time,
        context_days=context_days,
        min_score=min_score,
        data_dir=data_dir,
        preferences=config.get("preferences"),
        max_items=None,
        config=config,
    )

    if not to_push:
        print("ℹ️ RSS: 无新消息")
        return "", None, None

    candidate_limit = target_items * 3
    to_push = to_push[:candidate_limit]
    print(
        f"📊 Digest candidate pool: kept={len(to_push)}, "
        f"target={target_items}, multiplier=3"
    )

    push_context_days = config["filter"].get("push_context_days", 5)
    recent = load_recent_push_content(push_context_days, data_dir=data_dir)
    recent = format_recent_push_summary_context(recent)

    try:
        raw = await compose_digest(
            to_push,
            context,
            config["llm"],
            recent_push_context=recent,
            max_items=target_items,
        )
    except Exception as e:
        msg = _llm_error_message("compose_digest 失败", e)
        print(f"⚠️ RSS: {msg}")
        return "", None, msg

    date_str = (now or datetime.now()).strftime("%Y-%m-%d")
    body, metadata = parse_digest_with_metadata(raw or "", date_str)
    body = _fill_digest_items(
        body,
        to_push,
        target_items,
        config.get("output_language", "zh"),
    )
    return body, metadata, None
