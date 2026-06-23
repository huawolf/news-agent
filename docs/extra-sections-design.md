# extra-sections-design.md — GitHub Trending / Hacker News / 行业洞察 板块设计

> 本文是对现有 push job 的能力扩展设计。在保持 RSS 主流程不变的前提下，新增三个板块仅在「早报」时段输出：GitHub 趋势、Hacker News 热议、跨板块行业洞察。
>
> 配套修改的源码模块、配置字段、数据契约见各章节。落地后需要把架构层结论合并回 `docs/tech-spec.md`，运行参数变更同步 `README.md`，进度记录写入 `docs/plan.md`。
>
> created: 2026-05-16
> updated: 2026-05-16（按用户决策重写：四模块独立、单页 GH trending、HN 单选 K=1、insights 结构由 prompt 决定）

## 1. 目标与范围

- 在每天的早报推送中，除了现有的 RSS 精选 digest，再加入：
  - **GitHub Trending**：当日热门开源项目中筛选 1-3 个 AI 相关项目
  - **Hacker News**：HN 首页中筛选 1 个最有讨论价值的 AI 相关热议（带评论与外链正文摘要）
  - **行业洞察**：基于上述三个板块 + 历史 insights 段做一段跨板块趋势小结
- 这三段内容**只在早报时段生成**（当天 `schedule.push_cron` 列表里最早那次触发），其余时段维持现有纯 RSS digest 行为
- 单板块失败 → 降级推送其他板块，整体任务仍算成功；RSS 失败 → 同现有行为，任务非 0 退出
- 不引入数据库；状态全部落到 `news-data/` 的本地文件

## 2. 架构总览

```
push_job (cron 触发)
│
├─ Step 1: 早报判定
│      └─ 否 → 走原有纯 RSS digest 流程，结束
│
├─ Step 2: 四模块编排（前三路 asyncio.gather 并发，insights 串行后置）
│      ├─ run_rss_section(config, now)
│      ├─ run_github_section(config, now)
│      └─ run_hackernews_section(config, now)
│              ↓
│      └─ run_insights_section(rss_md, gh_md, hn_md, config, now)
│
└─ Step 3: push_job 上游
       └─ sentinel 包裹四段 → 拼装 markdown → 推送 → 写 push-*.md
```

**关键设计选择**：

1. **模块自治**。每个板块封装为 `run_xxx_section(...) -> (markdown, error)`，板块内部从抓取、enrich、LLM 总结全包；板块互不感知。
2. **sentinel 由 push_job 统一包**。模块返回**裸 markdown**（不含 sentinel），由上游 `_assemble_with_sentinels()` 包入 `<!-- SECTION:xxx BEGIN/END -->`。这样模块不需要知道自己的板块标识，便于后续替换或加新板块。
3. **失败自吞**。模块内部捕获异常，返回 `("", error)`；上游根据返回值决定是否在最终 push 中省略该段、是否走告警通道。
4. **RSS 是核心**。其他三个模块失败都是降级；RSS 模块失败仍按现有行为整体退出非 0。

**并发模型**：`asyncio.gather`，与项目通体异步风格一致。

## 3. 模块结构

```
src/
├── sections/                       ← 新增
│   ├── __init__.py                 # 暴露 run_rss_section / run_github_section / ...
│   ├── rss/
│   │   ├── __init__.py
│   │   └── section.py              # 把现有 collect_entries_for_push + compose_digest 流程搬入
│   ├── github/
│   │   ├── __init__.py
│   │   ├── trending_scraper.py     # HTML 抓取 + 解析(单页 https://github.com/trending)
│   │   ├── repo_enricher.py        # GitHub REST API:metadata + README
│   │   ├── history.py              # trending-history.json 读写 + 过期清理
│   │   └── section.py              # run_github_section 入口
│   ├── hackernews/
│   │   ├── __init__.py
│   │   ├── frontpage_scraper.py    # HTML 抓首页 30 条
│   │   ├── item_enricher.py        # Algolia /items/{id} + 外链正文(html_to_markdown)
│   │   └── section.py              # run_hackernews_section 入口
│   └── insights/
│       ├── __init__.py
│       └── section.py              # run_insights_section 入口
├── llm.py                          # 新增 4 个函数(GH/HN select/HN summarize/insights)
├── storage.py                      # 新增:sentinel 切片、trending-history、profile 字段
├── main.py                         # push_job 升级:早报判定 + 四模块编排
└── push/                           # 不变,平台层无感知

prompts/                            ← 新增 4 个
├── section_github.md               # 输入 enriched repo 数组 → 选 1-3 + 写 markdown
├── section_hackernews_select.md    # 30 条 frontpage 元数据 → 选 K 个 id(默认 K=1)
├── section_hackernews.md           # enriched story → 写 markdown(K=1 时不挑选,只行文)
└── insights.md                     # 输入三段成品 + 近 N 天 insights 段历史 → 写洞察段

news-data/
├── fetch-*.json                    # 不变
├── notify-*.md                     # 不变
├── push-*.md                       # 内容升级:含 sentinel 与 profile frontmatter
└── trending-history.json           # 新增:GH 已查阅 repo 索引
```

