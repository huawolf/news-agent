"""GitHub REST API enrich:metadata + README → enriched repo dict

匿名调用受 60 req/hr 限,设置 GITHUB_TOKEN 环境变量后走 5000 req/hr。
"""

import asyncio
import base64
import os
from typing import Dict, List, Optional, Tuple

import aiohttp

API_BASE = "https://api.github.com"


def _auth_headers(token_env: str = "GITHUB_TOKEN") -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        print(f"🔑 GH: 已配置 {token_env},鉴权调用 (5000 req/hr)")
    else:
        print(f"⚠️ GH: 未配置 {token_env},匿名调用 (60 req/hr)")
    return headers


async def _get_json(
    session: aiohttp.ClientSession, url: str, timeout: int = 10
) -> Optional[Dict]:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=timeout)
    ) as resp:
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise RuntimeError(f"GitHub API {resp.status} for {url}")
        return await resp.json()


async def enrich_repo(
    session: aiohttp.ClientSession,
    repo: Dict,
    token_env: str = "GITHUB_TOKEN",
    readme_max_chars: int = 3000,
    timeout: int = 10,
) -> Optional[Dict]:
    """单 repo 双调用 enrich。返回 None 表示该 repo 应剔除(archived 或 metadata 不可达)。

    任一调用失败 raise → 调用方按 return_exceptions 模式聚合错误。
    """
    full_name = repo["full_name"]
    meta_url = f"{API_BASE}/repos/{full_name}"
    readme_url = f"{API_BASE}/repos/{full_name}/readme"

    meta, readme = await asyncio.gather(
        _get_json(session, meta_url, timeout=timeout),
        _get_json(session, readme_url, timeout=timeout),
    )

    if meta is None:
        return None
    if meta.get("archived"):
        return None

    license_spdx = ""
    if isinstance(meta.get("license"), dict):
        license_spdx = meta["license"].get("spdx_id") or ""

    readme_excerpt = ""
    if readme and readme.get("content"):
        try:
            raw = base64.b64decode(readme["content"]).decode("utf-8", errors="replace")
            readme_excerpt = raw[:readme_max_chars]
        except Exception:
            readme_excerpt = ""

    return {
        **repo,
        "topics": meta.get("topics") or [],
        "license": license_spdx,
        "pushed_at": meta.get("pushed_at") or "",
        "readme_excerpt": readme_excerpt,
    }


async def enrich_repos(
    candidates: List[Dict],
    token_env: str = "GITHUB_TOKEN",
    readme_max_chars: int = 3000,
    timeout: int = 10,
) -> Tuple[List[Dict], List[str]]:
    """并发 enrich 多个 repo。返回 (enriched_list_with_archived_filtered, errors)"""
    errors: List[str] = []
    headers = _auth_headers(token_env)

    async with aiohttp.ClientSession(headers=headers, trust_env=True) as session:
        results = await asyncio.gather(
            *[
                enrich_repo(session, r, token_env, readme_max_chars, timeout)
                for r in candidates
            ],
            return_exceptions=True,
        )

    enriched: List[Dict] = []
    for r, candidate in zip(results, candidates):
        if isinstance(r, Exception):
            errors.append(f"enrich {candidate['full_name']} 失败: {r}")
        elif r is not None:
            enriched.append(r)
    return enriched, errors
