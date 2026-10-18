# slowtrans

[![CI](https://github.com/yunfang1718128/slowtrans/actions/workflows/ci.yml/badge.svg)](https://github.com/yunfang1718128/slowtrans/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> 用各大平台白送的免费额度，把一本 EPUB 译成中文。**成本 ¥0**，慢一点，但比整本丢进机翻 API 好得多。

[English](README.en.md) · [**从零到出书**](docs/quickstart.md) · [变更记录](CHANGELOG.md) · [实测数据](docs/benchmark.md) · [参与开发](CONTRIBUTING.md)

## 这个项目在做什么

硅基流动的 `Qwen2.5-7B`、智谱的 `glm-4-flash`、腾讯的 `Hunyuan-MT`——这些免费模型本来没人拿来跑一整本书：
单次对话够用，但一本书上千段，会超时、会限流、会断。

**slowtrans 就是把这件「没人愿意做」的事做掉**：把一本书拆成几千个可恢复的小任务，
单并发、慢慢喂给免费模型；中途断了能续，译名能统一，最后拼成一本能读的 epub。

说白了就是**薅平台免费额度的羊毛**，但薅得比粗暴调用体面：不浪费 token、不重复付费、不产出坏文件。

实测一本 300 页的学术书（`Fantasy: How It Works`，Oxford）：

| | |
|---|---|
| 分段 | 1460 段 |
| 耗时 | 86 分钟（挂机，不用管） |
| **API 花费** | **¥0** |
| 峰值内存 | 44.6 MB（跑在 1.7 GB 的小服务器上） |
| 质量 | 术语统一、有风格约束，比整本机翻自然；个别段落仍需人工过一眼 |

## 成本对照

一本书大约 10 万～15 万字符：

| 方案 | 一本书 | 说明 |
|---|---|---|
| Google / DeepL 机翻 API | ¥20–60 | 通顺，但术语乱、无上下文，双语对照要自己做 |
| 付费小模型（GPT-4o-mini / Qwen-14B） | ¥0.05–3 | 质量好，但每本都要付 |
| 人工翻译 | ¥8000+ | 质量最好，不适合「只是想读完」 |
| **slowtrans + 免费模型** | **¥0** | 术语表 + 风格卡 + 章节摘要，比机翻统一 |

> 免费额度是平台补贴，随时会变。项目里所有价格都可配置：某个模型开始收费时，
> 把 `price_in` / `price_out` 填进 `config.toml`，预算控制会在到点前自动停下。
> 换句话说——**免费时白嫖，收费时也不会让你爆账单**。

## 为什么不是「又一个翻译脚本」

1. **机器很弱**：常驻内存 36 MB（实测），单并发，不跟同机服务抢资源。
2. **免费 API 又慢又会限流**：令牌桶、退避、熔断、预算、时间窗、批量失败自动降级。
3. **长文本译名会漂**：术语表 + 风格卡 + 前情摘要持久化在 SQLite 里，改术语只重译命中段。
4. **免费模型会丢格式**：内联标签用占位符保护，校验不过就降级重译，最后才保留原文。

## 和现有项目的区别

| 项目 | 定位 | slowtrans 的不同 |
|---|---|---|
| `bilingual_book_maker` | 一次性生成双语书 | 有服务端、状态机、断点续传、术语版本化 |
| `TranslateBooksWithLLMs` | Web UI 跑一本书 | 不做 UI 重活，只暴露 HTTP，客户端可换 |
| `epub-translator` | CLI 保持 epub 结构 | 结构与它同级，但多了「试译→审核→长跑」流程 |

简单说：**它们假设「跑完就好」，slowtrans 假设「跑 8 小时、中途会停、术语会改」。**

## 特性

- **低内存**：常驻 36 MB、峰值 45 MB；逐章解析，译文只落 SQLite，最后才组装 zip。
- **可中断**：`kill -9` 后重跑自动续，已完成段落不重复付费；
  面板上「暂停」保留进度、「终止」连记录和成品一起删干净。
- **半成品也诚实**：随时可以生成当前进度，下载文件名带 `.partial`，并说明还有多少块没译完。
- **一致性**：术语表（只注入命中项）+ 风格卡 + 章节摘要；改术语后按缓存键选择性重译。
- **慢速友好**：单并发、令牌桶、指数退避、熔断、预算上限、可设夜间时间窗。
- **结构安全**：`mimetype` 第一且 STORE、内联标签用占位符保护、写完回读自检。
- **双份成品**：默认同时产出双语对照版（`book.zh.epub`）和纯译文版（`book.zh.mono.epub`）；
  纯译文只出中文，给阅读器用，`--no-mono` 可关掉。
- **桥接优先**：REST + SSE，AstrBot 插件与面板共用同一套事件。
- **自带控制台**：书库封面墙（读 epub 元数据 + 抽封面）、任务详情/日志/章节预览、
  设置页里直接填 API key。单文件、无构建、无 CDN。

## 快速开始

> 完整的两条路线（本地 / 服务器，含上传书、隧道、排错）见 **[docs/quickstart.md](docs/quickstart.md)**。

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[api]"

export SILICONFLOW_API_KEY=sk-...                  # 或写进 .env（已在 .gitignore）
slowtrans inspect --src book.epub                  # 先看结构，不花钱
slowtrans run --src book.epub --out book.zh.epub \
  --from en --to zh-CN \
  --engine siliconflow:Qwen/Qwen2.5-7B-Instruct \
  --batch 4
```

跑之前建议先小规模验证：

```bash
slowtrans run --src book.epub --out test.epub --limit 40 --batch 4
```

`--limit` 只入库前 N 段。之后去掉 `--limit` 重跑会自动补齐剩余段。

跑完会得到两个文件：

```
book.zh.epub        双语对照：原文段 + 译文段，适合对照学习
book.zh.mono.epub   纯中文：只有译文，适合正常阅读
```

### 免费模型（2026-10 核对）

判定依据：硅基流动定价页 RSC payload 里 `pricing = 0/0` 或 DisplayName 带 `(Free)`；
智谱官方模型概览页标注「免费文本模型」，并逐个实测可用性。

| provider | model | 状态 | 备注 |
|---|---|---|---|
| siliconflow | `Qwen/Qwen2.5-7B-Instruct` | **免费·默认** | 指令跟随最好，批量可靠 |
| siliconflow | `THUDM/GLM-4-9B-0414` | 免费 | 中文稳，语感略硬 |
| siliconflow | `Qwen/Qwen3-8B` | 免费 | 会先思考，吃额外 token |
| siliconflow | `tencent/Hunyuan-MT-7B` | 免费 | 翻译专用、最快，但会把指令当正文译 |
| siliconflow | `deepseek-ai/DeepSeek-R1-0528-Qwen3-8B` | 免费 | 推理模型，不适合翻译 |
| siliconflow | `Qwen/Qwen2.5-14B-Instruct` | 输入免费 | 输出 ¥0.35/M |
| zhipu | `glm-4-flash` | **免费·默认** | 稳定，备用通道 |
| zhipu | `glm-4-flash-250414` | 免费 | 快照版 |
| zhipu | `glm-4.5-flash` | 免费 | 128K 上下文 |
| zhipu | `glm-4.7-flash` | 免费 | 200K 上下文，会给思考留 token |

> 智谱的 `/models` 接口**不返回免费模型**（只列 glm-4.5 以上的付费型号），
> 但 `glm-4-flash` 系列可以直接调。别被接口列表误导。

#### 任何 OpenAI 兼容服务

不需要写代码，面板「设置 → 添加 OpenAI 兼容服务商」填 name / base_url / key / model 即可：

| 预设 | base_url |
|---|---|
| DeepSeek | `https://api.deepseek.com/v1` |
| 百炼 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Moonshot | `https://api.moonshot.cn/v1` |
| Ollama | `http://127.0.0.1:11434/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |

填完点「测试」，服务会发一个最小请求（`pong`）并返回延迟，确认通不通再保存。

> 免费模型的可用性/额度随时会变。`--engine` 只是 `provider:model`，换模型不用改代码。
> 引擎层统一走 OpenAI 兼容协议，任何兼容 endpoint（DeepSeek、百炼、Ollama、vLLM）都能直接用。

## 界面

`slowtrans serve` 自带一个单页控制台，无构建、无 CDN、无前端工程：

| 视图 | 内容 |
|---|---|
| 书库 | 读 epub 的 OPF 元数据 + 封面，封面墙点一下开工；已译过的书角上有状态 |
| 任务 | 进度环、章节完成度、待复核段及原因、事件日志、单章预览、双语/纯中文两份下载 |
| 设置 | API key（只回显掩码）、默认模型（免费模型排前面）、书库目录、自定义服务商 + 探活 |

> 仓库暂未附截图：书库会对真实书籍封面截图，涉及版权素材，建议你自己跑起来截图后
> 放进 `docs/` 并在 Pull Request 里替换这一行。

## 它是怎么保证译名一致的

一本书里同一个人名出现 200 次。如果每次翻译都靠模型「记住」，必然漂。

slowtrans 的做法是**把约定变成持久化状态**：

1. **分析阶段**（只跑一次，花费几毛钱）
   - 本地统计候选专名（不花 API 钱）
   - 一次批量调用让模型定名，写入 `glossary` 表
   - 从开头几段生成 `style_card`（≤8 条硬规则）

2. **翻译阶段**
   - 每段只注入**命中该段**的术语（不是全表），通常 3–10 条
   - 前面 2–3 章的摘要作为「前情」
   - 严格按 spine 顺序串行，术语表随进度生长

3. **校验阶段**
   - 占位符集合必须一致（内联标签丢失 → 重试 → 降级去标记重译 → 仍失败则保留原文）
   - 译文/原文长度比异常 → 判为漏译/截断

4. **改术语之后**
   ```bash
   # 面板或 API 里改一个译名 -> glossary_version 变
   slowtrans run --src book.epub --out book.zh.epub   # 只重译命中旧译名的段落
   ```
   缓存键包含 `glossary_version + style_version + prompt_version`，其余段落直接命中缓存。

## 桥接 API

服务只监听 `127.0.0.1`。远程访问请用 SSH 隧道或 Tailscale，不要直接暴露公网。

```bash
slowtrans serve --host 127.0.0.1 --port 8790
# 浏览器打开 http://127.0.0.1:8790/
```

面板里有三块：

| 视图 | 作用 |
|---|---|
| 书库 | 扫描目录里的 epub，用封面 + 书名展示；点一下就能开工 |
| 任务 | 进度环、章节完成度、待复核段、事件日志、成品下载、单章预览 |
| 设置 | 填 API key（只显示掩码）、选默认模型、改书库目录 |

**key 的优先级是「面板里填的 > 环境变量」**：显式操作应该压过部署脚本的默认值。
面板写入的是 `data/settings.json`（600），接口永远只返回 `sk-abc…wxyz（51 字符）` 这种掩码。

```
POST /api/v1/jobs                提交任务（type=translate|convert）
GET  /api/v1/jobs?state=         任务列表
GET  /api/v1/jobs/{id}           进度、章节、待复核段
POST /api/v1/jobs/{id}/pause|resume|cancel
GET  /api/v1/jobs/{id}/glossary  读术语表
PUT  /api/v1/jobs/{id}/glossary  改译名（?replan 提示只重译命中段）
GET  /api/v1/jobs/{id}/events    SSE，支持 Last-Event-ID 续传
GET  /api/v1/jobs/{id}/log       事件日志（普通 JSON，面板用）
GET  /api/v1/jobs/{id}/preview   成品某一章的原始 XHTML（iframe 预览）
GET  /api/v1/library             书库：epub 列表 + 元数据 + 是否已有任务
GET  /api/v1/library/cover       封面原图（带一天缓存）
GET  /api/v1/settings            配置视图（key 已掩码）
PUT  /api/v1/settings            写 key / 默认模型 / 书库目录
GET  /api/v1/events              全局事件流
POST /api/v1/hooks               webhook 注册
GET  /api/v1/artifacts/{id}      下载成品
GET  /api/v1/health | /capabilities
```

创建任务既可以用服务器绝对路径，也可以用书库相对路径：

```http
POST /api/v1/jobs {"type":"translate","rel":"books/dune.epub","lang_to":"zh-CN"}
```

**格式转换尚未实现**，接口已预留：

```http
POST /api/v1/jobs {"type":"convert", ...}
HTTP/1.1 501 Not Implemented
{"error":"not_implemented","feature":"convert","planned":["md","txt","html","docx","pdf(仅文本)"]}
```

事件格式是三个客户端唯一的共同契约：

```json
{"schema_version":1,"seq":42,"job_id":"j_ab12","event":"progress",
 "ts":"2026-10-18T01:20:00Z",
 "data":{"state":"translating","chapter":3,"chapter_total":12,
         "done":112,"total":410,"tokens_in":18234,"tokens_out":9201}}
```

鉴权：设置 `SLOWTRANS_TOKEN` 后所有 `/api/v1/*`（除 `health`、`capabilities`、SSE）需要
`Authorization: Bearer <token>`。不设则只有在回环地址上才安全。

## 部署（systemd，低内存）

```bash
sudo useradd -r -s /usr/sbin/nologin slowtrans
sudo mkdir -p /srv/slowtrans /etc/slowtrans
sudo install -m 600 deploy/env.example /etc/slowtrans/env   # 填入 key
sudo install -m 644 deploy/slowtrans.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now slowtrans
```

unit 里已经限死：

```ini
MemoryHigh=280M
MemoryMax=320M
CPUQuota=80%
Nice=10
```

## 实测

样书 `Fantasy: How It Works`（Oxford，883 KB）：

| 指标 | 值 |
|---|---|
| 分段 | 1460（旧分段规则）→ 1716（内联容器元素展开后） |
| 完成 | 1452/1460，待复核 8，失败 0 |
| 耗时 | 86 分钟（约 16 段/分钟） |
| 峰值内存 | **51.9 MB**（本机 psutil）/ **44.6 MB**（服务器 `ru_maxrss`，口径不同） |
| 成品 | 双语 1.08 MB / 纯中文 0.86 MB |
| 服务器实机 | Debian 13 / Python 3.13 / 2 核 1.7 GB：常驻 **36 MB**，全量分段+组装峰值 **44.6 MB** |

细节、免费模型核对方式和踩过的 9 个坑见 [`docs/benchmark.md`](docs/benchmark.md)。

## 已知限制

- 图片里的文字不翻译。
- DRM 书无法处理，会直接报错，不会产出坏文件。
- PDF 转 EPUB 尚未实现；将来也只保证文本，不保证版式/公式/图表。
- 免费小模型偶尔丢行内标记：会先重试，再降级为「去标记重译」，最后保留原文并标 `review`。
- 日文 `ruby` 注音默认丢弃（中文读者不需要），竖排样式会强制改为横排。

## Roadmap

- [x] epub 分段/重写/自检
- [x] 术语表 + 风格卡 + 缓存版本化
- [x] 断点续传、预算、退避、熔断
- [x] REST + SSE 桥接
- [x] 内置面板（书库 / 任务 / 设置 / 预览）
- [x] AstrBot 插件
- [x] 双语 + 纯中文双份成品
- [x] 任意 OpenAI 兼容服务商（面板增删 + 探活）
- [x] 英文 README / CI / 贡献指南
- [ ] 半自动审核（`awaiting_review` 状态）
- [ ] 格式转换：md/txt/html/docx → epub（同一 job 队列）
- [ ] 一致性自动巡检（译名漂移扫描）
- [ ] Dockerfile / PyPI 发布

## 许可证

MIT。参考了 `bilingual_book_maker`、`epub-translator`、`TranslateBooksWithLLMs` 的思路，
但未复制其代码；后者的 AGPL 代码请不要直接粘进来。