## 4. 数据契约

### 4.1 push 文件分段 sentinel

push 文件保留**完整拼接**写入磁盘和发送各平台，但用 HTML 注释 sentinel 划分板块边界。HTML 注释在 markdown 渲染中不显示，机器可解析，下游做"按段查重"时能精确切片。

```markdown
---
pushDate: "2026-05-16T08:00:03+08:00"
profile: "morning"
sourceCount: 12
totalEntries: 12
---

<!-- SECTION:rss BEGIN -->
# 📰 AI Daily 每日精选 | 2026-05-16

*开头一句定调...*

### 1️⃣ ...
<!-- SECTION:rss END -->

<!-- SECTION:github BEGIN -->
## ⭐ GitHub 趋势

- **owner/repo** ⭐234 — 一句话价值定位
<!-- SECTION:github END -->

<!-- SECTION:hackernews BEGIN -->
## 🟧 Hacker News 热议

### 标题 (120 pts · 45 comments)
- 链接: url
- 要点:...
- HN 讨论: comments_url
<!-- SECTION:hackernews END -->

<!-- SECTION:insights BEGIN -->
## 💡 今日洞察

(行文结构由 prompts/insights.md 决定,代码不强加格式)
<!-- SECTION:insights END -->
```

某板块 markdown 为空 → 对应 sentinel 段**整段省略**（不留空标记，不留空 SECTION）。

### 4.2 分段提取函数（storage.py 新增）

```python
def extract_section(push_md: str, section: str) -> str:
    """从 push 文件内容中切出 <!-- SECTION:{section} BEGIN/END --> 之间的 markdown。

    向后兼容:
    - 新 push 文件(带 sentinel): 按 sentinel 边界切片
    - 老 push 文件(无 sentinel) 且 section=='rss': 返回整个 body(老文件视为全 RSS)
    - 老 push 文件且 section in {github, hackernews, insights}: 返回空字符串
    """

def load_recent_section_titles(section: str, days: int, data_dir="news-data") -> str:
    """汇总近 days 天 push-*.md 的指定板块,提取标题级别清单(沿用 _extract_push_titles 思路)。

    仅 insights 板块在新增模块中使用本函数加载历史。
    GH/HN 板块按用户决策不传历史上下文给 LLM,不调用本函数。
    """
```

四个 LLM 与历史上下文的关系：

| LLM 调用 | recent context 数据源 |
|---|---|
| `compose_digest` (RSS) | 维持现有 `load_recent_push_titles(filter.push_context_days)`（老接口在 sentinel 升级后等价于 `load_recent_section_titles("rss", ...)`） |
| `summarize_github_trending` | 不传 |
| `select_ai_related_hn` | 不传 |
| `summarize_hackernews` | 不传 |
| `generate_trend_insights` | `load_recent_section_titles("insights", filter.push_context_days)` |

### 4.3 trending-history.json

```json
{
  "repos": {
    "https://github.com/owner/repo-a": "2026-05-15",
    "https://github.com/owner/repo-b": "2026-05-12"
  },
  "updated_at": "2026-05-16T08:00:01+08:00"
}
```

**写入语义**（用户决策的精确语义）：

每次早报触发，按下列顺序处理：

1. 加载 history，剔除 `last_seen_date < today - filter.keep_days` 的条目
2. 抓 trending 页 → 得到 `all_repos`
3. 对 `all_repos` 中每个 url：
   - 若已在 history → `history.touch(url, today)`（更新日期），**从候选移除**
   - 不在 history → 进入 `candidates`
4. 把 `candidates` 中每个 url 也 `history.touch(url, today)` 写入 history
5. 持久化 history（覆盖写）
6. 对 `candidates`（即今日新出现的 repo）做后续 deep-dive 与 LLM 总结

效果：repo 在 trending 上挂多久就被屏蔽多久；过 `keep_days` 天没再出现则可重新推荐。

## 5. 模块详设

### 5.1 RSS 模块（迁移既有逻辑）

`src/sections/rss/section.py::run_rss_section(config, now) -> (str, Optional[str])`：

把现有 `run_push_job` 中"收集 + compose_digest"的部分原样迁过来，返回裸 markdown（不含 sentinel）+ 错误信息。无新增功能。

### 5.2 GitHub 模块

`src/sections/github/section.py::run_github_section(config, now) -> (str, Optional[str])`：

