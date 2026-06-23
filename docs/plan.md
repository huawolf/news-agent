## TODO

当前待办

- [ ] 优化提示词： 推送格式；参考链接去除非官方信息； insights 不够深度
- [ ] 日志系统，保存到文件，push和fetch分开，
- [ ] 早报内容格式优化：参考appso / xiaohu / ai gap
  - [ ] 优先级顺序
  - [ ] 美化排版
- [ ] 添加更多信息源，如 TechCrunch、
- [ ] 允许fetch链接中的内容对信息进行扩展
- [ ] 更多信息源以及信息获取不全： https://www.anthropic.com/research/glasswing-initial-update / github blog


长期待办

- [ ] 增加图片/信息图
- [ ] 推送到知乎 / 小红书 / 网站
- [ ] llm api fallback

## 技术决策记录

| 决策 | 方案 | 原因 |
|------|------|------|
| 定时调度 | systemd timer（生产）+ croniter（loop 模式） | 进程崩溃/服务器重启可自愈，配置热更新，比内置 asyncio.gather 更稳健 |
| 包管理 | uv | 速度快、单工具管理 venv/pip/lockfile，项目独立 `.venv` |
| LLM 健康检查 | 仅在 `install.sh` / `loop` 启动时校验 | 每次 timer 触发都校验会增加无意义的 LLM API 调用，运行期异常由 `notify_llm_errors` 兜底 |
| 日志方案 | journald 命名空间 `dnews` + `MaxRetentionSec` | 与系统其他服务隔离，按 `log.retention_days` 自动轮转，无需写文件日志 |
| 数据格式 | JSON | 结构清晰、易处理、支持嵌套 |
| 推送文件 | Markdown+YAML | 人工可读、Frontmatter 元数据 |
| LLM 评分 | 批量 JSON | 减少 API 调用次数 |
| 状态追踪 | 文件时间戳 | 无需外部数据库 |
| RSS延迟防护 | fetch_lookback_minutes | 防止RSS延迟导致漏读 |
| LLM异常通知 | 调用方统一上报 | 避免批次级刷屏，同时保留关键异常通知 |
| 报告 metadata 统一 | 全部从 LLM frontmatter 解析(早报/晚报/即时) | 取代之前的 `extract_title_from_content` h1 提取+硬编日期标题；个性化标题 + lead 导读 + highlights 列表统一承载 |

## 开发进度

**2026-05-22**

- ✅ 推送 metadata 统一改造：晚报与即时消息也走 frontmatter（之前只有早报）
  - `prompts/digest.md` 增加 frontmatter（title/lead/highlights），删除正文「开头一句话定调」段
  - `prompts/insights.md` frontmatter 增加 `lead`（综合三段的 60-100 字前言）与 `highlights`（2-3 条卡片重点）
  - `prompts/immediate_push.md` 将 `# 标题` 迁到 frontmatter `title` 字段
  - `src/llm.py` 新增 `parse_digest_with_metadata` / `parse_immediate_push_with_metadata`，统一 `_parse_frontmatter` 帮手；移除 `extract_title_from_content`
  - `src/sections/rss/section.py` 返回三元组 `(body, metadata, err)`；早报丢弃 digest metadata，由 insights 段覆盖
  - 晚报现在拥有个性化标题 + lead + highlights，与早报对齐


**2026-05-17**

- ✅ 早报扩展板块上线：在 RSS digest 之上叠加 GitHub Trending / Hacker News / 跨板块洞察三段，仅 `schedule.morning_cron` 命中时触发，晚报维持纯 RSS 行为
- 设计与实施详见 [`docs/extra-sections-design.md`](extra-sections-design.md) 与 [`docs/superpowers/plans/2026-05-17-extra-sections.md`](superpowers/plans/2026-05-17-extra-sections.md)

**2026-05-15**
- 优化提示词，修复长时运行下的新闻报告措辞趋同问题

**2026-05-14**
- [x] 外置定时（已切换到 systemd timer）
- [x] 系统服务一键运行（`scripts/install.sh`）
- 使用uv进行python项目管理
- 配置变更：`config.json` 新增 `log.retention_days` 字段
- 新增 `daily-news` 系统级包装脚本：装在 `/usr/local/bin/daily-news`，提供 `start/stop/restart/status/logs` 等命令，封装 systemctl/journalctl 调用细节

**2026-03-08**
- 新增 LLM 异常通知：`compose_digest`、`generate_immediate_push` 与 `score_batch` 的错误会通过现有推送渠道发送简单告警
- 优化批量评分容错：`score_batch` 在批次返回数量不匹配时会按 `link` 回收可用结果，并聚合错误返回给调用方
- 移除 `generate_immediate_push` 的 fallback 内容，生成失败时由调用方告警并跳过本次即时推送
- 新增启动前 LLM 可用性检查：主程序在启动 fetch/push 双循环前先探测 LLM 接口，失败则直接退出
- 修复 pytest 中遗留的旧推送平台命名问题，将 `wecom` 测试更新为当前 `feishu` 实现

**2026-03-03**
- 采用 MIT 许可开源项目，添加 LICENSE 和 NOTICE 文件
- 更新 RSS 源说明，致谢 BestBlogs 项目

**2026-03-02**
- 修复RSS延迟漏读问题：新增 fetch_lookback_minutes 参数，fetch时读取过去更长一段时间的RSS条目进行去重
- 新增飞书 Webhook 推送支持：使用卡片消息格式，支持 Markdown 渲染
- 新增测试脚本 test_fetch_lookback.py
- 更新 cleanup_old_files 函数支持 notify 文件清理

**2026-03-01**
- 优化评分系统：通过更新 score 提示词提升评分质量
- 即时推送去重：新增 notify-*.md 文件存储即时推送，LLM 调用时传入近期推送上下文避免重复
- 汇总推送优化：新增 push_context_days 配置，汇总推送时传入近期推送上下文进行去重
- 修复 score 类型问题：确保 LLM 返回的 score 为整数类型
- 完善测试脚本：添加上下文参数和保存功能

**2026-02-28**
- 初始化项目，MVP 已完成，支持 RSS 抓取、LLM 评分、定时推送、即时推送。
