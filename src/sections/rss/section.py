"""RSS 板块:沿用现有 collect_entries_for_push + compose_digest 流程

迁移自 src/main.py::run_push_job 中 RSS digest 部分,行为保持一致。
"""

from datetime import datetime
from typing import Dict, Optional, Tuple

from src.llm import compose_digest, parse_digest_with_metadata
from src.storage import (
    extract_push_time,
    get_last_push_file,
    load_recent_push_content,
)


async def run_rss_section(
    config: Dict, now: Optional[datetime] = None
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

    last_push_file = get_last_push_file()
    last_push_time = extract_push_time(last_push_file) if last_push_file else None

    min_score = config["filter"]["min_score"]
    context_days = config["filter"]["context_days"]

    to_push, context = collect_entries_for_push(
        last_push_time=last_push_time,
        context_days=context_days,
        min_score=min_score,
    )

    if not to_push:
        print("ℹ️ RSS: 无新消息")
        return "", None, None

    push_context_days = config["filter"].get("push_context_days", 5)
    recent = load_recent_push_content(push_context_days)

    try:
        raw = await compose_digest(
            to_push, context, config["llm"], recent_push_context=recent
        )
    except Exception as e:
        msg = f"compose_digest 失败: {e}"
        print(f"⚠️ RSS: {msg}")
        return "", None, msg

    date_str = (now or datetime.now()).strftime("%Y-%m-%d")
    body, metadata = parse_digest_with_metadata(raw or "", date_str)
    return body, metadata, None