```
1. 抓取 trending 单页(HTML)
   GET https://github.com/trending
   解析 → all_repos: [{url, full_name, description, language, stars_today, stars_total}]

2. 加载 history 并清理
   history = load_trending_history(sections.github_trending.history_file)
   history.cleanup(keep_days=filter.keep_days)

3. 候选筛选(按 §4.3 语义)
   candidates = []
   for repo in all_repos:
       if repo.url in history:
           history.touch(repo.url, today)
       else:
           candidates.append(repo)

4. 候选写回 history + 持久化
   for repo in candidates:
       history.touch(repo.url, today)
   history.save()

5. 数量护栏
   if not candidates: return ("", None)              # 静默
   if len(candidates) > max_deep_dive:
       candidates = candidates[:max_deep_dive]        # 截断,默认 10

6. 并发 deep-dive(REST API)
   async for repo in candidates:
       meta = await fetch_repo_metadata(owner, repo)   # GET /repos/{o}/{r}
       readme = await fetch_readme(owner, repo)         # GET /repos/{o}/{r}/readme
   enriched = [{...repo, topics, license, pushed_at, readme_excerpt}]
   - 单 repo 任一请求失败 → 该 repo 跳过 + 错误聚合,不阻塞其他

7. LLM:summarize_github_trending(enriched, config)
   prompt: 候选数组(含 readme_excerpt) → 选 1-max_items + 写 markdown
   不传 recent_section_titles

8. 返回 (markdown, error)
```

**REST API 调用细节**：

| 调用 | 路径 | 取什么 |
|---|---|---|
| metadata | `GET /repos/{owner}/{repo}` | `description, topics, language, license.spdx_id, pushed_at, stargazers_count, archived` |
| readme | `GET /repos/{owner}/{repo}/readme` | `content` (base64) → decode → 截断到 `readme_max_chars` |

- `archived=true` 的 repo 从候选剔除（trending 偶尔出现僵尸归档项目）
- README 截断策略：前 `readme_max_chars` 字符（默认 10000，基于 trending 页 README 长度分布 p50≈25k 选定，详见 §14 决策记录）
- 鉴权：`config.sections.github_trending.tokenName`（默认 `"GITHUB_TOKEN"`）对应的环境变量存在时走 `Authorization: Bearer {token}`，否则匿名调用并接受 60 req/hr 上限（日 10 个 repo × 2 calls = 20 calls，匿名安全）

**enriched repo 字段（喂给最终 LLM）**：

```json
{
  "url": "https://github.com/owner/repo",
  "full_name": "owner/repo",
  "description": "(来自 trending 页)",
  "language": "Python",
  "stars_today": 234,
  "stars_total": 12340,
  "topics": ["llm", "rag", "agent"],
  "license": "MIT",
  "pushed_at": "2026-05-15",
  "readme_excerpt": "(前 3000 chars)"
}
```

### 5.3 Hacker News 模块

`src/sections/hackernews/section.py::run_hackernews_section(config, now) -> (str, Optional[str])`：

```
1. 抓首页(HTML)
   GET https://news.ycombinator.com/news
   解析 30 条 → front: [{id, title, url, site, points, comments, comments_url}]

2. 轻 LLM 初筛:select_ai_related_hn
   输入: 30 条 frontpage 元数据(无正文)
   输出: K = sections.hackernews.select_k 个 story id(默认 K=1)
   if K 个为空 → return ("", None)                    # 静默

3. 并发 enrich 选中的 K 个 story
   async for story in selected:
     - 评论树:Algolia GET /api/v1/items/{id}
         → 取前 top_comments 条 L1(默认 30,按 HN ranking)
         → 每条 L1 下挂前 top_l2_per_l1 条 L2 回复(默认 3)
         → 每条 text 过 html_to_markdown,单条截断到 comment_max_chars(默认 2000)
         → 累计达 comments_total_chars(默认 60000) 立即停止,防离群 story 撑爆 prompt
         → 输出 tree JSON: [{"l1": "...", "replies": ["...", "..."]}, ...]
     - 外链正文:
         if story.url 指向 https://news.ycombinator.com/item?id=... (Show HN/Ask HN):
             从 Algolia 同次返回的 root.text 字段取(无外部请求)
         else:
             GET story.url → html_to_markdown → 截断到 link_content_max_chars(默认 50000,p50≈10k)
     - 单 story 任一失败 → 字段留空,metadata 仍传给最终 LLM

4. LLM:summarize_hackernews(enriched_stories, config)
   prompt: 对输入的 K 个 story 全部行文（K 通常 = 1）
   不传 recent_section_titles

5. 返回 (markdown, error)
```

**Algolia API 接口**：

```
GET https://hn.algolia.com/api/v1/items/{id}
```

返回 JSON：

```json
{
  "id": 12345678,
  "title": "...",
  "url": "...",
  "points": 120,
  "author": "...",
  "text": null,                      // Show HN/Ask HN 的正文在这里
  "children": [                       // 顶层评论数组(按 HN ranking 排序)
    {"id": ..., "text": "<HTML>", "author": "...", "children": [...]},
    ...
  ]
}
```

