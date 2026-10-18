# 参与开发

## 本地跑起来

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[api]" pytest ruff

pytest -q                       # 32 个用例，全部离线，不花 API 钱
ruff check src tests
```

## 不联网也能测的部分

`engines/openai_compat.py` 里有 `MockEngine`，它把原文加上 `[译]` 前缀返回。
`tests/` 用它覆盖了分段、占位符还原、断点续传、缓存命中，所以 CI 不需要任何 API key。

手工跑一遍完整链路（不花钱）：

```bash
slowtrans run --src tests/fixtures/any.epub --out /tmp/out.epub --engine mock:echo
```

## 改动时请守住这几条

1. **不把整本书读进内存**。分段、组装都必须按章处理；新增功能如果引入整本 DOM，请说明理由。
2. **不改 `mimetype` 的处理方式**（第一且 STORE）。这在 `epub/package.py` 里，写错了阅读器直接拒开。
3. **占位符校验不能被绕过**。模型输出必须先过 `_validate`，不合格就走降级链路。
4. **任何会让"已译段"失效的改动**（prompt、分段规则、缓存键），都要考虑老任务：
   `db.insert_segments` 会比对 `src_text` 并重置变化的段。
5. **密钥绝不进代码/日志/提交**。`settings.json` 与 `.env` 都在 `.gitignore` 里；
   接口返回 key 一律走 `settings.mask()`。

## 提交信息

中文或英文都行，格式大致 `<type>: <一句话>`，type 用 `feat/fix/docs/chore/test/refactor`。
如果修的是真实踩过的坑，在 `docs/benchmark.md` 的"走过的弯路"里补一条——那部分是给后来者的。

## 新增模型/服务商

- 只是某个 OpenAI 兼容服务：不用改代码，面板里"添加服务商"即可。
- 想进"免费模型推荐列表"：改 `src/slowtrans/models.py`，注明核对方式和日期。
