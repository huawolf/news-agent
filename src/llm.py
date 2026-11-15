"""LLM模块 - 评分和汇总"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.llm_protocol import (
    ANTHROPIC_MESSAGES,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    infer_llm_protocol,
    resolve_llm_endpoint,
)
from src.markdown_utils import normalize_str_list, parse_frontmatter
from src.dedup.tokenizer import build_keywords, normalize_keywords
from src.regions import REGION_SENSITIVE_SECTORS, normalize_signal_region

logger = logging.getLogger("news_agent.llm")


def _json_default(value):
    return value.isoformat() if isinstance(value, datetime) else str(value)


class LLMOperationError(str):
    """LLM failure message carrying whether push notification is warranted."""

    def __new__(cls, message: str, *, connection_failure: bool = False):
        value = super().__new__(cls, message)
        value.connection_failure = connection_failure
        return value


def _is_llm_connection_error(error: BaseException) -> bool:
    """Return whether an exception chain represents an LLM connection failure."""
    import aiohttp

    connection_errors = (
        aiohttp.ClientConnectionError,
        asyncio.TimeoutError,
        ConnectionError,
        TimeoutError,
    )
    current: Optional[BaseException] = error
    seen = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, connection_errors):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def _llm_error_message(prefix: str, error: BaseException) -> LLMOperationError:
    detail = str(error) or error.__class__.__name__
    return LLMOperationError(
        f"{prefix}: {detail}",
        connection_failure=_is_llm_connection_error(error),
    )


def load_prompt(prompt_path: str, **kwargs) -> str:
    """加载提示词模板并填充变量"""
    path = Path(prompt_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
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


def _output_language(config: Dict) -> str:
    return "zh" if config.get("output_language") == "zh" else "en"


def _output_language_instruction(config: Dict, *, json_mode: bool = False) -> str:
    if _output_language(config) == "zh":
        if json_mode:
            return (
                "Output language: Simplified Chinese. Keep JSON keys exactly as specified. "
                "Write all natural-language values such as title, summary and keywords in "
                "Simplified Chinese, keeping those three fields in the same language. "
                "Keep product names, company names, model names, repository names, asset names, and URLs unchanged. "
                "Asset names are always English regardless of the output language."
            )
        return (
            "Output language: Simplified Chinese. All user-visible content, including YAML frontmatter values, "
            "Markdown headings, summaries, highlights, labels, and analysis, must be written in Simplified Chinese. "
            "Keep product names, company names, model names, repository names, asset names, URLs, and code "
            "identifiers unchanged. Asset names are always English regardless of the output language."
        )
    if json_mode:
        return (
            "Output language: English. Keep JSON keys exactly as specified. "
            "Write all natural-language values such as title, summary and keywords in English, "
            "keeping those three fields in the same language. "
            "Keep product names, company names, model names, repository names, and URLs unchanged."
        )
    return (
        "Output language: English. All user-visible content, including YAML frontmatter values, "
        "Markdown headings, summaries, highlights, labels, and analysis, must be written in English. "
        "Translate fixed section headings and labels into natural English. Keep product names, company names, "
        "model names, repository names, URLs, and code identifiers unchanged. "
        "Do not output Chinese fixed labels. Use '[Read original]' instead of '[查看原文]'; "
        "use '## Today’s Insights' instead of '## 今日洞察'; use '## ⭐ GitHub Trends' instead of "
        "'## ⭐ GitHub 趋势'; use 'Daily Brief' or 'Daily Highlights' instead of '每日精选'."
    )


def _llm_request(
    prompt: str,
    config: Dict,
    api_key: str,
    response_format: Optional[Dict] = None,
) -> tuple[str, str, Dict, Dict]:
    """Build a protocol-specific URL, headers, and request payload."""
    model = config.get("model", "gpt-4o-mini")
    base_url = config.get("baseUrl", "https://api.openai.com/v1")
    protocol = config.get("protocol") or infer_llm_protocol(base_url, model)
    url = resolve_llm_endpoint(base_url, protocol)

    if protocol == ANTHROPIC_MESSAGES:
        headers = {
            "x-api-key": api_key,
            "anthropic-version": str(config.get("anthropic_version", "2023-06-01")),
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(config.get("max_tokens", 4096)),
            "temperature": 0.3,
        }
    elif protocol == OPENAI_RESPONSES:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": model, "input": prompt, "stream": False}
        if response_format is not None:
            payload["text"] = {"format": response_format}
    else:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        is_reasoning_model = bool(re.search(r"^o[1-9]", model.lower()))
        if is_reasoning_model:
            payload["reasoning_effort"] = "low"
        else:
            payload["temperature"] = 0.3

        if response_format is not None:
            payload["response_format"] = response_format
    return protocol, url, headers, payload


def _llm_response_text(data: Dict, protocol: str) -> str:
    """Extract plain text from a protocol-specific response."""
    if protocol == OPENAI_CHAT:
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    if protocol == OPENAI_RESPONSES:
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
        return "".join(
            str(part.get("text", ""))
            for item in data.get("output", [])
            if isinstance(item, dict) and item.get("type") == "message"
            for part in item.get("content", [])
            if isinstance(part, dict) and part.get("type") == "output_text"
        )
    return "".join(
        str(part.get("text", ""))
        for part in data.get("content", [])
        if isinstance(part, dict) and part.get("type") == "text"
    )


async def call_llm(
    prompt: str,
    config: Dict,
    response_format: Optional[Dict] = None,
    *,
    api_key: Optional[str] = None,
    direct_url: Optional[str] = None,
) -> str:
    """Call an OpenAI Chat, OpenAI Responses, or Anthropic Messages API."""
    api_key_name = config.get("apiKeyName", "OPENAI_API_KEY")
    resolved_api_key = api_key or os.environ.get(api_key_name)
    if resolved_api_key:
        resolved_api_key = resolved_api_key.strip("'\" \t\r\n")
    if not resolved_api_key:
        raise ValueError(f"未设置{api_key_name}环境变量")

    import aiohttp

    protocol, url, headers, payload = _llm_request(
        prompt, config, resolved_api_key, response_format
    )
    if direct_url:
        url = direct_url

    timeout = aiohttp.ClientTimeout(total=int(config.get("request_timeout", 120)))
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LLM API错误: {resp.status} - {text}")

            data = await resp.json()
            text = _llm_response_text(data, protocol)
            if not text:
                raise RuntimeError("LLM API returned no text content")
            return text


async def check_llm_available(
    config: Dict, timeout_seconds: int = 15, *, api_key: Optional[str] = None
) -> str:
    """启动时检查 LLM 接口可用性"""
    prompt = "Reply with OK only."

    try:
        response = await asyncio.wait_for(
            call_llm(prompt, config, api_key=api_key), timeout=timeout_seconds
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"LLM可用性检查超时({timeout_seconds}s)") from exc
    except Exception as exc:
        raise RuntimeError(f"LLM可用性检查失败: {exc}") from exc

    response_text = response.strip()
    if not response_text:
        raise RuntimeError("LLM可用性检查返回空响应")

    return response_text


def _sectors_prompt_block(sectors: Optional[List[Dict]]) -> str:
    """Render enabled sectors and their keywords for the scoring prompt.

    Sectors always come from the caller (the ``sector`` table filtered by
    ``enabled``). The seed file is only an import source, never a runtime
    fallback, so disabling a sector in the database takes effect immediately.
    """
    if not sectors:
        return "（本实例未配置板块，sectors 一律输出空数组）"

    lines = []
    for sector in sectors:
        keywords = sector.get("keywords") or []
        scope = (
            "地区敏感：必须输出 region"
            if sector["name"] in REGION_SENSITIVE_SECTORS
            else "地区无关：region 必须输出 null"
        )
        lines.append(f"- {sector['name']}：{'、'.join(keywords)}；{scope}")
    return "\n".join(lines)


def _entry_prompt_view(entry: Dict) -> Dict:
    """Project one entry into the exact shape rendered into the batch prompt."""
    return {
        "link": entry.get("link", ""),
        "title": entry.get("title", "无标题"),
        "source": entry.get("source", "未知来源"),
        "published": entry.get("published", ""),
        "content": entry.get("content", "")[:1000],
    }


def _build_batch_prompt(
    entries: List[Dict],
    prompt_path: str = None,
    preferences: str = "",
    output_language_instruction: str = "",
    sectors: Optional[List[Dict]] = None,
    duplicate_titles: Optional[List[Dict]] = None,
    short_references: bool = False,
) -> str:
    """构建批量评分prompt"""
    # 构建entries JSON列表（只包含必要字段）
    prompt_entries = (
        _scoring_reference_entries(entries) if short_references else entries
    )
    entries_for_llm = [_entry_prompt_view(e) for e in prompt_entries]

    entries_json = json.dumps(
        entries_for_llm, ensure_ascii=False, indent=2, default=_json_default
    )

    # 从文件加载提示词模板，如果未指定则使用默认路径
    if prompt_path is None:
        prompt_path = "prompts/score_batch.md"

    return load_prompt(
        prompt_path,
        entries_json=entries_json,
        preferences=preferences,
        output_language_instruction=output_language_instruction,
        sectors=_sectors_prompt_block(sectors),
        recent_titles_json=json.dumps(
            duplicate_titles or [],
            ensure_ascii=False,
            indent=2,
            default=_json_default,
        ),
    )


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


def _entry_json_cost(entry: Dict) -> int:
    """Return the exact characters one entry adds to the rendered entries JSON.

    The prompt renders the entries array with ``indent=2``. For ``n`` entries
    the rendered array is::

        L = sum(len(element_i)) + 2 * n + 2

    where each element is indented by two spaces and the elements are joined
    by ``",\n"``. Measuring with the real renderer keeps the batch budget in
    sync with the prompt that is actually sent.
    """
    rendered = json.dumps(
        _entry_prompt_view(entry),
        ensure_ascii=False,
        indent=2,
        default=_json_default,
    )
    element = "\n".join("  " + line for line in rendered.split("\n"))
    return len(element) + 2


def _scoring_reference_entries(entries: List[Dict]) -> List[Dict]:
    """Give each scoring row a short, batch-local correlation key."""
    references = []
    for index, entry in enumerate(entries, start=1):
        references.append({**entry, "link": f"item-{index}"})
    return references


def _split_entries_for_batch(
    entries: List[Dict],
    max_prompt_chars: int = 10000,
    base_prompt_chars: Optional[int] = None,
    prompt_path: Optional[str] = None,
    preferences: str = "",
    output_language_instruction: str = "",
    sectors: Optional[List[Dict]] = None,
    duplicate_titles: Optional[List[Dict]] = None,
) -> List[List[Dict]]:
    """Split entries into batches that fit within ``max_prompt_chars``.

    ``base_prompt_chars`` is the length of the prompt rendered without any
    entries; when omitted it is measured with the same arguments used to
    build the real prompt.
    """
    if not entries:
        return []

    if base_prompt_chars is None:
        base_prompt_chars = len(
            _build_batch_prompt(
                [],
                prompt_path,
                preferences=preferences,
                output_language_instruction=output_language_instruction,
                sectors=sectors,
                duplicate_titles=duplicate_titles,
                short_references=True,
            )
        )

    # The rendered prompt replaces the empty entries array ("[]") with the
    # real one, so the entries JSON may occupy this many characters.
    entries_budget = max_prompt_chars - base_prompt_chars + 2

    batches = []
    current_batch = []
    current_chars = 0

    for entry in entries:
        entry_chars = _entry_json_cost(_scoring_reference_entries([entry])[0])

        # The rendered array is sum(elements) + 2 * count + 2.
        projected = current_chars + entry_chars + 2 * (len(current_batch) + 1) + 2
        if projected > entries_budget and current_batch:
            batches.append(current_batch)
            current_batch = [entry]
            current_chars = entry_chars
        else:
            current_batch.append(entry)
            current_chars += entry_chars

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

    if len(results) != len(entries) or len(matched_results) != len(entries):
        missing_links = sorted(entry_links - result_links)
        message = (
            "批次{batch} 评分结果: 输入{input_count}, 返回{output_count}, "
            "保留{matched_count}, 模型省略({missing_count}): {missing}"
        ).format(
            batch=batch_index + 1,
            input_count=len(entries),
            output_count=len(results),
            matched_count=len(matched_results),
            missing_count=len(missing_links),
            missing=missing_links,
        )
        print(f"ℹ️ {message}")

    # Reconciliation gaps are recorded locally but do not warrant a push alert.
    return matched_results, []


async def _score_single_batch(
    entries: List[Dict],
    config: Dict,
    batch_index: int = 0,
    semaphore: Optional[asyncio.Semaphore] = None,
    sectors: Optional[List[Dict]] = None,
    duplicate_titles: Optional[List[Dict]] = None,
) -> Tuple[List[Dict], List[str]]:
    """对单批entries进行评分，失败后等待指定时间重试"""
    # 从config获取批量评分提示词路径
    prompt_path = config.get("prompts", {}).get("score_batch", "prompts/score_batch.md")
    preferences = config.get("personal_preferences", "")
    # Scoring uses a short, batch-local correlation key instead of the source
    # URL. Google News links are long opaque strings and models occasionally
    # copy the wrong one, which then attributes a score to the wrong event.
    # This key only correlates a response row with its input row; it carries no
    # event identity and is never persisted.
    prompt_entries = _scoring_reference_entries(entries)
    original_link_by_reference = {}
    reference_by_original = {}
    for entry, prompt_entry in zip(entries, prompt_entries, strict=True):
        reference = str(prompt_entry["link"])
        original_link_by_reference[reference] = entry.get("link") or reference
        if entry.get("link"):
            reference_by_original[str(entry["link"])] = reference

    prompt = _build_batch_prompt(
        prompt_entries,
        prompt_path,
        preferences=preferences,
        output_language_instruction=_output_language_instruction(config, json_mode=True),
        sectors=sectors,
        duplicate_titles=duplicate_titles,
        short_references=True,
    )

    max_retries = int(config.get("batch_max_retries", 1))
    retry_delay = int(config.get("batch_retry_delay_seconds", 300))
    total_attempts = 1 + max(0, max_retries)
    last_exception = None

    for attempt in range(total_attempts):
        try:
            if semaphore is not None:
                async with semaphore:
                    response = await call_llm(
                        prompt, config, response_format={"type": "json_object"}
                    )
            else:
                response = await call_llm(
                    prompt, config, response_format={"type": "json_object"}
                )

            results = _parse_score_response(response)

            if not isinstance(results, list):
                raise ValueError(f"LLM返回的不是数组: {type(results)}")

            for item in results:
                if isinstance(item, dict):
                    original_link = str(item.get("link") or "")
                    if original_link in reference_by_original:
                        item["link"] = reference_by_original[original_link]

            matched_results, errors = _reconcile_batch_results(
                prompt_entries, results, batch_index
            )
            for item in matched_results:
                reference = str(item.get("link") or "")
                item["link"] = original_link_by_reference.get(reference, reference)
            if attempt > 0:
                print(f"✅ 批次{batch_index + 1} 评分重试成功")
                logger.info("批次%s 评分重试成功 (第%s次尝试)", batch_index + 1, attempt + 1)
            return matched_results, errors

        except Exception as e:
            last_exception = e
            if attempt < total_attempts - 1:
                print(f"⚠️ 批次{batch_index + 1} 评分失败 ({e})，将在 {retry_delay} 秒后重试...")
                logger.warning(
                    "LLM scoring batch failed, retrying in %ss: batch=%s error=%s (attempt %s/%s)",
                    retry_delay,
                    batch_index + 1,
                    e,
                    attempt + 1,
                    total_attempts,
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)

    error_message = _llm_error_message(f"批次{batch_index + 1} 评分失败", last_exception)
    logger.exception(
        "LLM scoring batch failed after %s attempts batch=%s entries=%s error_type=%s",
        total_attempts,
        batch_index + 1,
        len(entries),
        last_exception.__class__.__name__ if last_exception else "Unknown",
    )
    print(f"⚠️ {error_message}")
    alert_errors = [error_message] if error_message.connection_failure else []
    return [], alert_errors


async def score_batch(
    entries: List[Dict],
    config: Dict,
    sectors: Optional[List[Dict]] = None,
    duplicate_titles: Optional[List[Dict]] = None,
) -> Tuple[List[Dict], List[str]]:
    """
    批量评分 - 智能分批处理

    根据数据量自动决定分批策略：
    - 小批量：一次性发送
    - 大批量：分成多个批次并行处理

    ``sectors`` 是本次评分允许使用的板块列表（来自 ``sector`` 表中
    ``enabled=true`` 的记录），同时用于 prompt 注入与落库前过滤。
    """
    if not entries:
        return [], []

    # 获取分批配置
    max_prompt_chars = config.get("max_prompt_chars", 10000)
    max_concurrent_batches = config.get("max_concurrent_batches", 3)
    consecutive_threshold = int(config.get("batch_consecutive_failure_threshold", 3))

    # 分批
    prompt_path = config.get("prompts", {}).get("score_batch", "prompts/score_batch.md")
    batches = _split_entries_for_batch(
        entries,
        max_prompt_chars,
        prompt_path=prompt_path,
        preferences=config.get("personal_preferences", ""),
        output_language_instruction=_output_language_instruction(config, json_mode=True),
        sectors=sectors,
        duplicate_titles=duplicate_titles,
    )
    print(
        f"📦 分成 {len(batches)} 个批次评分 (共 {len(entries)} 条, "
        f"max_prompt_chars={max_prompt_chars})"
    )

    # 如果只有一批，直接处理
    if len(batches) == 1:
        scores, errors = await _score_single_batch(
            batches[0],
            config,
            batch_index=0,
            sectors=sectors,
            duplicate_titles=duplicate_titles,
        )
        alert_errors = errors if len(batches) >= consecutive_threshold else []
        if errors and not alert_errors:
            logger.warning(
                "Scoring batch failure did not reach consecutive threshold (%s), skipping alert notification",
                consecutive_threshold,
            )
        return _merge_scores(entries, scores, sectors), alert_errors

    # 多批并行处理（限制并发数）
    semaphore = asyncio.Semaphore(max_concurrent_batches)

    # 并发处理所有批次
    batch_tasks = [
        _score_single_batch(
            batch,
            config,
            batch_index=batch_index,
            semaphore=semaphore,
            sectors=sectors,
            duplicate_titles=duplicate_titles,
        )
        for batch_index, batch in enumerate(batches)
    ]
    batch_results = await asyncio.gather(*batch_tasks)

    # 合并所有评分结果，并检查连续失败批次数
    all_scores = []
    all_errors = []
    current_consecutive_failures = 0
    has_alertable_streak = False

    for scores, errors in batch_results:
        all_scores.extend(scores)
        if errors:
            current_consecutive_failures += 1
            all_errors.extend(errors)
            if current_consecutive_failures >= consecutive_threshold:
                has_alertable_streak = True
        else:
            current_consecutive_failures = 0

    alert_errors = all_errors if has_alertable_streak else []
    if all_errors and not alert_errors:
        logger.warning(
            "Scoring batch failures (%s) did not reach consecutive threshold (%s), skipping alert notification",
            len(all_errors),
            consecutive_threshold,
        )

    return _merge_scores(entries, all_scores, sectors), alert_errors


MAX_ASSETS = 5


def normalize_assets(assets: object, limit: int = MAX_ASSETS) -> List[str]:
    """Normalize asset symbols so one entity always maps to a single form.

    Assets are English-only by contract: strip ``$`` and whitespace, uppercase,
    and drop anything non-ASCII. Dropping non-ASCII is deliberate — a Chinese
    name means the model ignored the prompt, and keeping it would reintroduce
    the second spelling of the same entity. At most ``limit`` survive: beyond
    that the list is noise, not signal.

    Public because the master's report path must apply the same rules to values
    a slaver passed through; a second implementation would drift.
    """
    if not isinstance(assets, list):
        return []

    normalized: List[str] = []
    for raw in assets:
        if not isinstance(raw, str):
            continue
        name = raw.strip().lstrip("$").strip()
        name = "".join(name.split())
        if not name or len(name) > 40 or not name.isascii():
            continue
        name = name.upper()
        if name not in normalized:
            normalized.append(name)
        if len(normalized) >= limit:
            break
    return normalized


WATCHLIST_ASSET_CLASSES = ("stock", "crypto", "commodity")

#: The prompt asks for the English enum, but a model slip can still return the
#: Chinese label — map it back instead of dropping the whole extraction.
_ASSET_CLASS_ALIASES = {
    "stock": "stock",
    "equity": "stock",
    "股票": "stock",
    "股票etf": "stock",
    "etf": "stock",
    "index": "stock",
    "指数": "stock",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "加密货币": "crypto",
    "代币": "crypto",
    "commodity": "commodity",
    "commodities": "commodity",
    "大宗商品": "commodity",
    "商品": "commodity",
}

WATCHLIST_ALIAS_LIMIT = 10
WATCHLIST_ASSET_LIMIT = 20


def normalize_asset_class(value: object) -> str:
    """Map a model-returned asset class onto the three allowed values."""
    text = str(value or "").strip().lower()
    return _ASSET_CLASS_ALIASES.get(text, "stock")


def normalize_watchlist_aliases(value: object, asset: str = "") -> List[str]:
    """Clean the alias list: strip, dedupe, cap, and drop the symbol itself."""

    def clean(raw: object) -> str:
        return str(raw).strip() if isinstance(raw, str) else ""

    if isinstance(value, str):
        raw_items: List[object] = [value]
    elif isinstance(value, list):
        raw_items = value
    else:
        return []

    symbol = asset.strip().upper()
    result: List[str] = []
    seen = set()
    for raw in raw_items:
        name = clean(raw)
        if not name or len(name) > 64:
            continue
        key = name.casefold()
        if symbol and key == symbol.casefold():
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= WATCHLIST_ALIAS_LIMIT:
            break
    return result


def _parse_json_object(response: str) -> Dict:
    """Parse a single JSON object, tolerating markdown fences and extra prose."""
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
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        raise ValueError(f"无法从响应中解析 JSON 对象: {response[:200]}...")
    return parsed


async def recognize_watchlist_assets(text: str, config: Dict) -> List[Dict]:
    """Recognise every explicit, normalisable watchlist asset in pasted text."""
    prompt_path = str(
        (config.get("prompts") or {}).get(
            "watchlist_asset", "prompts/watchlist_asset.md"
        )
    )
    prompt = load_prompt(prompt_path, text=text)
    response = await call_llm(
        prompt, config, response_format={"type": "json_object"}
    )
    data = _parse_json_object(response)

    candidates = data.get("assets")
    if not isinstance(candidates, list):
        candidates = [data] if data.get("asset") else []

    result: List[Dict] = []
    positions = {}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized = normalize_assets([candidate.get("asset")], limit=1)
        if not normalized:
            continue
        asset = normalized[0]
        aliases = normalize_watchlist_aliases(candidate.get("aliases"), asset)
        if asset in positions:
            existing = result[positions[asset]]
            existing["aliases"] = normalize_watchlist_aliases(
                [*(existing["aliases"] or []), *aliases], asset
            )
            continue
        positions[asset] = len(result)
        result.append(
            {
                "asset": asset,
                "asset_class": normalize_asset_class(candidate.get("asset_class")),
                "aliases": aliases,
            }
        )
        if len(result) >= WATCHLIST_ASSET_LIMIT:
            break
    return result


def _filter_sectors(sectors: object, known: List[Dict]) -> List[str]:
    """Keep only sector names that exist, so hallucinated labels never persist."""
    if not isinstance(sectors, list) or not known:
        return []

    allowed = {s["name"] for s in known}
    result: List[str] = []
    for name in sectors:
        if isinstance(name, str) and name in allowed and name not in result:
            result.append(name)
    return result


DIRECTIONS = ("bullish", "bearish", "neutral")


def _clean_text(value: object) -> str:
    """Collapse a model-returned display string, rejecting non-string values.

    A JSON field can arrive as ``null``, a number or a nested object; writing
    that straight into ``Signal.title`` would either crash the insert or store
    a value the portal cannot render.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