**优势 vs HTML 解析**：免去 HN 的 `td.ind[indent="0"]` indent-tree 解析；评论文本是干净 HTML 字符串，直接 `html_to_markdown`。

**Show HN / Ask HN 特例**：
- 首页解析时 `url` 字段就是 `https://news.ycombinator.com/item?id=X`，作为"非外链"标记
- enrich 时只调一次 Algolia（覆盖评论 + post 正文 `text` 字段），不再发外部请求

**enriched story 字段（喂给最终 LLM）**：

```json
{
  "id": "12345678",
  "title": "...",
  "url": "...",
  "site": "example.com",
  "points": 120,
  "comments": 45,
  "comments_url": "https://news.ycombinator.com/item?id=12345678",
  "link_content": "(markdown, ≤3000 chars; Show HN 时是 post 正文)",
  "top_comments": [
    {"l1": "(markdown, ≤comment_max_chars)", "replies": ["(markdown, ≤comment_max_chars)", "..."]},
    ...
  ]
}
```

### 5.4 Insights 模块

`src/sections/insights/section.py::run_insights_section(rss_md, gh_md, hn_md, config, now) -> (str, Optional[str])`：

```
1. 加载历史
   recent = load_recent_section_titles("insights", filter.push_context_days)

2. LLM:generate_trend_insights
   输入: {"rss": rss_md, "github": gh_md, "hackernews": hn_md} + recent
   输出: insights 段 markdown
   注:行文结构、bullet 数量、风格约束等全部交给 prompts/insights.md,
      代码层不强加固定格式

3. 返回 (markdown, error)
```

如果某板块返回空（失败或本日无内容），prompt 里对应键标记 `"(本次无内容)"`，LLM 自行适配。

## 6. LLM 调用与 Prompt 策略

### 6.1 新增 LLM 函数（src/llm.py）

```python
async def select_ai_related_hn(
    candidates: list[dict],     # 首页 30 条元数据(无正文)
    k: int,                      # 期望返回数量,默认 1
    config: dict,
) -> tuple[list[str], Optional[str]]:
    """轻量 LLM:从 HN 首页候选中挑出 k 个 AI 相关的 story id,只读 title/site/points/comments。
    返回 ([id1, ...], error)。"""

async def summarize_github_trending(
    enriched_repos: list[dict],   # 已 deep-dive 的 repo 候选(含 readme_excerpt + topics)
    config: dict,
) -> tuple[str, Optional[str]]:
    """选 1-max_items + 写 markdown 段。不传历史上下文。"""

async def summarize_hackernews(
    enriched_stories: list[dict], # 已 enrich(含 link_content 与 top_comments)
    config: dict,
) -> tuple[str, Optional[str]]:
    """对输入的 K 个 enriched stories 全部行文。K 由配置 select_k 决定，默认 1。
    不传历史上下文。"""

async def generate_trend_insights(
    sections: dict[str, str],   # {"rss": md, "github": md, "hackernews": md}
    recent_insights: str,        # load_recent_section_titles("insights", days)
    config: dict,
) -> tuple[str, Optional[str]]:
    """输入三段成品 + 近期 insights 标题,返回洞察段 markdown。"""
```

返回风格沿用 `generate_immediate_push`：成功返回 `(content, None)`，失败返回 `("", error_msg)`。

**单次早报推送的 LLM 调用预算**：

| 调用 | 输入规模 | 用途 |
|---|---|---|
| `compose_digest` | 当日符合条件的 RSS 条目 | 现有，RSS digest 主体 |
| `select_ai_related_hn` | 30 条 HN 首页元数据 | 轻量；只读 title/site/points/comments |
| `summarize_github_trending` | ≤ `max_deep_dive`=10 个 enriched repos | 选 1-3 + 行文 |
| `summarize_hackernews` | `K = select_k` 个 enriched stories（默认 1） | 行文 K 条 |
| `generate_trend_insights` | 三段已生成 markdown + 近期 insights 标题 | 一段洞察 |

合计 5 次 / 早报。晚报维持现有 1 次。

### 6.2 关注领域（GitHub / HN 共用）

为避免领域定义在 3 个 prompt 里漂移，统一在此沉淀；各 prompt 在自身骨架里直接引用本节，不做重新发明。

**正面关注**：

- **AI Agent**：智能体架构、工具链、多智能体、自主规划、Agent 框架
- **AI 模型**：训练、推理、微调、量化部署、模型服务、语音 / 多模态 / 视觉模型
- **AI 基础设施**：GPU 调度、芯片硬件、数据中心、推理优化、分布式训练、向量数据库、RAG 框架
- **大厂 / 前沿动态**：Apple、Google、Meta、OpenAI、Anthropic、Microsoft、xAI 等公司的官方动作与战略
- **AI 集成的开发者工具**：API 网关、自动化脚本、低代码平台等明确与 AI 协同的工具
- **有创新性的开源产品**：日增长显著且有清晰用户价值（GH 板块专属）

