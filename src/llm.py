"""LLM模块 - 评分和汇总"""

import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.markdown_utils import normalize_str_list, parse_frontmatter


def load_prompt(prompt_path: str, **kwargs) -> str:
    """加载提示词模板并填充变量"""
    path = Path(prompt_path)
    if not path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {prompt_path}")

    with open(path, "r", encoding="utf-8") as f:
        template = f.read()

    # 先把模板中的 {{ 和 }} 替换成占位符，避免与format冲突
    template = template.replace("{{", "\x00LEFT_BRACE\x00").replace(
        "}}", "\x00RIGHT_BRACE\x00"
    )

    # 替换变量
    for key, value in kwargs.items():
        template = template.replace(f"{{{key}}}", str(value))

    # 恢复 {{ 和 }}
    template = template.replace("\x00LEFT_BRACE\x00", "{").replace(
        "\x00RIGHT_BRACE\x00", "}"
    )

    return template


async def call_llm(
    prompt: str, config: Dict, response_format: Optional[Dict] = None
) -> str:
    """调用LLM API - 统一使用OpenAI兼容接口"""
    model = config.get("model", "gpt-4o-mini")
    base_url = config.get("baseUrl", "https://api.openai.com/v1")
    api_key_name = config.get("apiKeyName", "OPENAI_API_KEY")

    api_key = os.environ.get(api_key_name)
    if not api_key:
        raise ValueError(f"未设置{api_key_name}环境变量")

    import aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    if response_format is not None:
        payload["response_format"] = response_format

    url = f"{base_url}/chat/completions"

    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API错误: {resp.status} - {text}")

            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def check_llm_available(config: Dict, timeout_seconds: int = 15) -> str:
    """启动时检查 LLM 接口可用性"""
    prompt = "Reply with OK only."

    try:
        response = await asyncio.wait_for(
            call_llm(prompt, config), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"LLM可用性检查超时({timeout_seconds}s)") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM可用性检查失败: {exc}") from exc

    response_text = response.strip()
    if not response_text:
        raise RuntimeError("LLM可用性检查返回空响应")

    return response_text


def _build_batch_prompt(
    entries: List[Dict], prompt_path: str = None, preferences: str = ""
) -> str:
    """构建批量评分prompt"""
    # 构建entries JSON列表（只包含必要字段）
    entries_for_llm = []
    for e in entries:
        entries_for_llm.append(
            {
                "link": e.get("link", ""),
                "title": e.get("title", "无标题"),
                "source": e.get("source", "未知来源"),
                "published": e.get("published", ""),
                "content": e.get("content", "")[:2000],  # 限制内容长度
            }
        )

    entries_json = json.dumps(entries_for_llm, ensure_ascii=False, indent=2)

    # 从文件加载提示词模板，如果未指定则使用默认路径
    if prompt_path is None:
        prompt_path = "prompts/score_batch.md"

    return load_prompt(prompt_path, entries_json=entries_json, preferences=preferences)


def _parse_llm_json_response(response: str) -> List[Dict]:
    """解析LLM返回的JSON响应"""
    text = response.strip()

    # 尝试去除markdown代码块
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]

    if text.endswith("```"):
        text = text[:-3]

    text = text.strip()

    # 尝试查找JSON数组
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print("⚠️ 直接解析JSON失败，尝试从文本中提取JSON数组")
            pass

    # 尝试从文本中提取JSON数组
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            print("⚠️ 从文本中提取JSON数组失败:", text)
            pass

    raise ValueError(f"无法从响应中解析JSON: {response[:200]}...")