#: Phrases a model reaches for when the input carries no facts. The prompt
#: tells it to drop such input instead, but a model that ignores the
#: instruction will happily write a sentence about how little it knows —
#: which then shows up in the reader's digest as a meta-commentary line.
#: These are matched on the summary, and the entry is dropped.
LOW_CONTENT_MARKERS = (
    "无法确认",
    "无法验证",
    "不可验证",
    "缺乏事实",
    "缺乏可验证",
    "无实质信息",
    "无明确事实",
    "不足以判断",
    "难以形成明确判断",
    "不构成可判断",
    "不构成有效信号",
    "仅作去重存档",
    "仅作存档",
    "需等待原文",
    "待原文补充",
    "内容缺失",
    "正文缺失",
    "无法解析",
    "未抓取到正文",
    "no verifiable facts",
    "cannot be verified",
    "insufficient information",
    "not enough information",
    "no clear investment",
)


def looks_like_low_content(summary: str) -> bool:
    """Whether a summary is commentary about the input rather than about it.

    The wording alone is not enough: a solid analysis often notes that the
    market has no revenue data yet ("尚无用户与收入数据"), which is normal
    research judgment, not a confession. Only the sentence-enders that name
    the *whole input* as unusable count — "无法确认"、"缺乏事实"、
    "不构成可判断信号" — because those describe the entry, not the company.
    """
    text = (summary or "").strip()
    if not text:
        return False
    lowered = text.lower()
    return any(marker in text or marker.lower() in lowered for marker in LOW_CONTENT_MARKERS)