**负面排除（一律剔除）**：

- 嵌入式开发（Arduino、ESP32、树莓派、单片机）
- 底层系统编程（内存分配器、编译器、链接器，与 AI 工作负载无明显关联时）
- 通用开发工具（命名规范、代码风格、纯前端模板、UI 组件库、管理后台模板、静态网站主题）
- 学习资源（纯教程仓库、面试题合集、Roadmap，除非是含实用代码的深度技术指南）
- 配置文件集合（Dotfiles、配置模板）
- 与 AI / 科技无关的内容（电子书、资源搬运、刷榜项目、明星项目搬运）
- 纯娱乐 / 高风险误用（deepfake 等无明确基础设施价值的项目）

### 6.3 Prompt 文件

#### prompts/section_github.md（骨架）

- 角色定位（"开源情报分析师"）
- 输入 schema 说明（JSON 数组：url / full_name / description / language / stars_today / stars_total / topics / license / pushed_at / readme_excerpt）
- 关注领域：引用 §6.2（正面列表与负面排除完整复制进 prompt）
- 选项规则：
  - 从候选中挑 1-`max_items` 个最值得关注的项目
  - 优先信号：stars_today 高 + topics 含 AI 标签（agent/llm/rag/inference/training 等）+ readme 描述明确 + 非纯模板/教程仓库
  - 必跳过：`archived=true`（理论上已在 enricher 剔除，prompt 层兜底）、纯 awesome-list、个人配置 dotfiles
- 输出格式（markdown 列表）：
  - `- **owner/repo** ⭐{stars_today} — 一句话价值定位 [link]`
  - 一句话需点明"解决什么问题"，避免营销语
- 风格约束：与 `prompts/digest.md` 同源；负面句式（"震撼""炸裂""革命性"）禁用；避免套话

#### prompts/section_hackernews_select.md（骨架）

- 角色定位（"HN 早间选题人"）
- 输入：JSON 数组（30 条 frontpage 元数据：id / title / site / points / comments）
- 关注领域：引用 §6.2
- 任务：挑 `k` 个最符合关注领域的 story id（K 默认 1）
- 决策原则：title + site 不足以判定 AI 相关时，**宁可漏选不可错选**（错选会让最终 LLM 写出与 AI Daily 调性无关的内容）
- 输出：纯 JSON id 数组，如 `["12345"]` 或 `[]`（无任何匹配时返回空数组）
- 严禁输出任何解释性文字

#### prompts/section_hackernews.md（骨架）

- 角色定位（"HN 早间编辑"）
- 输入 schema 说明（K 个 enriched story，含 `link_content` 与 `top_comments`）
- 关注领域：引用 §6.2（K=1 时通常无需筛选，仅作为行文背景参考）
- 任务：对输入的 `K` 个 enriched stories 全部行文（不再二次挑选）
- 内容要求（每条 story）：
  - 提炼原文核心（背景 / 要点 / 结论）
  - 汇总 HN 评论区的有价值观点（支持 / 反对 / 补充），不是简单复述
  - 若评论中出现明显反驳原文的观点，必须保留并标注
- 输出格式建议（最终以 prompt 实测为准）：
  ```
  ### 标题 (N pts · M comments)
  - 链接: url
  - 内容总结: 2-3 条核心要点
  - 💬 HN 讨论: 1-2 条最有价值的观点（含反对意见）
  - 🔗 HN 讨论页: comments_url
  ```
- 风格约束：客观、犀利、克制；避免与 RSS digest 句式雷同；不做宏大叙事

#### prompts/insights.md（骨架）

- 角色定位（"AI 行业观察员"）
- 输入：三段成品 markdown + 近 N 天 insights 板块清单
- 任务：基于三段产出做跨板块小结
- 风格约束：避免与 RSS digest 句式雷同；避免简单复述已经在其他板块出现过的具体新闻
- 结构与 bullet 数量交由 prompt 内部约定，code 层不限制

### 6.4 调用顺序与并发

```python
async def _run_morning_push(config):
    rss_md, gh_md, hn_md = await asyncio.gather(
        run_rss_section(config, now),
        run_github_section(config, now),
        run_hackernews_section(config, now),
        return_exceptions=False,            # 各 section 自吞异常,不抛
    )

    insights_md, _ = await run_insights_section(rss_md, gh_md, hn_md, config, now)

    final = _assemble_with_sentinels({
        "rss": rss_md,
        "github": gh_md,
        "hackernews": hn_md,
        "insights": insights_md,
    })

    await send_to_platforms(final, config["push"])
    save_push_file(get_push_file(), final, profile="morning", ...)
```

