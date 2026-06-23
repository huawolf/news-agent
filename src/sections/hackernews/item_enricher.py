"""HN 单 story enrich:Algolia 评论树 + 外链正文。

Algolia API: GET /api/v1/items/{id}
- root.text 是 Show HN / Ask HN 的 post 正文
- root.children[] 是顶层评论(按 HN ranking 排序)
- 每条 child 自己还有 children[],承载嵌套回复

enrich 策略:取 L1 + 每个 L1 下前 N 条 L2 回复,合并为 tree JSON:
    [{"l1": "顶层评论", "replies": ["回复 1", "回复 2"]}, ...]

外链正文优先走 Jina Reader (https://r.jina.ai/<url>, 返回 markdown),
失败回退到直接 GET + html_to_markdown。Jina API key 可选(配置后免费额度更高),
环境变量名通过 `sections.hackernews.jinaTokenName` 配置(默认 JINA_API_KEY)。
"""

import asyncio
import os
from typing import Dict, List, Optional, Tuple

import aiohttp

from src.processor import html_to_markdown

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

JINA_READER_BASE = "https://r.jina.ai"


def _is_internal_hn_url(url: str) -> bool:
    return url.startswith("https://news.ycombinator.com/item?id=")


async def _fetch_algolia_item(
    session: aiohttp.ClientSession, item_id: str, algolia_base: str, timeout: int
) -> Dict:
    url = f"{algolia_base}/items/{item_id}"
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Algolia /items/{item_id} 返回 {resp.status}")
        return await resp.json()


async def _fetch_url_html(
    session: aiohttp.ClientSession, url: str, timeout: int
) -> str:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"外链 {url} 返回 {resp.status}")
        return await resp.text()


async def _fetch_via_jina(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    jina_token_env: str = "JINA_API_KEY",
) -> str:
    """通过 Jina Reader 拉取外链 markdown。`jina_token_env` 指定 API key 环境变量名,配置后免费额度更高。"""
    jina_url = f"{JINA_READER_BASE}/{url}"
    headers = {"Accept": "text/markdown"}
    api_key = os.environ.get(jina_token_env) if jina_token_env else None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    async with session.get(
        jina_url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Jina Reader {url} 返回 {resp.status}")
        return await resp.text()


async def _fetch_external_markdown(
    session: aiohttp.ClientSession,
    url: str,
    timeout: int,
    jina_token_env: str = "JINA_API_KEY",
) -> str:
    """获取外链正文 markdown:先走 Jina Reader,失败回退到直接 GET + html_to_markdown。"""
    try:
        return await _fetch_via_jina(session, url, timeout, jina_token_env=jina_token_env)
    except Exception:
        html = await _fetch_url_html(session, url, timeout)
        return html_to_markdown(html, base_url=url)


def _collect_comments_tree(
    root: Dict,
    top_comments: int,
    top_l2_per_l1: int,
    comment_max_chars: int,
    comments_total_chars: int,
) -> List[Dict]:
    """从 Algolia 根节点提取评论树,返回 [{l1, replies}]。

    规则:
    - L1 上限 `top_comments`,每个 L1 下取前 `top_l2_per_l1` 条 L2 作为 replies
    - 每条 text 过 html_to_markdown,单条截断到 `comment_max_chars`
    - 累计字符达 `comments_total_chars` 时立即停止(防离群 story 撑爆 prompt)
    - 跳过空 text;空 replies 仍保留 `replies: []`,schema 一致
    """
    out: List[Dict] = []
    consumed = 0
    l1_children = (root.get("children") or [])[:top_comments]
    for l1 in l1_children:
        if consumed >= comments_total_chars:
            break
        l1_raw = (l1 or {}).get("text") or ""
        if not l1_raw:
            continue
        l1_md = html_to_markdown(l1_raw)[:comment_max_chars]
        consumed += len(l1_md)
        replies: List[str] = []
        for l2 in ((l1 or {}).get("children") or [])[:top_l2_per_l1]:
            if consumed >= comments_total_chars:
                break
            l2_raw = (l2 or {}).get("text") or ""
            if not l2_raw:
                continue
            l2_md = html_to_markdown(l2_raw)[:comment_max_chars]
            consumed += len(l2_md)
            replies.append(l2_md)
        out.append({"l1": l1_md, "replies": replies})
    return out


async def enrich_story(
    session: aiohttp.ClientSession,
    story: Dict,
    top_comments: int,
    top_l2_per_l1: int,
    comment_max_chars: int,
    comments_total_chars: int,
    link_content_max_chars: int,
    algolia_base: str,
    timeout: int,
    jina_token_env: str = "JINA_API_KEY",
) -> Dict:
    """对单 story enrich。任一子任务失败 → 对应字段留空,不抛。"""
    item_id = story["id"]
    is_internal = _is_internal_hn_url(story["url"])

    tasks = [
        _fetch_algolia_item(
            session, item_id, algolia_base=algolia_base, timeout=timeout
        )
    ]
    if not is_internal:
        tasks.append(
            _fetch_external_markdown(
                session, story["url"], timeout=timeout, jina_token_env=jina_token_env
            )
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    algolia_result = results[0]
    external_markdown_result = results[1] if not is_internal else None

    comments_tree: List[Dict] = []
    post_text = ""
    if not isinstance(algolia_result, Exception) and algolia_result:
        post_text = algolia_result.get("text") or ""
        comments_tree = _collect_comments_tree(
            algolia_result,
            top_comments=top_comments,
            top_l2_per_l1=top_l2_per_l1,
            comment_max_chars=comment_max_chars,
            comments_total_chars=comments_total_chars,
        )

    link_content = ""
    if is_internal:
        if post_text:
            link_content = html_to_markdown(post_text)[:link_content_max_chars]
    else:
        if (
            not isinstance(external_markdown_result, Exception)
            and external_markdown_result
        ):
            link_content = external_markdown_result[:link_content_max_chars]

    return {
        **story,
        "link_content": link_content,
        "top_comments": comments_tree,
    }


async def enrich_stories(
    stories: List[Dict],
    top_comments: int,
    top_l2_per_l1: int,
    comment_max_chars: int,
    comments_total_chars: int,
    link_content_max_chars: int,
    algolia_base: str = "https://hn.algolia.com/api/v1",
    timeout: int = 10,
    jina_token_env: str = "JINA_API_KEY",
) -> Tuple[List[Dict], List[str]]:
    """并发 enrich 多个 stories。"""
    errors: List[str] = []
    if os.environ.get(jina_token_env):
        print(f"🔑 HN: 已配置 {jina_token_env},Jina Reader 鉴权调用")
    else:
        print(f"⚠️ HN: 未配置 {jina_token_env},Jina Reader 匿名调用 (额度受限)")
    async with aiohttp.ClientSession(
        headers={"User-Agent": USER_AGENT}, trust_env=True
    ) as session:
        results = await asyncio.gather(
            *[
                enrich_story(
                    session,
                    s,
                    top_comments=top_comments,
                    top_l2_per_l1=top_l2_per_l1,
                    comment_max_chars=comment_max_chars,
                    comments_total_chars=comments_total_chars,
                    link_content_max_chars=link_content_max_chars,
                    algolia_base=algolia_base,
                    timeout=timeout,
                    jina_token_env=jina_token_env,
                )
                for s in stories
            ],
            return_exceptions=True,
        )
    enriched: List[Dict] = []
    for r, src in zip(results, stories):
        if isinstance(r, Exception):
            errors.append(f"enrich story {src['id']} 失败: {r}")
        else:
            enriched.append(r)
    return enriched, errors
