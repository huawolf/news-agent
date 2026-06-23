# AI 每日资讯推送系统

AI 驱动的 RSS 新闻聚合与推送系统，支持 400+ 信息源，使用 LLM 智能评分筛选，定时推送到 Discord/企业微信。

当前阶段：MVP 已完成，支持 RSS 抓取、LLM 评分、定时推送、即时推送。

## 技术栈

- 语言：Python 3.10+
- 框架：asyncio
- 依赖：feedparser, aiohttp, croniter, openai
- 构建：pip
- 测试：pytest

## 开发规则

1. 任何代码改动如果与 docs/ 下的文档不一致，必须同步更新对应文档
2. 产品决策变更（功能取舍、交互调整、设计修改）和任务进度 写入 docs/plan.md 的`## 技术决策记录` 和 `## 开发进度`
3. 不确定的产品问题先问用户，不要自行决定
4. 敏感信息（API Keys、Webhook URLs）通过环境变量管理，不硬编码
5. config 有更新需要即时更新对应文档 

## 文档索引

- 技术架构 → [docs/tech-spec.md](docs/tech-spec.md)
- 开发计划 → [docs/plan.md](docs/plan.md)
- 早报扩展板块设计（GitHub / HN / 洞察）→ [docs/extra-sections-design.md](docs/extra-sections-design.md)
- 实施计划归档 → [docs/superpowers/plans/](docs/superpowers/plans/)