`_assemble_with_sentinels(sections: dict[str, str]) -> str` 的契约：

- 按固定顺序拼装 `rss → github → hackernews → insights`
- 空 markdown 段整段省略（连同 sentinel）
- 段间留一个空行

## 7. 行业洞察板块设计

按用户决策，本节**不在 code 层规定 insights 的格式**：

- bullet 数量、子标题、字数限制、固定栏目等都属于 prompt 工程范畴
- 调整方法：编辑 `prompts/insights.md` 而非改代码
- 输入合同（code 层保证）：
  - `sections["rss" | "github" | "hackernews"]` 三个键的 markdown
  - 任一板块为空时该键值为 `"(本次无内容)"`
  - `recent_insights`：近 `filter.push_context_days` 天 insights 段标题清单（防风格趋同）
- 输出合同（code 层不校验）：直接作为 markdown 段插入

## 8. 配置 schema 增量

```json
{
  "filter": {
    "min_score": 60,
    "hot_threshold": 90,
    "context_days": 2,
    "keep_days": 7,
    "push_context_days": 5,
    "no_content_marker": "[NO_NEW_CONTENT]"
  },
  "schedule": {
    "fetch_interval_minutes": 30,
    "fetch_lookback_minutes": 120,
    "push_cron": ["0 8 * * *", "0 17 * * *"],
    "timezone_hours": 8
  },
  "sections": {
    "github_trending": {
      "enabled": true,
      "max_items": 3,
      "max_deep_dive": 10,
      "readme_max_chars": 10000,
      "history_file": "news-data/trending-history.json",
      "request_timeout": 10,
      "tokenName": "GITHUB_TOKEN"
    },
    "hackernews": {
      "enabled": true,
      "select_k": 1,
      "top_comments": 30,
      "top_l2_per_l1": 3,
      "comment_max_chars": 2000,
      "comments_total_chars": 60000,
      "link_content_max_chars": 50000,
      "request_timeout": 10,
      "algolia_base": "https://hn.algolia.com/api/v1"
    },
    "insights": {
      "enabled": true
    }
  },
  "llm": {
    "prompts": {
      "score_batch": "prompts/score_batch.md",
      "immediate_push": "prompts/immediate_push.md",
      "digest": "prompts/digest.md",
      "section_github": "prompts/section_github.md",
      "section_hackernews_select": "prompts/section_hackernews_select.md",
      "section_hackernews": "prompts/section_hackernews.md",
      "insights": "prompts/insights.md"
    }
  }
}
```

向后兼容：

- `sections` 整段缺失 → 等价于全部 `enabled=false` → push_job 走原有纯 RSS 路径
- `push_cron` 为空 → 无早报触发,不生成新板块
- 旧 push 文件没有 sentinel → `extract_section("rss", ...)` 返回整个 body，其他 section 返回空字符串
- `GITHUB_TOKEN` 未设 → GH 模块匿名调用，照常运行

## 9. 失败隔离与降级策略

| 失败位置 | 行为 |
|---|---|
| `run_rss_section` 失败 | 整个 push_job 退出非 0（核心承诺不变） |
| GH trending 抓取 / 解析失败 | `run_github_section` 返回 `("", error)` → 板块整段省略 → 告警 |
| GH 单 repo metadata/readme 失败 | 该 repo 跳过 + 错误聚合，不阻塞其他 repo |
| GH summarize LLM 失败 | 板块整段省略，告警 `notify_llm_errors("section_github", ...)` |
| HN 首页抓取失败 | 同 GH |
| HN 轻 LLM 初筛失败或返回空 | 板块整段省略，告警（初筛失败）或静默（结果为空） |
| HN 单 story enrich 失败 | 字段留空，metadata 仍传给最终 LLM |
| HN summarize LLM 失败 | 板块整段省略，告警 |
| insights LLM 失败 | 洞察段省略，其他板块照常推送，告警 |
| 早报判定为否 | 完全跳过 GH/HN/insights，不消耗任何额外 API |
| `sections.xxx.enabled=false` | 对应模块直接返回 `("", None)`，静默跳过 |

整体准则：**RSS 是核心，其余是增强**。除 RSS 外的任何失败都不阻塞推送，但都通过现有 `notify_llm_errors` 通道发简单告警，方便事后排查。

## 10. 早报判定逻辑

```python
def is_morning_push(now: datetime, config: Dict) -> bool:
    cron_list = config.get("schedule", {}).get("push_cron", [])
    if not cron_list:
        return False
    if len(cron_list) == 1:
        return True  # 唯一定时即"最早",任何触发都视为早报

    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_fires = [croniter(c, base).get_next(datetime) for c in cron_list]
    closest = min(today_fires, key=lambda f: abs(now - f))
    return closest == min(today_fires)
```

