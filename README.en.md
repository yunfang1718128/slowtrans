# slowtrans

> Translate EPUB books with **free-tier** LLM APIs for **$0**. Slow, resumable, and still better than dumping the whole book into a machine-translation API.

[中文文档](README.md) · [Changelog](CHANGELOG.md) · [Benchmark](docs/benchmark.md)

## What it does

SiliconFlow's `Qwen2.5-7B`, Zhipu's `glm-4-flash`, Tencent's `Hunyuan-MT` — the free tiers are meant for a few hundred tokens of chat,
not for a 1,400-paragraph book that will time out, get rate-limited and break halfway.

slowtrans does that unglamorous work: split the book into thousands of resumable segments,
feed them to a free model one at a time, keep terminology consistent, and assemble a readable epub.

Measured on a 300-page academic book (Oxford University Press):

| | |
|---|---|
| Segments | 1,460 |
| Wall time | 86 minutes (unattended) |
| **API cost** | **$0** |
| Peak RSS | 44.6 MB on a 1.7 GB server |

## Cost comparison

A book is roughly 100k–150k characters:

| Option | Per book | Note |
|---|---|---|
| Google / DeepL translation API | $3–9 | fluent, but terminology drifts, no context |
| Paid small LLM (GPT-4o-mini / Qwen-14B) | $0.01–0.4 | good, but you pay every time |
| Human translation | $1,000+ | best quality, not for "I just want to read it" |
| **slowtrans + free tier** | **$0** | glossary + style card + chapter summaries |

> Free tiers are vendor subsidies and change without notice. Every price is configurable:
> when a model starts charging, put `price_in` / `price_out` in `config.toml` and the budget
> guard stops the job before it costs you anything unexpected.

## Why not just another translation script

1. **Weak machine** — 36 MB resident, single concurrency, does not fight other services on the box.
2. **Free APIs are slow and rate-limited** — token bucket, exponential backoff, circuit breaker, budget, time window.
3. **Long books drift** — glossary + style card + chapter summaries in SQLite; changing a term only retranslates matching segments.
4. **Small models break markup** — inline tags are placeholder-protected; failures degrade to plain retranslation, then to the original text.

## Why another EPUB translator

Existing tools assume "run it once and wait". This one assumes **the machine is weak, the API
is free-but-slow, and the job will be interrupted**:

| Problem | What slowtrans does |
|---|---|
| 1.7 GB RAM shared with a bot and a WebDAV server | ~52 MB peak RSS; chapter-at-a-time parsing, translations live in SQLite, the zip is assembled last |
| Free APIs rate-limit and time out | dynamic batching by character count, exponential backoff, circuit breaker, budget, optional night window |
| Long books drift: the same name gets five translations | glossary + style card + per-chapter summary, persisted; only *matching* terms are injected per segment |
| You change a term after 8 hours of work | cache key includes `glossary_version`, so only segments hitting that term are retranslated |
| Model output breaks HTML | inline markup is replaced by `{{n}}` placeholders and verified before writing; failures fall back to plain-text retranslation, then to the original text |

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[api]"

export SILICONFLOW_API_KEY=sk-...      # or put it in .env (gitignored)
slowtrans inspect --src book.epub      # structure only, no API calls
slowtrans run --src book.epub --out book.zh.epub --from en --to zh-CN
```

Outputs two files:

```
book.zh.epub        bilingual: original paragraph + translated paragraph
book.zh.mono.epub   translated only, for normal reading
```

Built-in web console (loopback only):

```bash
slowtrans serve --port 8790     # http://127.0.0.1:8790/
```

Library wall with covers, task progress/logs, per-chapter preview, API-key setup, and
any OpenAI-compatible endpoint you want to add.

## Free models (verified 2026-10)

| Provider | Model | Note |
|---|---|---|
| SiliconFlow | `Qwen/Qwen2.5-7B-Instruct` | free, default, best instruction following |
| SiliconFlow | `THUDM/GLM-4-9B-0414` | free |
| SiliconFlow | `tencent/Hunyuan-MT-7B` | free, fastest, but translates your instructions too |
| Zhipu | `glm-4-flash` | free, default fallback |
| Zhipu | `glm-4.7-flash` | free, 200K context |

Anything OpenAI-compatible works (DeepSeek, DashScope, Moonshot, Ollama, vLLM) — add it in
the console or under `[providers.*]` in `config.toml`.

## Status

`0.1.0`, beta. Full book translated on Windows/Python 3.13 (see benchmark);
**not yet deployed on a real low-memory server**. Format conversion (md/docx/pdf → epub)
is an explicit stub returning HTTP 501.

## License

MIT. Inspired by `bilingual_book_maker`, `epub-translator` and `TranslateBooksWithLLMs`;
no code copied from them (the last one is AGPL — do not paste its code into this project).