def _parse_score_response(response: str) -> List[Dict]:
    """解析评分LLM响应。

    json_object 模式下应返回 {"items": [...]} 形式的对象;
    兼容直接数组与 markdown 包裹作为兜底路径。
    """
    text = response.strip()

    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        for pattern in (r"\{.*\}", r"\[.*\]"):
            match = re.search(pattern, text, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    break
                except json.JSONDecodeError:
                    continue

    if parsed is None:
        print(f"无法从响应中解析JSON: {response}")
        raise ValueError(f"无法从响应中解析JSON: {response[:200]}...")

    if isinstance(parsed, list):
        return parsed

    if isinstance(parsed, dict):
        for key in ("items", "results", "data", "scores"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        list_values = [v for v in parsed.values() if isinstance(v, list)]
        if len(list_values) == 1:
            return list_values[0]

    print(f"无法从响应中提取评分数组: {response}")
    raise ValueError(f"无法从响应中提取评分数组: {response[:200]}...")


def _split_entries_for_batch(
    entries: List[Dict], max_prompt_chars: int = 10000
) -> List[List[Dict]]:
    """将entries分成多个批次，每批不超过max_prompt_chars字符"""
    if not entries:
        return []

    batches = []
    current_batch = []
    current_chars = 0

    # 预留prompt模板和JSON包装的空间
    overhead = len(_build_batch_prompt([])) + 500

    for entry in entries:
        # 估算该entry在JSON中的字符数
        entry_chars = len(
            json.dumps(
                {
                    "link": entry.get("link", ""),
                    "title": entry.get("title", "")[:100],
                    "source": entry.get("source", ""),
                    "published": entry.get("published", ""),
                    "content": entry.get("content", "")[:2000],
                },
                ensure_ascii=False,
            )
        )

        # 如果当前批次加上这个entry会超出限制，且当前批次不为空，则创建新批次
        if current_chars + entry_chars + overhead > max_prompt_chars and current_batch:
            batches.append(current_batch)
            current_batch = [entry]
            current_chars = entry_chars
        else:
            current_batch.append(entry)
            current_chars += entry_chars

    # 添加最后一个批次
    if current_batch:
        batches.append(current_batch)

    return batches


def _reconcile_batch_results(
    entries: List[Dict], results: List[Dict], batch_index: int
) -> Tuple[List[Dict], List[str]]:
    """对单批评分结果按 link 过滤，保留可回收结果"""
    entry_links = {entry.get("link") for entry in entries if entry.get("link")}
    matched_results = []
    result_links = set()

    for item in results:
        if not isinstance(item, dict):
            continue

        link = item.get("link")
        if link:
            result_links.add(link)
            if link in entry_links:
                matched_results.append(item)

    errors = []
    if len(results) != len(entries) or len(matched_results) != len(entries):
        missing_links = sorted(entry_links - result_links)
        error_message = (
            "批次{batch} 评分结果异常: 输入{input_count}, 返回{output_count}, "
            "匹配{matched_count}, 未评分链接({missing_count}): {missing}"
        ).format(
            batch=batch_index + 1,
            input_count=len(entries),
            output_count=len(results),
            matched_count=len(matched_results),
            missing_count=len(missing_links),
            missing=missing_links,
        )
        print(f"⚠️ {error_message}")
        errors.append(error_message)

    return matched_results, errors


async def _score_single_batch(
    entries: List[Dict], config: Dict, batch_index: int = 0
) -> Tuple[List[Dict], List[str]]:
    """对单批entries进行评分"""
    # 从config获取批量评分提示词路径
    prompt_path = config.get("prompts", {}).get("score_batch", "prompts/score_batch.md")
    preferences = config.get("personal_preferences", "")
    prompt = _build_batch_prompt(entries, prompt_path, preferences=preferences)


    try:
        response = await call_llm(
            prompt, config, response_format={"type": "json_object"}
        )
        results = _parse_score_response(response)

        if not isinstance(results, list):
            raise ValueError(f"LLM返回的不是数组: {type(results)}")

        return _reconcile_batch_results(entries, results, batch_index)

    except Exception as e:
        error_message = f"批次{batch_index + 1} 评分失败: {e}"
        print(f"⚠️ {error_message}")
        return [], [error_message]


async def score_batch(
    entries: List[Dict], config: Dict
) -> Tuple[List[Dict], List[str]]:
    """
    批量评分 - 智能分批处理

    根据数据量自动决定分批策略：
    - 小批量：一次性发送
    - 大批量：分成多个批次并行处理
    """
    if not entries:
        return [], []

    # 获取分批配置
    max_prompt_chars = config.get("max_prompt_chars", 10000)
    max_concurrent_batches = config.get("max_concurrent_batches", 3)

    # 分批
    batches = _split_entries_for_batch(entries, max_prompt_chars)
    print(f"📦 分成 {len(batches)} 个批次评分 (共 {len(entries)} 条)")

    # 如果只有一批，直接处理
    if len(batches) == 1:
        scores, errors = await _score_single_batch(batches[0], config, batch_index=0)
        return _merge_scores(entries, scores), errors

    # 多批并行处理（限制并发数）
    semaphore = asyncio.Semaphore(max_concurrent_batches)

    async def score_with_limit(batch_index: int, batch: List[Dict]):
        async with semaphore:
            return await _score_single_batch(batch, config, batch_index=batch_index)

    # 并发处理所有批次
    batch_tasks = [
        score_with_limit(batch_index, batch)
        for batch_index, batch in enumerate(batches)
    ]
    batch_results = await asyncio.gather(*batch_tasks)

    # 合并所有评分结果
    all_scores = []
    all_errors = []
    for scores, errors in batch_results:
        all_scores.extend(scores)
        all_errors.extend(errors)

    return _merge_scores(entries, all_scores), all_errors


def _merge_scores(entries: List[Dict], scores: List[Dict]) -> List[Dict]:
    """将评分结果合并到原始entries中"""
    # 构建link到score的映射
    score_map = {s.get("link"): s for s in scores if s.get("link")}

    merged = []
    for entry in entries:
        link = entry.get("link")
        score_data = score_map.get(link, {})

        # 确保 score 为整数类型
        score_value = score_data.get("score", entry.get("score"))
        if isinstance(score_value, str):
            try:
                score_value = int(score_value)
            except (ValueError, TypeError):
                score_value = 0

        merged.append(
            {
                **entry,
                "tags": score_data.get("tags", entry.get("tags", [])),
                "score": score_value,
                "summary": score_data.get("summary", entry.get("summary", "")),
                "quality_score": score_data.get("quality_score", 0),
                "interest_score": score_data.get("interest_score", 0),
                "keywords": score_data.get("keywords", []),
            }
        )

    return merged



async def generate_immediate_push(
    entries: List[Dict], config: Dict, recent_push_context: str = ""
) -> Tuple[str, Optional[str]]:
    """生成即时推送内容

    Args:
        entries: 原始entries列表（调用方已筛选好高分条目）
        config: LLM配置
        recent_push_context: 近期推送上下文，用于去重
    """
    prompt_path = config.get("prompts", {}).get(
        "immediate_push", "prompts/immediate_push.txt"
    )

    # 直接使用传入的entries，转为JSON格式传给prompt
    prompt = load_prompt(
        prompt_path,
        count=len(entries),
        entries=json.dumps(entries, ensure_ascii=False, indent=2),
        recent_push_context=recent_push_context,
    )

    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        error_message = f"生成即时推送失败: {e}"
        print(f"⚠️ {error_message}")
        return "", error_message


async def compose_digest(
    entries: List[Dict],
    context: List[Dict],
    config: Dict,
    recent_push_context: str = "",
) -> str:
    """生成定时汇总推送内容

    Args:
        entries: 原始entries列表
        context: 历史碎片化信息（用于去重参考），只保留 title, published, tags, summary, source
        config: LLM配置
        recent_push_context: 近期汇总推送上下文，用于去重
    """
    prompt_path = config.get("prompts", {}).get("digest", "prompts/digest.md")

    # context 只保留必要字段，拼接成字符串
    context_text = []
    for c in context:
        tags_str = ", ".join(c.get("tags", [])) if c.get("tags") else ""
        context_text.append(
            f"[score: {c.get('score', 0)}] title:{c.get('title', '')}\n"
            f"published: {c.get('published', '')}\n"
            f"tags: {tags_str}\n"
            f"source: {c.get('source', '')}\n"
            f"summary: {c.get('summary', '')}"
        )

    prompt = load_prompt(
        prompt_path,
        count=len(entries),
        entries=json.dumps(entries, ensure_ascii=False, indent=2),
        context="\n\n".join(context_text),
        recent_push_context=recent_push_context,
        date=datetime.now().strftime("%Y-%m-%d"),
    )

    try:
        return await call_llm(prompt, config)
    except Exception:
        raise


async def summarize_github_trending(
    enriched_repos: List[Dict], config: Dict
) -> Tuple[str, Optional[str]]:
    """GH 板块总结:从 enriched 候选中选 1-max_items + 写 markdown。不传历史上下文。"""
    prompt_path = config.get("prompts", {}).get(
        "section_github", "prompts/section_github.md"
    )
    max_items = (
        config.get("sections", {}).get("github_trending", {}).get("max_items", 3)
    )
    prompt = load_prompt(
        prompt_path,
        repos_json=json.dumps(enriched_repos, ensure_ascii=False, indent=2),
        max_items=max_items,
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"summarize_github_trending 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg


async def select_ai_related_hn(
    candidates: List[Dict], k: int, config: Dict
) -> Tuple[List[str], Optional[str]]:
    """轻 LLM:从 HN 首页候选元数据中挑 k 个 AI 相关 id。

    输入候选只含 id/title/site/points/comments 字段(不含正文)。
    """
    prompt_path = config.get("prompts", {}).get(
        "section_hackernews_select", "prompts/section_hackernews_select.md"
    )
    slim = [
        {
            "id": c.get("id"),
            "title": c.get("title", ""),
            "site": c.get("site", ""),
            "points": c.get("points", 0),
            "comments": c.get("comments", 0),
        }
        for c in candidates
    ]
    prompt = load_prompt(
        prompt_path,
        k=k,
        candidates_json=json.dumps(slim, ensure_ascii=False, indent=2),
    )
    try:
        response = await call_llm(prompt, config)
    except Exception as e:
        msg = f"select_ai_related_hn 失败: {e}"
        print(f"⚠️ {msg}")
        return [], msg

    try:
        ids = _parse_llm_json_response(response)
    except ValueError as e:
        msg = f"select_ai_related_hn 解析失败: {e}"
        print(f"⚠️ {msg}")
        return [], msg

    if not isinstance(ids, list):
        return [], "select_ai_related_hn 返回非数组"
    return [str(x) for x in ids][:k], None


async def summarize_hackernews(
    enriched_stories: List[Dict], config: Dict
) -> Tuple[str, Optional[str]]:
    """对输入的 K 个 enriched stories 行文(K 由 select_k 决定)。不传历史上下文。"""
    prompt_path = config.get("prompts", {}).get(
        "section_hackernews", "prompts/section_hackernews.md"
    )
    prompt = load_prompt(
        prompt_path,
        stories_json=json.dumps(enriched_stories, ensure_ascii=False, indent=2),
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"summarize_hackernews 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg


async def generate_trend_insights(
    sections: Dict[str, str], config: Dict
) -> Tuple[str, Optional[str]]:
    """输入三段成品,返回洞察段 markdown(含 frontmatter)。"""
    prompt_path = config.get("prompts", {}).get("insights", "prompts/insights.md")
    prompt = load_prompt(
        prompt_path,
        rss=sections.get("rss", ""),
        github=sections.get("github", ""),
        hackernews=sections.get("hackernews", ""),
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = f"generate_trend_insights 失败: {e}"
        print(f"⚠️ {msg}")
        return "", msg


def parse_insights_with_metadata(llm_output: str, date: str) -> Tuple[str, Dict]:
    """解析 insights LLM 输出,返回 (insights_md, metadata)。

    metadata 字段:title / excerpt / seotitle / seodescription / lead / highlights /
    profile / date。缺失字段补默认值。
    """
    meta, body = parse_frontmatter(llm_output)
    insights_md = body if meta else llm_output

    metadata = {
        "title": meta.get("title") or f"📰 AI Daily 每日精选 | {date}",
        "excerpt": meta.get("excerpt", ""),
        "seotitle": meta.get("seotitle", ""),
        "seodescription": meta.get("seodescription", ""),
        "lead": meta.get("lead", ""),
        "highlights": normalize_str_list(meta.get("highlights")),
        "profile": "morning",
        "date": date,
    }
    return insights_md, metadata


def parse_digest_with_metadata(llm_output: str, date: str) -> Tuple[str, Dict]:
    """解析 digest LLM 输出,返回 (digest_md, metadata)。

    metadata 字段:title / lead / highlights / profile / date。
    无 frontmatter 时回退到 "🌙 AI Daily 晚报 | {date}" 标题。
    """
    meta, body = parse_frontmatter(llm_output)
    digest_md = body if meta else llm_output

    metadata = {
        "title": meta.get("title") or f"🌙 AI Daily 晚报 | {date}",
        "lead": meta.get("lead", ""),
        "highlights": normalize_str_list(meta.get("highlights")),
        "profile": "default",
        "date": date,
    }
    return digest_md, metadata


def parse_immediate_push_with_metadata(
    llm_output: str, default_title: str
) -> Tuple[str, Dict]:
    """解析即时推送 LLM 输出,返回 (body, metadata)。

    metadata 仅含 title / profile。无 frontmatter 时降级到旧式 `# ` 标题提取,
    再降级到 default_title。
    """
    meta, body = parse_frontmatter(llm_output)

    if meta and meta.get("title"):
        return body, {"title": meta["title"], "profile": "hotspot"}

    # 兼容旧格式:从正文一级标题提取
    match = re.search(r"^\s*#\s+(.+?)\s*\n(.*)$", llm_output, re.DOTALL | re.MULTILINE)
    if match:
        return match.group(2).rstrip(), {
            "title": match.group(1).strip(),
            "profile": "hotspot",
        }

    return llm_output, {"title": default_title, "profile": "hotspot"}