规则:`push_cron` 中 now 离哪条 cron 最近就归为那条;最近的那条若是当天最早的 cron,则视为早报。

为什么不再用单独的 `morning_cron` + 容差:
- 复用 `push_cron`,少一个配置项,加 cron 自动延展
- 「最近匹配」自动等价于容差 = 到次近 cron 距离的一半,systemd timer 漂移更稳健
- 单条 cron 部署自然变成"每次推送都是早报",与用户预期一致

## 11. 与现有模块的集成点

- `src/main.py::run_push_job` 改造：
  1. 开头加 `is_morning = is_morning_push(now, config)`
  2. `is_morning=False` 时走原路径（`compose_digest` only）
  3. `is_morning=True` 时进入 `_run_morning_push` 编排（四模块 + sentinel 拼装）
- `src/storage.py` 新增：
  - `extract_section(content, section)` + `load_recent_section_titles(section, days, data_dir)`
  - `load_trending_history(path) -> TrendingHistory`、`TrendingHistory.cleanup/touch/save`
- `save_push_file` 微调：frontmatter 带上 `profile: "morning"|"default"`，方便后续按 profile 分析
- `cleanup_old_files` 增加对 `trending-history.json` 的"过期条目剪枝"（不删整个文件）
- `src/push/` **不动**，平台层无感知
- `src/llm.py` 新增 4 个函数：`select_ai_related_hn / summarize_github_trending / summarize_hackernews / generate_trend_insights`
- `src/sections/` 全新模块树（4 个子包）

## 12. 测试策略

新增单元测试：

- `tests/pytest/test_sections_github_scraper.py`：本地 HTML fixture（保存几个真实 trending 页面快照）→ 测试解析
- `tests/pytest/test_sections_github_history.py`：测试 `TrendingHistory.touch/cleanup`、URL 已存在/不存在分支
- `tests/pytest/test_sections_github_enricher.py`：mock `aiohttp` → 测试 REST API 字段映射、archived 过滤、token 鉴权头
- `tests/pytest/test_sections_hackernews_scraper.py`：HN 首页 fixture → 测试 30 条解析
- `tests/pytest/test_sections_hackernews_enricher.py`：mock Algolia → 测试评论 top_comments 截断、Show HN 特例
- `tests/pytest/test_storage_sections.py`：sentinel 切片（含老文件 fallback）、`load_recent_section_titles`
- `tests/pytest/test_morning_detection.py`：cron 边界、容差、跨时区

新增交互式脚本（与现有 `tests/*.py` 风格一致）：

- `tests/run_morning_push.py`：模拟一次完整早报，强制 `is_morning=True`
- `tests/fetch_trending.py`：单独跑 GH 抓取 + deep-dive
- `tests/fetch_hackernews.py`：单独跑 HN 抓取 + enrich

## 13. 实施步骤建议

按依赖顺序实施，每步可独立 PR / commit：

1. **storage 层**：sentinel 切片、`trending-history.json` 读写、`load_recent_section_titles`、`save_push_file` profile 字段、`cleanup_old_files` 对 history 文件的处理 + 单测
2. **RSS 模块迁移**：把现有 `run_push_job` 中的 RSS 流程提取为 `run_rss_section`，验证行为不变
3. **GitHub 模块**：trending scraper → history → enricher → section 入口 + 单测 + fixture
4. **Hacker News 模块**：frontpage scraper → Algolia enricher → section 入口 + 单测 + fixture
5. **LLM 函数 + Prompt 文件**：4 个新 LLM 函数 + 4 个 prompt
6. **Insights 模块**：依赖 §5.4，相对简单
7. **push_job 升级**：早报判定 + 四模块编排 + sentinel 拼装 + 失败隔离
8. **配置 schema 升级**：`config.json.example` 与 `config.json` 同步；README 配置详解章节补全
9. **文档同步**：`docs/tech-spec.md` 把架构升级合并；`docs/plan.md` 写进度

## 14. 关键决策记录

