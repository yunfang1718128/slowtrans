# Changelog

格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 SemVer。

## [0.1.0] - 2026-10-18

首个可用版本。目标场景：1.7 GB 内存的小服务器 + 免费/廉价 LLM API，慢速翻完整本 EPUB。

### 新增

- **EPUB 处理**：`zipfile` 逐条读取、OPF/spine 解析、块级分段、`{{n}}` 占位符保护内联标记、
  `mimetype` 第一且 STORE、成品回读自检。不依赖 ebooklib（它会把整本书载入内存）。
- **一致性**：术语表（命中式注入）、风格卡、章节摘要；缓存键包含
  `glossary_version + style_version + prompt_version`，改术语只重译命中段。
- **慢速调度**：单并发、按字符数动态分批（默认 2600）、指数退避、熔断、预算、
  可配置夜间时间窗。
- **断点续传**：状态落 SQLite，`kill -9` 后重跑自动续；已完成段直接计入"未花费"。
- **降级链路**：占位符不一致 → 重试 → 去标记重译 → 仍失败则保留原文并标 `review`，
  绝不把坏结构写进成品。
- **双份成品**：双语对照 `book.zh.epub` + 纯译文 `book.zh.mono.epub`。
- **HTTP 桥接**：REST + SSE（`Last-Event-ID` 续传）、Bearer token、仅回环监听。
- **控制台**：书库封面墙（读 OPF 元数据、抽封面）、任务进度/章节/日志/单章预览、
  设置页填 API key（只回显掩码）、免费模型目录、任意 OpenAI 兼容服务商增删与探活。
- **客户端**：AstrBot 插件（提交/查进度/完成推送）、本地面板访问说明。
- **部署**：systemd unit（`MemoryMax=320M`、`CPUQuota=80%`）、env 模板、install.sh。
- **测试**：16 个 pytest 用例（分段/占位符/还原/断点/掩码/路径穿越）。

### 已知限制

- 格式转换（md/txt/html/docx/pdf → epub）只有接口桩，返回 501。
- 图片内文字不译；DRM 书直接报错。
- 日文 `ruby` 注音默认丢弃，竖排强制改横排。
- 免费模型清单是 2026-10 的快照，额度随时会变（`scripts/scan_free.py` 可重新核对）。
- 已在阿里云 ECS（Debian 13 / Python 3.13 / 2 核 1.7 GB）实机部署验证：
  服务常驻 36 MB，全量分段 + 组装峰值 44.6 MB，远低于 `MemoryMax=320M`。

### 实测（样书 `Fantasy: How It Works`）

- 1460 段 → 1452 完成、8 待复核、0 失败，86 分钟，峰值 RSS 51.9 MB。
- 详见 `docs/benchmark.md`。注意该数据产生于"内联容器元素展开"修复之前，
  修复后同一本书为 1716 段（标题文字开始进入翻译队列），需要重译一次。