#: Below this length the body carries no facts to analyse — a title, a
#: publisher line, a subscription prompt.
LOW_CONTENT_BODY_CHARS = 300


def is_low_content_entry(summary: str, content: str) -> bool:
    """Whether an entry has nothing to read.

    Two independent signals must agree: the body is too short to carry facts,
    *and* the summary is the model commenting on the absence of facts. Either
    alone misfires — a short body can still be a crisp one-liner worth
    keeping, and a long body analysed with caveats is perfectly good.
    """
    body = (content or "").strip()
    if len(body) >= LOW_CONTENT_BODY_CHARS:
        return False
    return looks_like_low_content(summary)

# Marks a merged entry as actually scored. Callers that must not treat an
# unscored entry as a zero-scored signal (master's report pipeline) read it.
SCORED_FLAG = "scored_ok"


def _merge_scores(
    entries: List[Dict],
    scores: List[Dict],
    sectors: Optional[List[Dict]] = None,
) -> List[Dict]:
    """将评分结果合并到原始entries中"""
    # 构建link到score的映射
    score_map = {s.get("link"): s for s in scores if s.get("link")}
    known_sectors = sectors or []

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
        if score_value is None:
            # `Signal.score` is NOT NULL; an entry the model never saw is
            # zero-scored here and flagged as unscored via SCORED_FLAG.
            score_value = 0

        direction = score_data.get("direction")
        if direction not in DIRECTIONS:
            # A slaver may pass its own extraction through (§6.3); keep it when
            # the model returned nothing usable rather than silently dropping it.
            direction = entry.get("direction")
            if direction not in DIRECTIONS:
                direction = "neutral"

        assets = normalize_assets(score_data.get("assets")) or normalize_assets(
            entry.get("assets")
        )

        # `keywords` drives signal-to-signal dedup, so an entry the model left
        # without a usable list still needs the fixed 10 keywords built locally
        # (§5.2) — same fallback the shared-pool import path uses.
        keywords = normalize_keywords(score_data.get("keywords"))
        if not keywords:
            keywords = build_keywords(
                f"{entry.get('title', '')} {entry.get('content', '')}"
            )

        # `title` and `summary` are display fields the pool shows verbatim, so an
        # untouched RSS title next to a model-written summary is what produced
        # mixed-language rows (English title + Chinese summary). Overwrite both
        # with the configured output language and only fall back to the source
        # text when the model returned nothing usable for that field.
        title = _clean_text(score_data.get("title")) or _clean_text(entry.get("title"))
        summary = _clean_text(score_data.get("summary")) or _clean_text(
            entry.get("summary")
        )

        # A model that ignored the "drop empty input" instruction writes a
        # sentence about how little it knows. That is commentary, not
        # content, and it must never reach the reader's digest.
        low_content = bool(score_data.get("is_low_content", False))
        if not low_content and is_low_content_entry(
            summary, str(entry.get("content") or "")
        ):
            low_content = True
        if low_content:
            title = ""
            summary = ""

        filtered_sectors = _filter_sectors(
            score_data.get("sectors"), known_sectors
        )
        merged.append(
            {
                **entry,
                "title": title,
                "score": score_value,
                "summary": summary,
                "quality_score": score_data.get("quality_score", 0),
                "interest_score": score_data.get("interest_score", 0),
                # Compatibility with older models: accept the legacy marker,
                # but the current prompt omits duplicate items entirely.
                "is_duplicate": bool(score_data.get("is_duplicate", False)),
                "duplicate_of": str(score_data.get("duplicate_of") or ""),
                # A daily/weekly roundup carries several unrelated events in
                # one row. The prompt tells the model to flag it (§5.1) and
                # the pipeline drops it instead of storing a row that reads
                # like five signals at once.
                "is_aggregate": bool(score_data.get("is_aggregate", False)),
                # Only a title and a publisher: there is nothing for the
                # reader to read. The prompt tells the model to flag it and
                # leave the summary empty (§5.1).
                "is_low_content": low_content,
                # Readable but with no actionable increment for an investor:
                # price recaps, event announcements, version notes, rumour.
                "is_low_value": bool(score_data.get("is_low_value", False)),
                "keywords": keywords,
                "assets": assets,
                "direction": direction,
                "sectors": filtered_sectors,
                "region": normalize_signal_region(
                    score_data.get("region"), filtered_sectors
                ),
                SCORED_FLAG: link in score_map,
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
        "immediate_push", "prompts/immediate_push.md"
    )

    # 直接使用传入的entries，转为JSON格式传给prompt
    prompt = load_prompt(
        prompt_path,
        count=len(entries),
        entries=json.dumps(entries, ensure_ascii=False, indent=2),
        recent_push_context=recent_push_context,
        output_language_instruction=_output_language_instruction(config),
    )

    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        error_message = _llm_error_message("生成即时推送失败", e)
        print(f"⚠️ {error_message}")
        return "", error_message


async def compose_digest(
    entries: List[Dict],
    context: List[Dict],
    config: Dict,
    recent_push_context: str = "",
    max_items: int = 10,
) -> str:
    """生成定时汇总推送内容

    Args:
        entries: 原始entries列表
        context: 历史碎片化信息（用于去重参考），只保留 title, published, sectors, summary, source
        config: LLM配置
        recent_push_context: 近期汇总推送上下文，用于去重
    """
    prompt_path = config.get("prompts", {}).get("digest", "prompts/digest.md")

    # context 只保留必要字段，拼接成字符串
    context_text = []
    for c in context:
        sectors_str = ", ".join(c.get("sectors", [])) if c.get("sectors") else ""
        context_text.append(
            f"[score: {c.get('score', 0)}] title:{c.get('title', '')}\n"
            f"published: {c.get('published', '')}\n"
            f"sectors: {sectors_str}\n"
            f"source: {c.get('source', '')}\n"
            f"summary: {c.get('summary', '')}"
        )

    digest_entries = [
        {
            "title": entry.get("title", ""),
            "source": entry.get("source", ""),
            "link": entry.get("link", ""),
            "score": entry.get("score", 0),
            "quality_score": entry.get("quality_score", 0),
            "interest_score": entry.get("interest_score", 0),
            "summary": entry.get("summary", ""),
            "sectors": entry.get("sectors", []),
            "published": entry.get("published", ""),
            "content": str(entry.get("content", ""))[:1500],
        }
        for entry in entries
    ]

    prompt = load_prompt(
        prompt_path,
        count=len(entries),
        max_items=max(1, int(max_items)),
        entries=json.dumps(digest_entries, ensure_ascii=False, indent=2),
        context="\n\n".join(context_text),
        recent_push_context=recent_push_context,
        date=datetime.now().strftime("%Y-%m-%d"),
        output_language_instruction=_output_language_instruction(config),
    )

    try:
        return await call_llm(prompt, config)
    except Exception:
        raise


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
        output_language_instruction=_output_language_instruction(config),
    )
    try:
        return await call_llm(prompt, config), None
    except Exception as e:
        msg = _llm_error_message("generate_trend_insights 失败", e)
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
        "title": meta.get("title") or f"📰 News Agent Daily Brief | {date}",
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
    无 frontmatter 时回退到 "🌙 News Agent 晚报 | {date}" 标题。
    """
    meta, body = parse_frontmatter(llm_output)
    digest_md = body if meta else llm_output

    metadata = {
        "title": meta.get("title") or f"🌙 News Agent Evening Brief | {date}",
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