| 决策 | 方案 | 原因 |
|---|---|---|
| 新板块时机 | 仅早报（当天最早一次 `push_cron` 触发） | 板块价值更适合一日一报；晚报维持原有 RSS 节奏；复用 push_cron 不引入新配置 |
| 模块边界 | `src/sections/<board>/` 各自封装抓取+LLM+总结 | 模块自治便于扩展、替换、单测；上游编排极简 |
| sentinel 归属 | push_job 上游统一包 | 模块不感知自己的板块标识；新增板块零修改成本 |
| GH trending 数据源 | 单页 HTML `https://github.com/trending`，无语言/since 过滤 | 用户决策：最简、最稳；语言过滤靠 topics + readme 在 LLM 层判 |
| GH deep-dive 内容 | REST API 拿 metadata + topics + README | topics 是 AI 相关性最强信号；metadata 补 license/pushed_at；README 给内容深度 |
| GH 鉴权 | GITHUB_TOKEN 可选 | 日 ~20 calls 远低于匿名 60 req/hr 上限；零配置即可跑 |
| GH 筛选策略 | history 过滤 → 全部 deep-dive → 一次 LLM 选 1-3 | 候选量小（5-15）；单次 LLM 比两阶段简单且选择质量高 |
| GH 候选护栏 | `max_deep_dive=10` | 极端日（trending 大改）限制 HTTP 与 token 消耗 |
| HN 数据源 | 首页 HTML + 评论/正文 Algolia | 首页要"现场感"走 HTML；Algolia 评论 JSON 结构清晰，免去 indent-tree 解析 |
| HN 筛选策略 | 30 条 → 轻 LLM 选 K=1 → enrich → 最终 LLM 行文 | 用户决策：把 enrich 工作量压到 1 个 story；轻 LLM 用 title 已足够判 AI 相关 |
| HN 评论结构 | L1 + 每个 L1 下挂 N 条 L2 回复,tree JSON 喂 LLM | 真实数据:L2 信息量与 L1 持平(24 条/8k chars vs 18/6k);拍平丢失父子关系,LLM 无法识别"回复反驳了顶层"。Tree 结构让 LLM 看清论辩链 |
| HN 评论上限 | `top_comments=30`(L1)+ `top_l2_per_l1=3` + `comments_total_chars=60000`(总预算) | 真实平均:L1 18 条/6k chars,L2 24 条/8k。30+3 给足余量但平均只跑 ~14k。总预算硬上限拦住离群 story(309 评论那种) |
| 无历史上下文 | GH / HN 板块均不传 recent_section_titles | 用户决策：避免不必要的上下文污染；GH/HN 风格与 RSS digest 差异已足够大 |
| insights 历史窗口 | 复用 `filter.push_context_days` | 不引入新字段；insights 段需要历史防风格趋同 |
| insights 输出结构 | 由 prompt 决定，code 不强加 | 用户决策：bullet 数量与栏目属于 prompt 工程，便于迭代 |
| GH / HN 关注领域沉淀 | 在 §6.2 集中定义正面列表 + 负面排除，3 个 prompt 引用 | 避免领域定义在 prompt 间漂移；用户已明确兴趣边界（AI Agent / 模型 / 基础设施 / 大厂动态），排除嵌入式、底层系统、纯前端模板、学习资源等 |
| 板块 sentinel 用 HTML 注释 | `<!-- SECTION:xxx BEGIN/END -->` | markdown 渲染不显示；机器易解析；老 push 文件零冲突 |
| 失败降级粒度 | 单板块失败省略本段；RSS 失败整体退出 | RSS 是核心承诺，其他是增强 |
| 早报判定 | cron + 容差，而非"今天第一次" | 早报失败时晚报不会错误升级为长版本 |
| 截断参数初始值（2026-05-17 合入） | `readme_max_chars=3000` / `top_comments=20` / `comment_max_chars=500` / `link_content_max_chars=3000` | 凭直觉给出的保守默认；上线后通过真实数据校准 |
| 截断参数校准（2026-05-17 合入后,激进路径） | `readme_max_chars: 5000→10000` / `top_comments: 20→50` / `link_content_max_chars: 6000→50000` / `comment_max_chars: 800` 不变 / `max_prompt_chars: 64000→150000` | 用户决策:把 LLM 上下文用到 DeepSeek v4 flash 128k tokens 限的合理水平,优先内容深度而非 API 成本。Worst-case 单次 LLM prompt:GH≈110k chars / HN≈95k chars,均在 150k budget 内。中英混合 100k chars ≈ 30-50k tokens,远低于 128k 模型限 |
| 单板块 CLI（2026-05-17 合入后） | `python -m src.main github` / `hackernews` | 便于 prompt 调优期反复跑单板块而不消耗全套 LLM 调用 |


hacker news 评论统计

┌─────────────┬──────────┬──────────────┬────────────────┬──────────┐
│    层级     │ 平均条数 │ 平均 md 字符 │ 平均单条 chars │ 最大单条 │
├─────────────┼──────────┼──────────────┼────────────────┼──────────┤
│ L1 顶层评论 │ 18       │ 5,952        │ ~330           │ 1,755    │
├─────────────┼──────────┼──────────────┼────────────────┼──────────┤
│ L2 一级回复 │ 24       │ 8,166        │ ~340           │ 2,381    │
├─────────────┼──────────┼──────────────┼────────────────┼──────────┤
│ L3+ 更深层  │ 41       │ 13,048       │ ~315           │ 2,291    │
├─────────────┼──────────┼──────────────┼────────────────┼──────────┤
│ ALL 全部    │ 83       │ 27,166       │ ~330           │ 2,381    │
└─────────────┴──────────┴──────────────┴────────────────┴──────────┘
